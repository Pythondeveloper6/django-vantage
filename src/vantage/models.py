"""The five things Vantage stores: its settings, what people did, how they look, how
each list is tuned, and what is still running.

Vantage configures itself from ``settings.VANTAGE``, so owning tables at all is a
decision that has to be justified once per table. Five earn it.

**The settings cascade.** A project owner states their settings in
``settings.VANTAGE``; the person who actually runs the panel day to day is usually not
the person who can edit that file. :class:`SiteConfiguration` gives them a row that
sits *underneath* attribute access on :data:`~vantage.conf.vantage_settings`, so the
branding can be changed from the panel without a deploy, and without a single call
site knowing a database is involved.

**The audit log.** :class:`VantageLogEntry` records what someone did *in the panel* —
one generic-FK row per create, edit, delete or bulk action. It is deliberately not row
history: django-simple-history is the tool for that, and requiring it would put a
model and a migration in the *user's* app and roughly double their write volume. See
:mod:`vantage.audit`.

**Saved views.** :class:`SavedView`, :class:`SavedViewGrant` and
:class:`SavedViewUserState` store a named filter/sort/search state for a list page.
See :mod:`vantage.savedviews`.

**Per-list customisation.** :class:`ListPreference` is to one model's list page what
:class:`SiteConfiguration` is to the whole panel: a row that sits *underneath* the
Python declaration, so the columns, filters, search, stats, charts and summary of one
list can be tuned from the UI without a deploy. One row per person per model, plus an
optional row with a ``NULL`` user that is the site-wide default. See
:mod:`vantage.listprefs`.

**Background jobs.** :class:`VantageJob` is one row per bulk action that was too large
to run in the request. It stores the *selection predicate*, never the rows it resolved
to, which is what lets a 40,000-row job be a few hundred bytes and what lets the worker
re-resolve it against the database as it stands at execution time. See
:mod:`vantage.jobs`.

Three rules shape the schema.

**Every column is nullable, and ``NULL`` means "not overridden".** That is the only way
to distinguish "the site owner cleared this field" from "the site owner never touched
it" — a blank ``CharField`` is a legitimate override (an empty ``ENVIRONMENT`` hides a
banner that ``settings.py`` asked for), so the usual Django advice to avoid ``null`` on
text columns is exactly wrong here.

**Only settings that are safe in a web form get a column.** ``AUTO_REGISTER``,
``EXCLUDE``, ``INCLUDE_ONLY``, ``NAV``, ``DASHBOARD``, ``PERMISSIONS`` and
``SETTINGS_PERMISSION`` are code, not configuration: they decide what exists and who
may reach it. The allowlist lives in :mod:`vantage.siteconfig`.

**Uploads are treated as hostile.** ``FileField`` rather than ``ImageField`` because
Pillow is not a dependency and never will be for this, which means the validation
Pillow would have done has to be written out — see :func:`validate_branding_upload`.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings as django_settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from vantage.color import parse_hex

# Imported eagerly, not inside the receiver below: this module is imported while the app
# registry loads, and it is what gets ``siteconfig``'s per-request invalidation hooked up
# before the first request rather than after it.
from vantage.siteconfig import invalidate

__all__ = [
    "ALLOWED_UPLOAD_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "SETTING_COLUMNS",
    "ListPreference",
    "SavedView",
    "SavedViewGrant",
    "SavedViewUserState",
    "SiteConfiguration",
    "VantageJob",
    "VantageLogEntry",
    "validate_brand_color",
    "validate_branding_upload",
    "validate_css_length",
]

#: What a logo or favicon may be. SVG earns its place — it is what a designer hands
#: over — and is the reason the rest of this function exists.
ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset({".svg", ".png", ".jpg", ".jpeg", ".webp"})

#: 2 MiB. A logo is a logo; anything larger is a mistake or an attempt to fill a disk.
MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024

#: Constructs that turn an uploaded SVG into stored XSS. An SVG is served from the
#: panel's own origin, so a ``<script>`` inside one runs with the session cookie of
#: whoever views the page — the logo is on every screen, so that is everyone.
#:
#: This is a rejection filter, not a sanitiser: it refuses files it does not
#: understand rather than trying to rewrite them into safety, because a rewriter that
#: is wrong once is worse than a refusal that is occasionally annoying. Serving media
#: from a separate origin remains the defence in depth this cannot replace.
_SVG_HAZARDS: tuple[tuple[re.Pattern[bytes], str], ...] = (
    (re.compile(rb"<\s*(?:[a-z0-9]+:)?script\b", re.IGNORECASE), "a <script> element"),
    (re.compile(rb"\son[a-z]+\s*=", re.IGNORECASE), "an inline event handler"),
    (
        re.compile(rb"attributeName\s*=\s*[\"']?\s*on", re.IGNORECASE),
        "an animation that writes an event handler",
    ),
    (re.compile(rb"javascript\s*:", re.IGNORECASE), "a javascript: URL"),
    (
        re.compile(rb"<\s*(?:[a-z0-9]+:)?(?:foreignObject|iframe|embed|object)\b", re.IGNORECASE),
        "an embedded document",
    ),
)

#: A CSS length the token stylesheet can emit verbatim. Anything else would be
#: interpolated straight into a ``<style>`` block.
_CSS_LENGTH = re.compile(r"^(?:0|\d*\.?\d+(?:rem|em|px|%))$")


def validate_branding_upload(value) -> None:
    """Reject anything that is not plausibly a logo, and any SVG that can execute.

    Runs on the field, not only in the form, so a shell or a data migration cannot
    route around it. An already-stored file is skipped: it was validated when it
    arrived, and re-reading it from storage on every ``full_clean`` would turn a form
    render into a download.
    """
    if getattr(value, "_committed", False):
        return

    name = getattr(value, "name", "") or ""
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_SUFFIXES))
        raise ValidationError(
            f"{name or 'That file'} is not an image this panel will serve. Use one of: {allowed}."
        )

    size = getattr(value, "size", None) or 0
    if size > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"That file is {size // 1024} KB. The limit is {MAX_UPLOAD_BYTES // 1024} KB."
        )

    if suffix != ".svg":
        return

    try:
        value.seek(0)
        payload = value.read()
    finally:
        value.seek(0)
    if isinstance(payload, str):  # pragma: no cover - only a text-mode file object
        payload = payload.encode("utf-8", "replace")

    for pattern, description in _SVG_HAZARDS:
        if pattern.search(payload):
            raise ValidationError(
                f"That SVG contains {description}, which would run as script on every "
                "page of the panel. Export it without scripting, or upload a PNG."
            )


def validate_brand_color(value: str) -> None:
    """Accept exactly what the palette derivation can read, and say so when it cannot."""
    try:
        parse_hex(value)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def validate_css_length(value: str) -> None:
    """The radius is interpolated straight into a ``<style>`` block, so it is not text."""
    if not _CSS_LENGTH.match(value.strip()):
        raise ValidationError(f"{value!r} is not a CSS length like '0.5rem', '8px' or '0'.")


def _branding_field(**kwargs) -> models.FileField:
    """Every uploadable brand asset, so the validators can never disagree."""
    return models.FileField(
        upload_to="vantage/branding/",
        blank=True,
        validators=[validate_branding_upload],
        **kwargs,
    )


class SiteConfiguration(models.Model):
    """UI-editable overrides for the ``VANTAGE`` settings dict. Exactly one row.

    A singleton rather than a key/value table: a fixed set of columns is what makes
    the resolution layer a dict lookup instead of a query per setting, and it lets the
    database type-check what the form accepted.
    """

    #: There is one site configuration, so there is one row, and it has a known
    #: primary key. Enforcing it in ``save`` rather than with a constraint keeps a
    #: second concurrent writer from creating a second row it would then have to lose.
    SINGLETON_PK = 1

    # Every ``null=True`` on a text column below carries a ``noqa``. The usual advice
    # against it assumes NULL and "" mean the same thing; here they are the two halves
    # of the feature. NULL is "inherit from settings.py", "" is "override it with
    # nothing" — an ENVIRONMENT of "" is how a site owner switches off a banner their
    # settings file turned on, and collapsing the two would make that unsayable.

    # ── branding ────────────────────────────────────────────────────────────
    site_title = models.CharField(max_length=120, null=True, blank=True)  # noqa: DJ001
    site_acronym = models.CharField(max_length=8, null=True, blank=True)  # noqa: DJ001
    logo = _branding_field()
    logo_dark = _branding_field()
    favicon = _branding_field()
    login_image = _branding_field()

    # ── appearance ──────────────────────────────────────────────────────────
    brand_color = models.CharField(  # noqa: DJ001
        max_length=7, null=True, blank=True, validators=[validate_brand_color]
    )
    brand_radius = models.CharField(  # noqa: DJ001
        max_length=16, null=True, blank=True, validators=[validate_css_length]
    )
    theme = models.CharField(  # noqa: DJ001
        max_length=8,
        null=True,
        blank=True,
        choices=[("light", "Light"), ("dark", "Dark"), ("system", "Follow the device")],
    )
    theme_toggle = models.BooleanField(null=True, blank=True)

    # ── behaviour ───────────────────────────────────────────────────────────
    environment = models.CharField(max_length=120, null=True, blank=True)  # noqa: DJ001
    default_per_page = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(500)]
    )
    command_palette = models.BooleanField(null=True, blank=True)
    read_only = models.BooleanField(null=True, blank=True)

    # ── provenance ──────────────────────────────────────────────────────────
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "site configuration"
        verbose_name_plural = "site configuration"

    def __str__(self) -> str:
        return "Site configuration"

    def save(self, *args, **kwargs) -> None:
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> SiteConfiguration:
        """The row, or an unsaved instance standing in for it.

        Never ``get_or_create``: opening a settings screen must not write to the
        database, and a row full of nulls is indistinguishable from no row at all.
        """
        return cls.objects.filter(pk=cls.SINGLETON_PK).first() or cls()


@receiver(post_save, sender=SiteConfiguration)
@receiver(post_delete, sender=SiteConfiguration)
def _invalidate(sender, **kwargs) -> None:
    """Saving the row must change the panel on the next read, not on the next restart."""
    invalidate()


# ── the audit log ───────────────────────────────────────────────────────────


class VantageLogEntry(models.Model):
    """One thing a person did through the panel.

    Scoped to *the panel*, not to the table. Row history — every write from every
    management command, task and shell session — is a different feature with a
    different cost, and django-simple-history already does it well. Requiring it would
    add a model and a migration to the **user's** app and roughly double their write
    volume, so this model is the smaller thing that answers the question people
    actually ask of an admin panel: who changed this, and to what.

    That scope is also why nothing here is wired to ``post_save``. A signal cannot see
    who was logged in, and it would faithfully record writes that never came through
    the panel — turning "what someone did in the admin" into a half-built,
    twice-as-expensive row history that is wrong about the actor. Entries are written
    from the form views and the action runner instead; see :mod:`vantage.audit`.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        ACTION = "action", "Ran an action"

    # ── who ─────────────────────────────────────────────────────────────────

    #: ``SET_NULL`` rather than ``CASCADE``: deleting a colleague must not delete the
    #: record of what they did. That is the one deletion an audit log cannot survive.
    actor = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    #: The username as it read at the time, kept for the same reason as
    #: ``object_repr``: the row above can go to NULL, and "somebody deleted the March
    #: invoices" is not an audit trail.
    actor_repr = models.CharField(max_length=150, blank=True)

    # ── what ────────────────────────────────────────────────────────────────

    action = models.CharField(max_length=10, choices=Action.choices)
    #: The bulk action's name, for ``action`` entries only. Kept separate from
    #: ``action`` so filtering by "every delete" and by "every run of ``purge``" are
    #: different questions with different answers.
    action_name = models.CharField(max_length=100, blank=True)

    # ── to what ─────────────────────────────────────────────────────────────

    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Text, not an integer: primary keys are UUIDs and character codes as often as
    #: they are integers, and a composite key has to encode as a string anyway.
    object_id = models.CharField(max_length=255, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")
    #: ``str(obj)`` at the time of the write. The generic FK dangles the moment the row
    #: is deleted — which is exactly when the log matters most — so the label is
    #: copied rather than dereferenced.
    object_repr = models.CharField(max_length=200, blank=True)

    #: ``{"field": {"from": ..., "to": ...}}``. Never a sensitive field and never the
    #: bytes of a file; :func:`vantage.audit.diff_from_form` is the only writer.
    changes = models.JSONField(default=dict, blank=True)

    # ── from where ──────────────────────────────────────────────────────────

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "log entry"
        verbose_name_plural = "log entries"
        ordering = ["-created_at", "-pk"]
        indexes = [
            # The per-record history panel. Both columns, because a generic FK with
            # only ``object_id`` indexed matches every model's row 7.
            models.Index(fields=["content_type", "object_id"], name="vantage_log_object_idx"),
            # The audit page's default order, and its "what did this person do" filter.
            models.Index(fields=["-created_at"], name="vantage_log_recent_idx"),
            models.Index(fields=["actor", "-created_at"], name="vantage_log_actor_idx"),
        ]

    def __str__(self) -> str:
        who = self.actor_repr or "Someone"
        what = self.action_name or self.get_action_display().lower()
        return f"{who} {what} {self.object_repr or 'a record'}"

    @property
    def model_label(self) -> str:
        """``"shop.order"``, or an empty string once the content type is gone."""
        return self.content_type.model if self.content_type_id else ""

    @property
    def change_list(self) -> list[dict]:
        """The diff as an ordered list, because templates cannot iterate a dict's items
        and sort them at the same time."""
        return [
            {"field": name, "from": delta.get("from"), "to": delta.get("to")}
            for name, delta in sorted((self.changes or {}).items())
        ]


# ── saved views ─────────────────────────────────────────────────────────────


class SavedView(models.Model):
    """A named filter, sort and search state for one model's list page.

    Two axes, kept apart on purpose. **Visibility** is who can see the view exists;
    **edit scope** is who can change it. Airtable conflates them and then has to warn
    its users that a personal view is not a security feature — because once "only I can
    see it" and "only I can edit it" are the same switch, the first sentence has to
    carry a promise the second one cannot keep. Neither axis is a security boundary
    here either: a view is a stored query string, and the rows it returns are filtered
    by :meth:`~vantage.permissions.PanelPermissions.filter_queryset` on every request
    regardless of who saved it.

    The state is stored as the panel's own filter grammar — the query parameters
    :mod:`vantage.filters` reads — rather than as resolved SQL or resolved dates. A
    relative window is stored as its ISO-8601 duration, so "last 7 days" still means
    the last seven days a month from now.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Only me"
        SHARED = "shared", "Everyone who can see this model"

    class EditScope(models.TextChoices):
        OWNER = "owner", "Only the owner"
        EDITORS = "editors", "The owner and named editors"
        EVERYONE = "everyone", "Anyone who can see it"

    #: ``"shop.order"``, lowercased, matching ``Options.label_lower``. A label rather
    #: than a ``ContentType`` FK: a view for a model that has since been deleted must
    #: still be listable so it can be explained and removed, and a dangling FK to a
    #: reaped content type cannot do that.
    model_label = models.CharField(max_length=100)

    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)

    owner = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="vantage_saved_views",
    )

    visibility = models.CharField(
        max_length=10, choices=Visibility.choices, default=Visibility.PRIVATE
    )
    edit_scope = models.CharField(
        max_length=10, choices=EditScope.choices, default=EditScope.OWNER
    )
    #: Freezes the configuration, not the data — and binds the creator too. A lock
    #: only the owner can ignore is a lock that protects nobody from the one person
    #: most likely to change the view by accident.
    locked = models.BooleanField(default=False)

    #: ``{"filters": {"status": ["paid"]}, "q": "", "sort": "-created_at"}``.
    state = models.JSONField(default=dict, blank=True)

    #: Optimistic locking. Bumped on every write to a shared view; a save carrying a
    #: stale number is refused rather than silently overwriting whatever the other
    #: person did in the meantime.
    version = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "saved view"
        verbose_name_plural = "saved views"
        ordering = ["name", "pk"]
        indexes = [
            models.Index(fields=["model_label"], name="vantage_view_model_idx"),
            models.Index(fields=["owner", "model_label"], name="vantage_view_owner_idx"),
        ]
        constraints = [
            # Two views called "Unpaid" on the same list page is a support ticket, not
            # a feature. Scoped per owner, so your name for a view never collides with
            # someone else's.
            models.UniqueConstraint(
                fields=["owner", "model_label", "name"],
                name="vantage_view_unique_name_per_owner",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_shared(self) -> bool:
        return self.visibility == self.Visibility.SHARED

    @property
    def autosaves(self) -> bool:
        """Whether typing in the filter rail may quietly rewrite this view.

        Only a locked-down personal view: private, owner-only, unlocked. Airtable's
        entire personal/locked view feature line exists because collaborative views
        autosave, and one person narrowing a filter silently changed what everyone
        else was looking at. A shared view therefore never autosaves — not even for
        its owner, because the owner is precisely the person whose habitual edits
        would land on everyone else's screen.
        """
        return (
            self.visibility == self.Visibility.PRIVATE
            and self.edit_scope == self.EditScope.OWNER
            and not self.locked
        )


class SavedViewGrant(models.Model):
    """Names one user or one group as a viewer or an editor of a shared view.

    Grants widen who may *see and edit the view*; they never widen who may see the
    **rows**. That check happens again, server-side, on every request.
    """

    class Role(models.TextChoices):
        VIEWER = "viewer", "Can use"
        EDITOR = "editor", "Can use and change"

    view = models.ForeignKey(SavedView, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
    )
    group = models.ForeignKey(
        "auth.Group", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "saved view grant"
        verbose_name_plural = "saved view grants"
        constraints = [
            # Exactly one subject. A grant with neither would silently apply to nobody
            # and a grant with both would need a precedence rule nobody would guess.
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, group__isnull=True)
                    | models.Q(user__isnull=True, group__isnull=False)
                ),
                name="vantage_grant_user_xor_group",
            ),
            models.UniqueConstraint(
                fields=["view", "user"],
                condition=models.Q(user__isnull=False),
                name="vantage_grant_unique_user",
            ),
            models.UniqueConstraint(
                fields=["view", "group"],
                condition=models.Q(group__isnull=False),
                name="vantage_grant_unique_group",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} — {self.get_role_display().lower()}"

    @property
    def subject(self) -> str:
        if self.user_id:
            return self.user.get_username()
        return str(self.group)


class SavedViewUserState(models.Model):
    """One person's relationship with one view: pinned, default, last used.

    Separate from :class:`SavedView` because it is the half that is *per viewer*. A
    shared view has one configuration and as many of these rows as there are people
    using it, which is what lets "make this my default" be a personal act rather than
    a change to everyone else's screen.
    """

    view = models.ForeignKey(SavedView, on_delete=models.CASCADE, related_name="user_states")
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    pinned = models.BooleanField(default=False)
    #: Opens instead of the bare list page. Enforced as one per user per model in
    #: :func:`vantage.savedviews.set_default`, not by a constraint: the model label
    #: lives on the view, and a constraint cannot reach through a join.
    is_default = models.BooleanField(default=False)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "saved view state"
        verbose_name_plural = "saved view states"
        constraints = [
            models.UniqueConstraint(
                fields=["view", "user"], name="vantage_view_state_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} · {self.view}"


# ── per-list customisation ──────────────────────────────────────────────────


class ListPreference(models.Model):
    """One person's tuning of one model's list page — or, with a ``NULL`` user, the
    site-wide default for it.

    The same shape as :class:`SiteConfiguration`, one level down: a fixed set of
    columns, every one nullable, where ``NULL`` means *the code decides*. That is what
    lets someone reorder the columns of the orders list without also freezing its
    filters, its search fields and its stats at whatever they happened to be that
    afternoon — the per-attribute fallthrough :class:`~vantage.panels.ModelPanel`
    already guarantees for Python, extended to the database layer above it. Resolution
    lives in :mod:`vantage.listprefs`; nothing here interprets its own columns.

    Two rows can apply to one page and the order between them is not negotiable: a
    per-user row wins over the site-wide row, attribute by attribute. Anything else
    would let a colleague changing the default silently overwrite a choice somebody
    else made deliberately.

    ``resource_key`` is ``"app_label.modelname"`` as text, matching
    ``Options.label_lower``, rather than a ``ContentType`` foreign key. A stored
    preference for a model that is no longer registered — renamed, moved to another
    app, deleted — must degrade into an ignored row that can still be listed and
    cleaned up, not into a cascade that deletes it or a dangling FK to a reaped content
    type. It is also why every value in the JSON columns below is re-validated against
    the live panel on the way *out*: what a panel exposed when the row was written is
    not what it exposes today.
    """

    #: ``"shop.order"``. Matched exactly, so a model that is not registered simply
    #: never matches and its row is inert.
    resource_key = models.CharField(max_length=100)

    #: ``NULL`` is the site-wide default. ``CASCADE`` because a personal display
    #: preference has no meaning once the person is gone — unlike an audit entry,
    #: nothing is lost by removing it.
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="vantage_list_preferences",
    )

    # ── columns and paging ──────────────────────────────────────────────────

    #: An ordered list of column names. Order is the list itself; a separate position
    #: column would be a second source of truth for something JSON already expresses.
    columns = models.JSONField(null=True, blank=True)
    per_page = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(500)]
    )

    # ── search ──────────────────────────────────────────────────────────────

    search_enabled = models.BooleanField(null=True, blank=True)
    search_fields = models.JSONField(null=True, blank=True)

    # ── filters ─────────────────────────────────────────────────────────────

    filters_enabled = models.BooleanField(null=True, blank=True)
    filter_fields = models.JSONField(null=True, blank=True)
    #: Tri-state on purpose. ``NULL`` leaves counts where the code left them — opt-in
    #: per request via ``?_facets`` — because forcing them on for a list nobody has
    #: measured is how a page acquires an aggregate per render by accident.
    facet_counts = models.BooleanField(null=True, blank=True)

    # ── analytics ───────────────────────────────────────────────────────────

    stats_enabled = models.BooleanField(null=True, blank=True)
    #: Which declared stats survive, in order. ``NULL`` is "all of them".
    stat_keys = models.JSONField(null=True, blank=True)
    #: Stats built in the UI: ``[{"field": "total", "aggregate": "sum", "label": …}]``.
    #: Stored as the ingredients, never as an expression — a stored callable or a
    #: stored ORM object would be code arriving from a form.
    extra_stats = models.JSONField(null=True, blank=True)

    charts_enabled = models.BooleanField(null=True, blank=True)
    chart_keys = models.JSONField(null=True, blank=True)
    extra_charts = models.JSONField(null=True, blank=True)

    # ── summary bar ─────────────────────────────────────────────────────────

    summary_enabled = models.BooleanField(null=True, blank=True)
    #: ``{"total": "sum", "stock": "none"}``. ``"none"`` is a real choice — switching
    #: off an aggregate the inference offered — and is why this is not merely a list.
    summary_aggregates = models.JSONField(null=True, blank=True)

    # ── provenance ──────────────────────────────────────────────────────────

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "list preference"
        verbose_name_plural = "list preferences"
        ordering = ["resource_key", "pk"]
        indexes = [
            # The resolution query: both rows that can apply to one page, in one go.
            models.Index(fields=["resource_key", "user"], name="vantage_listpref_idx"),
        ]
        constraints = [
            # Two constraints rather than one, because SQL treats NULLs as distinct and
            # a single UniqueConstraint over (resource_key, user) would therefore
            # police the per-user rows and let site-wide rows multiply unnoticed.
            models.UniqueConstraint(
                fields=["resource_key", "user"],
                condition=models.Q(user__isnull=False),
                name="vantage_listpref_unique_user",
            ),
            models.UniqueConstraint(
                fields=["resource_key"],
                condition=models.Q(user__isnull=True),
                name="vantage_listpref_unique_default",
            ),
        ]

    def __str__(self) -> str:
        who = self.user.get_username() if self.user_id else "everyone"
        return f"{self.resource_key} · {who}"

    @property
    def is_site_default(self) -> bool:
        return self.user_id is None

    def is_empty(self) -> bool:
        """Whether every setting has been reset back to what the code declares.

        A row of nothing but NULLs resolves identically to no row at all, so the write
        path deletes it instead of keeping a tombstone that later has to be explained.
        """
        return all(getattr(self, name) is None for name in SETTING_COLUMNS)


#: Every column :mod:`vantage.listprefs` may read or write on a
#: :class:`ListPreference`. Declared next to the model so "is this row empty" and "what
#: does resetting a section clear" can never drift from the schema.
SETTING_COLUMNS: tuple[str, ...] = (
    "columns",
    "per_page",
    "search_enabled",
    "search_fields",
    "filters_enabled",
    "filter_fields",
    "facet_counts",
    "stats_enabled",
    "stat_keys",
    "extra_stats",
    "charts_enabled",
    "chart_keys",
    "extra_charts",
    "summary_enabled",
    "summary_aggregates",
)


# ── background jobs ─────────────────────────────────────────────────────────


class VantageJob(models.Model):
    """One bulk action that was moved off the request, and how far it has got.

    The load-bearing column is :attr:`selection`. It stores the *predicate* the list
    page transmitted — the filter query string plus the handful of rows the user
    unticked — and never the primary keys it resolves to. That is the same decision
    :class:`~vantage.actions.Selection` makes for the POST body, taken one step
    further: a predicate is a few hundred bytes whether it matches nine rows or ninety
    thousand, it survives being written to a database and read back by a worker
    process, and it re-resolves against the data *as it stands when the job runs*
    rather than as it stood when someone clicked. A materialised id list would be none
    of those things, and at 40,000 rows would be a megabyte of JSON per click.

    Counts are stored, not derived. ``processed``/``total``/``failed`` are written as
    the worker goes so the progress bar has something honest to read, and so a job the
    worker died in the middle of still says how far it got. ``failed`` is separate from
    ``processed`` because "38,000 succeeded, 12 failed" is the normal outcome at scale
    and collapsing it into one number is how a report starts lying.

    ``errors`` holds ``(row, pk, error)`` triples up to a cap — enough to produce the
    downloadable CSV, bounded so a pathological job cannot write a 40,000-element JSON
    blob into one column. Past the cap the count in ``failed`` is still exact; it is
    only the individual messages that stop being kept, and the CSV says so.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Finished"
        FAILED = "failed", "Failed"
        #: Not a failure of the job — a refusal. The actor's permission was withdrawn
        #: between enqueue and execution, or part-way through it, and the run stopped
        #: where it stood. Distinct from FAILED because nothing went wrong: the answer
        #: changed, and the count already processed is real work that stands.
        DENIED = "denied", "Stopped — permission withdrawn"

    #: Statuses after which nothing more will happen. The progress bar polls until it
    #: sees one of these and then stops; a bar that polls forever is a bug, so the set
    #: lives here rather than being re-listed in the view and again in the JavaScript.
    TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "denied"})

    # ── who ─────────────────────────────────────────────────────────────────

    #: ``SET_NULL`` for the same reason as the audit log: deleting a colleague must not
    #: delete the record of the 40,000 rows they changed last Tuesday.
    actor = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vantage_jobs",
    )
    actor_repr = models.CharField(max_length=150, blank=True)

    # ── what ────────────────────────────────────────────────────────────────

    #: ``"shop.order"``, matching ``Options.label_lower`` — a label rather than a
    #: ``ContentType`` FK, exactly as :class:`ListPreference` does it, so a job for a
    #: model that has since been unregistered stays listable and explainable instead of
    #: cascading away or dangling.
    resource_key = models.CharField(max_length=100)
    action_name = models.CharField(max_length=100)
    #: The label as the button read at the time. Kept alongside the name because a
    #: panel author renaming an action must not retitle last month's job history.
    action_label = models.CharField(max_length=150, blank=True)

    #: ``{"select_all": true, "excluded": ["12"], "query": "status=paid&q=acme"}``.
    selection = models.JSONField(default=dict, blank=True)

    # ── how far ─────────────────────────────────────────────────────────────

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    total = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)

    #: One sentence naming every non-zero outcome, in the shape
    #: :meth:`vantage.importer.ImportResult.summary` produces. Never "some rows failed".
    summary = models.CharField(max_length=300, blank=True)
    #: ``[{"row": 39001, "pk": "8123", "error": "…"}]``, capped.
    errors = models.JSONField(default=list, blank=True)
    #: The traceback-free description of a failure that stopped the whole job, as
    #: opposed to the per-row failures above. Empty for a job that merely had bad rows.
    error = models.TextField(blank=True)

    # ── where it ran ────────────────────────────────────────────────────────

    #: Which queue actually took it: ``django_tasks``, ``celery``, ``rq`` or
    #: ``inprocess``. Recorded rather than inferred, because the answer depends on what
    #: was installed at enqueue time and a job outlives that.
    backend = models.CharField(max_length=20, blank=True)
    #: The queue's own identifier, when it has one. Blank for the in-process runner.
    task_id = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "background job"
        verbose_name_plural = "background jobs"
        ordering = ["-created_at", "-pk"]
        indexes = [
            # The jobs page: mine, newest first. Also the only query the progress
            # poller's neighbours make, so it is worth an index of its own.
            models.Index(fields=["actor", "-created_at"], name="vantage_job_actor_idx"),
            # "Is anything still running?" — asked by the sweeper and by the badge.
            models.Index(fields=["status", "-created_at"], name="vantage_job_status_idx"),
            models.Index(fields=["resource_key", "-created_at"], name="vantage_job_model_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.action_label or self.action_name} · {self.resource_key}"

    def get_absolute_url(self) -> str:
        """The jobs page, anchored on this row. There is no per-job page: one job is
        three numbers and a sentence, which is a row, not a screen."""
        from django.urls import reverse

        return f"{reverse('vantage:jobs')}#job-{self.pk}"

    # ── reading ─────────────────────────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL

    @property
    def succeeded(self) -> int:
        """Rows that came through cleanly. ``processed`` counts attempts, not wins."""
        return max(self.processed - self.failed, 0)

    @property
    def percent(self) -> int:
        """Nought to a hundred, and never a lie by rounding.

        A job that has finished reads 100 even if ``total`` was re-resolved smaller
        part-way through, and a job with nine of ten thousand rows done reads 0 rather
        than being rounded up into looking like progress that has not happened.
        """
        if self.is_terminal:
            return 100
        if not self.total:
            return 0
        return min(int(self.processed * 100 / self.total), 100)

    def may_be_viewed_by(self, user) -> bool:
        """Its own actor, or a superuser. Nobody else.

        A job row names a model, an action and a row count for data the viewer may have
        no access to at all, so it is *not* gated on the model permission the action
        needed — that permission can be granted and revoked, and a job is a record of
        one person's activity. This is the same reasoning that keeps the audit log
        behind its own gate rather than behind per-model view rights.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        return self.actor_id is not None and self.actor_id == user.pk
