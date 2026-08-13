"""The three things Vantage stores: its settings, what people did, and how they look.

Vantage configures itself from ``settings.VANTAGE``, so owning tables at all is a
decision that has to be justified once per table. Three earn it.

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
    "SavedView",
    "SavedViewGrant",
    "SavedViewUserState",
    "SiteConfiguration",
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
