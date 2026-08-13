"""The global settings layer.

Everything a project owner can configure lives in a single ``VANTAGE`` dict in Django
settings. Access it through the module-level ``vantage_settings`` object, never by
reading ``settings.VANTAGE`` directly — this layer supplies defaults, derives values,
and rejects typos.
"""

from __future__ import annotations

import difflib
from typing import Any

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

#: Every recognised setting, with its default. A key absent from here is a typo.
DEFAULTS: dict[str, Any] = {
    # ── branding ────────────────────────────────────────────────────────────
    "SITE_TITLE": "Django Vantage",
    "SITE_ACRONYM": None,  # derived from SITE_TITLE when unset
    "LOGO": None,
    "LOGO_DARK": None,  # falls back to LOGO
    "FAVICON": None,
    "BRAND_COLOR": "#6366f1",
    "BRAND_RADIUS": "0.5rem",
    "THEME": "system",
    "THEME_TOGGLE": True,
    "CUSTOM_CSS": None,
    "LOGIN_IMAGE": None,
    # ── discovery ───────────────────────────────────────────────────────────
    "AUTO_REGISTER": True,
    "EXCLUDE": [],
    "INCLUDE_ONLY": None,
    # ── navigation ──────────────────────────────────────────────────────────
    "NAV": None,
    # What happens to registered models the NAV declaration does not mention.
    # "append" keeps them reachable, so adding a model never silently hides it.
    "NAV_UNLISTED": "append",
    "DASHBOARD": None,
    # ── behaviour ───────────────────────────────────────────────────────────
    "PERMISSIONS": None,  # dotted path to a PanelPermissions subclass
    "READ_ONLY": False,
    "ENVIRONMENT": None,
    "AUDIT_LOG": True,
    "COMMAND_PALETTE": True,
    "DEFAULT_PER_PAGE": 50,
}

#: Values that must be one of a fixed set.
CHOICES: dict[str, tuple[str, ...]] = {
    "THEME": ("light", "dark", "system"),
    "NAV_UNLISTED": ("append", "hide"),
}


def _acronym(title: str) -> str:
    """Derive a short mark for the collapsed sidebar: 'Acme Corporation' -> 'AC'."""
    words = title.split()
    if len(words) >= 2:
        return (words[0][:1] + words[1][:1]).upper()
    return title[:2].upper()


class VantageSettings:
    """Lazy, cached, self-invalidating access to the ``VANTAGE`` settings dict."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def _resolve(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache

        user = getattr(django_settings, "VANTAGE", None) or {}
        if not isinstance(user, dict):
            raise ImproperlyConfigured(
                f"VANTAGE must be a dict, got {type(user).__name__}."
            )

        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ImproperlyConfigured(_unknown_key_message(sorted(unknown)))

        for key, allowed in CHOICES.items():
            value = user.get(key, DEFAULTS[key])
            if value not in allowed:
                raise ImproperlyConfigured(
                    f"VANTAGE['{key}'] must be one of {', '.join(allowed)}; got {value!r}."
                )

        resolved = {**DEFAULTS, **user}

        # Derived values. Each is computed only when the user left it unset, so an
        # explicit value always wins.
        if resolved["SITE_ACRONYM"] is None:
            resolved["SITE_ACRONYM"] = _acronym(resolved["SITE_TITLE"])
        if resolved["LOGO_DARK"] is None:
            resolved["LOGO_DARK"] = resolved["LOGO"]

        self._cache = resolved
        return resolved

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in DEFAULTS:
            raise AttributeError(f"{name!r} is not a Vantage setting.")
        return self._resolve()[name]

    def as_dict(self) -> dict[str, Any]:
        return dict(self._resolve())

    def reset(self) -> None:
        self._cache = None


def _unknown_key_message(unknown: list[str]) -> str:
    lines = []
    for key in unknown:
        close = difflib.get_close_matches(key, DEFAULTS, n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        lines.append(f"  {key!r} is not a Vantage setting.{hint}")
    return "Unrecognised key(s) in the VANTAGE setting:\n" + "\n".join(lines)


vantage_settings = VantageSettings()


@receiver(setting_changed)
def _invalidate(sender, setting, **kwargs):  # pragma: no cover - trivial
    if setting == "VANTAGE":
        vantage_settings.reset()
        from vantage.permissions import _reset

        _reset()
