"""Resolving a setting against the database.

The cascade, narrowest first::

    SiteConfiguration row  >  settings.VANTAGE  >  conf.DEFAULTS

This module supplies the leftmost step and nothing else. It is called from
:meth:`vantage.conf.VantageSettings._resolve`, which means it sits *underneath* every
``vantage_settings.X`` in the package: no consumer knows a database is involved, and
none of them had to change.

Three properties matter more than anything else here, because getting any of them
wrong breaks the package in a place nobody would think to look:

**Lazy.** Nothing touches the database at import or at ``AppConfig.ready`` time. The
first read happens when a setting is first *used*, which is always inside a request or
a command, never during app loading.

**Failure-tolerant.** ``vantage_settings.SITE_TITLE`` is read during ``manage.py
check``, during ``migrate`` on an empty database, and by tests that never asked for a
database. Every one of those must quietly resolve to the file settings. A settings
layer that raises when the table is missing makes the migration that would create the
table impossible to run.

**Cached, and invalidated on save.** The row is read once per request and held for the
process between requests. Per-request refresh is what makes a save visible to every
worker rather than only to the one that handled the form, and it keeps the query count
of a page constant instead of "one extra on whichever request happened to be first".
"""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.core.signals import request_started
from django.db.models import FileField
from django.db.utils import OperationalError, ProgrammingError
from django.dispatch import receiver

__all__ = ["UI_EDITABLE", "code_defaults", "database_overrides", "invalidate"]

#: The settings a non-developer may change from the panel, mapped to their column.
#:
#: An explicit allowlist rather than "every key with a matching column", because the
#: line it draws is a security boundary and it should be readable in one screenful.
#: Deliberately absent: ``AUTO_REGISTER``, ``EXCLUDE``, ``INCLUDE_ONLY``, ``NAV``,
#: ``NAV_UNLISTED``, ``DASHBOARD``, ``PERMISSIONS``, ``SETTINGS_PERMISSION``,
#: ``AUDIT_LOG`` and ``CUSTOM_CSS`` — see the module docstring of
#: :mod:`vantage.models` and the report in the settings screen's help text.
UI_EDITABLE: dict[str, str] = {
    "SITE_TITLE": "site_title",
    "SITE_ACRONYM": "site_acronym",
    "LOGO": "logo",
    "LOGO_DARK": "logo_dark",
    "FAVICON": "favicon",
    "LOGIN_IMAGE": "login_image",
    "BRAND_COLOR": "brand_color",
    "BRAND_RADIUS": "brand_radius",
    "THEME": "theme",
    "THEME_TOGGLE": "theme_toggle",
    "ENVIRONMENT": "environment",
    "DEFAULT_PER_PAGE": "default_per_page",
    "COMMAND_PALETTE": "command_palette",
    "READ_ONLY": "read_only",
}

_cache: dict[str, Any] | None = None


# ── the cascade's first step ────────────────────────────────────────────────


def database_overrides() -> dict[str, Any]:
    """Every setting the stored row overrides. Empty when there is no row, or no table.

    A miss is cached; an *unavailable database* is not. Caching unavailability would
    mean a process that started before ``migrate`` never noticed the table appearing.
    """
    global _cache
    if _cache is not None:
        return _cache

    values = _read()
    if values is None:
        return {}
    _cache = values
    return _cache


def code_defaults() -> dict[str, Any]:
    """What the cascade resolves to with the database taken out of it.

    The settings screen shows this as the value each field falls back to, so "clear
    the field" has a visible meaning rather than being a leap of faith.
    """
    from vantage.conf import DEFAULTS, _acronym

    user = getattr(django_settings, "VANTAGE", None)
    resolved = {**DEFAULTS, **(user if isinstance(user, dict) else {})}
    if resolved["SITE_ACRONYM"] is None:
        resolved["SITE_ACRONYM"] = _acronym(resolved["SITE_TITLE"])
    if resolved["LOGO_DARK"] is None:
        resolved["LOGO_DARK"] = resolved["LOGO"]
    return resolved


def invalidate() -> None:
    """Drop the stored overrides and the merged settings built on top of them."""
    global _cache
    _cache = None

    from vantage.conf import vantage_settings

    vantage_settings.reset()


# ── reading the row ─────────────────────────────────────────────────────────


def _model():
    """The model class, or ``None`` if asking is not safe yet."""
    from django.apps import apps

    if not apps.ready:
        return None
    try:
        return apps.get_model("vantage", "SiteConfiguration")
    except LookupError:  # pragma: no cover - only when the app is not installed
        return None


def _read() -> dict[str, Any] | None:
    """The stored overrides, or ``None`` when the database cannot answer."""
    model = _model()
    if model is None:
        return None

    try:
        row = model.objects.filter(pk=model.SINGLETON_PK).first()
    except (OperationalError, ProgrammingError):
        # No table yet (pre-migrate), or no database reachable. Both mean the same
        # thing to this layer: the file settings are the whole truth for now.
        return None
    except RuntimeError as exc:
        # pytest-django blocks the connection outright for tests that did not ask for
        # a database, and a settings read is not a reason to fail one. Narrow on the
        # blocker's own message so a genuine RuntimeError still surfaces.
        if "Database access not allowed" not in str(exc):
            raise
        return None

    return {} if row is None else _overrides(row)


def _overrides(row) -> dict[str, Any]:
    from vantage.conf import CHOICES, DEFAULTS

    values: dict[str, Any] = {}
    for key, field_name in UI_EDITABLE.items():
        value = getattr(row, field_name)

        if isinstance(row._meta.get_field(field_name), FileField):
            # A FieldFile is falsy when no file was uploaded, which is this layer's
            # "not overridden". Its URL is what the templates want, and asking for one
            # raises if the project never configured MEDIA_URL.
            if not value:
                continue
            try:
                values[key] = value.url
            except ValueError:  # pragma: no cover - misconfigured MEDIA_URL
                continue
            continue

        # NULL is "not overridden". An empty string is not: blanking ENVIRONMENT is a
        # legitimate way to switch off a banner that settings.py turned on.
        if value is None:
            continue

        allowed = CHOICES.get(key)
        if allowed is not None and value not in allowed:
            continue
        if key == "DEFAULT_PER_PAGE" and (not isinstance(value, int) or value < 1):
            continue
        if key not in DEFAULTS:  # pragma: no cover - guarded by test_siteconfig
            continue
        values[key] = value

    return values


@receiver(request_started)
def _invalidate_per_request(sender, **kwargs) -> None:
    """One read per request.

    Without this, a worker that did not handle the save would serve the old branding
    until it was restarted — and the first request of a process would pay for a query
    that later requests did not, which makes query-count assertions unrepeatable.
    """
    invalidate()
