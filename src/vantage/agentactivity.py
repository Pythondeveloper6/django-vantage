"""What agents did: one stream of rows, shared by the command and the admin page.

Everything here is read-only and built on rows that already exist. An agent's writes
are audit entries (:class:`~vantage.models.VantageLogEntry` with ``agent`` set) and its
runs are :class:`~vantage.models.AgentRun` rows; this module only selects, orders and
flattens them.

**What a row can carry.** The audit diff, and nothing more. No prompt, no tool call, no
request body: the panel never sees them, and the audit diff is already the record of
what changed. A sensitive field is reported as changed with no value, using the same
re-application of sensitivity the audit page does at display time, so a field declared
sensitive after the entry was written is masked here too. Free text goes through
:func:`vantage.agentdata.scrub_text`, and the delimited-text writer neutralises formula
cells with the export module's own rule.

**Streaming.** Rows come from ``.iterator()`` and are produced one at a time; nothing
builds a list of the whole result.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

from django.apps import apps as django_apps
from django.utils import timezone

from vantage.agentdata import scrub_text
from vantage.models import AgentIdentity, AgentRun, VantageLogEntry

__all__ = [
    "ENTRY_COLUMNS",
    "FORMATS",
    "RUN_COLUMNS",
    "ActivityError",
    "agent_table",
    "entry_queryset",
    "iter_entry_rows",
    "iter_run_rows",
    "parse_moment",
    "render",
    "run_queryset",
    "summarise",
]

FORMATS = ("csv", "json", "ndjson")

ENTRY_COLUMNS = (
    "timestamp",
    "agent",
    "owner",
    "service_user",
    "run_key",
    "action",
    "action_name",
    "model",
    "object_id",
    "object_repr",
    "changes",
    "ip",
)
RUN_COLUMNS = ("agent", "run_key", "started", "last_seen", "writes", "rows_changed", "deletes")

#: Characters per value in the change summary, and per summary. A long text field is
#: not worth a megabyte of spreadsheet cell.
VALUE_CHARS = 200
SUMMARY_CHARS = 2000
CELL_CHARS = 500
CHUNK = 500

_RELATIVE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class ActivityError(ValueError):
    """A filter value that cannot be understood."""


# ── filters ─────────────────────────────────────────────────────────────────


def parse_moment(text: str, *, end_of_day: bool = False) -> dt.datetime:
    """``7d``, ``24h``, ``2w``, ``30m``, an ISO date or an ISO date-time, as an aware datetime.

    A bare date means its start, or with ``end_of_day`` the start of the day after (use
    it for an exclusive upper bound that includes the whole named day).
    """
    raw = (text or "").strip()
    match = _RELATIVE.match(raw)
    if match:
        return timezone.now() - dt.timedelta(
            **{_UNITS[match.group(2).lower()]: int(match.group(1))}
        )
    try:
        if len(raw) <= 10:
            day = dt.date.fromisoformat(raw)
            if end_of_day:
                day += dt.timedelta(days=1)
            value = dt.datetime.combine(day, dt.time.min)
        else:
            value = dt.datetime.fromisoformat(raw)
    except ValueError:
        raise ActivityError(
            f"{raw!r} is not a date. Use 7d, 24h, 2w, 30m, 2026-01-31 or 2026-01-31T09:00."
        ) from None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _model_filter(label: str):
    try:
        model = django_apps.get_model(label)
    except (LookupError, ValueError):
        raise ActivityError(f"{label!r} is not a model. Use app_label.model.") from None
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(model)


def entry_queryset(
    *,
    since=None,
    until=None,
    agents: Iterable[str] = (),
    run: str = "",
    model: str = "",
    action: str = "",
):
    """Audit entries made by an agent, oldest first, with everything a row needs joined."""
    queryset = VantageLogEntry.objects.filter(agent__isnull=False).select_related(
        "agent", "agent__owner", "agent_run", "actor", "content_type"
    )
    if since is not None:
        queryset = queryset.filter(created_at__gte=since)
    if until is not None:
        queryset = queryset.filter(created_at__lt=until)
    names = [name for name in agents if name]
    if names:
        queryset = queryset.filter(agent__name__in=names)
    if run:
        queryset = queryset.filter(agent_run__run_key=run)
    if model:
        queryset = queryset.filter(content_type=_model_filter(model))
    if action:
        if action not in VantageLogEntry.Action.values:
            known = ", ".join(VantageLogEntry.Action.values)
            raise ActivityError(f"{action!r} is not an action. Use one of: {known}.")
        queryset = queryset.filter(action=action)
    return queryset.order_by("created_at", "id")


def run_queryset(*, since=None, until=None, agents: Iterable[str] = (), run: str = ""):
    queryset = AgentRun.objects.select_related("identity")
    if since is not None:
        queryset = queryset.filter(started_at__gte=since)
    if until is not None:
        queryset = queryset.filter(started_at__lt=until)
    names = [name for name in agents if name]
    if names:
        queryset = queryset.filter(identity__name__in=names)
    if run:
        queryset = queryset.filter(run_key=run)
    return queryset.order_by("started_at", "id")


# ── rows ────────────────────────────────────────────────────────────────────


def _clean(value: Any, limit: int = CELL_CHARS) -> str:
    return scrub_text(value, limit) if value not in (None, "") else ""


def summarise(entry: VantageLogEntry) -> str:
    """``field: old -> new`` pairs for one entry; a sensitive field carries no value.

    Uses :attr:`VantageLogEntry.change_list`, which masks a field that is sensitive *now*
    as well as one that was when the entry was written.
    """
    parts = []
    for change in entry.change_list:
        name = _clean(change["field"], 100)
        if change["from"] is None and change["to"] is None:
            parts.append(f"{name}: changed (value not recorded)")
        elif change["from"] is None:
            parts.append(f"{name}: {_clean(change['to'], VALUE_CHARS)}")
        elif change["to"] is None:
            parts.append(f"{name}: {_clean(change['from'], VALUE_CHARS)} -> (empty)")
        else:
            old, new = _clean(change["from"], VALUE_CHARS), _clean(change["to"], VALUE_CHARS)
            parts.append(f"{name}: {old} -> {new}")
    return _clean("; ".join(parts), SUMMARY_CHARS)


def _model_label(entry: VantageLogEntry) -> str:
    if not entry.content_type_id:
        return ""
    return f"{entry.content_type.app_label}.{entry.content_type.model}"


def entry_row(entry: VantageLogEntry) -> dict[str, Any]:
    agent = entry.agent
    owner = agent.owner.get_username() if agent is not None and agent.owner_id else ""
    return {
        "timestamp": entry.created_at.isoformat(),
        "agent": _clean(agent.name if agent is not None else "", 100),
        "owner": _clean(owner, 150),
        "service_user": _clean(entry.actor.get_username() if entry.actor_id else "", 150),
        "run_key": _clean(entry.agent_run.run_key if entry.agent_run_id else "", 64),
        "action": entry.action,
        "action_name": _clean(entry.action_name, 100),
        "model": _model_label(entry),
        "object_id": _clean(entry.object_id, 255),
        "object_repr": _clean(entry.object_repr, 200),
        "changes": summarise(entry),
        "ip": entry.ip_address or "",
    }


def _agent_hidden(entry: VantageLogEntry, cache: dict[int, frozenset[str]]) -> frozenset[str]:
    """The fields a panel hides from agents, for the model this entry is about.

    This output is the agent-facing view of the log, so those names are left out of it
    whoever runs the command. The stored entry is not touched. One lookup per model.
    """
    ct_id = entry.content_type_id
    if not ct_id:
        return frozenset()
    if ct_id not in cache:
        from vantage.sensitive import _panel_for

        model = entry.content_type.model_class()
        panel = _panel_for(model) if model is not None else None
        declared = getattr(panel, "agent_hidden_fields", None) or ()
        # Sensitive fields keep the "changed, value not recorded" line they always had.
        cache[ct_id] = frozenset(declared)
    return cache[ct_id]


def iter_entry_rows(queryset, *, request=None) -> Iterator[dict[str, Any]]:
    """One dict per entry, streamed. With ``request``, fields hidden from that person are
    left out of the summary, exactly as the audit page leaves them out. Fields a panel
    hides from agents are always left out."""
    from vantage.fieldperms import hide_entry_fields

    hidden_by_model: dict[int, frozenset[str]] = {}
    for entry in queryset.select_related("content_type").iterator(chunk_size=CHUNK):
        if request is not None:
            entry = hide_entry_fields(request, [entry])[0]
        agent_hidden = _agent_hidden(entry, hidden_by_model)
        if agent_hidden:
            entry.vantage_hidden_fields = (
                frozenset(getattr(entry, "vantage_hidden_fields", ())) | agent_hidden
            )
        yield entry_row(entry)


def iter_run_rows(queryset) -> Iterator[dict[str, Any]]:
    for run in queryset.iterator(chunk_size=CHUNK):
        yield {
            "agent": _clean(run.identity.name, 100),
            "run_key": _clean(run.run_key, 64),
            "started": run.started_at.isoformat(),
            "last_seen": run.last_seen_at.isoformat(),
            "writes": run.writes,
            "rows_changed": run.rows_changed,
            "deletes": run.deletes,
        }


# ── writers ─────────────────────────────────────────────────────────────────


class _Line:
    """``csv.writer`` wants a file; this hands each formatted line back."""

    def write(self, value: str) -> str:
        return value


def render(columns: tuple[str, ...], rows: Iterable[dict[str, Any]], fmt: str) -> Iterator[str]:
    """The rows as text chunks in ``csv``, ``json`` (one array) or ``ndjson``."""
    if fmt not in FORMATS:
        raise ActivityError(f"Unknown format {fmt!r}. Use one of: {', '.join(FORMATS)}.")
    if fmt == "csv":
        from vantage.export import _cell

        writer = csv.writer(_Line(), lineterminator="\r\n")
        yield writer.writerow(columns)
        for row in rows:
            yield writer.writerow([_cell(row[name], armoured=True) for name in columns])
    elif fmt == "ndjson":
        for row in rows:
            yield json.dumps({name: row[name] for name in columns}, ensure_ascii=False) + "\n"
    else:
        yield "["
        first = True
        for row in rows:
            yield (
                ("" if first else ",")
                + "\n"
                + json.dumps({name: row[name] for name in columns}, ensure_ascii=False)
            )
            first = False
        yield "\n]\n"


# ── the page's agent table ──────────────────────────────────────────────────


def agent_table(*, days: int = 7) -> list[dict[str, Any]]:
    """Every identity with its recent runs and writes, in two queries however many there are."""
    from django.db.models import Count, Q, Sum

    since = timezone.now() - dt.timedelta(days=days)
    recent = Q(runs__started_at__gte=since)
    identities = (
        AgentIdentity.objects.select_related("user", "owner")
        .annotate(
            recent_runs=Count("runs", filter=recent),
            recent_writes=Sum("runs__writes", filter=recent),
        )
        .order_by("name")
    )
    return [
        {
            "identity": identity,
            "owner": identity.owner.get_username() if identity.owner_id else "",
            "service_user": identity.user.get_username(),
            "scopes": ", ".join(identity.allowed_models or []),
            "status": identity.status,
            "recent_runs": identity.recent_runs or 0,
            "recent_writes": identity.recent_writes or 0,
        }
        for identity in identities
    ]
