"""Undo a whole agent run, from the audit log.

Everything an agent writes is tagged with its run (``VantageLogEntry.agent_run``), so a
run can be read back and reverted as one unit. The existing undo tokens cannot do this:
they are single-use, bound to a session and expire in seconds. History restore
(:mod:`vantage.history`) can, and is what updates use.

**What is done per record**, decided from the run's entries on it:

* *Created by the run.* The row is deleted, but only if it still holds the values the run
  left, nothing refers to it, and the caller may delete it.
* *Updated by the run.* The fields the run changed are put back to what
  :func:`vantage.history.reconstruct` says they held before the run, through the panel's
  own form (validation, ``save_model``, audit). Every field the run touched must be
  restorable, or the record is left alone whole: restoring half a change would leave a
  state nobody chose.
* *Deleted by the run.* Left alone and reported as skipped. The audit log's snapshot
  leaves out sensitive and file fields and the related rows a cascade took, so a row
  rebuilt from it would not be the row that was deleted.
* *Bulk actions.* Skipped: the log keeps one entry per run, not the values of each row.

**Never overwritten.** A record someone else touched after the run, or whose current
values are not what the run left (a write the audit log did not see), is a *conflict* and
is not changed. The undo's own entries do not count as someone else, so running the undo
twice is safe.

**Sensitive fields** are never shown in the report; a record whose run touched one is
skipped, because its old value was never recorded.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, router, transaction
from django.http import Http404, HttpRequest

from vantage import audit, concurrency, formviews, history
from vantage.models import VantageLogEntry

logger = logging.getLogger("vantage.agents")

__all__ = ["undo_run"]

_CHANGES = (
    VantageLogEntry.Action.CREATE,
    VantageLogEntry.Action.UPDATE,
    VantageLogEntry.Action.DELETE,
    VantageLogEntry.Action.TRASH,
    VantageLogEntry.Action.RESTORE,
)


def marker(run) -> str:
    """The ``action_name`` of every entry this undo writes."""
    return f"undo of run {run.run_key}"[:100]


def _request_for(user) -> HttpRequest:
    request = HttpRequest()
    request.method = "POST"
    request.user = user
    request.META["REMOTE_ADDR"] = ""
    return request


def _may_undo(run, user) -> bool:
    if user is None or not getattr(user, "is_active", False):
        return False
    if user.is_superuser:
        return True
    owner_id = run.identity.owner_id
    return owner_id is not None and owner_id == user.pk


class _Row:
    """Collects one record's outcome."""

    def __init__(self, label: str, repr_: str) -> None:
        self.object = label
        self.label = repr_

    def skipped(self, reason: str) -> tuple[str, dict[str, Any]]:
        return "skipped", {"object": self.object, "label": self.label, "reason": reason}

    def conflict(self, reason: str) -> tuple[str, dict[str, Any]]:
        return "conflicts", {"object": self.object, "label": self.label, "reason": reason}

    def reverted(self, action: str, fields: list[str]) -> tuple[str, dict[str, Any]]:
        return "reverted", {
            "object": self.object,
            "label": self.label,
            "action": action,
            "fields": fields,
        }


def undo_run(run, by_user, *, dry_run: bool = False) -> dict[str, Any]:
    """Revert ``run``'s writes as ``by_user``. Returns the report.

    ``{"run", "agent", "dry_run", "reverted": [...], "skipped": [{"object", "reason"}],
    "conflicts": [{"object", "reason"}]}``. Raises :class:`vantage.agents.UndoNotAllowed`
    unless ``by_user`` is a superuser or the identity's owner. With ``dry_run`` nothing
    is written and ``reverted`` lists what would be.
    """
    from vantage.agents import UndoNotAllowed

    if not _may_undo(run, by_user):
        raise UndoNotAllowed("Only a superuser or the agent's owner may undo a run.")
    request = _request_for(by_user)
    report: dict[str, Any] = {
        "run": run.run_key,
        "agent": run.identity.name,
        "dry_run": dry_run,
        "reverted": [],
        "skipped": [],
        "conflicts": [],
    }
    entries = list(VantageLogEntry.objects.filter(agent_run=run).order_by("pk"))
    groups: dict[tuple[int | None, str], list[VantageLogEntry]] = defaultdict(list)
    for entry in entries:
        if entry.action not in _CHANGES and entry.action != VantageLogEntry.Action.ACTION:
            continue
        if entry.object_id == "":
            if entry.action == VantageLogEntry.Action.ACTION:
                report["skipped"].append(
                    {
                        "object": _model_label(entry.content_type),
                        "label": entry.object_repr,
                        "reason": f"Bulk action '{entry.action_name}': the log does not keep "
                        "each row's values, so it cannot be reverted.",
                    }
                )
            continue
        if entry.action == VantageLogEntry.Action.ACTION:
            continue  # an approval request or decision: no change was made by it
        groups[(entry.content_type_id, entry.object_id)].append(entry)

    for (_ct, _pk), group in groups.items():
        bucket, item = _undo_object(request, run, group, dry_run)
        report[bucket].append(item)
    return report


def _model_label(content_type) -> str:
    return content_type.model_class()._meta.label_lower if content_type else ""


def _undo_object(request, run, group, dry_run) -> tuple[str, dict[str, Any]]:
    first, last = group[0], group[-1]
    model = first.content_type.model_class() if first.content_type else None
    pk = first.object_id
    row = _Row(f"{model._meta.label_lower if model else '?'}:{pk}", last.object_repr)
    try:
        return _decide(request, run, group, dry_run, row, model, pk)
    except PermissionDenied as exc:
        return row.skipped(str(exc) or "You may not change this record.")
    except (DatabaseError, ValueError) as exc:
        logger.warning("Undoing run %s on %s failed: %s", run.run_key, row.object, exc)
        return row.skipped("The change could not be applied; nothing was written.")


def _decide(request, run, group, dry_run, row, model, pk):
    from vantage.permissions import get_permissions
    from vantage.sites import site

    first, last = group[0], group[-1]
    if model is None or not site.is_registered(model):
        return row.skipped("This model is no longer registered in the panel.")
    panel = site.get_panel(model)
    permissions = get_permissions()

    created = first.action == VantageLogEntry.Action.CREATE
    deleted = last.action == VantageLogEntry.Action.DELETE
    if any(
        e.action in (VantageLogEntry.Action.TRASH, VantageLogEntry.Action.RESTORE) for e in group
    ):
        return row.skipped("Moving records to or from the trash is not reverted.")

    exists = model._base_manager.filter(pk=pk).exists()
    outsiders = _outsiders(run, first)
    if outsiders.filter(action=VantageLogEntry.Action.DELETE).exists():
        return row.conflict("Someone else deleted this record after the run.")
    if not exists:
        if deleted and created:
            return row.skipped("Created and deleted within the run; nothing to undo.")
        if deleted:
            return row.skipped(
                "Deleted by the run. A deleted record cannot be rebuilt from the audit log."
            )
        if VantageLogEntry.objects.filter(
            content_type=first.content_type, object_id=pk, action_name=marker(run)
        ).exists():
            return row.skipped("Already undone.")
        return row.conflict("The record no longer exists.")
    if deleted:
        return row.conflict("The record exists again after the run deleted it.")
    if outsiders.exists():
        return row.conflict("Someone else changed this record after the run.")

    verb = "delete" if created else "change"
    try:
        obj = formviews.get_record(request, panel, permissions, pk, verb)
    except Http404:
        return row.skipped("You cannot see this record.")
    except PermissionDenied:
        return row.skipped(f"You do not have permission to {verb} this record.")

    tracked = {f.name: f for f in history._tracked_fields(panel, request)[0]}
    if created:
        return _undo_create(request, run, panel, obj, group, tracked, dry_run, row)
    return _undo_update(request, run, panel, obj, group, dry_run, row)


def _outsiders(run, first):
    """Changes to the record after the run began that are not the run's or its undo's."""
    return (
        VantageLogEntry.objects.filter(
            content_type=first.content_type, object_id=first.object_id, pk__gt=first.pk
        )
        .filter(action__in=_CHANGES)
        .exclude(agent_run=run)
        .exclude(action_name=marker(run))
    )


def _final_values(group) -> dict[str, Any]:
    """What the run left in each field it wrote, as the log recorded it."""
    final: dict[str, Any] = {}
    for entry in group:
        for name, delta in (entry.changes or {}).items():
            if isinstance(delta, dict) and "to" in delta and not delta.get("redacted"):
                final[name] = delta["to"]
    return final


def _undo_create(request, run, panel, obj, group, tracked, dry_run, row):
    # The row must still be what the run left. A write the log did not see is a conflict.
    for name, value in _final_values(group).items():
        model_field = tracked.get(name)
        if model_field is None or name in panel.get_sensitive_fields():
            continue
        if history._by_label_only(model_field) or history._unfaithful(model_field, value):
            continue
        if not history._same(model_field, value, history._current(obj, model_field)):
            return row.conflict("The record was changed after the run, outside the audit log.")
    cascade = formviews.collect_cascade(obj)
    if cascade.is_blocked or cascade.unavailable:
        return row.conflict("The record cannot be deleted: other records depend on it.")
    if cascade.total:
        return row.conflict(
            f"Deleting it would also delete {cascade.total} related record(s) the run did not make."
        )
    if dry_run:
        return row.reverted("deleted", [])
    audit.record(
        request,
        VantageLogEntry.Action.DELETE,
        obj=obj,
        changes=audit.snapshot(obj),
        action_name=marker(run),
    )
    with transaction.atomic(using=router.db_for_write(type(obj))):
        obj.delete()
    return row.reverted("deleted", [])


def _undo_update(request, run, panel, obj, group, dry_run, row):
    from vantage.approvals import guarded_for
    from vantage.fieldperms import access
    from vantage.widgets import build_form_class

    touched: list[str] = []
    for entry in group:
        for name in entry.changes or {}:
            if name not in touched:
                touched.append(name)
    if not touched:
        return row.skipped("The run recorded no field changes on this record.")

    entries, _truncated = history._record_entries(obj)
    first = group[0]
    before = [e for e in entries if e.pk < first.pk]
    if before:
        anchor = before[-1]
        position = history.Position(at=anchor.created_at, entry_id=anchor.pk)
        listing = entries
    else:
        # Nothing earlier in the log: a stand-in entry, one tick before the run, so the
        # reconstruction has a point to stand at. It records no change of its own.
        stand_in = VantageLogEntry(
            action=VantageLogEntry.Action.UPDATE,
            changes={},
            created_at=first.created_at - timedelta(microseconds=1),
        )
        position = history.Position(at=stand_in.created_at)
        listing = [stand_in, *entries]
    reconstruction = history.reconstruct(panel, obj, position, entries=listing, request=request)
    if reconstruction.status != "ok":
        return row.skipped("Its earlier state cannot be worked out from the audit log.")

    states = {state.name: state for state in reconstruction.fields}
    rules = access(request, panel)
    restore: list[str] = []
    skip_reason = conflict_reason = ""
    for name in touched:
        state = states.get(name)
        model_field = None
        try:
            model_field = obj._meta.get_field(name)
        except Exception:
            pass
        if state is None or model_field is None:
            skip_reason = skip_reason or "A field the run changed is not available to you."
        elif state.masked:
            skip_reason = skip_reason or "The run changed a sensitive field; it cannot be restored."
        elif not state.known:
            if history._by_label_only(model_field) or history._unfaithful(model_field, None):
                skip_reason = skip_reason or (
                    "The run changed a relation, file or JSON field, whose earlier value "
                    "the audit log does not keep exactly."
                )
            else:
                conflict_reason = (
                    conflict_reason
                    or "A field was also changed outside the audit log, so its earlier "
                    "value cannot be trusted."
                )
        elif state.changed:
            if not rules.can_change(name):
                skip_reason = skip_reason or "You may not change a field the run changed."
            else:
                restore.append(name)
    if conflict_reason:
        return row.conflict(conflict_reason)
    if skip_reason:
        return row.skipped(skip_reason)
    if not restore:
        return row.skipped("Already holds its earlier values.")
    if set(guarded_for(request, panel, obj)) & set(restore):
        return row.skipped("A field the run changed needs approval to change.")

    if dry_run:
        return row.reverted("restored", restore)

    data = {}
    for name in restore:
        value = states[name].value
        data[name] = "" if value is None else (value if isinstance(value, bool) else str(value))
    form = build_form_class(panel, restore)(data=data, instance=obj)
    if not form.is_valid():
        return row.skipped("The earlier values are no longer valid, so nothing was changed.")
    try:
        formviews.save_form(
            request,
            panel,
            form,
            change=True,
            version=concurrency.token_for(obj),
            action_name=marker(run),
        )
    except concurrency.StaleWrite:
        return row.conflict("Someone changed this record while the undo ran.")
    return row.reverted("restored", restore)
