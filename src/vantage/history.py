"""A record as it was: replaying the audit log backwards, and restoring from it.

The audit log already keeps a diff of every change made through the panel. Read in
reverse from the row as it stands now, those diffs say what each field held at any
earlier point — *as far as the panel saw it*. That qualifier is the whole design. The
log is not row history (:mod:`vantage.audit` explains why it deliberately is not), so
a value written by a script, a migration or the shell leaves no trace in it, and a
reconstruction that ignored that would present a guess in the same typeface as a fact.

So every field comes back with a verdict as well as a value, and the rules for the
verdict are the feature.

**A value is only claimed when the log is consistent with itself.** Each change entry
carries the value it replaced and the value it wrote. Walking a field's entries from
the point in question to now, each entry's "from" must equal the previous entry's
"to", and the last "to" must equal what the row holds today. A gap anywhere in that
chain means something the panel never saw moved the field, and the field is reported
as unknown rather than reconstructed from the half of the chain that happens to
survive. This is the check that turns "the audit log has a diff" into "the audit log
can be trusted for this field".

**Some values were never recorded faithfully, and saying so is not optional.** A
sensitive field's diff carries no value by design; a file is recorded by name; a
relation is recorded by label rather than by key; a JSON value loses its list order;
a long text is clipped. Each of those is shown with the reason, or not shown at all,
and none of them is offered for restore. The undo feature draws the same line for the
same reasons (:data:`vantage.undo._UNRESTORABLE`).

**A sensitive value never appears here** — not the historical one, not the current
one, not a hash of either. The answer comes from the panel, as on every other surface,
and it is the answer *now*: a field declared sensitive after an entry was written is
masked in that entry too.

**A restore is an edit.** It goes through the panel's ``ModelForm``, its validation,
its ``save_model`` hook, its tenancy guard and its audit diff — :func:`vantage.formviews.
save_form`, the function the edit page and the JSON API write through — with the
optimistic-lock token the history page was rendered with, so a record somebody changed
in the meantime conflicts instead of being quietly rolled back over their work. Its
audit entry is an ordinary update with ``action_name`` saying where the values came
from, which also means the restore is itself replayable history.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from vantage.audit import MAX_VALUE_CHARS, _jsonify
from vantage.conf import vantage_settings
from vantage.models import VantageLogEntry

__all__ = [
    "REPLAY_LIMIT",
    "RESTORE_PREFIX",
    "FieldState",
    "Position",
    "Reconstruction",
    "enabled_for",
    "fingerprint",
    "position_from",
    "reconstruct",
    "restore_allowed",
    "restore_data",
    "timeline",
]

#: How many of a record's entries a reconstruction reads. Replaying is linear in the
#: entries, and a record edited ten thousand times by an integration must not turn its
#: history page into the slowest page in the panel. Points older than the oldest entry
#: read are reported as "before the history begins" rather than half-reconstructed.
REPLAY_LIMIT = 2000

#: The ``action_name`` a restore's audit entry carries, followed by the ISO timestamp.
RESTORE_PREFIX = "restored as of "

#: An entry that recorded no "before" — a create.
_MISSING = object()


# ── configuration ───────────────────────────────────────────────────────────


def enabled_for(panel) -> bool:
    """Whether ``panel`` has a history page at all.

    Off with the audit log, since there is nothing to replay, and off when the panel
    says ``history = False``.
    """
    return bool(vantage_settings.AUDIT_LOG) and getattr(panel, "history", None) is not False


def restore_allowed(panel) -> bool:
    """Whether the history page may offer to restore. ``history = "view"`` says no."""
    return enabled_for(panel) and getattr(panel, "history", None) != "view"


# ── the point in time ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """A point in a record's history: a moment, optionally pinned to one entry.

    Pinned, because timestamps are not unique. Two entries written in the same
    microsecond — a save and its inline children — are still ordered by primary key,
    and "as it was after this change" has to mean after *this* one, not after
    whichever of the two the database returned last.
    """

    at: dt.datetime
    entry_id: int | None = None

    def includes(self, entry) -> bool:
        """Whether ``entry`` had already happened at this point."""
        if entry.created_at != self.at or self.entry_id is None:
            return entry.created_at <= self.at
        return entry.pk <= self.entry_id

    @property
    def query(self) -> dict[str, str]:
        """The query parameters that bring the page back to this point."""
        if self.entry_id is not None:
            return {"entry": str(self.entry_id)}
        return {"at": self.at.isoformat()}


def position_from(params, entries) -> Position | None:
    """The point the request asks for: ``?entry=<pk>``, or ``?at=<datetime>``.

    An entry must belong to this record — ``entries`` is the record's own list, so an
    entry id from another record is simply not found. A malformed value is no position
    at all, never an error: a stale bookmark degrades to the page without a
    reconstruction.
    """
    raw_entry = (params.get("entry") or "").strip()
    if raw_entry.isdigit():
        wanted = int(raw_entry)
        for entry in entries:
            if entry.pk == wanted:
                return Position(at=entry.created_at, entry_id=entry.pk)
        return None
    raw_at = (params.get("at") or "").strip()
    if not raw_at:
        return None
    try:
        parsed = parse_datetime(raw_at)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.combine(dt.date.fromisoformat(raw_at), dt.time.max)
        except ValueError:
            return None
    if timezone.is_naive(parsed) and _use_tz():
        # A ``datetime-local`` input has no offset; it means the panel's local time.
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    elif timezone.is_aware(parsed) and not _use_tz():
        parsed = timezone.make_naive(parsed)
    return Position(at=parsed)


def _use_tz() -> bool:
    from django.conf import settings

    return bool(settings.USE_TZ)


# ── reading the log ─────────────────────────────────────────────────────────


def _record_entries(obj):
    """This record's entries, oldest first, at most :data:`REPLAY_LIMIT` of them.

    The newest are the ones kept: a reconstruction starts from the row as it is now and
    walks backwards, so the recent end of the log is the end it cannot do without.
    """
    newest = list(
        VantageLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(type(obj)), object_id=str(obj.pk)
        )
        .select_related("actor")
        .order_by("-created_at", "-pk")[: REPLAY_LIMIT + 1]
    )
    truncated = len(newest) > REPLAY_LIMIT
    return list(reversed(newest[:REPLAY_LIMIT])), truncated


def timeline(obj) -> tuple[list[VantageLogEntry], bool]:
    """``(entries, truncated)``, oldest first. What the history page lists."""
    return _record_entries(obj)


def _bulk_runs_after(model, position: Position) -> list[VantageLogEntry]:
    """Model-wide entries — bulk actions, one-statement bulk edits, undos — since then.

    They carry no object id, because one entry per run rather than per row is what
    keeps the log readable. The price is that the record's own history cannot say
    whether one of them touched it, and that is reported rather than assumed away.
    """
    return list(
        VantageLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model),
            object_id="",
            action=VantageLogEntry.Action.ACTION,
            created_at__gte=position.at,
        ).only("pk", "created_at", "action_name", "changes")
    )


# ── the verdicts ────────────────────────────────────────────────────────────


@dataclass
class FieldState:
    """One field at one point in time, and how far that answer can be trusted."""

    name: str
    label: str
    #: The value as the log recorded it (JSON-shaped), when one is claimed.
    value: Any = None
    #: The value the row holds now, in the same shape.
    current: Any = None
    #: True when ``value`` is a reconstruction this module stands behind.
    known: bool = False
    #: True when the value is worth showing even though it is not ``known``.
    shown: bool = False
    masked: bool = False
    #: Why the value is unknown, shown only, or not restorable. Empty when it is all three.
    reason: str = ""
    #: Whether the value differs from now. Only meaningful when ``known``.
    changed: bool = False
    #: Whether this field may be ticked for restore.
    restorable: bool = False
    #: What a person should read for each side.
    value_display: str = ""
    current_display: str = ""


@dataclass
class Reconstruction:
    """A record as it was at ``position``.

    ``status`` is ``"ok"`` when there is something to show, and otherwise says why not:
    ``"empty"`` (nothing recorded), ``"before_create"`` (it did not exist yet),
    ``"before_history"`` (earlier than the log reaches) or ``"deleted"``.
    """

    position: Position
    status: str = "ok"
    starts_at: dt.datetime | None = None
    fields: list[FieldState] = field(default_factory=list)
    #: Labels of fields the log never records: not on the form, so no diff names them.
    unrecorded: list[str] = field(default_factory=list)
    #: How many model-wide bulk runs happened after ``position``.
    bulk_runs: int = 0
    truncated: bool = False

    @property
    def restorable(self) -> list[FieldState]:
        return [state for state in self.fields if state.restorable]

    @property
    def changed(self) -> list[FieldState]:
        return [state for state in self.fields if state.known and state.changed]


@dataclass
class _Event:
    """One entry's word on one field, reduced to what it says before and after."""

    before: Any
    after: Any
    redacted: bool = False


def _events(entries, position: Position) -> dict[str, tuple[list[_Event], list[_Event]]]:
    """Per field: the events up to and including ``position``, and those after it."""
    out: dict[str, tuple[list[_Event], list[_Event]]] = {}
    for entry in entries:
        side = 0 if position.includes(entry) else 1
        for name, delta in (entry.changes or {}).items():
            if not isinstance(delta, dict):
                continue
            if delta.get("redacted"):
                event = _Event(_MISSING, _MISSING, redacted=True)
            elif entry.action == VantageLogEntry.Action.CREATE:
                # A create's "from" is the form's initial value, not a state the row
                # was ever in; a duplicate's create is a snapshot, "from" only.
                event = _Event(_MISSING, delta.get("to", delta.get("from")))
            elif "to" in delta:
                event = _Event(delta.get("from"), delta.get("to"))
            else:
                # A snapshot — trash, restore, delete — observes without changing.
                value = delta.get("from")
                event = _Event(value, value)
            out.setdefault(name, ([], []))[side].append(event)
    return out


def _tracked_fields(panel) -> tuple[list[models.Field], list[str]]:
    """``(fields, unrecorded_labels)``: what can appear in a diff, and what cannot.

    A diff only ever names a field the change form wrote, so a field that is not an
    input on it — an ``auto_now`` timestamp, a non-editable column, anything in
    ``exclude`` or ``readonly_fields`` — has no history in the log at all. Those are
    listed, by label, so the page can say so instead of showing their current value as
    if it were the old one.
    """
    from vantage.formviews import form_editable_names
    from vantage.introspection import concrete_fields, m2m_fields

    model = panel.model
    on_form = set(form_editable_names(panel))
    tracked, unrecorded = [], []
    for model_field in [*concrete_fields(model), *m2m_fields(model)]:
        if model_field.primary_key:
            continue
        if model_field.name in on_form and model_field.editable:
            tracked.append(model_field)
        else:
            unrecorded.append(_label(model_field))
    return tracked, unrecorded


def _label(model_field) -> str:
    return str(getattr(model_field, "verbose_name", model_field.name)).capitalize()


def _current(obj, model_field) -> Any:
    """What the row holds now, in the shape the log stored it in."""
    if model_field.many_to_many:
        return _jsonify(getattr(obj, model_field.name))
    if isinstance(model_field, models.FileField):
        stored = getattr(obj, model_field.name, None)
        return getattr(stored, "name", None) or None
    return _jsonify(getattr(obj, model_field.attname, None))


def _comparable(model_field, value) -> Any:
    """``value`` reduced so two spellings of the same thing compare equal.

    ``"12.5"`` and ``"12.50"`` are the same decimal, and a blank string is the same
    absence as ``None`` on the forms this log was written from. Anything the field
    cannot parse is compared as it is.
    """
    if value is None or value == "" or value == []:
        return None
    if model_field.is_relation:
        return value
    try:
        return model_field.to_python(value)
    except (ValidationError, TypeError, ValueError):
        return value


def _same(model_field, left, right) -> bool:
    if left is _MISSING or right is _MISSING:
        return False
    return _comparable(model_field, left) == _comparable(model_field, right)


def _by_label_only(model_field) -> bool:
    """A single relation or a file: never shown, because what was recorded is not it."""
    if isinstance(model_field, models.FileField):
        return True
    return bool(model_field.is_relation) and not model_field.many_to_many


def _unfaithful(model_field, value) -> str:
    """Why this field's recorded value cannot be written back, or ``""``."""
    if model_field.is_relation:
        return str(_("Relations are recorded by their label, not by the record they point to."))
    if isinstance(model_field, models.FileField):
        return str(_("Files are recorded by name only; their contents were never kept."))
    if isinstance(model_field, models.BinaryField):
        return str(_("Binary values are never recorded."))
    if isinstance(model_field, models.JSONField):
        return str(_("JSON values are recorded approximately, so they cannot be restored."))
    if isinstance(value, str) and len(value) >= MAX_VALUE_CHARS and value.endswith("…"):
        return str(_("The value was too long to be recorded in full."))
    return ""


def _display(model_field, value) -> str:
    from vantage.concurrency import _display as display

    if value is None or value is _MISSING:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    parsed = _comparable(model_field, value)
    if isinstance(parsed, dt.datetime) and timezone.is_aware(parsed):
        parsed = timezone.localtime(parsed)
    return display(model_field, parsed)


def reconstruct(panel, obj, position: Position, *, entries=None) -> Reconstruction:
    """``obj`` as the audit log says it was at ``position``. See the module docstring."""
    if entries is None:
        entries, truncated = _record_entries(obj)
    else:
        truncated = False
    result = Reconstruction(position=position, truncated=truncated)
    if not entries:
        result.status = "empty"
        return result
    result.starts_at = entries[0].created_at
    if not position.includes(entries[0]):
        first_is_create = entries[0].action == VantageLogEntry.Action.CREATE and not truncated
        result.status = "before_create" if first_is_create else "before_history"
        return result
    prior = [entry for entry in entries if position.includes(entry)]
    if prior[-1].action == VantageLogEntry.Action.DELETE:
        result.status = "deleted"
        return result

    bulk = _bulk_runs_after(panel.model, position)
    result.bulk_runs = len(bulk)
    # A one-statement bulk edit says which fields it set; any other run could have
    # set anything.
    touched: set[str] = set()
    every_field = False
    for run in bulk:
        if run.action_name == "bulk_edit" and isinstance(run.changes, dict):
            touched.update(run.changes)
        else:
            every_field = True

    tracked, result.unrecorded = _tracked_fields(panel)
    sensitive = panel.get_sensitive_fields()
    events = _events(entries, position)

    for model_field in tracked:
        name = model_field.name
        state = FieldState(name=name, label=_label(model_field))
        result.fields.append(state)
        if name in sensitive:
            # Neither side, ever. Not even "changed": comparing two masked values
            # would tell the reader whether a secret has been rotated since.
            state.masked = True
            state.reason = str(_("Sensitive values are never shown in history."))
            continue

        if _by_label_only(model_field):
            # Recorded as a label on one side of a diff and a key on the other, or as
            # a file name the storage may since have changed. Showing either would be
            # showing something that is not the value.
            state.reason = _unfaithful(model_field, None)
            continue

        current = _current(obj, model_field)
        state.current = current
        state.current_display = _display(model_field, current)
        before, after = events.get(name, ([], []))
        anchor = before[-1] if before else None

        if any(event.redacted for event in after) or (anchor is not None and anchor.redacted):
            state.reason = str(
                _("This field changed while it was sensitive, so its value was not recorded.")
            )
            continue

        value = after[0].before if after else current
        if value is _MISSING:
            state.reason = str(_("There is no record of this value."))
            continue

        chain = ([anchor] if anchor is not None else []) + after
        consistent = all(_same(model_field, a.after, b.before) for a, b in pairwise(chain))
        if chain and not _same(model_field, chain[-1].after, current):
            consistent = False
        if not consistent:
            state.reason = str(
                _(
                    "This field was also changed somewhere the audit log does not see — a "
                    "script, an import, the shell — so its value here cannot be worked out."
                )
            )
            continue

        state.value = value
        state.value_display = _display(model_field, value)
        state.changed = not _same(model_field, value, current)
        state.shown = True

        unfaithful = _unfaithful(model_field, value)
        if anchor is None and (every_field or name in touched):
            state.reason = str(
                _(
                    "A bulk change ran on these records after this point, and nothing "
                    "earlier in this record's history confirms the value."
                )
            )
            continue
        if unfaithful:
            state.reason = unfaithful
            continue

        state.known = True
        # Every tracked field is one the edit form writes, so a known, changed value
        # can go back through that form. Who may press the button is the view's call.
        state.restorable = state.changed and restore_allowed(panel)
    return result


# ── restoring ───────────────────────────────────────────────────────────────


def fingerprint(reconstruction: Reconstruction) -> str:
    """A digest of the current values the page was rendered against.

    The version column is the real guard, but not every model has one. This catches
    the same race for a model that does not: a restore whose fields have moved since
    the page was drawn is refused. Sensitive fields are left out — even a digest of a
    four-digit PIN is a lookup table away from the PIN.
    """
    payload = {
        state.name: state.current
        for state in reconstruction.fields
        if not state.masked and state.name
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def restore_data(reconstruction: Reconstruction, names: list[str]) -> dict[str, Any]:
    """Form data that writes the chosen fields back to their reconstructed values.

    The values go through the panel's ``ModelForm`` as if typed, so the form decides
    whether they are still acceptable — a choice since removed, a ``max_length`` since
    shortened and a unique value since taken all fail validation rather than the save.
    """
    states = {state.name: state for state in reconstruction.restorable}
    data: dict[str, Any] = {}
    for name in names:
        value = states[name].value
        if value is None:
            data[name] = ""
        elif isinstance(value, bool):
            data[name] = value
        else:
            data[name] = str(value)
    return data
