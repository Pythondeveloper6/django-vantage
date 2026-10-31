"""The six things Vantage stores: its settings, its sidebar, what people did, how they
look, how each list is tuned, and what is still running.

Vantage configures itself from ``settings.VANTAGE``, so owning tables at all is a
decision that has to be justified once per table. Six earn it.

**The settings cascade.** A project owner states their settings in
``settings.VANTAGE``; the person who actually runs the panel day to day is usually not
the person who can edit that file. :class:`SiteConfiguration` gives them a row that
sits *underneath* attribute access on :data:`~vantage.conf.vantage_settings`, so the
branding can be changed from the panel without a deploy, and without a single call
site knowing a database is involved.

**The sidebar.** :class:`NavConfiguration` is one row holding the arrangement someone
built in the settings screen: the groups, their order, what is in them, what each item
is called and who may see it. It sits *above* ``VANTAGE["NAV"]`` rather than inside the
settings cascade, which is the distinction that keeps the allowlist in
:mod:`vantage.siteconfig` honest — a stored arrangement is presentation, and
:mod:`vantage.nav` re-checks every item against the live registry and the permission
layer on the way out, so it can reorder, rename and hide but never reveal.

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
``EXCLUDE``, ``INCLUDE_ONLY``, ``DASHBOARD``, ``PERMISSIONS`` and
``SETTINGS_PERMISSION`` are code, not configuration: they decide what exists and who
may reach it. The allowlist lives in :mod:`vantage.siteconfig`. ``NAV`` is absent from
it too, and stays absent: the sidebar is editable from the UI through
:class:`NavConfiguration`, which is a *presentation* layer over the same registry, not
a settings value the cascade hands back to ``vantage_settings.NAV``.

**Uploads are treated as hostile.** ``FileField`` rather than ``ImageField`` because
Pillow is not a dependency and never will be for this, which means the validation
Pillow would have done has to be written out — see :func:`validate_branding_upload`.
"""

from __future__ import annotations

import re
from datetime import time as dt_time
from pathlib import Path
from uuid import uuid4

from django.conf import settings as django_settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from vantage.color import parse_hex

# Imported eagerly, not inside the receiver below: this module is imported while the app
# registry loads, and it is what gets ``siteconfig``'s per-request invalidation hooked up
# before the first request rather than after it.
from vantage.siteconfig import invalidate

__all__ = [
    "ALLOWED_UPLOAD_SUFFIXES",
    "MAX_UPLOAD_BYTES",
    "SETTING_COLUMNS",
    "ChangeRequest",
    "DashboardLayout",
    "ListPreference",
    "NavConfiguration",
    "SavedView",
    "SavedViewGrant",
    "SavedViewUserState",
    "SiteConfiguration",
    "TwoFactorDevice",
    "TwoFactorRecoveryCode",
    "UndoEntry",
    "VantageJob",
    "VantageLogEntry",
    "Webhook",
    "WebhookDelivery",
    "validate_brand_color",
    "validate_branding_upload",
    "validate_css_length",
]

#: What a logo or favicon may be. SVG earns its place — it is what a designer hands
#: over — and is the reason the rest of this function exists.
ALLOWED_UPLOAD_SUFFIXES: frozenset[str] = frozenset({".svg", ".png", ".jpg", ".jpeg", ".webp"})

#: 2 MiB. A logo is a logo; anything larger is a mistake or an attempt to fill a disk.
MAX_UPLOAD_BYTES: int = 2 * 1024 * 1024

#: Display names for what :func:`vantage.security.markup_hazard` refuses: the
#: constructs that turn an uploaded SVG into stored XSS. An SVG is served from the
#: panel's own origin, so a ``<script>`` inside one runs with the session cookie of
#: whoever views the page — the logo is on every screen, so that is everyone.
#:
#: This is a rejection filter, not a sanitiser: it refuses files it does not
#: understand rather than trying to rewrite them into safety, because a rewriter that
#: is wrong once is worse than a refusal that is occasionally annoying. Serving media
#: from a separate origin remains the defence in depth this cannot replace.
_HAZARD_LABELS: dict[str, str | Promise] = {
    # Translators: each of these names one thing found inside an uploaded SVG, and is
    # substituted into "That SVG contains %(hazard)s, which would run as script ...".
    "a <script> element": _("a <script> element"),
    "an inline event handler": _("an inline event handler"),
    "an animation that writes an event handler": _("an animation that writes an event handler"),
    "a javascript: URL": _("a javascript: URL"),
    "an embedded data: document": _("an embedded data: document"),
    "an embedded document": _("an embedded document"),
    "an entity declaration": _("an entity declaration"),
}

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
        # Two whole sentences rather than one with the filename swapped for a pronoun:
        # a translator cannot inflect around a fragment they never see in context.
        if name:
            raise ValidationError(
                _("%(name)s is not an image this panel will serve. Use one of: %(allowed)s.")
                % {"name": name, "allowed": allowed}
            )
        raise ValidationError(
            _("That file is not an image this panel will serve. Use one of: %(allowed)s.")
            % {"allowed": allowed}
        )

    size = getattr(value, "size", None) or 0
    if size > MAX_UPLOAD_BYTES:
        raise ValidationError(
            _("That file is %(size)s KB. The limit is %(limit)s KB.")
            % {"size": size // 1024, "limit": MAX_UPLOAD_BYTES // 1024}
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

    # One scanner for all three upload layers; see vantage.security.markup_hazard for
    # the normalisation that makes it hard to spell a hazard some other way.
    from vantage.security import markup_hazard

    description = markup_hazard(payload)
    if description:
        raise ValidationError(
            _(
                "That SVG contains %(hazard)s, which would run as script on every "
                "page of the panel. Export it without scripting, or upload a PNG."
            )
            % {"hazard": _HAZARD_LABELS.get(description, description)}
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
        raise ValidationError(
            _("%(value)r is not a CSS length like '0.5rem', '8px' or '0'.") % {"value": value}
        )


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
        choices=[
            ("light", _("Light")),
            ("dark", _("Dark")),
            ("system", _("Follow the device")),
        ],
    )
    theme_toggle = models.BooleanField(null=True, blank=True)
    # The preset *name*; its choices are not a static list (UI_THEMES registers more), so
    # the settings form validates it against the live registry instead of ``choices``.
    ui_theme = models.CharField(max_length=32, null=True, blank=True)  # noqa: DJ001
    ui_theme_toggle = models.BooleanField(null=True, blank=True)

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
        verbose_name = _("site configuration")
        verbose_name_plural = _("site configuration")

    def __str__(self) -> str:
        return str(_("Site configuration"))

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


# ── the sidebar ─────────────────────────────────────────────────────────────


class NavConfiguration(models.Model):
    """The sidebar as somebody arranged it in the UI. Exactly one row.

    The same singleton shape as :class:`SiteConfiguration`, and for the same reason:
    there is one sidebar, so there is one row, and ``NULL`` means *the code decides*.
    Clearing :attr:`groups` — which is what the screen's Reset button does by deleting
    the row outright — hands the sidebar back to ``VANTAGE["NAV"]``, and back to
    per-app inference when there is no declaration either.

    One JSON column rather than a table of groups and a table of items. Three reasons,
    and the third is the one that decides it:

    * Order is the list, as it is for :attr:`ListPreference.columns`. A position column
      is a second source of truth for something a JSON array already expresses exactly.
    * The tree is read whole, on every page, by one query. Two joined tables would be
      two queries and a sort for something that is a few kilobytes.
    * **Nothing here is a foreign key, deliberately.** An item names a model as the
      text ``"shop.order"``, a permission as ``"shop.view_order"`` and an audience as a
      group *name*. Unregistering a model, dropping a permission or renaming a group
      must leave an inert entry that the editor can show and remove — never a cascade
      that silently deletes somebody's arrangement, and never a dangling id.

    Nothing in this class interprets its own column. Every value is re-validated
    against the live registry and the live permission layer each time it is read; see
    :func:`vantage.nav.clean_declaration` and :func:`vantage.nav.build_nav`.
    """

    SINGLETON_PK = 1

    #: ``[{"label": "Commerce", "icon": "shopping-cart", "items": [...]}]``. ``NULL``
    #: is "no arrangement stored", which is not the same as an empty list — an empty
    #: list is a sidebar somebody emptied, and the two must stay distinguishable for
    #: the same reason every column on :class:`SiteConfiguration` is nullable.
    groups = models.JSONField(null=True, blank=True)

    #: The top bar's arrangement, on the same row rather than in a table of its own.
    #: Both are one site-wide statement of how the chrome is laid out, edited on the
    #: same screen and saved together, so a second singleton would be a second query
    #: and a second thing to keep consistent for no gain. ``NULL`` means "not
    #: arranged", distinct from an empty list, exactly as ``groups`` above.
    #:
    #: :mod:`vantage.topbar` reads this through :mod:`vantage.nav`'s request-scoped
    #: cache, so rendering both costs one query between them.
    topbar = models.JSONField(null=True, blank=True)

    #: Every model key that has ever been *claimed* — placed in a group, hidden or
    #: not — by a save of :attr:`groups`. This is what lets :mod:`vantage.nav` tell
    #: "never arranged, so still an unlisted leftover" from "arranged once, then
    #: explicitly removed", the same distinction :attr:`DashboardLayout.known_keys`
    #: draws for the dashboard. It only ever grows: a key already here stays here even
    #: once its item is removed, because removing it *is* the fact this field exists to
    #: remember. A key that was never claimed — the sidebar editor never turns an
    #: unlisted leftover into an editable row — is untouched by a save and keeps
    #: reappearing through ``NAV_UNLISTED`` for as long as nobody places it.
    known_keys = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("sidebar")
        verbose_name_plural = _("sidebar")

    def __str__(self) -> str:
        return str(_("Sidebar"))

    def save(self, *args, **kwargs) -> None:
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> NavConfiguration:
        """The row, or an unsaved instance standing in for it.

        Never ``get_or_create``: opening the settings screen must not write a row, and
        a row holding ``NULL`` resolves identically to no row at all.
        """
        return cls.objects.filter(pk=cls.SINGLETON_PK).first() or cls()


@receiver(post_save, sender=NavConfiguration)
@receiver(post_delete, sender=NavConfiguration)
def _invalidate_nav(sender, **kwargs) -> None:
    """Saving the arrangement must change the sidebar on the next read.

    Imported inside the receiver rather than at module scope: :mod:`vantage.nav`
    reaches back into the app registry for this very model, and importing it while the
    registry is still loading this module would be a cycle waiting for a bad day.
    """
    from vantage.nav import invalidate_nav

    invalidate_nav()


# ── the audit log ───────────────────────────────────────────────────────────


#: The past-tense verb for each action, as a whole word. ``VantageLogEntry.__str__``
#: reads as a sentence — "ada updated Invoice 12" — so the choice *label* ("Updated")
#: cannot simply be lowercased on the way in: case is not a mechanical transform in
#: German, where a lowercased noun is a spelling mistake, or in Turkish, where it moves
#: a dot. One translated verb per action instead.
_ACTION_VERBS: dict[str, str | Promise] = {
    "create": _("created"),
    "update": _("updated"),
    "delete": _("deleted"),
    "action": _("ran an action"),
}


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
        CREATE = "create", _("Created")
        UPDATE = "update", _("Updated")
        DELETE = "delete", _("Deleted")
        # Distinct from DELETE because the difference is the whole point: one is
        # recoverable and one is not, and a log that calls both "Deleted" cannot
        # answer the only question anyone asks it afterwards.
        TRASH = "trash", _("Moved to trash")
        RESTORE = "restore", _("Restored")
        ACTION = "action", _("Ran an action")

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
        verbose_name = _("log entry")
        verbose_name_plural = _("log entries")
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
        # One sentence with named parts rather than three pieces joined by spaces:
        # subject-verb-object is an English word order, and a translator has to be able
        # to move the object in front of the verb without editing Python.
        return _("%(who)s %(did)s %(what)s") % {
            "who": self.actor_repr or _("Someone"),
            "did": self.action_name or _ACTION_VERBS.get(self.action, self.action),
            "what": self.object_repr or _("a record"),
        }

    @property
    def model_label(self) -> str:
        """``"shop.order"``, or an empty string once the content type is gone."""
        return self.content_type.model if self.content_type_id else ""

    @property
    def change_list(self) -> list[dict]:
        """The diff as an ordered list, because templates cannot iterate a dict's items
        and sort them at the same time.

        A field that is sensitive *now* shows as "changed — value not recorded" even if
        the entry was written before it was declared so: masking is applied when the log
        is read as well as when it is written. The content type comes from Django's
        per-process cache, so this costs no query per entry.
        """
        hidden = self._sensitive_now()
        # Set by vantage.fieldperms.hide_entry_fields for the person reading the log:
        # a field hidden from them is left out altogether, not even "changed".
        absent = getattr(self, "vantage_hidden_fields", frozenset())
        return [
            {"field": name, "from": None, "to": None}
            if name in hidden
            else {"field": name, "from": delta.get("from"), "to": delta.get("to")}
            for name, delta in sorted((self.changes or {}).items())
            if name not in absent
        ]

    @property
    def revealed_field(self) -> str:
        """The field name for a "revealed <field> on <record>" entry, else ``""``."""
        name = self.action_name or ""
        if self.action != self.Action.ACTION or not name.startswith("revealed "):
            return ""
        return name.removeprefix("revealed ").removesuffix(" on")

    @property
    def restored_as_of(self):
        """The moment a "restored as of <when>" entry restored to, else ``None``.

        Parsed rather than shown as stored, so the page can format it the way it
        formats every other timestamp.
        """
        from django.utils.dateparse import parse_datetime

        name = self.action_name or ""
        prefix = "restored as of "
        if self.action != self.Action.UPDATE or not name.startswith(prefix):
            return None
        try:
            return parse_datetime(name.removeprefix(prefix))
        except ValueError:
            return None

    def _sensitive_now(self) -> frozenset[str]:
        if not self.content_type_id or not self.changes:
            return frozenset()
        try:
            model = ContentType.objects.get_for_id(self.content_type_id).model_class()
        except ContentType.DoesNotExist:
            return frozenset()
        if model is None:
            return frozenset()
        from vantage.sensitive import sensitive_names

        return sensitive_names(model)


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
        PRIVATE = "private", _("Only me")
        SHARED = "shared", _("Everyone who can see this model")

    class EditScope(models.TextChoices):
        OWNER = "owner", _("Only the owner")
        EDITORS = "editors", _("The owner and named editors")
        EVERYONE = "everyone", _("Anyone who can see it")

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
    edit_scope = models.CharField(max_length=10, choices=EditScope.choices, default=EditScope.OWNER)
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
        verbose_name = _("saved view")
        verbose_name_plural = _("saved views")
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


#: The lowercase form of each role label, for the phrase ``SavedViewGrant.__str__``
#: builds. Same reason as :data:`_ACTION_VERBS`: ``get_role_display().lower()`` would
#: be lowercasing a word a translator wrote.
_ROLE_PHRASES: dict[str, str | Promise] = {
    "viewer": _("can use"),
    "editor": _("can use and change"),
}


class SavedViewGrant(models.Model):
    """Names one user or one group as a viewer or an editor of a shared view.

    Grants widen who may *see and edit the view*; they never widen who may see the
    **rows**. That check happens again, server-side, on every request.
    """

    class Role(models.TextChoices):
        VIEWER = "viewer", _("Can use")
        EDITOR = "editor", _("Can use and change")

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
        verbose_name = _("saved view grant")
        verbose_name_plural = _("saved view grants")
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
        return _("%(subject)s — %(role)s") % {
            "subject": self.subject,
            "role": _ROLE_PHRASES.get(self.role, self.get_role_display()),
        }

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
        verbose_name = _("saved view state")
        verbose_name_plural = _("saved view states")
        constraints = [
            models.UniqueConstraint(fields=["view", "user"], name="vantage_view_state_unique")
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
    #: ``{"total": 180}`` — pixel widths, keyed by column name. Only the columns a
    #: person has actually dragged appear here; everything else keeps the width the
    #: browser gives it. Bounds live in :mod:`vantage.listprefs`, next to the rest of
    #: the validation, and are re-applied on every read: a width stored against a
    #: column that has since changed still has to render inside them.
    column_widths = models.JSONField(null=True, blank=True)
    #: Column names frozen to the inline-start edge, in the order they render — not
    #: the order they were pinned in. A plain list rather than a rank map: the table
    #: itself already supplies the order sticky offsets have to accumulate in.
    pinned_columns = models.JSONField(null=True, blank=True)

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

    # ── which view ──────────────────────────────────────────────────────────

    #: The list view this person last chose — ``"table"``, ``"kanban"``, … — read by
    #: :mod:`vantage.listviews`. Not part of any rail section: it is set by using the
    #: switcher, not by a form, and resetting the columns must not forget it. ``NULL``
    #: rather than ``""`` for the reason every column here is nullable: ``NULL`` is
    #: "the code decides", and no empty string means anything else.
    list_view = models.CharField(max_length=20, null=True, blank=True)  # noqa: DJ001
    #: ``{"kanban": "priority"}``: the field each mode was last arranged by, from the
    #: board's "Group by" and the calendar's "Dates from" pickers. Validated against
    #: the live panel on every read, like everything else in this row.
    list_view_fields = models.JSONField(null=True, blank=True)

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
        verbose_name = _("list preference")
        verbose_name_plural = _("list preferences")
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
        # "everyone" is the site-wide row's stand-in for a person's name, so it is read
        # by whoever is looking at the list of preferences and has to be translated.
        who = self.user.get_username() if self.user_id else _("everyone")
        return f"{self.resource_key} · {who}"

    @property
    def is_site_default(self) -> bool:
        return self.user_id is None

    def is_empty(self) -> bool:
        """Whether every setting has been reset back to what the code declares.

        A row of nothing but NULLs resolves identically to no row at all, so the write
        path deletes it instead of keeping a tombstone that later has to be explained.
        """
        if self.list_view is not None or self.list_view_fields:
            # A remembered view is a setting too, just not one a rail section owns.
            return False
        return all(getattr(self, name) is None for name in SETTING_COLUMNS)


#: Every column :mod:`vantage.listprefs` may read or write on a
#: :class:`ListPreference`. Declared next to the model so "is this row empty" and "what
#: does resetting a section clear" can never drift from the schema.
SETTING_COLUMNS: tuple[str, ...] = (
    "columns",
    "per_page",
    "column_widths",
    "pinned_columns",
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
        PENDING = "pending", _("Queued")
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Finished")
        FAILED = "failed", _("Failed")
        #: Not a failure of the job — a refusal. The actor's permission was withdrawn
        #: between enqueue and execution, or part-way through it, and the run stopped
        #: where it stood. Distinct from FAILED because nothing went wrong: the answer
        #: changed, and the count already processed is real work that stands.
        DENIED = "denied", _("Stopped — permission withdrawn")

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
        verbose_name = _("background job")
        verbose_name_plural = _("background jobs")
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


# ── webhooks ────────────────────────────────────────────────────────────────


class Webhook(models.Model):
    """A URL that is told, with a signed POST, when a record is created, changed or deleted.

    The subscription is two lists rather than two relations: ``resources`` holds
    ``Options.label_lower`` strings (``"shop.order"``) exactly as :class:`VantageJob`'s
    ``resource_key`` does, so a webhook for a model that has since been unregistered
    stays listable and editable instead of cascading away. An empty ``resources`` list means
    every model, and an empty ``events`` list means every event; the form never produces
    an empty ``events`` list, but a row written by hand still does something sensible.

    ``secret`` is stored in the clear because the HMAC cannot be computed from a hash.
    That is the same trade every webhook sender makes; the page shows it once, when it is
    generated, and never prints it again. See :mod:`vantage.webhooks`.
    """

    name = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    resources = models.JSONField(default=list, blank=True)
    events = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    secret = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    #: ``SET_NULL`` for the same reason as the audit log's actor.
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("webhook")
        verbose_name_plural = _("webhooks")
        ordering = ["name", "pk"]
        permissions = [("manage_webhooks", _("Can manage webhooks"))]

    def __str__(self) -> str:
        return self.name

    def subscribes_to(self, resource_key: str, event: str) -> bool:
        """Whether a change of ``event`` to a ``resource_key`` record concerns this hook."""
        if not self.active:
            return False
        if self.resources and resource_key not in self.resources:
            return False
        return not self.events or event in self.events


class WebhookDelivery(models.Model):
    """One attempt chain to send one event to one webhook, and what happened.

    The body is stored, not rebuilt, so a retry sends what the first attempt sent and a
    redelivery sends what the original did. ``response_body`` is truncated on the way in:
    the log exists to answer "what did they say", and a 2 MB error page is not an answer.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Queued")
        RETRYING = "retrying", _("Waiting to retry")
        SUCCEEDED = "succeeded", _("Delivered")
        FAILED = "failed", _("Failed")

    TERMINAL: frozenset[str] = frozenset({"succeeded", "failed"})

    #: ``CASCADE``: deleting a webhook deletes its log, which is what its owner expects.
    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name="deliveries")
    #: Sent as ``X-Vantage-Delivery``. Unique per chain; a redelivery gets a new one.
    uuid = models.UUIDField(default=uuid4, editable=False, unique=True)
    event = models.CharField(max_length=20)
    resource_key = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    response_code = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.CharField(max_length=300, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    #: The delivery this one re-sent, if it was a redelivery.
    redelivery_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = _("webhook delivery")
        verbose_name_plural = _("webhook deliveries")
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["webhook", "-created_at"], name="vantage_whd_hook_idx"),
            models.Index(fields=["status", "next_attempt_at"], name="vantage_whd_due_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event} → {self.webhook_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL


# ── scheduled reports ───────────────────────────────────────────────────────


class ReportSubscription(models.Model):
    """A saved view, mailed to some addresses as a CSV on a schedule.

    It stores *which* view and *whose* it is, never a result and never a permission
    snapshot: every send re-runs the view as ``user`` as the database knows them at that
    moment, so a colleague who has since left, or lost access to the model, receives
    nothing. See :mod:`vantage.reports`.

    ``next_run_at`` is the whole schedule. The cron command only asks "which active rows
    are due", and claims one by a conditional update on this column, which is what keeps
    two overlapping runs from sending it twice. ``NULL`` while paused.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily", _("Every day")
        WEEKLY = "weekly", _("Every week")
        MONTHLY = "monthly", _("Every month")

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")

    #: ``CASCADE``: a subscription to a deleted view has nothing left to run.
    view = models.ForeignKey(SavedView, on_delete=models.CASCADE, related_name="reports")
    #: Whose permissions, tenant and column choices the export runs under.
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vantage_reports",
    )
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default="weekly")
    #: 0 is Monday, as in :meth:`datetime.date.weekday`. Used by ``weekly`` only.
    weekday = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(6)]
    )
    #: 1 to 31, clamped to the month's last day. Used by ``monthly`` only.
    day_of_month = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(31)]
    )
    #: Wall-clock time in the site timezone (``TIME_ZONE``).
    send_time = models.TimeField(default=dt_time(8, 0))
    #: Whether the subscriber's own address is a recipient. Their address is read from
    #: the account at send time, so changing it changes where the report goes.
    to_self = models.BooleanField(default=True)
    #: Other addresses, which only a user holding ``vantage.send_reports_to_others`` may set.
    recipients = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)
    #: Why it is paused when something other than the owner's Pause button did it.
    paused_reason = models.CharField(max_length=200, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=10, choices=Status.choices, blank=True)
    #: The tenant the subscription was made under, as :func:`vantage.tenancy.snapshot`
    #: writes it. Context for the export, not a permission: a worker has no host to
    #: resolve one from.
    tenant = models.CharField(max_length=255, blank=True)
    #: ``https://panel.example.com/``, from the request that created it, so an email can
    #: link back to the panel without a setting. ``REPORTS_BASE_URL`` wins when set.
    origin = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("report subscription")
        verbose_name_plural = _("report subscriptions")
        ordering = ["view__name", "pk"]
        permissions = [
            ("manage_reports", _("Can manage everyone's scheduled reports")),
            ("send_reports_to_others", _("Can send scheduled reports to other addresses")),
        ]
        indexes = [
            models.Index(fields=["active", "next_run_at"], name="vantage_rep_due_idx"),
            models.Index(fields=["user"], name="vantage_rep_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.view_id} → {self.user_id} ({self.frequency})"


class ReportRun(models.Model):
    """One attempt to send one subscription, and what became of it.

    ``reason`` carries the skip explanation (the account is inactive, the permission is
    gone), ``error`` an unexpected failure. They are separate because the first is the
    system working as designed and the second is somebody's to fix.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Queued")
        RUNNING = "running", _("Running")
        SENT = "sent", _("Sent")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")

    TERMINAL: frozenset[str] = frozenset({"sent", "skipped", "failed"})

    class Trigger(models.TextChoices):
        SCHEDULE = "schedule", _("Scheduled")
        MANUAL = "manual", _("Send now")

    subscription = models.ForeignKey(
        ReportSubscription, on_delete=models.CASCADE, related_name="runs"
    )
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default="schedule")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    rows = models.PositiveIntegerField(null=True, blank=True)
    #: Whether the CSV went out as an attachment, or the email was the short note.
    attached = models.BooleanField(default=False)
    size_bytes = models.PositiveIntegerField(null=True, blank=True)
    recipients_count = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=300, blank=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("report run")
        verbose_name_plural = _("report runs")
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["subscription", "-created_at"], name="vantage_rrun_sub_idx"),
            models.Index(fields=["status", "created_at"], name="vantage_rrun_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.subscription_id} ({self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL


# ── the dashboard, as somebody arranged it ──────────────────────────────────


class DashboardLayout(models.Model):
    """One person's arrangement of the dashboard — or, with a ``NULL`` user, the
    arrangement everybody else opens on.

    :class:`ListPreference` is to one list page what this is to the landing page, and
    the two are deliberately the same shape: a nullable column where ``NULL`` means
    *the code decides*, a per-user row that wins over a site-wide row, and every value
    re-validated against the live registry on the way out as well as on the way in.

    One JSON column rather than a row per card. Order is the list itself — the same
    argument :attr:`ListPreference.columns` makes — and a dashboard is read whole, by
    one query, on the one page every session starts at. A card table would be a join
    and a sort for a few kilobytes.

    :attr:`cards` holds *ingredients*, never expressions. A card is a model label, an
    aggregate name, a column name, a grouping and a filter query string, all of which
    are checked against what the panel currently exposes before anything is computed
    from them. Storing an ORM object or a callable would be storing code that arrived
    from a form, which is the one thing this table must never do.

    A card can also be an entry pointing at a widget the *code* declares, by key. That
    is what lets the arrangement reorder, resize and hide the cards in
    ``VANTAGE["DASHBOARD"]`` without forking them: the declaration stays the source of
    what those cards mean, and this row only says where they sit and whether they show.
    """

    #: Which mounted panel this arrangement belongs to, matching
    #: :attr:`vantage.sites.VantageSite.name`. A project running a staff panel and a
    #: partner panel arranges each separately, and text rather than a foreign key for
    #: the same reason ``resource_key`` is text: a site that stops existing must leave
    #: an inert row behind.
    site_name = models.CharField(max_length=100, default="vantage")

    #: ``NULL`` is the arrangement everybody who has not made their own sees.
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="vantage_dashboard_layouts",
    )

    #: The ordered layout, or ``NULL`` for "nothing is arranged here". ``NULL`` and
    #: ``[]`` are different answers: the first falls through to the code, the second is
    #: a dashboard somebody deliberately emptied.
    cards = models.JSONField(null=True, blank=True)

    #: The declared-card keys that existed, independent of who was looking, the moment
    #: ``cards`` was last written. This is what lets a declared card absent from
    #: ``cards`` be read as a decision rather than an accident: a key recorded here and
    #: missing from ``cards`` was seen and removed on purpose, while a key that is
    #: neither here nor there is new to the code since and must not be swallowed. A row
    #: written before this field existed reads back an empty list, which is the safe
    #: direction to be wrong in: nothing looks removed that was not explicitly said so.
    known_keys = models.JSONField(default=list, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("dashboard layout")
        verbose_name_plural = _("dashboard layouts")
        ordering = ["site_name", "pk"]
        indexes = [
            models.Index(fields=["site_name", "user"], name="vantage_dashlayout_idx"),
        ]
        constraints = [
            # Two constraints rather than one, for the reason ``ListPreference``
            # spells out: SQL treats NULLs as distinct, so a single unique index over
            # (site_name, user) would police the per-user rows and let the site-wide
            # rows multiply unnoticed.
            models.UniqueConstraint(
                fields=["site_name", "user"],
                condition=models.Q(user__isnull=False),
                name="vantage_dashlayout_unique_user",
            ),
            models.UniqueConstraint(
                fields=["site_name"],
                condition=models.Q(user__isnull=True),
                name="vantage_dashlayout_unique_default",
            ),
        ]

    def __str__(self) -> str:
        who = self.user.get_username() if self.user_id else _("everyone")
        return f"{self.site_name} · {who}"

    @property
    def is_site_default(self) -> bool:
        return self.user_id is None

    def is_empty(self) -> bool:
        """Whether this row has stopped saying anything, and can be deleted.

        Only ``NULL`` counts. An empty *list* is a real arrangement — somebody removed
        every card — and deleting the row for it would silently restore the code's
        dashboard instead of showing the empty one they asked for.
        """
        return self.cards is None


# ── undo ────────────────────────────────────────────────────────────────────


class UndoEntry(models.Model):
    """One reversible change, for the few seconds the toast offers to reverse it.

    A table rather than the cache or the session, and that choice is the feature. The
    undo has to be honoured by whichever worker process the click lands on, so per-process
    memory is out; it must be consumed exactly once under a double click, which wants a
    conditional ``UPDATE`` rather than a cache's best-effort ``delete``; and a hard
    delete's snapshot can be hundreds of rows, which no cookie-backed session can hold.
    See :mod:`vantage.undo` for what goes in :attr:`payload` and why.

    Rows are short-lived by construction: :attr:`expires_at` is seconds away, expired rows
    are purged on every write to this table, and a successful undo blanks the payload at
    once. Nothing here is meant to be read by a person, so there is no admin for it.
    """

    #: SHA-256 of the token the browser holds. The token itself is never stored, so a
    #: read of this table does not hand out working undo buttons.
    token_hash = models.CharField(max_length=64, unique=True)
    #: ``CASCADE``, unlike the audit log: an undo is not a record of anything, and one
    #: that outlived the account it was issued to could never be redeemed anyway.
    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    #: A random value kept in the issuing session. Binds the token to that session
    #: without depending on the session *key*, which signed-cookie sessions rotate on
    #: every write.
    session_nonce = models.CharField(max_length=64)
    #: ``"shop.order"`` — a label, for the reason :class:`VantageJob` gives.
    resource_key = models.CharField(max_length=100)
    #: The permission verb the original change needed. The undo needs the same one.
    verb = models.CharField(max_length=20)
    #: What the change was, as the audit entry will phrase it: "delete of 3 orders".
    summary = models.CharField(max_length=200)
    #: JSON, as text: the steps that reverse the change. Text rather than a
    #: ``JSONField`` so a snapshot's microsecond timestamps survive the round trip
    #: exactly — the encoding is :mod:`vantage.undo`'s, not the database driver's.
    payload = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    #: What redeeming it said, so a second click gets the same answer as the first.
    result = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = _("undo entry")
        verbose_name_plural = _("undo entries")
        indexes = [
            models.Index(fields=["expires_at"], name="vantage_undo_expiry_idx"),
            models.Index(fields=["user", "-created_at"], name="vantage_undo_user_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.summary} · {self.resource_key}"


class TwoFactorDevice(models.Model):
    """One person's authenticator app: the shared TOTP secret and its bookkeeping.

    One row per person, and a *new* row per enrolment rather than an edit of the old
    one: :attr:`tag` is random per row, and a session's proof that it passed the second
    step names the tag it passed against. Re-enrolling therefore invalidates every
    session verified against the previous authenticator without having to find them.

    The secret is stored sealed (see :func:`vantage.twofactor.seal`), never shown
    again once :attr:`confirmed_at` is set, and the table is excluded from the panel's
    own CRUD screens — the only ways in are the account page and the reset screen.
    """

    user = models.OneToOneField(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    #: The 20-byte secret, encrypted with a key derived from ``SECRET_KEY``.
    secret = models.CharField(max_length=200)
    #: Random, not secret. What a verified session remembers it was verified against.
    tag = models.CharField(max_length=32, unique=True)
    #: ``NULL`` while enrolment is in progress: the code has been shown but nobody has
    #: yet proved, by typing a code, that an app really holds it.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    #: The highest 30-second step a code has been accepted for. A code is only good
    #: for a step above this, which is what makes one code good for one sign-in.
    last_step = models.BigIntegerField(default=0)
    #: Attempts since the last right answer or the last lockout. Each is counted
    #: before its answer is compared, so parallel requests cannot outrun the lock.
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("two-factor device")
        verbose_name_plural = _("two-factor devices")

    def __str__(self) -> str:
        # Never the secret, and not even its length.
        state = "confirmed" if self.confirmed_at else "pending"
        return f"two-factor device for user {self.user_id} ({state})"


class TwoFactorRecoveryCode(models.Model):
    """One single-use recovery code, stored only as its SHA-256.

    A fast hash is enough here, unlike for a password, because the code is not chosen
    by a person: it carries 80 random bits, which no amount of hashing speed brings
    within reach of guessing. A fast, unsalted hash is also what lets a code be found
    with one indexed lookup instead of being compared against every row.
    """

    device = models.ForeignKey(TwoFactorDevice, on_delete=models.CASCADE, related_name="codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("two-factor recovery code")
        verbose_name_plural = _("two-factor recovery codes")
        constraints = [
            models.UniqueConstraint(fields=["device", "code_hash"], name="vantage_2fa_code_unique")
        ]

    def __str__(self) -> str:
        return f"recovery code for device {self.device_id}"


# ── changes waiting for approval ────────────────────────────────────────────


class ChangeRequest(models.Model):
    """A change to fields a panel lists in ``requires_approval``, held until approved.

    The record keeps its old values while this row is pending; nothing about it is
    written to the record until someone allowed to approve does so. See
    :mod:`vantage.approvals`.

    The record is named the way :class:`VantageJob` and :class:`Webhook` name a model —
    ``resource_key`` is ``Options.label_lower`` and ``object_id`` is the primary key as
    text — rather than with a foreign key, so a request survives its record being
    deleted, and can still be read (and rejected) afterwards.

    ``changes`` holds, per field, the value the requester saw (``from``) and the value
    they asked for (``to``), both as ``Field.value_to_string`` gives them. ``from`` is
    the concurrency check: approving compares it with the record as it stands, and a
    field that has moved since makes the request stale rather than silently reverting
    whatever moved it. The ``*_display`` strings are what the queue shows, captured when
    the request was made; a sensitive field never has one.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Waiting for approval")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    resource_key = models.CharField(max_length=100)
    object_id = models.CharField(max_length=255)
    #: The record's label when the request was made, for the queue and for a request
    #: whose record has since been deleted.
    object_repr = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict)
    #: Optional, from the person asking: why the change is wanted.
    note = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    #: ``SET_NULL`` for the reason the audit log's actor gives, with the username kept
    #: beside it.
    requested_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    requested_by_repr = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    decided_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    decided_by_repr = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    #: Why it was rejected. Required to reject; empty otherwise.
    reason = models.TextField(blank=True)

    class Meta:
        verbose_name = _("change request")
        verbose_name_plural = _("change requests")
        ordering = ["-requested_at", "-pk"]
        indexes = [
            models.Index(
                fields=["resource_key", "object_id", "status"], name="vantage_chreq_record_idx"
            ),
            models.Index(fields=["status", "-requested_at"], name="vantage_chreq_queue_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.resource_key} #{self.object_id} ({self.status})"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def field_names(self) -> list[str]:
        return list(self.changes or {})


class FieldPermission(models.Model):
    """One role's rule for one field of one model: hidden, or visible but read-only.

    Keyed to ``auth.Group`` because that is what a role *is* (see :mod:`vantage.roles`):
    the matrix edits groups, and a rule that lived anywhere else would be a second
    membership list to keep in step with the first. The model is named by its content
    type, like a ``Permission`` row, so a rule is dropped along with the model's
    permissions rather than left pointing at nothing.

    Only restrictions are stored. "No row" is the default — full access — so a project
    that never opens the field-access controls has an empty table, and granting a
    restriction can never widen anything. How several roles combine is
    :mod:`vantage.fieldperms`'s business, not this table's.
    """

    class Level(models.TextChoices):
        READONLY = "readonly", _("Read-only")
        HIDDEN = "hidden", _("Hidden")

    group = models.ForeignKey(
        "auth.Group", on_delete=models.CASCADE, related_name="vantage_field_permissions"
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    field_name = models.CharField(max_length=255)
    level = models.CharField(max_length=16, choices=Level.choices)

    class Meta:
        verbose_name = _("field permission")
        verbose_name_plural = _("field permissions")
        # Not an editable table in its own right: the roles matrix is the only way in.
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["group", "content_type", "field_name"], name="vantage_field_perm_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.field_name}: {self.level} for group {self.group_id}"


class ElevatedAccess(models.Model):
    """Time-boxed elevated access: a role, or named permissions, held for minutes.

    "Break glass" for people who need more than their standing access to fix something
    now, without anyone holding admin-wide rights the rest of the year. See
    :mod:`vantage.breakglass`.

    **Nothing is ever written to group membership.** A grant is a row, and whether it
    confers anything is decided at check time from ``starts_at``, ``expires_at`` and
    ``revoked_at``. An expired grant gives nothing the instant the clock passes it,
    whether or not anything ran — there is no cleanup job whose failure would leave
    someone elevated.

    A row starts as a *request* (``granted_at`` empty) unless the person creating it
    may grant it outright. Approving it sets ``granted_by``, ``starts_at`` and
    ``expires_at``; revoking, rejecting or dropping it sets ``revoked_at``.

    ``logged_state`` records which lifecycle entries the audit log already holds
    (``"active"``, then ``"expired"``), so activation and expiry are each recorded
    exactly once, by whichever check first observes them.
    """

    user = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    user_repr = models.CharField(max_length=150, blank=True)
    #: ``SET_NULL``: a deleted role confers nothing, and the grant stays readable.
    role = models.ForeignKey(
        "auth.Group", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    role_repr = models.CharField(max_length=150, blank=True)
    #: ``["app_label.codename", ...]`` granted beside (or instead of) the role.
    permissions = models.JSONField(default=list, blank=True)
    reason = models.TextField()
    #: What was asked for. The window itself is ``starts_at`` to ``expires_at``.
    minutes = models.PositiveIntegerField()

    requested_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    requested_by_repr = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    granted_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    granted_by_repr = models.CharField(max_length=150, blank=True)
    granted_at = models.DateTimeField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    revoked_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    revoked_by_repr = models.CharField(max_length=150, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.TextField(blank=True)

    #: Permissions this grant actually decided an allow for, each audited once.
    used = models.JSONField(default=list, blank=True)
    logged_state = models.CharField(max_length=10, blank=True)

    class Meta:
        verbose_name = _("elevated access")
        verbose_name_plural = _("elevated access grants")
        ordering = ["-requested_at", "-pk"]
        # Not an editable table in its own right: the break-glass screen is the way in.
        default_permissions = ()
        indexes = [
            models.Index(fields=["user", "expires_at"], name="vantage_elevated_user_idx"),
        ]

    def __str__(self) -> str:
        what = self.role_repr or ", ".join(self.permissions or []) or "-"
        return f"{self.user_repr or self.user_id}: {what}"

    @property
    def status(self) -> str:
        """``pending``, ``scheduled``, ``active``, ``expired``, ``revoked`` or ``rejected``.

        Computed from the clock, never stored: the stored columns only say what
        happened, and "is it active" is a question for the moment it is asked.
        """
        from django.utils import timezone

        if self.revoked_at is not None:
            return "revoked" if self.granted_at is not None else "rejected"
        if self.granted_at is None:
            return "pending"
        now = timezone.now()
        if self.expires_at is None or self.expires_at <= now:
            return "expired"
        if self.starts_at is not None and self.starts_at > now:
            return "scheduled"
        return "active"
