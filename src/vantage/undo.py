"""Undo: a few seconds in which a delete or an edit can be taken back.

After a destructive or bulk change the toast says "Deleted 3 orders · Undo", and for
``VANTAGE["UNDO_WINDOW"]`` seconds the button reverses it. The feature is about *not
losing data*, so every mechanism below was chosen by asking what it could destroy, and
where no mechanism was safe the toast simply has no button. A missing Undo is a
nuisance; an Undo that restores the wrong thing is a second data-loss incident caused by
the tool that was meant to prevent the first.

One mechanism per kind of change:

**Edits** — bulk edit, inline cells, ``@action(undo_fields=…)`` — record each row's
values before and after, read back from the database inside the writing transaction.
Undo puts the "before" back **only if every row still holds exactly the "after"**,
checked under a row lock. If anyone has touched one of those fields since, nothing is
changed at all and the conflict is reported. Restoring nine rows and silently skipping
the tenth would leave a state nobody chose; overwriting the tenth would destroy a
colleague's edit. The version column (:mod:`vantage.concurrency`) is advanced by the
undo exactly as by the edit, so a form someone has open on the old values now conflicts
instead of quietly writing them back.

**Soft deletes** — the model has a trash (:func:`vantage.patterns.detect_soft_delete`)
— are already reversible: the delete marks the row, the undo unmarks it, on the same
condition that the row still carries the mark we wrote.

**Hard deletes** use *snapshot and restore*: the delete runs now, exactly as it always
did, inside one transaction that first serialises every row the cascade is about to
remove, plus the old value of every foreign key ``SET_NULL``/``SET_DEFAULT`` will
rewrite. Undo re-inserts those rows with their original primary keys and puts those
foreign keys back. The alternative, *deferred delete* — keep the rows, hide them, delete
them when the window closes — was rejected, and the reasons are the design:

* Hiding only works where every query goes through this panel. The project's own site,
  its API, its workers and its reports would keep reading and writing rows the user was
  told were gone, for a window that has to be enforced by somebody.
* "Somebody" is a process that must run the deletes later. With several workers and no
  guaranteed scheduler, a deletion can be late, or never happen — and for a record
  deleted for privacy reasons, never is the failure that matters.
* The delete would run *after* the confirmation, against data that has moved: a new
  ``PROTECT`` reference makes it fail after the user was told it succeeded, and a new
  ``CASCADE`` child is destroyed without ever having been shown on the confirm page.

Snapshot and restore keeps the delete synchronous, honest and identical to before. Its
risks are real too, and each one is a condition under which **no undo is offered**:

* ``pre_delete``/``post_delete`` receivers on any model in the cascade. A receiver may
  have deleted a file, revoked a key or called a payment provider; re-inserting the row
  cannot reverse that, and would resurrect a record pointing at things that no longer
  exist.
* A database-level ``on_delete`` (``DB_CASCADE``, ``DB_SET_NULL``…) touching the graph:
  the database rewrites rows the snapshot never saw.
* An overridden ``Model.delete()`` or ``QuerySet.delete()``: the project's own delete
  has to run, and it cannot be snapshotted from outside.
* More than ``UNDO_MAX_ROWS`` rows, cascade and rewritten references included.

Nothing is fast-deleted on this path: every row is collected and deleted by primary key,
so a row inserted concurrently into the graph makes the delete fail its foreign-key check
rather than vanish unrecorded. The restore uses ``force_insert`` — a primary key or
unique value somebody has taken in the meantime is an ``IntegrityError``, never an
overwrite — and checks foreign-key integrity before it commits. Any failure rolls the
whole restore back and reports a conflict. ``pre_save``/``post_save`` do fire for the
restored rows, with ``raw=True``, as they do for ``loaddata``.

The token is single-use, bound to the user *and* the session, expires, needs the same
permission as the change it reverses, is redeemed only by a CSRF-protected POST, and is
consumed by a conditional ``UPDATE`` so a double click undoes once and reports the same
outcome twice. State lives in :class:`~vantage.models.UndoEntry`; see there for why a
table and not the cache.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core import serializers
from django.db import DatabaseError, IntegrityError, connections, models, router, transaction
from django.db.models import F, QuerySet, signals
from django.db.models.deletion import Collector, get_candidate_relations_to_delete
from django.utils import timezone
from django.utils.duration import duration_iso_string
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from vantage.concurrency import version_column
from vantage.conf import vantage_settings

__all__ = [
    "DeleteOutcome",
    "FieldCapture",
    "Offer",
    "Redemption",
    "UndoConflict",
    "UndoPlan",
    "arm",
    "armed",
    "delete",
    "enabled_for",
    "offer",
    "offer_from_message",
    "purge_expired",
    "redeem",
    "soft_delete",
    "window_seconds",
]

logger = logging.getLogger("vantage.undo")

#: The tag a toast's message carries, followed by ``<token>:<expiry epoch>``.
TAG_PREFIX = "vantage-undo:"

#: Ceiling on one entry's serialised payload. ``UNDO_MAX_ROWS`` bounds the row count;
#: this bounds the bytes, because a thousand rows of a model with a large text column is
#: a different thing from a thousand rows of an ``id`` and a ``status``.
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024

#: Live entries kept per user. A person cannot use more than the newest few toasts, and
#: a script hammering the delete endpoint must not be able to grow this table.
MAX_PER_USER = 20

SESSION_KEY = "vantage-undo-nonce"

#: Field classes whose value cannot be compared and restored faithfully by this module:
#: files (the bytes are not ours to snapshot), JSON (equality is backend-defined) and
#: binary. An edit touching one is not offered for undo.
_UNRESTORABLE = (models.FileField, models.JSONField, models.BinaryField)


# ── configuration ───────────────────────────────────────────────────────────


def window_seconds() -> int:
    return int(vantage_settings.UNDO_WINDOW or 0)


def max_rows() -> int:
    return int(vantage_settings.UNDO_MAX_ROWS)


def enabled_for(panel) -> bool:
    """Whether changes to ``panel``'s model may be offered for undo at all."""
    return window_seconds() > 0 and getattr(panel, "undo", None) is not False


def arm(request, panel) -> None:
    """Declare that this request will store an undo if the change produces one.

    Explicit rather than inferred from "is there a request": :mod:`vantage.jobs` runs the
    same action code against a synthetic request with no session and no toast, and
    building a snapshot there would be pure cost. Only the views that can show a toast
    arm the request.
    """
    if enabled_for(panel):
        try:
            request._vantage_undo = True
        except AttributeError:  # pragma: no cover - a request-like with __slots__
            pass


def armed(request) -> bool:
    return bool(getattr(request, "_vantage_undo", False)) and window_seconds() > 0


# ── value encoding ──────────────────────────────────────────────────────────


class _Encoder(json.JSONEncoder):
    """JSON with every value exact.

    Not ``DjangoJSONEncoder``: it truncates datetimes to milliseconds, and a restored
    timestamp that differs from the original by 0.4 ms is a different value — one that
    would also never compare equal to itself again.
    """

    def default(self, o):
        if isinstance(o, dt.datetime | dt.date | dt.time):
            return o.isoformat()
        if isinstance(o, dt.timedelta):
            return duration_iso_string(o)
        if isinstance(o, Decimal | UUID):
            return str(o)
        if isinstance(o, bytes | memoryview):  # pragma: no cover - filtered upstream
            raise TypeError("binary values are not snapshotted")
        return super().default(o)


def _dumps(value: Any) -> str:
    return json.dumps(value, cls=_Encoder, separators=(",", ":"))


def _plain(value: Any) -> Any:
    """A value as it will read back after a JSON round trip."""
    return json.loads(_dumps(value))


def _decode(field, raw: Any) -> Any:
    """The typed value for ``field``, from what :func:`_plain` produced."""
    return field.to_python(raw)


def _restorable(field) -> bool:
    return (
        getattr(field, "concrete", False)
        and not field.many_to_many
        and not field.primary_key
        and not isinstance(field, _UNRESTORABLE)
        and not getattr(field, "generated", False)
    )


# ── the plan ────────────────────────────────────────────────────────────────


class UndoConflict(Exception):
    """The change cannot be reversed without destroying something newer.

    Raised inside the restoring transaction, which rolls back; the message is shown to
    the person who pressed Undo. Custom undo hooks raise it too.
    """


@dataclass
class UndoPlan:
    """How to reverse one change, built by whoever made the change.

    ``steps`` is JSON-ready and run in order, in one transaction. ``summary`` completes
    the audit sentence "undid …"; ``done`` is the toast after a successful undo.
    """

    model: type[models.Model]
    verb: str
    summary: str
    done: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    #: Where the change came from. ``"inline"`` asks the undo response to carry the
    #: rows' fresh values, so the grid can show them without a reload.
    origin: str = ""


def _names(panel_or_model, count: int) -> tuple[str, str]:
    """``(singular, plural)`` labels, lowercased, for a sentence about ``count`` rows."""
    if hasattr(panel_or_model, "get_label"):
        return (
            str(panel_or_model.get_label()).lower(),
            str(panel_or_model.get_label_plural()).lower(),
        )
    opts = panel_or_model._meta
    return str(opts.verbose_name).lower(), str(opts.verbose_name_plural).lower()


def describe(panel, count: int, what: str, *, label: str = "", obj=None) -> tuple[str, str]:
    """``(summary, done)`` for a change of kind ``what`` over ``count`` rows.

    Whole sentences per kind, for the reason :meth:`vantage.actions.Selection.describe`
    gives: a translator cannot place a verb they are handed separately from its object.
    """
    one, many = _names(panel, count)
    values = {"count": count, "model": one, "models": many, "label": label, "object": obj}
    if what == "delete" and obj is not None:
        return (
            _("delete of %(model)s “%(object)s”") % values,
            _("Restored %(model)s “%(object)s”.") % values,
        )
    if what == "delete":
        return (
            ngettext("delete of %(count)s %(model)s", "delete of %(count)s %(models)s", count)
            % values,
            ngettext("Restored %(count)s %(model)s.", "Restored %(count)s %(models)s.", count)
            % values,
        )
    if what == "bulk_edit":
        return (
            ngettext("bulk edit of %(count)s %(model)s", "bulk edit of %(count)s %(models)s", count)
            % values,
            ngettext(
                "Reverted the edit to %(count)s %(model)s.",
                "Reverted the edit to %(count)s %(models)s.",
                count,
            )
            % values,
        )
    if what == "inline":
        return (
            ngettext(
                "inline edit of %(count)s %(model)s", "inline edit of %(count)s %(models)s", count
            )
            % values,
            ngettext(
                "Reverted the change to %(count)s %(model)s.",
                "Reverted the changes to %(count)s %(models)s.",
                count,
            )
            % values,
        )
    # An action, named by its label.
    return (
        ngettext("“%(label)s” on %(count)s %(model)s", "“%(label)s” on %(count)s %(models)s", count)
        % values,
        ngettext(
            "Reverted “%(label)s” on %(count)s %(model)s.",
            "Reverted “%(label)s” on %(count)s %(models)s.",
            count,
        )
        % values,
    )


# ── edits ───────────────────────────────────────────────────────────────────


class FieldCapture:
    """Before/after values for an edit, so the edit can be put back.

    Used in three steps, all inside the transaction that performs the edit::

        capture = FieldCapture(Order, ["status"], strategy="sql")
        queryset = capture.lock(queryset)      # bounded, row-locked, pk-pinned
        queryset.update(status="paid")
        capture.read_after()                   # what the database actually stored

    ``lock`` pins the queryset to the rows it locked, so the rows written are exactly
    the rows recorded. Rows whose value did not move are dropped from the plan: there is
    nothing to put back, and including them would make the undo conflict with an edit
    that merely set the same value again.

    ``active`` goes false — and the edit proceeds untouched, just without an undo — when
    a field cannot be restored faithfully or the selection is over ``UNDO_MAX_ROWS``.
    """

    def __init__(self, model, names, *, strategy: str = "sql", active: bool = True) -> None:
        self.model = model
        self.strategy = "sql" if strategy == "sql" else "instances"
        self.fields = {}
        self.active = active
        for name in names:
            try:
                model_field = model._meta.get_field(name)
            except Exception:
                self.active = False
                continue
            if not _restorable(model_field):
                self.active = False
            self.fields[model_field.attname] = model_field
        if not self.fields:
            self.active = False
        # pk (as JSON) -> {attname: plain value}
        self.before: dict[Any, dict[str, Any]] = {}
        self.after: dict[Any, dict[str, Any]] = {}
        # Per-row field subsets, for the inline grid where each row edits its own cells.
        self.row_fields: dict[Any, list[str]] = {}

    @property
    def db(self) -> str:
        return router.db_for_write(self.model)

    def lock(self, queryset: QuerySet) -> QuerySet:
        """Record the rows' current values under a lock, and pin ``queryset`` to them."""
        if not self.active:
            return queryset
        limit = max_rows()
        pks = list(queryset.order_by().values_list("pk", flat=True)[: limit + 1])
        if len(pks) > limit:
            self.active = False
            return queryset
        attnames = list(self.fields)
        rows = (
            self.model._base_manager.using(self.db)
            .select_for_update()
            .filter(pk__in=pks)
            .values_list("pk", *attnames)
        )
        for row in rows:
            self.before[_plain(row[0])] = dict(zip(attnames, _plain(list(row[1:])), strict=True))
        return queryset.filter(pk__in=pks)

    def note(self, obj, attnames: list[str] | None = None) -> None:
        """Record one already-loaded row's current values (the save-per-row paths)."""
        if not self.active:
            return
        if len(self.before) >= max_rows():
            self.active = False
            return
        names = [name for name in (attnames or list(self.fields)) if name in self.fields]
        key = _plain(obj.pk)
        self.before[key] = {name: _plain(getattr(obj, name)) for name in names}
        self.row_fields[key] = names

    def forget(self, obj) -> None:
        """Drop a row that ended up not being written (it failed validation)."""
        key = _plain(obj.pk)
        self.before.pop(key, None)
        self.row_fields.pop(key, None)

    def read_after(self) -> None:
        """Read what the edit stored, from the database, inside the same transaction."""
        if not self.active or not self.before:
            return
        attnames = list(self.fields)
        rows = (
            self.model._base_manager.using(self.db)
            .filter(pk__in=list(self.before))
            .values_list("pk", *attnames)
        )
        for row in rows:
            self.after[_plain(row[0])] = dict(zip(attnames, _plain(list(row[1:])), strict=True))

    def read_row(self, obj) -> None:
        """Read one row's stored values. Call inside the transaction that saved it.

        Inside, because a value read after the commit may already be somebody else's —
        and recording their value as ours would make the undo overwrite it.
        """
        key = _plain(obj.pk)
        if not self.active or key not in self.before:
            return
        attnames = list(self.fields)
        row = (
            self.model._base_manager.using(self.db)
            .filter(pk=obj.pk)
            .values_list("pk", *attnames)
            .first()
        )
        if row is not None:
            self.after[key] = dict(zip(attnames, _plain(list(row[1:])), strict=True))

    def changed_rows(self) -> list[dict[str, Any]]:
        rows = []
        for pk, before in self.before.items():
            after = self.after.get(pk)
            if after is None:
                continue
            names = self.row_fields.get(pk) or list(before)
            values = {
                name: [before[name], after[name]]
                for name in names
                if name in before and name in after and before[name] != after[name]
            }
            if values:
                rows.append({"pk": pk, "values": values})
        return rows

    def plan(self, panel, *, verb: str, what: str, label: str = "", origin: str = ""):
        """The :class:`UndoPlan`, or ``None`` when there is nothing to offer."""
        if not self.active:
            return None
        rows = self.changed_rows()
        if not rows:
            return None
        summary, done = describe(panel, len(rows), what, label=label)
        return UndoPlan(
            model=self.model,
            verb=verb,
            summary=summary,
            done=done,
            origin=origin,
            steps=[
                {
                    "kind": "fields",
                    "model": self.model._meta.label_lower,
                    "strategy": self.strategy,
                    "rows": rows,
                }
            ],
        )


# ── soft delete ─────────────────────────────────────────────────────────────


def _trash_value(pattern):
    return True if pattern.kind == "boolean" else timezone.now()


def soft_delete(panel, queryset: QuerySet, *, capture: bool = True, value: Any = None):
    """Mark ``queryset``'s live rows as trashed. Returns ``(count, step or None)``.

    ``update`` rather than ``save()``, for the reason
    :meth:`vantage.formviews.DeleteView.soft_delete` gives. The rows are pinned under a
    lock first so the step records exactly the rows that were marked, and the written
    value is read back so a ``DateField`` that stored a date — not the datetime we sent —
    still compares equal at undo time.
    """
    pattern = panel.get_patterns().soft_delete
    model = panel.model
    db = router.db_for_write(model)
    base = model._base_manager.using(db)
    with transaction.atomic(using=db):
        pks = list(queryset.filter(pattern.live).order_by().values_list("pk", flat=True))
        if not pks:
            return 0, None
        locked = list(base.select_for_update().filter(pk__in=pks).values_list("pk", flat=True))
        if value is None:
            value = _trash_value(pattern)
        count = base.filter(pk__in=locked).update(**{pattern.field: value})
        if not capture or len(locked) > max_rows():
            return count, None
        written = list(base.filter(pk__in=locked).values_list("pk", pattern.field))
    return count, {
        "kind": "untrash",
        "model": model._meta.label_lower,
        "field": pattern.field,
        "live": False if pattern.kind == "boolean" else None,
        "rows": [[_plain(pk), _plain(value)] for pk, value in written],
    }


# ── hard delete ─────────────────────────────────────────────────────────────


class _TooLarge(Exception):
    pass


class _UndoCollector(Collector):
    """Django's collector with every row materialised, and a ceiling.

    ``can_fast_delete`` is off so every row the cascade removes is a loaded instance we
    can snapshot and a primary key the delete names explicitly. Field updates
    (``SET_NULL`` and friends) are materialised the same way, recording each row's old
    value. Neither changes *what* is deleted or updated — only that it is known.
    """

    def __init__(self, using, origin, limit: int) -> None:
        super().__init__(using, origin=origin)
        self.limit = limit
        self.seen = 0
        #: (model, field, value written, [(pk, old value), …])
        self.rewrites: list[tuple[Any, Any, Any, list[tuple[Any, Any]]]] = []
        self.unsafe: str | None = None

    def _count(self, n: int) -> None:
        self.seen += n
        if self.seen > self.limit:
            raise _TooLarge

    def can_fast_delete(self, objs, from_field=None):
        return False

    def add(self, objs, source=None, nullable=False, reverse_dependency=False):
        new = super().add(objs, source, nullable, reverse_dependency=reverse_dependency)
        self._count(len(new))
        return new

    def add_field_update(self, field, value, objs):
        if isinstance(objs, QuerySet):
            remaining = self.limit - self.seen + 1
            rows = list(objs.order_by().values_list("pk", field.attname)[:remaining])
            self._count(len(rows))
            self.rewrites.append((objs.model, field, value, rows))
            objs = [objs.model(pk=pk) for pk, _old in rows]
        else:
            # Already-loaded instances, possibly with the field deferred: the old value
            # is not reliably in hand, so this graph is not offered for undo.
            self.unsafe = "a reference rewrite on pre-loaded rows"
        super().add_field_update(field, value, objs)


def _db_level_on_delete() -> tuple:
    from django.db.models import deletion

    return tuple(
        handler
        for handler in (
            getattr(deletion, "DB_CASCADE", None),
            getattr(deletion, "DB_SET_NULL", None),
            getattr(deletion, "DB_SET_DEFAULT", None),
        )
        if handler is not None
    )


def _unsafe_reason(collector: _UndoCollector) -> str | None:
    if collector.unsafe:
        return collector.unsafe
    db_handlers = _db_level_on_delete()
    for model in collector.data:
        if signals.pre_delete.has_listeners(model) or signals.post_delete.has_listeners(model):
            return f"{model._meta.label} has delete signal receivers"
        for related in get_candidate_relations_to_delete(model._meta):
            if related.field.remote_field.on_delete in db_handlers:
                return f"{related.related_model._meta.label} is cleared by the database itself"
    return None


def _snapshot(collector: _UndoCollector, db: str) -> tuple[dict[str, Any], Counter]:
    """Every collected row in full, in an order safe to re-insert, plus per-model counts."""
    collector.sort()
    objects: list[dict[str, Any]] = []
    counts: Counter = Counter()
    deleted_keys: dict[str, set] = defaultdict(set)
    tables: set[str] = set()
    # Reverse deletion order is insertion order: parents before the rows that point at
    # them, for backends that check a foreign key the moment a row is inserted.
    for model, instances in reversed(list(collector.data.items())):
        if not instances:
            continue
        pks = [obj.pk for obj in instances]
        concrete = model._meta.concrete_model
        names = [f.name for f in concrete._meta.local_fields if f.serialize and not f.primary_key]
        if any(isinstance(f, models.BinaryField) for f in concrete._meta.local_fields):
            raise _Unsafe(f"{model._meta.label} has a binary column")
        rows = []
        for start in range(0, len(pks), 500):
            chunk = pks[start : start + 500]
            rows.extend(model._base_manager.using(db).select_for_update().filter(pk__in=chunk))
        if len(rows) != len(pks):
            raise _Unsafe("a row in the cascade disappeared while it was being read")
        objects.extend(serializers.serialize("python", rows, fields=names))
        counts[model._meta.label] += len(instances)
        deleted_keys[concrete._meta.label_lower].update(_plain(pk) for pk in pks)
        tables.add(concrete._meta.db_table)

    updates = []
    for model, model_field, value, rows in collector.rewrites:
        concrete = model._meta.concrete_model
        gone = deleted_keys.get(concrete._meta.label_lower, set())
        kept = [[_plain(pk), _plain(old)] for pk, old in rows if _plain(pk) not in gone]
        if kept:
            updates.append(
                {
                    "model": concrete._meta.label_lower,
                    "field": model_field.attname,
                    "value": _plain(value),
                    "rows": kept,
                }
            )
            tables.add(concrete._meta.db_table)
    step = {
        "kind": "reinsert",
        "objects": _plain(objects),
        "updates": updates,
        "tables": sorted(tables),
    }
    return step, counts


class _Unsafe(Exception):
    pass


@dataclass
class DeleteOutcome:
    """What a hard delete removed, and how to put it back if that is possible."""

    #: Every row removed, cascade included.
    total: int
    #: ``{"shop.Order": 3, "shop.Line": 9}`` as Django's ``delete()`` reports it.
    per_model: dict[str, int]
    step: dict[str, Any] | None = None
    #: Why no undo was offered, for logs and tests. Never shown as an error.
    reason: str = ""


def _overrides_delete(target) -> bool:
    if isinstance(target, QuerySet):
        return type(target).delete is not QuerySet.delete
    return type(target).delete is not models.Model.delete


def delete(panel, target, *, request=None) -> DeleteOutcome:
    """Delete ``target`` — one instance or a queryset — recording a snapshot if possible.

    Behaves exactly like ``target.delete()``: same cascade, same signals, same
    ``ProtectedError``. The snapshot path runs only when the request is armed and every
    safety condition in the module docstring holds; otherwise this *is*
    ``target.delete()``.
    """
    model = panel.model
    db = router.db_for_write(model)
    reason = ""
    if request is not None and armed(request) and enabled_for(panel):
        if _overrides_delete(target):
            reason = "the model or queryset overrides delete()"
        else:
            try:
                return _delete_with_snapshot(model, target, db)
            except _TooLarge:
                reason = "more rows than UNDO_MAX_ROWS"
            except _Unsafe as exc:
                reason = str(exc)
    else:
        reason = "undo is not armed for this request"

    with transaction.atomic(using=db):
        total, per_model = target.delete()
    return DeleteOutcome(total=total, per_model=dict(per_model), reason=reason)


def _delete_with_snapshot(model, target, db) -> DeleteOutcome:
    pks = [target.pk] if isinstance(target, models.Model) else None
    with transaction.atomic(using=db):
        if pks is None:
            pks = list(target.order_by().values_list("pk", flat=True)[: max_rows() + 1])
            if len(pks) > max_rows():
                raise _TooLarge
        # Locking the roots is what stops a concurrent insert from attaching a new
        # child to them between the snapshot and the delete (it would need a share lock
        # on the parent row). Anything deeper fails the delete's own FK check instead.
        roots = list(model._base_manager.using(db).select_for_update().filter(pk__in=pks))
        origin = target if isinstance(target, models.Model) else roots
        collector = _UndoCollector(db, origin=origin, limit=max_rows())
        collector.collect(roots)
        reason = _unsafe_reason(collector)
        if reason is not None:
            raise _Unsafe(reason)
        step, counts = _snapshot(collector, db)
        total, per_model = collector.delete()
        if isinstance(target, models.Model):
            # Model.delete() leaves the caller's instance without a pk; so do we.
            setattr(target, model._meta.pk.attname, None)
    expected = {label: count for label, count in counts.items() if count}
    actual = {label: count for label, count in per_model.items() if count}
    if expected != actual:
        # The delete stands — it is what was asked for — but a snapshot that does not
        # match what was removed must not be offered as a way to get it back.
        logger.warning("Undo snapshot mismatch: expected %s, deleted %s.", expected, actual)
        return DeleteOutcome(total, dict(per_model), None, "snapshot did not match")
    return DeleteOutcome(total=total, per_model=dict(per_model), step=step)


# ── offering ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Offer:
    token: str
    expires_at: dt.datetime

    @property
    def tag(self) -> str:
        return f"{TAG_PREFIX}{self.token}:{int(self.expires_at.timestamp())}"

    def remaining(self) -> int:
        return max(int((self.expires_at - timezone.now()).total_seconds()), 0)

    def as_json(self, message: str = "") -> dict[str, Any]:
        from django.urls import reverse

        return {
            "token": self.token,
            "remaining": self.remaining(),
            "url": reverse("vantage:undo"),
            "message": message,
        }


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _nonce(request, *, create: bool) -> str | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    value = session.get(SESSION_KEY)
    if value is None and create:
        value = secrets.token_hex(16)
        session[SESSION_KEY] = value
    return value


def offer(request, panel, plan: UndoPlan | None) -> Offer | None:
    """Store ``plan`` and return the token for the toast, or ``None``.

    Never raises: the change has already happened, and a failure to *offer* its undo
    must not turn a successful delete into an error page. Runs in its own savepoint for
    the same reason — under ``ATOMIC_REQUESTS`` a failed insert here would otherwise
    poison the request's transaction and roll back the very delete it describes.
    """
    from vantage.models import UndoEntry

    if plan is None or not plan.steps or not enabled_for(panel) or not armed(request):
        return None
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    nonce = _nonce(request, create=True)
    if nonce is None:
        return None
    payload = _dumps({"v": 1, "done": plan.done, "origin": plan.origin, "steps": plan.steps})
    if len(payload) > MAX_PAYLOAD_BYTES:
        logger.info("Undo not offered: payload of %s bytes is over the cap.", len(payload))
        return None

    token = secrets.token_urlsafe(32)
    now = timezone.now()
    expires_at = now + dt.timedelta(seconds=window_seconds())
    try:
        with transaction.atomic(using=router.db_for_write(UndoEntry)):
            purge_expired(now=now)
            UndoEntry.objects.create(
                token_hash=_hash(token),
                user=user,
                session_nonce=nonce,
                resource_key=plan.model._meta.label_lower,
                verb=plan.verb or "view",
                summary=str(plan.summary)[:200],
                payload=payload,
                expires_at=expires_at,
            )
            stale = list(
                UndoEntry.objects.filter(user=user)
                .order_by("-created_at", "-pk")
                .values_list("pk", flat=True)[MAX_PER_USER:]
            )
            if stale:
                UndoEntry.objects.filter(pk__in=stale).delete()
    except DatabaseError:
        logger.exception("Could not store an undo entry; the change stands without one.")
        return None
    return Offer(token=token, expires_at=expires_at)


def offer_from_message(message) -> dict[str, Any] | None:
    """``{"token", "remaining"}`` from a message's tags, or ``None``.

    Read by the toast template. ``remaining`` is computed against the server clock at
    render time and handed to the browser as a duration, never as a timestamp, so a
    laptop whose clock is a minute out still hides the button at the right moment.
    """
    for tag in str(getattr(message, "extra_tags", "") or "").split():
        if not tag.startswith(TAG_PREFIX):
            continue
        token, _sep, epoch = tag[len(TAG_PREFIX) :].rpartition(":")
        try:
            remaining = int(epoch) - int(timezone.now().timestamp())
        except ValueError:
            return None
        if not token or remaining <= 0:
            return None
        return {"token": token, "remaining": remaining}
    return None


def purge_expired(*, now: dt.datetime | None = None) -> int:
    """Delete every entry past its window. Returns how many went.

    Called on every write to the table, which keeps it bounded by what a window's worth
    of activity can create. A snapshot of deleted rows is still a copy of deleted data,
    so it should not outlive its purpose; ``manage.py vantage_purge_undo`` exists for a
    deployment that wants that guarantee without waiting for the next delete.
    """
    from vantage.models import UndoEntry

    deleted, _per_model = UndoEntry.objects.filter(expires_at__lte=now or timezone.now()).delete()
    return deleted


# ── redeeming ───────────────────────────────────────────────────────────────


@dataclass
class Redemption:
    """The answer to one press of Undo."""

    status: int
    message: str
    level: str = "success"
    ok: bool = False
    already: bool = False
    resource_key: str = ""
    rows: list[dict[str, Any]] | None = None

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ok": self.ok,
            "already": self.already,
            "message": self.message,
            "level": self.level,
        }
        if self.rows is not None:
            body["rows"] = self.rows
        return body


def _gone() -> Redemption:
    # One answer for "no such token", "not yours" and "not this session": telling them
    # apart would tell a prober which tokens exist.
    return Redemption(404, _("This undo is no longer available. Nothing was changed."), "warning")


def redeem(request, site, token: str, permissions) -> Redemption:
    """Spend ``token``: authorise it, consume it exactly once, and reverse the change."""
    from vantage.models import UndoEntry

    if not token or len(token) > 200:
        return _gone()
    entry = UndoEntry.objects.filter(token_hash=_hash(token)).first()
    user = getattr(request, "user", None)
    if (
        entry is None
        or user is None
        or entry.user_id != user.pk
        or entry.session_nonce != _nonce(request, create=False)
    ):
        return _gone()
    if entry.used_at is not None:
        return _repeat(entry)
    now = timezone.now()
    if entry.expires_at <= now:
        return Redemption(
            410, _("The time to undo this has passed. Nothing was changed."), "warning"
        )

    panel = _panel_for(site, entry.resource_key)
    if panel is None:
        return _gone()
    if not permissions.has_model_perm(request, panel.model, entry.verb):
        return Redemption(
            403, _("You no longer have permission to undo this. Nothing was changed."), "error"
        )

    # The consumption is its own statement, committed before any restoring starts, so
    # a second click — even one that lands on another worker mid-restore — finds the
    # token spent and does nothing.
    claimed = UndoEntry.objects.filter(
        pk=entry.pk, used_at__isnull=True, expires_at__gt=now
    ).update(used_at=now)
    if not claimed:
        entry.refresh_from_db()
        if entry.used_at is not None:
            return _repeat(entry)
        return Redemption(
            410, _("The time to undo this has passed. Nothing was changed."), "warning"
        )

    try:
        plan = json.loads(entry.payload or "{}")
    except ValueError:
        plan = {}
    db = router.db_for_write(panel.model)
    restored: list[Any] = []
    try:
        with transaction.atomic(using=db):
            for step in plan.get("steps", []):
                restored.extend(_apply(request, panel, step, permissions, db) or [])
    except UndoConflict as exc:
        return _close(entry, Redemption(409, str(exc), "error", resource_key=entry.resource_key))
    except (IntegrityError, DatabaseError, serializers.base.DeserializationError) as exc:
        logger.warning("Undo of %s failed and was rolled back: %s", entry.summary, exc)
        return _close(
            entry,
            Redemption(
                409,
                _(
                    "This could not be undone: something else now occupies the place "
                    "of what was removed. Nothing was changed."
                ),
                "error",
                resource_key=entry.resource_key,
            ),
        )
    except Exception:
        # Rolled back like any other failure. Caught so the outcome is *recorded*: a
        # spent token with no result would tell the next press "already undone".
        logger.exception("Undo of %s raised and was rolled back.", entry.summary)
        return _close(
            entry,
            Redemption(
                500,
                _("Something went wrong while undoing this. Nothing was changed."),
                "error",
                resource_key=entry.resource_key,
            ),
        )

    from vantage import audit
    from vantage.models import VantageLogEntry

    audit.record(
        request,
        VantageLogEntry.Action.ACTION,
        model=panel.model,
        action_name="undo",
        object_repr=_("undid %(change)s") % {"change": entry.summary},
        changes={},
    )
    outcome = Redemption(
        200,
        plan.get("done") or _("Undone."),
        "success",
        ok=True,
        resource_key=entry.resource_key,
    )
    if plan.get("origin") == "inline":
        outcome.rows = _inline_rows(request, site, panel, restored)
    return _close(entry, outcome)


def _repeat(entry) -> Redemption:
    """The second press of a spent token: the first press's answer, unchanged."""
    message = entry.result or _("This undo has already been used.")
    return Redemption(200, message, "info", ok=True, already=True, resource_key=entry.resource_key)


def _close(entry, outcome: Redemption) -> Redemption:
    """Record the outcome and drop the payload: it is a copy of data nobody needs now."""
    from vantage.models import UndoEntry

    try:
        UndoEntry.objects.filter(pk=entry.pk).update(result=str(outcome.message)[:300], payload="")
    except DatabaseError:  # pragma: no cover - the undo itself already succeeded or not
        logger.exception("Could not record the outcome of an undo.")
    return outcome


def _panel_for(site, resource_key: str):
    for panel in site.panels():
        if panel.opts.label_lower == resource_key:
            return panel
    return None


def _inline_rows(request, site, panel, pks) -> list[dict[str, Any]]:
    from vantage.inline import InlineWriteView

    view = InlineWriteView(site=site, panel=panel)
    view.request = request
    view.kwargs = {}
    objs = panel.model._base_manager.filter(pk__in=pks)
    return [view.row_result(obj) for obj in objs]


# ── applying steps ──────────────────────────────────────────────────────────


def _model(label: str):
    from django.apps import apps

    return apps.get_model(label)


def _apply(request, panel, step: dict[str, Any], permissions, db: str) -> list[Any]:
    kind = step.get("kind")
    if kind == "fields":
        return _undo_fields(request, step, permissions, db)
    if kind == "untrash":
        return _undo_trash(request, step, permissions, db)
    if kind == "reinsert":
        return _undo_delete(step, db)
    if kind == "custom":
        return _undo_custom(request, panel, step)
    raise UndoConflict(_("This undo cannot be applied. Nothing was changed."))


def _in_scope(request, model, pks, permissions, db) -> set:
    """The subset of ``pks`` this user may still see — and therefore may still touch."""
    scoped = permissions.filter_queryset(
        request, model._base_manager.using(db).filter(pk__in=pks), "view"
    )
    return {_plain(pk) for pk in scoped.values_list("pk", flat=True)}


def _conflict(count: int) -> UndoConflict:
    return UndoConflict(
        ngettext(
            "%(count)s record has been changed by someone else since, so nothing was "
            "undone. Their change has been kept.",
            "%(count)s records have been changed by someone else since, so nothing was "
            "undone. Their changes have been kept.",
            count,
        )
        % {"count": count}
    )


def _undo_fields(request, step, permissions, db) -> list[Any]:
    model = _model(step["model"])
    rows = step["rows"]
    pks = [row["pk"] for row in rows]
    visible = _in_scope(request, model, pks, permissions, db)
    by_attname = {f.attname: f for f in model._meta.concrete_fields}
    locked = {
        _plain(obj.pk): obj
        for obj in model._base_manager.using(db).select_for_update().filter(pk__in=pks)
    }
    stale = 0
    for row in rows:
        obj = locked.get(row["pk"])
        if obj is None or row["pk"] not in visible:
            stale += 1
            continue
        for attname, (_before, after) in row["values"].items():
            model_field = by_attname[attname]
            if getattr(obj, attname) != _decode(model_field, after):
                stale += 1
                break
    if stale:
        raise _conflict(stale)

    column = version_column(model)
    if step.get("strategy") == "sql":
        groups: dict[tuple, list[Any]] = defaultdict(list)
        for row in rows:
            key = tuple(sorted((name, _dumps(pair[0])) for name, pair in row["values"].items()))
            groups[key].append(row["pk"])
        for key, group in groups.items():
            values = {name: _decode(by_attname[name], json.loads(raw)) for name, raw in key}
            if column is not None and column.field.attname not in values:
                values[column.name] = F(column.name) + 1 if column.is_counter else timezone.now()
            updated = model._base_manager.using(db).filter(pk__in=group).update(**values)
            if updated != len(group):  # pragma: no cover - the rows are locked
                raise _conflict(len(group) - updated)
    else:
        for row in rows:
            obj = locked[row["pk"]]
            names = []
            for attname, (before, _after) in row["values"].items():
                model_field = by_attname[attname]
                setattr(obj, attname, _decode(model_field, before))
                names.append(model_field.name)
            if column is not None and column.name not in names:
                if column.is_counter:
                    setattr(obj, column.name, getattr(obj, column.name) + 1)
                names.append(column.name)
            # ``save()`` with the fields named, not ``update()``: the edit went through
            # save() and its signals, so the reversal does too.
            obj.save(using=db, update_fields=names)
    return pks


def _undo_trash(request, step, permissions, db) -> list[Any]:
    model = _model(step["model"])
    model_field = model._meta.get_field(step["field"])
    rows = step["rows"]
    pks = [pk for pk, _value in rows]
    visible = _in_scope(request, model, pks, permissions, db)
    locked = (
        model._base_manager.using(db)
        .select_for_update()
        .filter(pk__in=pks)
        .values_list("pk", model_field.attname)
    )
    current = {_plain(pk): value for pk, value in locked}
    stale = sum(
        1
        for pk, written in rows
        if pk not in visible or pk not in current or current[pk] != _decode(model_field, written)
    )
    if stale:
        raise _conflict(stale)
    model._base_manager.using(db).filter(pk__in=pks).update(**{step["field"]: step["live"]})
    return pks


def _undo_delete(step, db) -> list[Any]:
    objects = list(serializers.deserialize("python", step["objects"], using=db))
    restored = []
    for wrapped in objects:
        # ``save_base`` with ``force_insert``: a row that has reappeared under the same
        # primary key is an IntegrityError, never an UPDATE that overwrites it.
        models.Model.save_base(wrapped.object, using=db, raw=True, force_insert=True)
        restored.append(wrapped.object.pk)
    for update in step.get("updates", []):
        model = _model(update["model"])
        model_field = next(f for f in model._meta.concrete_fields if f.attname == update["field"])
        written = _decode(model_field, update["value"])
        groups: dict[str, list[Any]] = defaultdict(list)
        for pk, old in update["rows"]:
            groups[_dumps(old)].append(pk)
        for raw, pks in groups.items():
            old = _decode(model_field, json.loads(raw))
            changed = (
                model._base_manager.using(db)
                .filter(pk__in=pks, **{model_field.attname: written})
                .update(**{model_field.attname: old})
            )
            if changed != len(pks):
                raise _conflict(len(pks) - changed)
    # Deferred constraints are checked at COMMIT, which may be somebody else's commit
    # under ATOMIC_REQUESTS — and in tests, never. Check now, inside our transaction.
    connections[db].check_constraints(table_names=step.get("tables") or None)
    return restored


def _undo_custom(request, panel, step) -> list[Any]:
    from vantage.actions import resolve_actions

    available = {act.name: act for act in resolve_actions(panel, request)}
    act = available.get(step.get("action", ""))
    hook = getattr(act, "undo", None) if act is not None else None
    if hook is None:
        raise UndoConflict(_("That action can no longer be undone. Nothing was changed."))
    if isinstance(hook, str):
        getattr(panel, hook)(request, step.get("state"))
    else:
        hook(panel, request, step.get("state"))
    return []
