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
    # The look, as opposed to THEME's light/dark: "vantage" (the original), "shadcn",
    # "slate", ... or a name registered in UI_THEMES. See vantage.presets.
    "UI_THEME": "vantage",
    # {"name": {"light": {...}, "dark": {...}, "primary": "#hex", "radius": ...}}.
    # Anything omitted is inherited from the "vantage" preset.
    "UI_THEMES": {},
    # Lets each person pick a preset for themselves, like THEME_TOGGLE does for
    # light/dark.
    "UI_THEME_TOGGLE": True,
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
    # ── plugins ─────────────────────────────────────────────────────────────
    # None means "load every plugin on the vantage.plugins entry-point group",
    # which is what makes `pip install` enough. A list becomes an allowlist.
    "PLUGINS": None,
    # Never loaded, whatever PLUGINS says. Separate from the allowlist because
    # switching a broken plugin off must not mean enumerating the good ones.
    "PLUGINS_EXCLUDE": [],
    # ── behaviour ───────────────────────────────────────────────────────────
    "PERMISSIONS": None,  # dotted path to a PanelPermissions subclass
    # Who may change the settings from inside the panel. None means superusers only,
    # which is the right default for a screen that can rebrand the whole site. A
    # permission string ("vantage.change_siteconfiguration") or a callable taking the
    # request widens it. Never itself editable from the UI — that would be a self-grant.
    "SETTINGS_PERMISSION": None,
    "LIST_PREFS_PERMISSION": None,
    # Who may open the roles & permissions screen. None means superusers only. A
    # permission string ("auth.change_group") or a callable taking the request widens
    # it; whoever is let in can still only grant permissions they hold themselves.
    # Code-only for the same reason as SETTINGS_PERMISSION: editable, it is a self-grant.
    "ROLES_PERMISSION": None,
    "READ_ONLY": False,
    # Whether a model with ``Meta.managed = False`` — a table Django did not create and
    # does not migrate — is read-only in the panel unless its panel says otherwise
    # (``ModelPanel.read_only``). Off by default for compatibility; recommended on for
    # a panel over a database another system owns.
    "UNMANAGED_READ_ONLY": False,
    "ENVIRONMENT": None,
    # ── multi-tenancy ───────────────────────────────────────────────────────
    # Off until TENANT_MODEL names a model, at which point every registered model must
    # either have a path to it or be declared global — see vantage.tenancy.
    "TENANT_MODEL": None,
    # Dotted path to a TenantResolver subclass or to any callable taking a request.
    # None means vantage.tenancy.UserAttributeResolver.
    "TENANT_RESOLVER": None,
    # The attribute the default resolver reads off the user, and the conventional
    # column name on tenant-owned models.
    "TENANT_FIELD": "tenant",
    # {"app.model": "lookup__path"} for anything inference cannot see, and
    # {"app.model": None} for a model that is deliberately shared.
    "TENANT_SCOPE": {},
    # Models every tenant shares. Reference data, mostly.
    "TENANT_EXEMPT": [],
    # What happens to a registered model with no path to the tenant and no exemption.
    # "deny" shows no rows, which is the only default that makes forgetting safe.
    "TENANT_UNSCOPED": "deny",
    "AUDIT_LOG": True,
    "COMMAND_PALETTE": True,
    # ── sensitive values ────────────────────────────────────────────────────
    # The names that mark a field as a secret, matched as whole words. None means the
    # built-in list (vantage.sensitive.DEFAULT_PATTERNS); a list replaces it; a list
    # containing "..." extends it: ["...", "nhs_number"].
    "SENSITIVE_PATTERNS": None,
    # ── security ────────────────────────────────────────────────────────────
    # Registered here, but described and defaulted in vantage.security.DEFAULTS,
    # which is where the reasoning for each number lives. They have to appear in
    # this registry as well because it rejects unknown keys on purpose: until they
    # did, writing one of these documented settings into VANTAGE raised
    # ImproperlyConfigured the next time anything read any setting — which is nearly
    # every page render. Following the documentation took the site down.
    #
    # None here means "whatever security.DEFAULTS says", so the reasoning stays in
    # one place and the two cannot drift into disagreeing about a default.
    "LOGIN_THROTTLE": None,
    "LOGIN_THROTTLE_LIMIT": None,
    "LOGIN_THROTTLE_WINDOW": None,
    "LOGIN_THROTTLE_CACHE": None,
    "LOGIN_THROTTLE_IP_HEADER": None,
    "RATE_LIMIT": None,
    "RATE_LIMIT_CACHE": None,
    "RATE_LIMITS": None,
    "UPLOAD_MAX_BYTES": None,
    # ── two-factor sign-in ──────────────────────────────────────────────────
    # Offered to everyone from the account page. False removes the feature: no
    # enrolment, no second step, and any codes already set up stop being asked for.
    "TWO_FACTOR": True,
    # Who may not use the panel without it. Code-only, like the permission settings:
    # a requirement the people it binds could switch off is not a requirement.
    "TWO_FACTOR_REQUIRED_SUPERUSERS": False,
    # Role (auth.Group) names whose members must use it, e.g. ["Finance"].
    "TWO_FACTOR_REQUIRED_ROLES": [],
    # The name an authenticator app files the code under. None means SITE_TITLE.
    "TWO_FACTOR_ISSUER": None,
    # Wrong codes in a row before the account's second step locks, and for how long.
    "TWO_FACTOR_MAX_ATTEMPTS": 5,
    "TWO_FACTOR_LOCKOUT_SECONDS": 300,
    "DEFAULT_PER_PAGE": 50,
    # ── list views ──────────────────────────────────────────────────────────
    # A raster tile URL template for the map view, e.g.
    # "https://tile.openstreetmap.org/{z}/{x}/{y}.png". None, the default, draws a
    # dependency-free SVG plot and fetches nothing from anywhere: sending every
    # viewer's location of interest to a tile server is the owner's call to make.
    "MAP_TILE_URL": None,
    # ── relation pickers ────────────────────────────────────────────────────
    # Which relation fields become the searchable chip picker rather than a plain
    # <select>. "always" is the default because one control for every relationship
    # is easier to learn than two, and because a native <select multiple> is a poor
    # control at any size — the selection hides behind ctrl-click and one ordinary
    # click discards it.
    #
    # "threshold" restores a plain <select> for short single-valued relations. Worth
    # setting on a panel used mostly from phones: a native select opens the platform
    # picker, which beats anything a page can draw. A many-to-many is unaffected —
    # it is always the picker.
    "RELATION_PICKER": "always",
    # ── scale ───────────────────────────────────────────────────────────────
    # Rows counted exactly before the page reports "N+". COUNT(*) on Postgres is a
    # full relation scan and does not care that you are showing 25 rows.
    "LIST_COUNT_CAP": 10_000,
    # Planner estimates above the cap. Postgres only, and unrestricted users only:
    # a whole-table estimate over-reports to someone whose rows are filtered.
    "LIST_COUNT_ESTIMATES": True,
    "LIST_ESTIMATE_CACHE_SECONDS": 300,
    # Offset depth above which keyset paging is warranted. 0 disables it.
    "LIST_KEYSET_THRESHOLD": 50_000,
    # Per-statement ceiling, so a pathological filter errors rather than pinning a
    # worker. 0 disables it.
    "LIST_STATEMENT_TIMEOUT_MS": 10_000,
    "IMPORT_MAX_BYTES": 5 * 1024 * 1024,
    "IMPORT_MAX_ROWS": 5000,
    # ── background jobs ─────────────────────────────────────────────────────
    "JOBS": True,
    # The line between running in the request and writing a job row, in seconds of
    # estimated work.
    "JOB_THRESHOLD_SECONDS": 2.0,
    "JOB_CHUNK_SIZE": 500,
    "JOB_MAX_STORED_ERRORS": 1000,
    "JOB_RETENTION_DAYS": 30,
    # Queue name for django.tasks / RQ. Celery uses its own routing.
    "JOB_QUEUE": "default",
    # ── webhooks ────────────────────────────────────────────────────────────
    # Off, no hook fires and the page is hidden. Existing rows are kept.
    "WEBHOOKS": True,
    # Webhooks refuse to call loopback, private, link-local and other non-public
    # addresses, checked against what the host name resolves to at send time. True
    # lifts that, for a receiver on your own network. Code-only: editable from the
    # panel, it would be a way for a webhook manager to reach your internal services.
    "WEBHOOKS_ALLOW_PRIVATE_NETWORKS": False,
    # Seconds one delivery may wait on the receiver, per socket operation.
    "WEBHOOKS_TIMEOUT": 5,
    # Attempts before a delivery is marked failed, the first one included.
    "WEBHOOKS_MAX_ATTEMPTS": 6,
    # Seconds before the first retry; each later wait doubles it (capped at a day).
    "WEBHOOKS_BACKOFF_SECONDS": 30,
    # Finished deliveries older than this are removed by `manage.py vantage_webhooks`.
    "WEBHOOKS_RETENTION_DAYS": 30,
    # ── scheduled reports ───────────────────────────────────────────────────
    # Off, nothing is sent and the pages and the Subscribe control are hidden.
    # Existing subscriptions are kept.
    "REPORTS": True,
    # Largest CSV attached to an email, in megabytes of file. A bigger report is sent
    # as a short note saying so, with no attachment.
    "REPORTS_MAX_ATTACHMENT_MB": 10,
    # Subscriptions one person may hold.
    "REPORTS_MAX_PER_USER": 25,
    # Finished sends older than this are removed by `manage.py vantage_reports`.
    "REPORTS_RETENTION_DAYS": 90,
    # Where links in report emails point, e.g. "https://panel.example.com". Empty uses
    # the address the subscription was made from.
    "REPORTS_BASE_URL": "",
    # ── undo ────────────────────────────────────────────────────────────────
    # Seconds the "Undo" button on a delete or edit toast stays redeemable. 0 turns
    # undo off everywhere; ModelPanel.undo = False turns it off for one model.
    "UNDO_WINDOW": 10,
    # The most rows one undo may carry, cascades included. Past it the change still
    # happens, it just is not offered for undo: the snapshot would be the problem.
    "UNDO_MAX_ROWS": 1000,
    # ── data quality ────────────────────────────────────────────────────────
    # A dict of the keys in DATA_QUALITY_DEFAULTS; unknown keys are rejected like any
    # other typo. Empty means "the defaults".
    "DATA_QUALITY": {},
}

#: The data quality page's own settings, nested under ``VANTAGE["DATA_QUALITY"]`` so
#: that eleven related knobs are one key in the registry rather than eleven.
DATA_QUALITY_DEFAULTS: dict[str, Any] = {
    "ENABLED": True,
    # None means superusers only. A permission string or a callable taking the request
    # widens it; each model is still gated by the resolver's own "view" answer.
    "PERMISSION": None,
    # Check names skipped everywhere, e.g. ["stale", "nulls"].
    "CHECKS_EXCLUDE": [],
    # Rows read per model. Above this a check looks at the newest rows by primary key
    # and the report says so.
    "SAMPLE_SIZE": 50_000,
    # Estimated rows above which a model's data checks are skipped outright. Only
    # reachable where a planner estimate exists (PostgreSQL).
    "SKIP_ABOVE": 5_000_000,
    # Seconds one check may spend on one model before it is cut off.
    "CHECK_TIME_BUDGET": 3.0,
    # Seconds a page render may spend auditing; the rest show as "not yet run".
    "PAGE_TIME_BUDGET": 12.0,
    "CACHE_SECONDS": 3600,
    # Share of empty values above which a field is a warning rather than a note.
    "NULL_WARN_RATE": 0.5,
    # Days without a change before a table is called stale.
    "STALE_DAYS": 365,
    # Extra field names to treat as "should be unique", e.g. ["external_id"].
    "DUPLICATE_FIELDS": [],
}

#: Values that must be one of a fixed set.
CHOICES: dict[str, tuple[str, ...]] = {
    "THEME": ("light", "dark", "system"),
    "NAV_UNLISTED": ("append", "hide"),
    "TENANT_UNSCOPED": ("deny", "allow"),
    "RELATION_PICKER": ("always", "threshold"),
}

#: Values that must be a whole number of at least this much. Checked rather than
#: coerced: ``"10"`` in a settings file is a typo for ``10``, and an undo window that
#: silently became zero would be an undo that silently stopped existing.
MINIMUMS: dict[str, int] = {
    "UNDO_WINDOW": 0,
    "UNDO_MAX_ROWS": 1,
    "TWO_FACTOR_MAX_ATTEMPTS": 1,
    "TWO_FACTOR_LOCKOUT_SECONDS": 1,
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
            raise ImproperlyConfigured(f"VANTAGE must be a dict, got {type(user).__name__}.")

        user = _translate_deprecated(user)
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ImproperlyConfigured(_unknown_key_message(sorted(unknown)))

        for key, allowed in CHOICES.items():
            value = user.get(key, DEFAULTS[key])
            if value not in allowed:
                raise ImproperlyConfigured(
                    f"VANTAGE['{key}'] must be one of {', '.join(allowed)}; got {value!r}."
                )

        tiles = user.get("MAP_TILE_URL")
        if tiles is not None and (
            not isinstance(tiles, str) or not all(part in tiles for part in ("{z}", "{x}", "{y}"))
        ):
            raise ImproperlyConfigured(
                "VANTAGE['MAP_TILE_URL'] must be a URL template containing {z}, {x} and {y}, "
                f"or None; got {tiles!r}."
            )

        from vantage.presets import validate_ui_themes

        problems = validate_ui_themes(
            user.get("UI_THEMES", DEFAULTS["UI_THEMES"]), user.get("UI_THEME", DEFAULTS["UI_THEME"])
        )
        if problems:
            raise ImproperlyConfigured("Invalid UI theme settings:\n  " + "\n  ".join(problems))

        for key, floor in MINIMUMS.items():
            value = user.get(key, DEFAULTS[key])
            if isinstance(value, bool) or not isinstance(value, int) or value < floor:
                raise ImproperlyConfigured(
                    f"VANTAGE['{key}'] must be a whole number of at least {floor}; got {value!r}."
                )

        _validate_sensitive_patterns(user.get("SENSITIVE_PATTERNS"))
        problem = two_factor_problem(user)
        if problem:
            raise ImproperlyConfigured(problem)

        resolved = {**DEFAULTS, **user}
        resolved["DATA_QUALITY"] = _resolve_data_quality(resolved["DATA_QUALITY"])

        # The database layer goes *under* attribute access rather than beside it, so
        # every existing ``vantage_settings.X`` picks up a UI override without knowing
        # a database exists. Imported here rather than at module scope because this
        # module is imported before the app registry is populated.
        from vantage.siteconfig import database_overrides

        overrides = database_overrides()
        resolved.update(overrides)
        # Remembered because "BRAND_COLOR is #6366f1" cannot tell an owner who chose it
        # from one who never chose anything, and a UI theme preset may only supply its
        # own primary in the second case.
        resolved["_explicit"] = frozenset(user) | frozenset(overrides)

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

    def is_explicit(self, name: str) -> bool:
        """Whether the owner set ``name`` (in ``VANTAGE`` or the settings screen)."""
        return name in self._resolve()["_explicit"]

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self._resolve().items() if not k.startswith("_")}

    def reset(self) -> None:
        self._cache = None


def _translate_deprecated(user: dict[str, Any]) -> dict[str, Any]:
    """Warn about deprecated keys, and carry a renamed key's value to its new name.

    Runs before the unknown-key check, so a key that was renamed keeps working for the
    whole deprecation period instead of becoming a typo the day it is renamed. Where
    both names are set the new one wins: it is the one the owner wrote on purpose.
    """
    from vantage.deprecation import DEPRECATED_SETTINGS, warn_deprecated

    used = [key for key in user if key in DEPRECATED_SETTINGS]
    if not used:
        return user
    user = dict(user)
    for key in used:
        deprecation = DEPRECATED_SETTINGS[key]
        warn_deprecated(deprecation, "The VANTAGE setting", stacklevel=5)
        if deprecation.replacement and key not in DEFAULTS:
            value = user.pop(key)
            user.setdefault(deprecation.replacement, value)
    return user


def _resolve_data_quality(value: Any) -> dict[str, Any]:
    """Merge ``VANTAGE["DATA_QUALITY"]`` over its defaults, rejecting unknown keys."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ImproperlyConfigured(
            f"VANTAGE['DATA_QUALITY'] must be a dict, got {type(value).__name__}."
        )
    unknown = sorted(set(value) - set(DATA_QUALITY_DEFAULTS))
    if unknown:
        lines = []
        for key in unknown:
            close = difflib.get_close_matches(key, DATA_QUALITY_DEFAULTS, n=1, cutoff=0.6)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            lines.append(f"  {key!r} is not a DATA_QUALITY setting.{hint}")
        raise ImproperlyConfigured(
            "Unrecognised key(s) in VANTAGE['DATA_QUALITY']:\n" + "\n".join(lines)
        )
    return {**DATA_QUALITY_DEFAULTS, **value}


def _validate_sensitive_patterns(value: Any) -> None:
    """A list of words, optionally with ``"..."`` meaning "and the defaults".

    Rejected loudly rather than coerced: a bare string would otherwise be iterated a
    character at a time and mask every column with an ``a`` in its name, and a typo'd
    type that silently disabled masking is worse.
    """
    if value is None:
        return
    if not isinstance(value, list | tuple) or any(
        not (entry is Ellipsis or (isinstance(entry, str) and entry.strip())) for entry in value
    ):
        raise ImproperlyConfigured(
            "VANTAGE['SENSITIVE_PATTERNS'] must be a list of field-name words, optionally "
            f'including "..." to keep the built-in ones; got {value!r}.'
        )


def two_factor_problem(user: dict) -> str | None:
    """What is wrong with the two-factor requirement settings, or ``None``.

    Shared with the system check, so ``manage.py check`` and the first page render
    say the same thing. Strict for the same reason as ``SENSITIVE_PATTERNS``: a bare
    string here would be iterated a letter at a time, and a requirement that silently
    matched no role is a requirement that silently does not exist.
    """
    superusers = user.get("TWO_FACTOR_REQUIRED_SUPERUSERS", False)
    if not isinstance(superusers, bool):
        return (
            f"VANTAGE['TWO_FACTOR_REQUIRED_SUPERUSERS'] must be True or False; got {superusers!r}."
        )
    roles = user.get("TWO_FACTOR_REQUIRED_ROLES", [])
    if not isinstance(roles, list | tuple) or any(
        not (isinstance(role, str) and role.strip()) for role in roles
    ):
        return f"VANTAGE['TWO_FACTOR_REQUIRED_ROLES'] must be a list of role names; got {roles!r}."
    issuer = user.get("TWO_FACTOR_ISSUER")
    if issuer is not None and (not isinstance(issuer, str) or not issuer.strip() or ":" in issuer):
        # The colon is the separator between issuer and account in an otpauth:// label;
        # an issuer containing one is filed by some apps under the wrong name.
        return (
            "VANTAGE['TWO_FACTOR_ISSUER'] must be a non-empty name without a colon, or "
            f"None; got {issuer!r}."
        )
    return None


def _unknown_key_message(unknown: list[str]) -> str:
    lines = []
    for key in unknown:
        close = difflib.get_close_matches(key, DEFAULTS, n=1, cutoff=0.6)
        hint = f" Did you mean {close[0]!r}?" if close else ""
        lines.append(f"  {key!r} is not a Vantage setting.{hint}")
    return "Unrecognised key(s) in the VANTAGE setting:\n" + "\n".join(lines)


vantage_settings = VantageSettings()


#: Settings whose value is baked into a resolved one. ``VANTAGE`` obviously; the storage
#: settings because a stored logo resolves to a URL, and that URL is built from them.
_INVALIDATING = frozenset(
    {"VANTAGE", "MEDIA_URL", "MEDIA_ROOT", "STORAGES", "DEFAULT_FILE_STORAGE"}
)


@receiver(setting_changed)
def _invalidate(sender, setting, **kwargs):  # pragma: no cover - trivial
    if setting == "VANTAGE":
        vantage_settings.reset()
        from vantage.permissions import _reset

        _reset()
    if setting in _INVALIDATING:
        from vantage.siteconfig import invalidate

        invalidate()
