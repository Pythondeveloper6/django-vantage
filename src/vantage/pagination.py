"""Paginators that do not get slower as the table gets bigger.

Django's :class:`~django.core.paginator.Paginator` does two unbounded things per page,
and both of them are invisible until the table has a few million rows in it.

It runs ``COUNT(*)`` to find ``num_pages``. On Postgres that is a full relation scan
under MVCC — 50-200 ms at 1M rows, 0.5-2 s at 10M — and it is paid on every render,
including the ones where the user is looking at 25 rows and has no interest in the
total. :class:`CappedPaginator` replaces it with a bounded read: below the cap the
count is exact, at the cap the page says ``10,000+``, and the cost is the same
constant at any table size.

It pages with ``LIMIT ... OFFSET ...``. ``OFFSET 500000`` is not a seek; the executor
produces half a million rows and throws them away, so page 10,000 costs 10,000 times
page 1 and the last page is the slowest thing on the site.
:class:`KeysetPaginator` replaces it with ``WHERE (sort_key, pk) < (%s, %s) LIMIT n``,
which is an index range scan and costs the same on every page.

They are not interchangeable, and the trade is the whole reason both exist — see
:class:`KeysetPaginator`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from functools import cached_property
from math import ceil
from typing import Any

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.paginator import Page, Paginator
from django.db.models import F, Field, Func, Q, QuerySet, Value
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import OrderBy
from django.db.models.lookups import GreaterThan, LessThan

from vantage.scale import RowCount, count_rows, setting, table_row_estimate

__all__ = [
    "CappedPage",
    "CappedPaginator",
    "InvalidCursor",
    "KeysetPage",
    "KeysetPaginator",
    "KeysetUnsupported",
    "OrderTerm",
    "decode_cursor",
    "encode_cursor",
    "is_totally_ordered",
    "keyset_predicate",
    "keyset_warranted",
    "ordering_terms",
    "with_total_ordering",
]

logger = logging.getLogger("vantage.pagination")


# ── capped counting ─────────────────────────────────────────────────────────


class CappedPage(Page):
    """A page that knows whether the total behind it is a count or a floor."""

    @property
    def row_count(self) -> RowCount:
        return self.paginator.row_count

    @property
    def has_more(self) -> bool:
        """Whether rows exist beyond the pager's window."""
        return self.paginator.has_more and self.number >= self.paginator.num_pages


class CappedPaginator(Paginator):
    """A paginator whose count, and whose deepest page, are both bounded.

    Reads ``per_page * K + 1`` primary keys instead of counting. Getting ``K`` pages'
    worth back plus one row is proof that there are more than ``per_page * K`` rows,
    which is all "10,000+" claims; getting fewer is an exact count. GitLab does the
    same thing for the same reason and renders ``1000+``.

    Two consequences worth stating plainly, because the second one is a real loss:

    * ``count`` is the *pager's* number and is clamped to the cap, so ``num_pages``
      never exceeds ``K``. ``row_count`` is the *display* number and is not clamped —
      when an estimate is available it may be ``~2,400,000``. They are deliberately
      different: a pager that trusted an estimate would offer a "last page" link whose
      OFFSET the planner made up.
    * At the cap, rows past ``per_page * K`` are not reachable by page number. That is
      the same bound GitLab ships, and it is defensible for the same reason: past that
      depth OFFSET is the wrong access path anyway, and the honest answers are to
      refine the filter or to page by cursor. :class:`KeysetPaginator` is the latter.

    ``request`` and ``permissions`` are threaded through only so the estimate path can
    be gated — see :func:`vantage.scale.is_unrestricted`. Omit them and no estimate is
    ever attempted, which is the safe default for a caller that has no request.
    """

    def __init__(
        self,
        object_list,
        per_page,
        orphans=0,
        allow_empty_first_page=True,
        *,
        cap: int | None = None,
        request=None,
        permissions=None,
        **kwargs,
    ):
        super().__init__(object_list, per_page, orphans, allow_empty_first_page, **kwargs)
        raw = int(setting("LIST_COUNT_CAP") if cap is None else cap)
        # Rounded up to a whole number of pages, so the final capped page is full and
        # the boundary lands where a user would expect it to.
        self.cap = self.per_page * ceil(raw / self.per_page) if raw > 0 else 0
        self.request = request
        self.permissions = permissions

    @cached_property
    def row_count(self) -> RowCount:
        """The number to render, with its own account of how reliable it is."""
        return count_rows(
            self.object_list,
            cap=self.cap,
            request=self.request,
            permissions=self.permissions,
            # No request means no way to establish that the user is unrestricted, and
            # an ungated estimate is the leak this whole path is careful about.
            estimates=None if self.request is not None else False,
        )

    @cached_property
    def count(self) -> int:
        """What the pager does arithmetic on: exact, or the cap. Never an estimate."""
        counted = self.row_count
        return counted.value if counted.exact else min(counted.value, self.cap)

    @property
    def has_more(self) -> bool:
        """Whether the true total is above the pager's window."""
        return self.row_count.is_approximate

    def _get_page(self, *args, **kwargs) -> CappedPage:
        return CappedPage(*args, **kwargs)


# ── keyset pagination ───────────────────────────────────────────────────────


class KeysetUnsupported(Exception):
    """This ordering cannot be paged by cursor, and pretending otherwise loses rows."""


class InvalidCursor(Exception):
    """The cursor did not decode against this ordering. Treat it as no cursor."""


@dataclass(frozen=True)
class OrderTerm:
    """One component of a keyset ordering, resolved to a concrete local field."""

    name: str
    descending: bool
    field: Field


class RowValue(Func):
    """A SQL row constructor, ``(a, b)``.

    Written out rather than imported: Django has its own ``Tuple`` for composite
    primary keys, but it lives in ``django.db.models.fields.tuple_lookups`` and is
    private, and this is four lines. ``output_field = Field()`` is the same trick
    Django's own version uses — a row of mixed types has no single output field, and
    nothing here ever converts the result, since it is consumed by a comparison.
    """

    template = "(%(expressions)s)"
    arg_joiner = ", "
    output_field = Field()


def ordering_terms(queryset: QuerySet) -> list[OrderTerm]:
    """The ordering, resolved to local concrete non-null fields, or a refusal.

    Deliberately narrow. A keyset window is a claim about a total order, and every
    shape rejected here is one where the claim is false in a way that *silently skips
    rows* rather than raising:

    * ``?`` — random ordering has no successor relation at all.
    * A related path (``author__name``) — the comparison would need the join to be
      1:1, and nothing enforces that.
    * An expression (``Lower("title")``, ``F("x") + 1``) — the cursor would have to
      store the computed value and re-derive it identically. Possible, and not worth
      the failure mode.
    * ``nulls_first``/``nulls_last``, or any nullable field — SQL comparison against
      NULL is three-valued, so ``WHERE (a, pk) > (NULL, 5)`` matches nothing and the
      user's next page is silently empty.
    """
    query = queryset.query
    parts: tuple = tuple(query.order_by)
    if not parts and query.default_ordering:
        parts = tuple(query.get_meta().ordering or ())
    if not parts:
        raise KeysetUnsupported("Keyset pagination needs an explicit ordering.")

    opts = queryset.model._meta
    terms: list[OrderTerm] = []
    for part in parts:
        name, descending = _term_name(part)
        if name is None:
            raise KeysetUnsupported(f"{part!r} is not a plain field ordering.")
        if name == "?":
            raise KeysetUnsupported("Random ordering has no cursor.")
        if LOOKUP_SEP in name:
            raise KeysetUnsupported(f"{name!r} orders across a relation.")

        try:
            field = opts.pk if name == "pk" else opts.get_field(name)
        except FieldDoesNotExist as exc:
            raise KeysetUnsupported(f"{name!r} is not a field of {opts.label}.") from exc
        if field.is_relation and name == field.name:
            # Django expands this to the related model's Meta ordering.
            raise KeysetUnsupported(f"{name!r} orders by a related model's ordering.")
        if field.null:
            raise KeysetUnsupported(f"{name!r} is nullable; a NULL key drops rows.")
        terms.append(OrderTerm(name=name, descending=descending, field=field))

    return terms


def _term_name(part: Any) -> tuple[str | None, bool]:
    if isinstance(part, str):
        return (part[1:], True) if part.startswith("-") else (part, False)
    if isinstance(part, F):
        return part.name, False
    if isinstance(part, OrderBy) and isinstance(part.expression, F):
        if part.nulls_first or part.nulls_last:
            return None, False
        return part.expression.name, bool(part.descending)
    return None, False


def is_totally_ordered(queryset: QuerySet) -> bool:
    """Whether the ordering distinguishes every row from every other row.

    Django 6.1 answers this itself with ``QuerySet.totally_ordered``, which knows about
    composite primary keys, ``unique_together`` and total unique constraints. Use it
    where it exists; on 5.2 fall back to the conservative subset — a term that is the
    primary key, or a unique non-null field. The fallback's errors are all false
    negatives, and a false negative is a refusal, which is the harmless direction.
    """
    answer = getattr(queryset, "totally_ordered", None)
    if answer is not None:
        return bool(answer)

    try:
        terms = ordering_terms(queryset)
    except KeysetUnsupported:
        return False
    opts = queryset.model._meta
    names = {term.name for term in terms} | {term.field.attname for term in terms}
    if "pk" in names or opts.pk.attname in names or opts.pk.name in names:
        return True
    if any(term.field.unique and not term.field.null for term in terms):
        return True
    return any(
        names.issuperset(constraint.fields) for constraint in opts.total_unique_constraints
    )


def with_total_ordering(queryset: QuerySet) -> QuerySet:
    """Append the primary key to the ordering unless it is already total.

    Separate from :class:`KeysetPaginator` on purpose. The paginator *refuses* a
    non-total ordering rather than fixing it, because silently appending a tiebreaker
    changes the order of rows the user is looking at, and doing that without being
    asked is how "the row I was reading moved" bugs happen. This is the explicit
    version of the same fix, for a caller that has decided it wants it.

    The tiebreaker takes the direction of the last ordering term, so it reads as a
    continuation of the sort rather than as a reversal buried at the end.
    """
    if is_totally_ordered(queryset):
        return queryset
    try:
        terms = ordering_terms(queryset)
    except KeysetUnsupported:
        terms = []
    descending = terms[-1].descending if terms else False
    existing = tuple(queryset.query.order_by) or tuple(
        queryset.query.get_meta().ordering or ()
    )
    return queryset.order_by(*existing, "-pk" if descending else "pk")


# ── cursors ─────────────────────────────────────────────────────────────────


def encode_cursor(values: list[Any], *, backwards: bool = False) -> str:
    """Pack the ordering values of one row into an opaque URL-safe token.

    Not signed, and it does not need to be: every value is fed back through the model
    field's own ``to_python`` before it reaches a query, so a tampered cursor produces
    either a validation error or a different — but still permission-filtered — window
    of the same queryset. A cursor confers no access. The base64 is there to keep
    timestamps and commas out of the query string, not to hide anything.
    """
    payload = json.dumps(
        {
            "v": [None if value is None else str(value) for value in values],
            "d": "prev" if backwards else "next",
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(token: str, terms: list[OrderTerm]) -> tuple[list[Any], bool]:
    """Unpack a token into typed values, or refuse it.

    Coerced through ``field.to_python`` rather than used raw, so ``"2026-01-01"``
    becomes a date and ``"' OR 1=1"`` becomes a ``ValidationError`` before it is ever
    near a query.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        raw = payload["v"]
        backwards = payload.get("d") == "prev"
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise InvalidCursor("Malformed cursor.") from exc

    if not isinstance(raw, list) or len(raw) != len(terms):
        raise InvalidCursor("Cursor does not match the current ordering.")

    values = []
    for term, item in zip(terms, raw, strict=True):
        if item is None:
            raise InvalidCursor("Cursor contains a null key.")
        try:
            values.append(term.field.to_python(item))
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidCursor(f"Cursor value for {term.name!r} is not usable.") from exc
    return values, backwards


def keyset_predicate(terms: list[OrderTerm], values: list[Any], *, backwards: bool = False):
    """The ``WHERE`` clause selecting everything after (or before) the cursor row.

    Two forms, and the choice is about whether Postgres can use the index:

    * All terms sorting the same way — ``WHERE (a, b, pk) > (%s, %s, %s)``. A row
      constructor comparison is one index range scan; it is the form in the spec and
      the reason keyset paging is O(page size) rather than O(offset).
    * Mixed directions — the lexicographic expansion,
      ``a > a0 OR (a = a0 AND b < b0) OR ...``. Row constructors compare all columns
      in one direction, so a mixed ordering cannot use them at all. The expansion is
      correct everywhere and is planned worse; it is the price of ``?sort=-created,name``.
    """
    if not terms:
        raise KeysetUnsupported("No ordering to build a cursor window from.")

    directions = [term.descending != backwards for term in terms]
    if len(set(directions)) == 1:
        lhs = RowValue(*(F(term.name) for term in terms))
        rhs = RowValue(
            *(
                Value(value, output_field=term.field)
                for term, value in zip(terms, values, strict=True)
            )
        )
        comparison = LessThan if directions[0] else GreaterThan
        return comparison(lhs, rhs)

    combined = Q()
    for index, term in enumerate(terms):
        clause = Q()
        for earlier, value in zip(terms[:index], values[:index], strict=True):
            clause &= Q(**{earlier.name: value})
        operator = "lt" if directions[index] else "gt"
        clause &= Q(**{f"{term.name}__{operator}": values[index]})
        combined |= clause
    return combined


# ── the paginator ───────────────────────────────────────────────────────────


@dataclass
class KeysetPage:
    """One cursor page. No number, because there is no page number to have."""

    object_list: list
    has_next: bool
    has_previous: bool
    next_cursor: str | None
    previous_cursor: str | None

    def __iter__(self):
        return iter(self.object_list)

    def __len__(self) -> int:
        return len(self.object_list)

    def __bool__(self) -> bool:
        return bool(self.object_list)


class KeysetPaginator:
    """Cursor pagination: ``WHERE (sort_key, pk) < (%s, %s) ORDER BY ... LIMIT n``.

    **What this gives up, explicitly: you cannot jump to page 8,472.** There is no page
    number, no total page count, and no way to land in the middle of the set without
    walking there. That is not an oversight; a cursor page is defined relative to the
    row before it, and a page number is defined relative to the start of the set, and
    the second definition is the one that requires counting past 8,471 pages of rows.

    Above the threshold that trade is the right way round, for three reasons:

    * Nobody navigates to page 8,472. They sort, filter or search. Deep page links are
      followed by crawlers and by the "last page" button, both of which are load without
      a user behind them.
    * ``OFFSET 423600`` makes Postgres produce and discard 423,600 rows to return 50.
      The keyset form is an index seek and returns the same 50 in the time page 1 takes.
    * Offset pages *shift* under concurrent writes: insert a row while someone reads
      page 3 and one record moves to page 4 unseen, while another is shown twice. A
      cursor is anchored to a row, so it cannot skip or duplicate.

    Below the threshold none of that is worth losing page numbers over, which is why
    this is not the default paginator — see :func:`keyset_warranted`.

    Construction validates the ordering and raises :class:`KeysetUnsupported` if it is
    not total. It refuses rather than quietly appending the primary key, because an
    ordering that is not total has rows the database may return in either order, and a
    cursor window over those rows drops some of them and repeats others — a silent,
    intermittent, unreproducible loss of data from a list. :func:`with_total_ordering`
    is how a caller opts into the fix.
    """

    def __init__(self, object_list: QuerySet, per_page: int):
        if not isinstance(object_list, QuerySet):
            raise KeysetUnsupported("Keyset pagination needs a QuerySet.")
        self.object_list = object_list
        self.per_page = int(per_page)
        self.terms = ordering_terms(object_list)
        if not is_totally_ordered(object_list):
            raise KeysetUnsupported(
                "Keyset pagination needs a totally ordered queryset; "
                f"{[term.name for term in self.terms]} does not identify a row uniquely. "
                "Use vantage.pagination.with_total_ordering() to append the primary key."
            )

    def cursor_for(self, instance) -> str:
        """The token that starts the page *after* this row."""
        return encode_cursor([getattr(instance, term.name) for term in self.terms])

    def page(self, token: str | None = None) -> KeysetPage:
        """The page at ``token``, or the first page when there is none.

        Fetches ``per_page + 1`` rows: the extra row is how "is there a next page?" is
        answered without a second query and without a count.
        """
        queryset = self.object_list
        backwards = False
        if token:
            values, backwards = decode_cursor(token, self.terms)
            queryset = queryset.filter(keyset_predicate(self.terms, values, backwards=backwards))
            if backwards:
                queryset = queryset.reverse()

        rows = list(queryset[: self.per_page + 1])
        has_extra = len(rows) > self.per_page
        rows = rows[: self.per_page]
        if backwards:
            # Read away from the cursor, so they come back in the mirror of the
            # requested order and have to be turned round again.
            rows.reverse()

        # Walking backwards proves a next page exists, and vice versa: the cursor row
        # itself is on the other side of the window.
        has_next = has_extra if not backwards else bool(token)
        has_previous = bool(token) if not backwards else has_extra
        return KeysetPage(
            object_list=rows,
            has_next=has_next,
            has_previous=has_previous,
            next_cursor=self.cursor_for(rows[-1]) if rows and has_next else None,
            previous_cursor=(
                encode_cursor(
                    [getattr(rows[0], term.name) for term in self.terms], backwards=True
                )
                if rows and has_previous
                else None
            ),
        )


def keyset_warranted(
    queryset: QuerySet,
    *,
    page_number: int = 1,
    per_page: int = 50,
    threshold: int | None = None,
) -> bool:
    """Whether this request is deep enough into a big enough table to want a cursor.

    Two signals, cheapest first. The offset depth of the page being asked for is exact,
    free and vendor-neutral — it is the number of rows the database would produce and
    discard. The whole-table ``reltuples`` estimate is the secondary one, catching the
    table that is large enough that the *next* few pages will be expensive too; it is
    O(1) and cached on Postgres and simply unavailable elsewhere, in which case only
    the depth signal applies.

    A gate, not a display: neither number is shown to anyone, so this needs no
    permission check.
    """
    limit = int(setting("LIST_KEYSET_THRESHOLD") if threshold is None else threshold)
    if limit <= 0:
        return False
    if max(page_number - 1, 0) * per_page >= limit:
        return True
    if not isinstance(queryset, QuerySet):
        return False
    estimate = table_row_estimate(queryset.model, using=queryset.db)
    return estimate is not None and estimate > limit
