"""The database half of the settings cascade.

A project owner states their settings in ``settings.VANTAGE``; the person who actually
runs the panel day to day is usually not the person who can edit that file. This model
gives them a row that sits *underneath* attribute access on
:data:`~vantage.conf.vantage_settings`, so the branding can be changed from the panel
without a deploy, and without a single call site knowing a database is involved.

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
    "SiteConfiguration",
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
