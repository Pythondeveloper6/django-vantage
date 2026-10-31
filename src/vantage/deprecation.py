"""The public API surface, how it changes, and the machinery that warns before it does.

Two questions an upgrade has to answer, answered here in data rather than prose:

* **What may I rely on?** Every ``VANTAGE`` setting and every declarative
  ``ModelPanel`` attribute is classified as *stable* or *provisional*. A stable name is
  frozen: it is not renamed, removed or given a different meaning without going
  through a deprecation first. A provisional name is new since the last release and may
  still change before the release after it, at which point it becomes stable. The test
  suite fails when a setting or attribute is added without being classified, so the
  surface cannot grow by accident.
* **What is about to change?** :data:`DEPRECATED_SETTINGS` and
  :data:`DEPRECATED_PANEL_ATTRIBUTES` list everything on its way out, with the version it
  was deprecated in, the version it goes in, and its replacement. A deprecated name
  keeps working for the whole deprecation period and emits
  :class:`VantageDeprecationWarning`; ``manage.py vantage_upgrade_check`` lists every
  use in a project ahead of time.

The policy itself is in ``docs/api-stability.md``.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

__all__ = [
    "DEPRECATED_PANEL_ATTRIBUTES",
    "DEPRECATED_SETTINGS",
    "PROVISIONAL_PANEL_ATTRIBUTES",
    "PROVISIONAL_SETTINGS",
    "STABLE_PANEL_ATTRIBUTES",
    "STABLE_SETTINGS",
    "UPGRADE_NOTES",
    "Deprecation",
    "VantageDeprecationWarning",
    "parse_version",
    "warn_deprecated",
]


class VantageDeprecationWarning(DeprecationWarning):
    """A Vantage name or behaviour that still works and will stop working.

    A subclass of :class:`DeprecationWarning`, so ``python -W error::DeprecationWarning``
    and pytest's ``filterwarnings = error`` catch it, and its own class, so a project
    can silence or escalate Vantage's warnings without touching everyone else's.
    """


@dataclass(frozen=True)
class Deprecation:
    """One name on its way out."""

    #: The deprecated setting key or panel attribute.
    name: str
    #: The release that first warned about it.
    since: str
    #: The first release that no longer accepts it.
    removed_in: str
    #: The name that replaces it, when the change is a rename. A renamed setting or
    #: attribute is translated to its replacement for the whole deprecation period.
    replacement: str | None = None
    #: What to do instead, for anything that is not a plain rename.
    note: str = ""

    def message(self, kind: str) -> str:
        instead = (
            f" Use {self.replacement!r} instead."
            if self.replacement
            else (f" {self.note}" if self.note else "")
        )
        return (
            f"{kind} {self.name!r} is deprecated since django-vantage {self.since} and will "
            f"be removed in {self.removed_in}.{instead}"
        )


#: ``VANTAGE`` keys on their way out. Empty today: nothing has been deprecated yet.
DEPRECATED_SETTINGS: dict[str, Deprecation] = {}

#: ``ModelPanel`` attributes on their way out. Empty today; ``permissions`` was removed
#: outright before this policy existed (see ``vantage.panel_checks.REMOVED_ATTRIBUTES``).
DEPRECATED_PANEL_ATTRIBUTES: dict[str, Deprecation] = {}


#: Settings shipped in a release. Frozen: renamed or removed only through a deprecation.
STABLE_SETTINGS: frozenset[str] = frozenset(
    {
        # branding
        "SITE_TITLE",
        "SITE_ACRONYM",
        "LOGO",
        "LOGO_DARK",
        "FAVICON",
        "BRAND_COLOR",
        "BRAND_RADIUS",
        "THEME",
        "THEME_TOGGLE",
        "UI_THEME",
        "UI_THEMES",
        "UI_THEME_TOGGLE",
        "CUSTOM_CSS",
        "LOGIN_IMAGE",
        # discovery and navigation
        "AUTO_REGISTER",
        "EXCLUDE",
        "INCLUDE_ONLY",
        "NAV",
        "NAV_UNLISTED",
        "DASHBOARD",
        "PLUGINS",
        "PLUGINS_EXCLUDE",
        # behaviour and access
        "PERMISSIONS",
        "SETTINGS_PERMISSION",
        "LIST_PREFS_PERMISSION",
        "ROLES_PERMISSION",
        "READ_ONLY",
        "ENVIRONMENT",
        "AUDIT_LOG",
        "COMMAND_PALETTE",
        "SENSITIVE_PATTERNS",
        # tenancy
        "TENANT_MODEL",
        "TENANT_RESOLVER",
        "TENANT_FIELD",
        "TENANT_SCOPE",
        "TENANT_EXEMPT",
        "TENANT_UNSCOPED",
        # security
        "LOGIN_THROTTLE",
        "LOGIN_THROTTLE_LIMIT",
        "LOGIN_THROTTLE_WINDOW",
        "LOGIN_THROTTLE_CACHE",
        "LOGIN_THROTTLE_IP_HEADER",
        "RATE_LIMIT",
        "RATE_LIMIT_CACHE",
        "RATE_LIMITS",
        "UPLOAD_MAX_BYTES",
        # lists, scale, import, jobs, undo, data quality
        "DEFAULT_PER_PAGE",
        "MAP_TILE_URL",
        "RELATION_PICKER",
        "LIST_COUNT_CAP",
        "LIST_COUNT_ESTIMATES",
        "LIST_ESTIMATE_CACHE_SECONDS",
        "LIST_KEYSET_THRESHOLD",
        "LIST_STATEMENT_TIMEOUT_MS",
        "IMPORT_MAX_BYTES",
        "IMPORT_MAX_ROWS",
        "JOBS",
        "JOB_THRESHOLD_SECONDS",
        "JOB_CHUNK_SIZE",
        "JOB_MAX_STORED_ERRORS",
        "JOB_RETENTION_DAYS",
        "JOB_QUEUE",
        "UNDO_WINDOW",
        "UNDO_MAX_ROWS",
        "DATA_QUALITY",
    }
)

#: Settings added since the last release. They may still change before the next one.
PROVISIONAL_SETTINGS: frozenset[str] = frozenset(
    {
        "TWO_FACTOR",
        "TWO_FACTOR_REQUIRED_SUPERUSERS",
        "TWO_FACTOR_REQUIRED_ROLES",
        "TWO_FACTOR_ISSUER",
        "TWO_FACTOR_MAX_ATTEMPTS",
        "TWO_FACTOR_LOCKOUT_SECONDS",
        "WEBHOOKS",
        "WEBHOOKS_ALLOW_PRIVATE_NETWORKS",
        "WEBHOOKS_TIMEOUT",
        "WEBHOOKS_MAX_ATTEMPTS",
        "WEBHOOKS_BACKOFF_SECONDS",
        "WEBHOOKS_RETENTION_DAYS",
        "REPORTS",
        "REPORTS_MAX_ATTACHMENT_MB",
        "REPORTS_MAX_PER_USER",
        "REPORTS_RETENTION_DAYS",
        "REPORTS_BASE_URL",
        "UNMANAGED_READ_ONLY",
        "ROLE_MAPPING",
        "METRICS",
        "METRICS_TOKEN",
        "SLOW_REQUEST_MS",
        "NOTIFICATIONS",
        "UPLOAD_ACTIVE_SUFFIXES",
        "ACCESS_REVIEW_PERMISSION",
        "BREAK_GLASS_PERMISSION",
        "BREAK_GLASS_SELF_SERVICE",
        "BREAK_GLASS_MAX_MINUTES",
    }
)

#: Declarative ``ModelPanel`` attributes shipped in a release. Frozen like settings.
STABLE_PANEL_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "label",
        "label_plural",
        "icon",
        "group",
        "order",
        "description",
        "list_display",
        "list_link",
        "list_filter",
        "list_search",
        "list_editable",
        "list_per_page",
        "list_select_related",
        "ordering",
        "list_stats",
        "list_charts",
        "list_aggregates",
        "list_views",
        "kanban_field",
        "calendar_field",
        "kanban_fields",
        "calendar_fields",
        "gallery_field",
        "map_fields",
        "card_fields",
        "fields",
        "exclude",
        "layout",
        "readonly_fields",
        "inlines",
        "related_tabs",
        "sensitive_fields",
        "actions",
        "row_actions",
        "undo",
        "data_quality",
        "search_fields",
        "checks_class",
    }
)

#: ``ModelPanel`` attributes added since the last release.
PROVISIONAL_PANEL_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "field_permissions",
        "history",
        "requires_approval",
        "formfield_overrides",
        "widgets",
        "read_only",
        "editable_fields",
    }
)


#: What an upgrade *to* each version needs beyond ``pip install -U``, oldest first.
#: ``vantage_upgrade_check --from X`` prints every entry newer than ``X``. ``"next"`` is
#: the release being prepared, whatever number it ends up with, and always sorts last.
UPGRADE_NOTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "0.2.0a1",
        (
            "Run `manage.py migrate vantage` (migrations 0012-0015: list view "
            "preferences, the UI theme setting and undo entries).",
        ),
    ),
    ("0.2.0a2", ("No schema or configuration changes.",)),
    (
        "next",
        (
            "Run `manage.py migrate vantage` (migrations 0016-0021: two-factor "
            "sign-in, webhooks, scheduled reports, approvals, field permissions and "
            "elevated access).",
            "Uploads of active content (SVG, HTML, XML, JavaScript) are now refused unless "
            "listed in VANTAGE['UPLOAD_ACTIVE_SUFFIXES'].",
            "Two-factor sign-in is offered to everyone from the account page by default; "
            "set VANTAGE['TWO_FACTOR'] = False to keep it off.",
            "Settings and panel attributes added in this release are provisional until "
            "the release after it; see docs/api-stability.md.",
        ),
    ),
)


def warn_deprecated(deprecation: Deprecation, kind: str, *, stacklevel: int = 3) -> None:
    """Emit the one warning a deprecated name produces."""
    warnings.warn(deprecation.message(kind), VantageDeprecationWarning, stacklevel=stacklevel)


def parse_version(value: str) -> tuple:
    """A sortable key for this project's versions: ``0.2.0a2`` < ``0.2.0`` < ``"next"``.

    Deliberately small rather than a dependency on ``packaging``: the package has one
    runtime dependency and its own version strings are all this has to order.
    """
    if value.strip() == "next":
        return (float("inf"),)
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?", value.strip())
    if not match:
        raise ValueError(f"{value!r} is not a django-vantage version.")
    major, minor, patch, stage, number = match.groups()
    # A final release sorts after every pre-release of the same version.
    stage_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[stage]
    return (int(major), int(minor), int(patch or 0), stage_rank, int(number or 0))
