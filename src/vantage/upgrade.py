"""What stands between a project and the next django-vantage release.

``manage.py vantage_upgrade_check`` is a thin shell over :func:`run_checks`. Each check
reads the project as it is — the raw ``VANTAGE`` dict, the registered panels, the
migration graph, the installed Django — and returns :class:`Finding` rows rather than
raising, so one command can report everything at once and a CI job can decide what
fails the build.

Nothing here resolves settings through :mod:`vantage.conf`: that layer *raises* on an
unknown key, which is exactly the situation this command exists to explain.
"""

from __future__ import annotations

import difflib
from dataclasses import asdict, dataclass

from django.conf import settings as django_settings

from vantage.deprecation import (
    DEPRECATED_PANEL_ATTRIBUTES,
    DEPRECATED_SETTINGS,
    PROVISIONAL_PANEL_ATTRIBUTES,
    PROVISIONAL_SETTINGS,
    UPGRADE_NOTES,
    parse_version,
)

#: Ordered from least to most serious; ``--fail-level`` compares against this.
LEVELS = ("info", "warning", "error")

#: The Django range this release is tested against. Kept equal to the bound in
#: ``pyproject.toml`` by a test, so the two cannot disagree.
SUPPORTED_DJANGO = ((5, 2), (6, 2))


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    message: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def check_settings() -> list[Finding]:
    """Unknown, deprecated and provisional keys in the raw ``VANTAGE`` dict."""
    from vantage.conf import DEFAULTS

    user = getattr(django_settings, "VANTAGE", None) or {}
    if not isinstance(user, dict):
        return [Finding("error", "U001", f"VANTAGE must be a dict, got {type(user).__name__}.")]
    findings: list[Finding] = []
    for key in sorted(user):
        deprecation = DEPRECATED_SETTINGS.get(key)
        if deprecation is not None:
            findings.append(
                Finding(
                    "warning",
                    "U002",
                    deprecation.message("The VANTAGE setting"),
                    hint=f"Rename it to {deprecation.replacement!r}."
                    if deprecation.replacement
                    else deprecation.note,
                )
            )
        elif key not in DEFAULTS:
            close = difflib.get_close_matches(key, DEFAULTS, n=1, cutoff=0.6)
            findings.append(
                Finding(
                    "error",
                    "U001",
                    f"VANTAGE[{key!r}] is not a setting this version recognises; the site "
                    "will refuse to start.",
                    hint=f"Did you mean {close[0]!r}?" if close else "Remove it.",
                )
            )
        elif key in PROVISIONAL_SETTINGS:
            findings.append(
                Finding(
                    "info",
                    "U008",
                    f"VANTAGE[{key!r}] is provisional: it may still change before the next "
                    "release.",
                    hint="See docs/api-stability.md.",
                )
            )
    return findings


def check_panels(site=None) -> list[Finding]:
    """Deprecated, removed and provisional attributes on every registered panel, plus
    the panel configuration errors ``manage.py check`` would raise."""
    from vantage.panel_checks import REMOVED_ATTRIBUTES

    if site is None:
        from vantage import site as default_site

        site = default_site
    findings: list[Finding] = []
    for panel in site.panels():
        cls = type(panel)
        where = f"{cls.__module__}.{cls.__qualname__} ({panel.opts.label})"
        declared = {
            name
            for klass in cls.__mro__
            if klass.__name__ != "ModelPanel" and klass is not object
            for name in vars(klass)
        }
        for name in sorted(declared):
            if name in DEPRECATED_PANEL_ATTRIBUTES:
                deprecation = DEPRECATED_PANEL_ATTRIBUTES[name]
                findings.append(
                    Finding("warning", "U003", f"{where}: {deprecation.message('Attribute')}")
                )
            elif name in REMOVED_ATTRIBUTES and getattr(panel, name, None) is not None:
                findings.append(
                    Finding(
                        "error",
                        "U004",
                        f"{where} sets {name!r}, which is no longer a panel attribute.",
                        hint=REMOVED_ATTRIBUTES[name],
                    )
                )
            elif name in PROVISIONAL_PANEL_ATTRIBUTES and getattr(panel, name, None) is not None:
                findings.append(
                    Finding(
                        "info",
                        "U008",
                        f"{where} sets {name!r}, which is provisional.",
                        hint="See docs/api-stability.md.",
                    )
                )
        for error in panel.check():
            if error.id == "vantage.E020":
                continue  # Reported above as U004, with the same hint.
            findings.append(
                Finding(
                    "error" if error.is_serious() else "warning",
                    "U007",
                    f"{where}: {error.msg} ({error.id})",
                    hint=error.hint or "",
                )
            )
    return findings


def check_migrations(database: str = "default") -> list[Finding]:
    """Vantage migrations that exist on disk and have not been applied."""
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor

    try:
        executor = MigrationExecutor(connections[database])
        targets = executor.loader.graph.leaf_nodes("vantage")
        plan = executor.migration_plan(targets)
    except Exception as exc:  # The database may simply not be reachable from here.
        return [
            Finding(
                "warning",
                "U005",
                f"Could not read the migration state of database {database!r}: {exc}",
                hint="Run with --skip-database to check configuration only.",
            )
        ]
    pending = [migration.name for migration, backwards in plan if not backwards]
    if not pending:
        return []
    return [
        Finding(
            "error",
            "U005",
            f"{len(pending)} vantage migration(s) not applied: {', '.join(pending)}.",
            hint="Run `manage.py migrate vantage` as part of the deploy.",
        )
    ]


def check_django(version: tuple[int, ...] | None = None) -> list[Finding]:
    import django

    current = tuple(version or django.VERSION[:2])[:2]
    low, high = SUPPORTED_DJANGO
    if low <= current < high:
        return []
    shown = ".".join(str(part) for part in current)
    return [
        Finding(
            "error",
            "U006",
            f"Django {shown} is outside the range this release is tested against "
            f"(>={low[0]}.{low[1]},<{high[0]}.{high[1]}).",
            hint="Pin Django inside the range, or upgrade django-vantage.",
        )
    ]


def notes_since(version: str) -> list[tuple[str, tuple[str, ...]]]:
    """Upgrade notes for every release newer than ``version``."""
    floor = parse_version(version)
    return [(name, notes) for name, notes in UPGRADE_NOTES if parse_version(name) > floor]


def run_checks(*, database: str | None = "default", site=None) -> list[Finding]:
    findings = [*check_settings(), *check_django(), *check_panels(site)]
    if database is not None:
        findings.extend(check_migrations(database))
    order = {level: index for index, level in enumerate(reversed(LEVELS))}
    return sorted(findings, key=lambda finding: (order[finding.level], finding.code))


def exceeds(findings: list[Finding], fail_level: str) -> bool:
    threshold = LEVELS.index(fail_level)
    return any(LEVELS.index(finding.level) >= threshold for finding in findings)


__all__ = [
    "LEVELS",
    "SUPPORTED_DJANGO",
    "Finding",
    "check_django",
    "check_migrations",
    "check_panels",
    "check_settings",
    "exceeds",
    "notes_since",
    "run_checks",
]
