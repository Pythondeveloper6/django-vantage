"""Optimistic locking: catching the write that would silently overwrite somebody else.

The grid is a view onto a production database that other people are also editing. The
default behaviour of every ORM save — ``UPDATE … WHERE pk = 3``, every column, last
writer wins — is therefore wrong here in a way nobody notices: the losing edit leaves
no trace at all, and the person who made it goes on believing it landed.

This module is the smallest thing that fixes that, and it is deliberately kept apart
from :mod:`vantage.inline` so the change form can adopt it unchanged. Three pieces:

**A version column, detected rather than declared.** An explicit integer ``version``
wins, then an ``auto_now`` timestamp — the column Django is *already* maintaining on
almost every model anyone writes. A model with neither cannot be locked, and says so
rather than pretending: :func:`token_for` returns ``None`` and the caller writes
unguarded, which is exactly what it does today.

**A compare-and-set, not a read-then-check.** ``SELECT`` the version, compare it in
Python and then ``save()`` is a race with a window the width of your own view. The
check has to be the write:

    UPDATE app_order SET updated_at = %s WHERE id = %s AND updated_at = %s

Zero rows affected means somebody moved it under you. Because the statement both tests
and advances the token, the loser of a race cannot win by retrying blindly — it has to
come back with the new token, which means it has to have seen the new state.

**The conflict is reported as a field-level diff.** "This record changed" is not an
answer anybody can act on; "they set status to Refunded, you set it to Paid" is. That
is why :func:`field_diff` lives here rather than in the view: a conflict is only useful
to the extent it can be resolved, and resolving it needs both sides, field by field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models, router
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

__all__ = [
    "StaleWrite",
    "VersionColumn",
    "claim",
    "field_diff",
    "token_for",
    "version_column",
]

#: Integer column names that mean "revision counter". Checked before timestamps
#: because declaring one of these is an explicit statement of intent, while an
#: ``auto_now`` column is something Django added for other reasons and we are
#: borrowing.
COUNTER_NAMES: tuple[str, ...] = ("version", "lock_version", "revision", "row_version")


class StaleWrite(Exception):
    """The row moved between being read and being written.

    Carries the object as it now stands in the database, because the only useful
    thing to do with this exception is show the user what they are up against.
    """

    def __init__(self, obj, token: str | None) -> None:
        super().__init__(f"{obj._meta.label} {obj.pk} was changed by someone else.")
        self.obj = obj
        self.token = token


@dataclass(frozen=True)
class VersionColumn:
    """The column a model's optimistic lock rides on."""

    field: models.Field
    #: True for an integer counter, false for a timestamp. Decides how the token is
    #: parsed and how the compare-and-set advances it.
    is_counter: bool

    @property
    def name(self) -> str:
        return self.field.name


_cache: dict[type[models.Model], VersionColumn | None] = {}


def version_column(model: type[models.Model]) -> VersionColumn | None:
    """The version column for ``model``, or ``None`` when it has none.

    Memoised per model class: this walks the field list and is asked once per row on
    a fifty-row page. Model classes are created once at import, so the cache is
    bounded by the size of the project.
    """
    if model in _cache:
        return _cache[model]
    _cache[model] = resolved = _detect(model)
    return resolved


def _detect(model: type[models.Model]) -> VersionColumn | None:
    fields = {f.name: f for f in model._meta.concrete_fields}

    for name in COUNTER_NAMES:
        field = fields.get(name)
        if isinstance(field, models.IntegerField) and not field.null:
            # Nullable is excluded on purpose: NULL never equals NULL in SQL, so the
            # compare-and-set below would match zero rows forever and every write
            # would report a conflict that is not one.
            return VersionColumn(field=field, is_counter=True)

    for field in model._meta.concrete_fields:
        if isinstance(field, models.DateTimeField) and getattr(field, "auto_now", False):
            return VersionColumn(field=field, is_counter=False)
    return None


def token_for(obj) -> str | None:
    """The opaque version token to hand the browser, or ``None`` when unlockable.

    A string rather than a typed value because it makes a round trip through JSON and
    an HTML attribute, and because the client must never do anything with it except
    give it back. Timestamps go out in ISO-8601 with whatever precision the database
    actually stored — generated from a value that was *read back*, never from one
    computed in Python, so a backend that truncates to whole seconds round-trips
    instead of conflicting with itself on every write.
    """
    column = version_column(type(obj))
    if column is None:
        return None
    value = getattr(obj, column.name, None)
    if value is None:
        return None
    return str(value) if column.is_counter else value.isoformat()


def _parse(column: VersionColumn, token: str) -> Any:
    if column.is_counter:
        return int(token)
    parsed = parse_datetime(token)
    if parsed is None:
        raise ValueError(f"{token!r} is not a datetime")
    return parsed


def claim(obj, token: str | None) -> str | None:
    """Take the row, or raise :class:`StaleWrite`. Returns the token now in force.

    Call this inside the transaction that performs the write and before the write
    itself. It is one statement and it is the entire lock: the ``WHERE`` clause is the
    check, so there is no window between testing the version and advancing it.

    ``None`` comes back when the model has no version column, which is not an error —
    it is the honest answer that this write is unguarded, and it keeps every caller on
    one code path instead of two.
    """
    column = version_column(type(obj))
    if column is None:
        return None

    current = token_for(obj)
    if current is None:
        # A nullable version column that has never been set. There is nothing to
        # compare against, so guarding here would refuse a write for no reason.
        return None
    if not token:
        # The row is lockable and the caller did not say which version it read. That
        # is precisely the blind overwrite this module exists to stop, so it is a
        # conflict rather than a permission to proceed.
        raise StaleWrite(obj, current)

    try:
        expected = _parse(column, token)
    except (TypeError, ValueError):
        # An unparseable token is a token that cannot match. Treated as stale rather
        # than as a bad request: the user's next step is the same either way, and
        # "your copy is out of date" is true.
        raise StaleWrite(obj, current) from None

    model = type(obj)
    using = router.db_for_write(model, instance=obj)
    if column.is_counter:
        advanced: Any = F(column.name) + 1
    else:
        advanced = timezone.now()

    rows = (
        model._base_manager.using(using)
        .filter(pk=obj.pk, **{column.name: expected})
        .update(**{column.name: advanced})
    )
    if not rows:
        # Re-read rather than trusting the instance in hand: the point of the 409 is
        # to show what is actually there now.
        fresh = model._base_manager.using(using).filter(pk=obj.pk).first()
        raise StaleWrite(fresh if fresh is not None else obj, token_for(fresh) if fresh else None)

    if column.is_counter:
        # The instance still holds the pre-claim number, and the save that follows
        # writes every column — without this it would put the old value straight
        # back and undo the increment we just made.
        setattr(obj, column.name, expected + 1)
        return str(expected + 1)
    # An ``auto_now`` column is rewritten by ``save()`` a moment later, so the
    # caller reads the final token off the instance once the write is done.
    setattr(obj, column.name, advanced)
    return advanced.isoformat()


# ── reporting the conflict ──────────────────────────────────────────────────


def field_diff(obj, submitted: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Mine versus theirs, one entry per submitted field.

    Only the fields the writer actually touched: a diff of the whole row would bury
    the one line that matters under thirty that did not move. Each entry carries both
    a raw value (so the client can re-submit it) and a display value (so a human can
    read "Refunded" rather than ``"refunded"``), plus a ``conflict`` flag — because a
    field both people set to the same thing is not a disagreement, and marking it as
    one turns a one-field decision into a five-field interrogation.
    """
    entries: dict[str, dict[str, Any]] = {}
    for name, raw in submitted.items():
        try:
            field = obj._meta.get_field(name)
        except FieldDoesNotExist:  # pragma: no cover - callers filter first
            continue

        theirs = getattr(obj, name, None)
        try:
            mine = field.to_python(raw)
        except ValidationError:
            # Their edit does not even parse. Still worth showing: it is what they
            # typed, and the resolution UI is where they get to fix it.
            mine = raw

        entries[name] = {
            "mine": _jsonify(mine),
            "mine_display": _display(field, mine),
            "theirs": _jsonify(theirs),
            "theirs_display": _display(field, theirs),
            "conflict": mine != theirs,
        }
    return entries


def _display(field: models.Field, value: Any) -> str:
    """What a person should read for this value, not what the database stores."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    choices = getattr(field, "flatchoices", None)
    if choices:
        return str(dict(choices).get(value, value))
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)


def _jsonify(value: Any) -> Any:
    """A JSON-safe stand-in that the client can hand straight back to the server."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, models.Model):
        return value.pk
    return str(value)
