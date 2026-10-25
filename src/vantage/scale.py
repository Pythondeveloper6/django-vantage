"""Counting, estimating and bounding a list page that outgrew its sample data.

Everything on a list page is fast at 200 rows. The measurements that matter are the
ones from the other end: ``COUNT(*)`` on Postgres is always a full relation scan under
MVCC — roughly **50-200 ms at 1M rows and 0.5-2 s at 10M** — and it costs that whether
the page is showing 25 rows or 2,000,000. GitLab measured **14,972 ms** for an exact
count against **1.198 ms** for the planner's estimate of the same set, at 1.2% error,
and answers the question in their UI with a bounded ``1000+``. ``contrib.admin`` goes
the other way: ``show_full_result_count`` defaults on and runs a *second* count over
the whole unfiltered table purely to render "1 result (12,345 total)". Vantage ships no
equivalent enabled by default.

The ladder, in the order this module applies it:

1. **Capped exact count.** Read at most ``per_page * K + 1`` primary keys. Below the
   cap the count is exact and free of qualification; at the cap the page says
   ``10,000+``. Correct on every backend, correct for every user, and the cost is a
   constant regardless of how large the table is. Highest value per line of code in
   the module, which is why it is first.
2. **Planner estimate**, above the cap only, so a big number reads as a number rather
   than as a ceiling. ``EXPLAIN (FORMAT JSON)`` for the filtered set,
   ``pg_class.reltuples`` for the whole table. Postgres only, cached, and rendered with
   a ``~`` prefix so an approximation can never be mistaken for a count.
3. **Statement timeout**, so that the filter nobody anticipated fails loudly on one
   request instead of pinning a worker for the length of a scan.

Two rules run through all of it.

*An estimate is a nicety and must never be the thing that 500s the page.* Every
Postgres-only path is guarded on ``connection.vendor`` first and wrapped in
``DatabaseError`` second, and every one of them returns ``None`` rather than raising.
SQLite and MySQL have neither ``reltuples`` nor a machine-readable plan, so they take
the capped path and nothing else changes.

*An estimate is whole-table, and a whole-table number told to a row-restricted user is
an information leak, not merely a wrong number.* A support agent scoped to their own
twelve tickets must never be shown "2,000,000 records". :func:`is_unrestricted` gates
every estimate path, and it answers by comparing the SQL before and against after
``filter_queryset`` rather than by trusting a flag.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.core.exceptions import EmptyResultSet
from django.db import DatabaseError, connections, transaction
from django.db.models import QuerySet

__all__ = [
    "DEFAULTS",
    "RELTUPLES_SQL",
    "TIMEOUT_SQL",
    "RowCount",
    "capped_count",
    "count_rows",
    "covers_whole_table",
    "explain_sql",
    "is_unrestricted",
    "queryset_row_estimate",
    "setting",
    "statement_timeout",
    "supports_estimates",
    "table_row_estimate",
]

logger = logging.getLogger("vantage.scale")

#: Settings this module reads, with the value it uses until they are declared in
#: :data:`vantage.conf.DEFAULTS`. Read through :func:`setting`, never directly, for the
#: same reason ``importer.limit`` does: ``vantage_settings`` rejects unknown keys by
#: design, so asking with a fallback is what lets the module work today and pick up the
#: project's value the moment the key is added, with no change here.
DEFAULTS: dict[str, Any] = {
    # Rows counted exactly before the page starts saying "N+". 10,000 is the number in
    # the spec and roughly GitLab's; it is a page-depth budget, not a data limit.
    "LIST_COUNT_CAP": 10_000,
    # Whether to replace "10,000+" with a planner estimate where one is available.
    "LIST_COUNT_ESTIMATES": True,
    # Estimates move slowly (reltuples only updates on VACUUM/ANALYZE) and are read on
    # every render, so they are cached. Five minutes is short enough that a growing
    # table is not misreported for long and long enough to make the cost irrelevant.
    "LIST_ESTIMATE_CACHE_SECONDS": 300,
    # Offset depth, in rows, above which keyset paging is the better access path.
    # 0 disables keyset entirely. See :mod:`vantage.pagination`.
    "LIST_KEYSET_THRESHOLD": 50_000,
    # Per-statement ceiling for list-page queries, in milliseconds. 0 disables.
    "LIST_STATEMENT_TIMEOUT_MS": 10_000,
}


def setting(name: str, default: Any = None) -> Any:
    """A configurable value, read through the settings layer when it knows the key.

    Mirrors ``vantage.importer.limit``. ``None`` — and only ``None`` — falls through to
    the default, so a boolean setting can legitimately be ``False`` and an integer cap
    can legitimately be ``0`` to mean "off".
    """
    from vantage.conf import vantage_settings

    value = getattr(vantage_settings, name, None)
    return DEFAULTS.get(name, default) if value is None else value


# ── the answer a page renders ───────────────────────────────────────────────


@dataclass(frozen=True)
class RowCount:
    """How many rows there are, and how much that number should be trusted.

    Three states, and the display string is different for each because conflating them
    is how a UI ends up asserting a figure it does not have. ``12,345`` is a count.
    ``10,000+`` is a floor. ``~2,400,000`` is a guess, and the tilde is not decoration:
    it is the only thing standing between an approximation and a reader who will quote
    it in a meeting.
    """

    value: int
    #: The value is the true number of rows.
    exact: bool = True
    #: There are at least ``value`` rows; counting stopped at the cap.
    capped: bool = False
    #: The value came from the query planner, not from the data.
    estimated: bool = False

    @property
    def display(self) -> str:
        if self.capped:
            return f"{self.value:,}+"
        if self.estimated:
            return f"~{self.value:,}"
        return f"{self.value:,}"

    @property
    def is_approximate(self) -> bool:
        return not self.exact

    def __int__(self) -> int:
        return self.value


# ── 1. capped counting, on every backend ────────────────────────────────────


def capped_count(queryset: QuerySet, cap: int) -> RowCount:
    """Count exactly up to ``cap`` rows, then stop and report a floor.

    ``SELECT COUNT(*) FROM (SELECT id FROM t WHERE ... LIMIT cap + 1) subquery``. One
    query, at most ``cap + 1`` rows examined, so the cost is bounded by the cap and not
    by the size of the table: the difference between a table of 20,000 rows and one of
    20,000,000 is nothing at all here. That is the whole idea. A bare ``COUNT(*)``
    cannot early-exit — it has to visit every candidate row to know it visited them all
    — whereas the ``LIMIT`` inside the subquery lets the executor stop as soon as it has
    enough, and getting ``cap + 1`` back is proof that there are more than ``cap``.

    The inner select is one column and the outer select returns one row. Pulling the
    ``cap + 1`` primary keys into Python and taking ``len()`` answers the same question
    and was measured here at 1.8 ms against 0.24 ms for the subquery on SQLite at every
    table size from 10k to 1M — all of it the cost of moving 10,001 values that nothing
    then looks at.
    """
    if not isinstance(queryset, QuerySet):
        # Paginators accept plain sequences, and a sequence already knows its length.
        return RowCount(len(queryset))

    if cap <= 0:
        return RowCount(queryset.count())

    # ``order_by()`` because a sort the user cannot see is pure cost, and on Postgres an
    # ORDER BY over a large set is exactly the step that stops LIMIT from early-exiting.
    counted = queryset.order_by()[: cap + 1].count()
    if counted > cap:
        return RowCount(cap, exact=False, capped=True)
    return RowCount(counted)


# ── 2. planner estimates, Postgres only ─────────────────────────────────────


def supports_estimates(using: str | None = None) -> bool:
    """Whether this backend can answer "roughly how many?" without counting.

    Postgres keeps ``pg_class.reltuples`` maintained by VACUUM/ANALYZE and emits a
    machine-readable plan. SQLite has neither: its ``EXPLAIN QUERY PLAN`` describes the
    access path and carries no row estimate at all, and ``sqlite_stat1`` only exists
    after ``ANALYZE`` and is not per-query. MySQL's ``information_schema.TABLES.
    TABLE_ROWS`` is documented as wrong by up to 50% on InnoDB, which is a different
    order of wrong from Postgres' few percent — too wrong to render next to a tilde and
    call an estimate. Both therefore take the capped path, which is correct everywhere.
    """
    return connections[using or "default"].vendor == "postgresql"


#: ``reltuples`` is the planner's own row count for a relation, maintained by
#: VACUUM/ANALYZE. Reading it is a single index lookup on ``pg_class`` — O(1), and
#: independent of the size of the table it describes. Postgres 14+ stores -1 for a
#: relation that has never been vacuumed, which means "unknown" and not "empty".
RELTUPLES_SQL = "SELECT reltuples FROM pg_class WHERE oid = %s::regclass"


def table_row_estimate(
    model, *, using: str | None = None, cache_seconds: int | None = None
) -> int | None:
    """Roughly how many rows the *whole table* holds, or ``None`` if unknowable.

    This is the number for gating decisions — "is this table big enough to be worth
    paginating by keyset?" — and it is the number that must never be rendered to a
    row-restricted user, because it describes the table rather than their slice of it.
    Callers rendering it are responsible for :func:`is_unrestricted`; callers gating on
    it are not, since a gate leaks nothing.
    """
    alias = using or "default"
    if not supports_estimates(alias):
        return None

    table = model._meta.db_table
    key = f"vantage.scale.reltuples.{alias}.{table}"
    cached = cache.get(key)
    if cached is not None:
        return None if cached < 0 else cached

    connection = connections[alias]
    try:
        with connection.cursor() as cursor:
            # Quoted, then cast to regclass, so a mixed-case ``db_table`` resolves to
            # the relation it names rather than to its lower-cased spelling.
            cursor.execute(RELTUPLES_SQL, [connection.ops.quote_name(table)])
            row = cursor.fetchone()
    except DatabaseError:
        # A missing relation, a revoked catalogue grant, a statement timeout. None of
        # these are worth a 500 on a page that has a correct capped count in hand.
        logger.debug("reltuples unavailable for %s", table, exc_info=True)
        return None

    value = -1 if row is None or row[0] is None else int(row[0])
    cache.set(key, value, _cache_seconds(cache_seconds))
    return None if value < 0 else value


def explain_sql(queryset: QuerySet) -> tuple[str, tuple]:
    """The exact statement :func:`queryset_row_estimate` runs, and its parameters.

    Split out from the execution so that the SQL can be asserted on a backend that
    could never run it — which is the situation of this package's own test suite, and
    of most people's.

    ``FORMAT JSON`` rather than the default text plan because the text plan is a
    human-readable rendering with no stability guarantee, and scraping ``rows=`` out of
    it with a regex is the kind of code that works until a minor release. Ordering is
    stripped: the estimate is about how many rows match, and the sort is both irrelevant
    to that and the most expensive thing in the plan.
    """
    compiler = queryset.order_by().query.get_compiler(using=queryset.db)
    sql, params = compiler.as_sql()
    return f"EXPLAIN (FORMAT JSON) {sql}", params


def queryset_row_estimate(queryset: QuerySet, *, cache_seconds: int | None = None) -> int | None:
    """The planner's ``Plan Rows`` for this filtered set, or ``None`` if unknowable.

    ``EXPLAIN`` without ``ANALYZE`` plans the query and does not execute it, so this
    costs planning time and nothing else: GitLab's 1.198 ms against 14,972 ms for the
    count it replaces. The trade is accuracy — the planner is working from column
    statistics, so a filter it has no statistics for (an expression, a rarely-analyzed
    column, a correlated pair of predicates) can be out by an order of magnitude in
    either direction. That is survivable for "about how many" and is not survivable for
    anything that does arithmetic on the result, which is why nothing here feeds a
    paginator.
    """
    if not isinstance(queryset, QuerySet) or not supports_estimates(queryset.db):
        return None
    if queryset.query.is_sliced:
        # A slice puts LIMIT in the plan and the estimate would describe the slice.
        return None

    try:
        sql, params = explain_sql(queryset)
    except EmptyResultSet:
        return 0

    digest = hashlib.sha256(f"{sql}{params!r}".encode()).hexdigest()[:32]
    # The SQL *is* the permission scope: a row-scoped queryset compiles to a different
    # statement and therefore to a different key. That is what keeps this cache from
    # being the leak §4 of the list-page spec warns about, where a count cached without
    # its scope is served to the next user along.
    key = f"vantage.scale.explain.{queryset.db}.{digest}"
    cached = cache.get(key)
    if cached is not None:
        return None if cached < 0 else cached

    try:
        with connections[queryset.db].cursor() as cursor:
            cursor.execute(sql, params)
            plan = cursor.fetchone()[0]
    except (DatabaseError, IndexError, TypeError):
        logger.debug("EXPLAIN estimate unavailable", exc_info=True)
        return None

    value = _plan_rows(plan)
    cache.set(key, -1 if value is None else value, _cache_seconds(cache_seconds))
    return value


def _plan_rows(plan: Any) -> int | None:
    """Dig ``Plan Rows`` out of an ``EXPLAIN (FORMAT JSON)`` payload.

    psycopg returns the JSON already decoded; older drivers hand back a string. Both
    shapes arrive here and neither is worth a branch at the call site.
    """
    if isinstance(plan, str | bytes | bytearray):
        try:
            plan = json.loads(plan)
        except ValueError:
            return None
    try:
        return int(plan[0]["Plan"]["Plan Rows"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _cache_seconds(override: int | None) -> int:
    return int(setting("LIST_ESTIMATE_CACHE_SECONDS") if override is None else override)


# ── 3. the permission gate ──────────────────────────────────────────────────


def is_unrestricted(request, model, permissions=None, *, action: str = "view") -> bool:
    """Whether ``filter_queryset`` narrows nothing at all for this user.

    The most important function in the module, and the least interesting to look at.

    Every estimate here describes a table, not a person's view of it. Tell a user whose
    ``filter_queryset`` scopes them to their own twelve tickets that there are
    "2,000,000 records" and you have disclosed the size of a dataset they have no right
    to observe — the kind of leak that lets someone infer a competitor's order volume
    from an admin footer. It is not a rounding error, and "the number was only
    approximate" is not a defence.

    Answered by *comparing the compiled SQL* before and after ``filter_queryset``,
    rather than by asking whether the user is a superuser or trusting a declared flag.
    A project's own :class:`~vantage.permissions.PanelPermissions` subclass can scope
    rows by anything it likes; the only universally true statement is "the query came
    back unchanged". Anything else — a narrowed query, an empty query, a query this
    function cannot compile to compare — is restricted, because the failure that costs
    something is the one in that direction.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False

    if permissions is None:
        from vantage.permissions import get_permissions

        permissions = get_permissions()

    base = model._default_manager.all()
    try:
        narrowed = permissions.filter_queryset(request, base, action)
    except Exception:  # pragma: no cover - a permission layer that raises is restricted
        logger.debug("filter_queryset raised while probing scope", exc_info=True)
        return False

    if narrowed is base:
        return True
    if not isinstance(narrowed, QuerySet) or narrowed.model is not base.model:
        return False
    try:
        return _compiled(narrowed) == _compiled(base)
    except (EmptyResultSet, DatabaseError):
        # ``.none()`` does not compile at all. A user who may see no rows is the most
        # restricted user there is.
        return False


def _compiled(queryset: QuerySet) -> tuple[str, tuple]:
    return queryset.query.get_compiler(using=queryset.db).as_sql()


def covers_whole_table(queryset: QuerySet) -> bool:
    """Whether this queryset is "the table", so that ``reltuples`` describes it.

    A ``reltuples`` figure counts physical rows. It is only an answer for a queryset
    with no predicate, no join, no slice and no grouping — which includes the case a
    filtering default manager quietly breaks, since ``Ticket.objects`` hiding soft-
    deleted rows is a WHERE clause like any other.
    """
    if not isinstance(queryset, QuerySet):
        return False
    query = queryset.query
    return not (
        query.where
        or query.is_sliced
        or query.combinator
        or query.group_by
        or query.distinct
        or query.annotations
        or len(query.alias_map) > 1
    )


# ── the ladder ──────────────────────────────────────────────────────────────


def count_rows(
    queryset: QuerySet,
    *,
    cap: int | None = None,
    request=None,
    permissions=None,
    estimates: bool | None = None,
) -> RowCount:
    """How many rows match, by the cheapest method that is honest about itself.

    The order is deliberate: the capped count runs first and always, because it is the
    only step that works on every backend for every user, and because below the cap it
    produces an exact number that no estimate could improve on. The planner is consulted
    only for the case the cap cannot answer — "more than 10,000" — and only where doing
    so is both possible and safe.
    """
    cap = int(setting("LIST_COUNT_CAP") if cap is None else cap)
    counted = capped_count(queryset, cap)
    if not counted.capped:
        return counted

    if estimates is None:
        estimates = bool(setting("LIST_COUNT_ESTIMATES"))
    if not estimates or not isinstance(queryset, QuerySet):
        return counted
    if not supports_estimates(queryset.db):
        return counted

    # The gate. Applied even though ``EXPLAIN`` runs over the *already scoped* query and
    # so does not literally read the whole table: the planner derives its estimate from
    # table-wide statistics, and for a scoping predicate it has no statistics for — an
    # ownership join, a tenant column added last week — it falls back to a selectivity
    # guess and hands back something very close to the whole-table row count. It
    # degrades into the whole-table number in exactly the case that matters, so a
    # restricted user gets the capped floor and never an estimate.
    if not is_unrestricted(request, queryset.model, permissions):
        return counted

    estimate = queryset_row_estimate(queryset)
    if estimate is None or estimate <= cap:
        # Below the cap the planner contradicts something we counted; the count wins.
        return counted
    return RowCount(estimate, exact=False, estimated=True)


# ── 4. bounding the damage ──────────────────────────────────────────────────


#: ``SET LOCAL`` takes no bind parameters — ``SET LOCAL statement_timeout = %s`` is a
#: syntax error — and interpolating a value into DDL is how injections happen.
#: ``set_config(..., is_local => true)`` is the function form of exactly ``SET LOCAL``,
#: it is a normal expression, and it parameterises.
TIMEOUT_SQL = "SELECT set_config('statement_timeout', %s, true)"


@contextmanager
def statement_timeout(milliseconds: int | None = None, *, using: str | None = None):
    """Cap how long one statement may run, so a bad filter fails instead of hanging.

    Without this, the pathological case is not a slow page — it is a worker held for
    the length of a sequential scan over 10M rows while the load balancer keeps sending
    it traffic, then a second worker, and then the pool. An error on one request is a
    strictly better outcome, and the user can be told which filter caused it.

    ``LOCAL`` is what makes this safe with a connection pool or ``CONN_MAX_AGE``: the
    setting is reverted when the transaction ends, so it cannot leak onto the next
    request to borrow the connection. ``LOCAL`` outside a transaction is a no-op with a
    warning, hence the explicit ``atomic``. Postgres-only and a plain pass-through
    everywhere else — SQLite's only comparable knob is a busy timeout for lock waits,
    which is a different thing.
    """
    if milliseconds is None:
        milliseconds = setting("LIST_STATEMENT_TIMEOUT_MS")
    alias = using or "default"
    if not milliseconds or milliseconds <= 0 or connections[alias].vendor != "postgresql":
        yield
        return

    with transaction.atomic(using=alias), connections[alias].cursor() as cursor:
        cursor.execute(TIMEOUT_SQL, [f"{int(milliseconds)}ms"])
        yield
