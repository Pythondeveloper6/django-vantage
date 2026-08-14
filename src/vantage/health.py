"""System health: the questions someone asks when a deployment looks wrong.

Deliberately a read-only diagnostic rather than a monitoring dashboard. The value is
having the four or five facts you actually check — are migrations applied, does the
cache work, what is the database, is a task backend configured — on one page inside
the panel, instead of shelling into a box to find out.

Every probe is defensive: a check that raises must report a failure, not take the page
down with it. A health page that 500s when something is unhealthy is worse than none.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

__all__ = ["Check", "run_checks"]


@dataclass(frozen=True)
class Check:
    label: str
    status: str  # ok | warn | fail | info
    detail: str
    hint: str | None = None


def _safe(label: str, probe) -> Check:
    try:
        return probe()
    except Exception as exc:
        return Check(label, "fail", f"{type(exc).__name__}: {exc}")


def check_migrations() -> Check:
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connections["default"])
    targets = executor.loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    if not plan:
        return Check(_("Migrations"), "ok", _("All applied."))
    names = ", ".join(f"{m.app_label}.{m.name}" for m, _unused in plan[:5])
    more = _(" (+%(count)d more)") % {"count": len(plan) - 5} if len(plan) > 5 else ""
    return Check(
        _("Migrations"),
        "fail",
        _("%(count)d unapplied: %(names)s%(more)s")
        % {"count": len(plan), "names": names, "more": more},
        hint=_("Run manage.py migrate."),
    )


def check_database() -> Check:
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    vendor = connection.vendor
    version = ".".join(str(part) for part in getattr(connection, "pg_version", ()) or ()) or None
    detail = f"{vendor} · {connection.settings_dict.get('NAME')}"
    if version:
        detail += f" · {version}"
    return Check(_("Database"), "ok", detail)


def check_cache() -> Check:
    from django.core.cache import cache

    cache.set("vantage-health", "ok", 10)
    if cache.get("vantage-health") != "ok":
        return Check(
            _("Cache"),
            "warn",
            _("A value written to the cache did not read back."),
            hint=_("A dummy or misconfigured backend. Faceted counts will not be cached."),
        )
    from django.conf import settings as django_settings

    backend = django_settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]
    return Check(_("Cache"), "ok", backend)


def check_tasks() -> Check:
    """Which background-task backend is available, if any.

    One detector, in ``vantage.jobs``, so the health page and the enqueue path can
    never disagree about whether this deployment actually has a worker. Probing
    ``settings.TASKS`` here reported "configured" on every Django 6.x project,
    because it has a default — it said there was a worker when there was not.
    """
    from vantage.jobs import IN_PROCESS, detect_backend

    backend = detect_backend()
    if backend is not IN_PROCESS:
        return Check(
            _("Background tasks"),
            "ok",
            _("%(backend)s detected.") % {"backend": backend.label},
        )
    return Check(_("Background tasks"), "info", _("None configured."), hint=backend.hint)



def check_debug() -> Check:
    from django.conf import settings as django_settings

    if django_settings.DEBUG:
        return Check(
            _("Debug mode"),
            "warn",
            _("DEBUG is on."),
            hint=_("Expected locally; a serious problem in production."),
        )
    return Check(_("Debug mode"), "ok", _("Off."))


def check_storage() -> Check:
    """Whether uploaded branding assets have somewhere to go."""
    from django.conf import settings as django_settings

    if not getattr(django_settings, "MEDIA_ROOT", None):
        return Check(
            _("Media storage"),
            "warn",
            _("MEDIA_ROOT is unset."),
            hint=_("Logo uploads on the settings screen will fail."),
        )
    return Check(_("Media storage"), "ok", str(django_settings.MEDIA_ROOT))


def check_versions() -> Check:
    import django

    from vantage import __version__

    python = ".".join(str(p) for p in sys.version_info[:3])
    detail = (
        f"Vantage {__version__} · Django {django.get_version()} · Python {python}"
    )
    return Check(_("Versions"), "info", detail)


def _importable(name: str) -> bool:
    from importlib.util import find_spec

    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):
        return False


PROBES = (
    (_("Migrations"), check_migrations),
    (_("Database"), check_database),
    (_("Cache"), check_cache),
    (_("Background tasks"), check_tasks),
    (_("Media storage"), check_storage),
    (_("Debug mode"), check_debug),
    (_("Versions"), check_versions),
)


def run_checks() -> list[Check]:
    return [_safe(label, probe) for label, probe in PROBES]
