"""Counting, estimating and bounding at a table size nobody develops against.

The test database is SQLite, so none of the Postgres paths can actually execute here.
What *is* tested is everything around them: the SQL they generate, the vendor guard
that stops them running at all, the parsing of a real captured plan, and — the one that
matters most — the permission gate that keeps a whole-table number away from a user who
is only allowed to see part of the table.
"""

import datetime as dt

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.db import DatabaseError
from django.db.utils import OperationalError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.testapp.models import LedgerEntry, Post, Ticket
from vantage import conf, scale
from vantage.permissions import PanelPermissions
from vantage.scale import (
    RELTUPLES_SQL,
    TIMEOUT_SQL,
    RowCount,
    capped_count,
    count_rows,
    covers_whole_table,
    explain_sql,
    is_unrestricted,
    queryset_row_estimate,
    setting,
    statement_timeout,
    supports_estimates,
    table_row_estimate,
)

# A real ``EXPLAIN (FORMAT JSON)`` payload from Postgres 16, trimmed to the keys this
# package reads. Kept verbatim so the parser is tested against the shape Postgres
# actually emits rather than against the shape it was written to expect.
POSTGRES_PLAN = [
    {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "testapp_ledgerentry",
            "Startup Cost": 0.00,
            "Total Cost": 21925.00,
            "Plan Rows": 1249823,
            "Plan Width": 44,
        }
    }
]


def make_entries(count, *, bucket="a", start=0):
    now = timezone.now()
    LedgerEntry.objects.bulk_create(
        [
            LedgerEntry(
                reference=f"REF-{start + index:07d}",
                bucket=bucket,
                amount=index,
                created_at=now - dt.timedelta(seconds=start + index),
            )
            for index in range(count)
        ],
        batch_size=2000,
    )


@pytest.fixture
def large(db):
    """10,000 rows. Enough that an unbounded count is measurably not free."""
    make_entries(10_000)
    return LedgerEntry.objects.all()


# ── fakes for the paths SQLite cannot run ───────────────────────────────────


class FakeCursor:
    def __init__(self, rows, error=None):
        self.rows = list(rows)
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeOps:
    @staticmethod
    def quote_name(name):
        return f'"{name}"'


class FakePostgres:
    """Just enough connection to answer the two questions this module asks one."""

    vendor = "postgresql"
    ops = FakeOps()

    def __init__(self, rows=(), error=None):
        self.last = None
        self._rows = rows
        self._error = error

    def cursor(self):
        self.last = FakeCursor(self._rows, self._error)
        return self.last


@pytest.fixture
def pretend_postgres(monkeypatch):
    """Swap the connection registry this module reads, and nothing else.

    ``transaction.atomic`` still talks to the real SQLite connection, which is what
    makes the timeout helper's transaction behaviour observable at all here.
    """

    def install(rows=(), error=None):
        connection = FakePostgres(rows, error)
        monkeypatch.setattr(scale, "connections", {"default": connection})
        return connection

    return install


@pytest.fixture(autouse=True)
def clear_estimate_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


class OwnRowsOnly(PanelPermissions):
    """A row-scoping permission layer of the kind a real project writes."""

    superuser_bypass = False

    def filter_queryset(self, request, queryset, action="view"):
        return queryset.filter(pk__in=[1, 2, 3])


class Nothing(PanelPermissions):
    def filter_queryset(self, request, queryset, action="view"):
        return queryset.none()


class Rebuilt(PanelPermissions):
    """Returns a different object with identical SQL — still unrestricted."""

    def filter_queryset(self, request, queryset, action="view"):
        return queryset.all()


def request_for(user, rf):
    request = rf.get("/")
    request.user = user
    return request


# ── the display contract ────────────────────────────────────────────────────


class TestRowCount:
    def test_an_exact_count_is_rendered_plainly(self):
        assert RowCount(1234).display == "1,234"

    def test_a_capped_count_is_rendered_as_a_floor(self):
        assert RowCount(10_000, exact=False, capped=True).display == "10,000+"

    def test_an_estimate_is_rendered_with_a_tilde(self):
        """The tilde is the only thing separating a guess from an assertion."""
        assert RowCount(2_400_000, exact=False, estimated=True).display == "~2,400,000"

    def test_only_an_exact_count_claims_to_be_one(self):
        assert RowCount(5).is_approximate is False
        assert RowCount(5, exact=False, capped=True).is_approximate is True


# ── capped counting ─────────────────────────────────────────────────────────


class TestCappedCount:
    def test_it_is_exact_below_the_cap(self, db):
        make_entries(37)
        assert capped_count(LedgerEntry.objects.all(), 100) == RowCount(37)

    def test_it_is_exact_at_exactly_the_cap(self, db):
        """The cap is a floor on "more than", so N == cap is still a count."""
        make_entries(50)
        counted = capped_count(LedgerEntry.objects.all(), 50)
        assert (counted.value, counted.exact) == (50, True)

    def test_it_reports_a_floor_above_the_cap(self, large):
        counted = capped_count(large, 100)
        assert (counted.value, counted.capped, counted.display) == (100, True, "100+")

    def test_it_counts_the_filtered_set_not_the_table(self, db):
        make_entries(20, bucket="a")
        make_entries(5, bucket="b", start=100)
        assert capped_count(LedgerEntry.objects.filter(bucket="b"), 100).value == 5

    def test_it_costs_one_query(self, large, django_assert_num_queries):
        with django_assert_num_queries(1):
            capped_count(large, 100)

    def test_the_cap_bounds_the_rows_read_whatever_the_table_size(self, large):
        """The point of the whole module: 10,000 rows, 101 read.

        A ``COUNT(*)`` here visits every row to know it visited them all. This asks for
        one more row than the cap and stops, so the statement it issues is the same
        statement at 10,000 rows and at 10,000,000.
        """
        with CaptureQueriesContext(_connection()) as captured:
            counted = capped_count(large, 100)
        assert len(captured) == 1
        assert captured[0]["sql"].startswith("SELECT COUNT(*) FROM (SELECT ")
        assert "LIMIT 101" in captured[0]["sql"]
        assert counted.display == "100+"

    def test_the_bounded_rows_are_counted_in_the_database_not_shipped(self, large):
        """One row on the wire, not 10,001 primary keys nothing then looks at."""
        with CaptureQueriesContext(_connection()) as captured:
            capped_count(large, 10_000)
        assert captured[0]["sql"].count("SELECT") == 2

    def test_it_does_not_sort_what_it_only_counts(self, large):
        """Meta ordering is stripped: it is pure cost, and it defeats LIMIT early-exit."""
        with CaptureQueriesContext(_connection()) as captured:
            capped_count(large, 100)
        assert "ORDER BY" not in captured[0]["sql"]

    def test_a_plain_sequence_already_knows_its_length(self):
        assert capped_count([1, 2, 3], 100) == RowCount(3)

    def test_a_cap_of_zero_means_count_properly(self, db):
        make_entries(12)
        assert capped_count(LedgerEntry.objects.all(), 0) == RowCount(12)


# ── the vendor guard ────────────────────────────────────────────────────────


class TestEstimatesAreSkippedOffPostgres:
    def test_sqlite_declares_itself_incapable(self):
        assert supports_estimates() is False

    def test_a_table_estimate_is_none_rather_than_an_error(self, db):
        assert table_row_estimate(LedgerEntry) is None

    def test_a_table_estimate_costs_no_query_at_all(self, db, django_assert_num_queries):
        with django_assert_num_queries(0):
            table_row_estimate(LedgerEntry)

    def test_a_queryset_estimate_is_none_rather_than_an_error(self, db):
        assert queryset_row_estimate(LedgerEntry.objects.all()) is None

    def test_the_ladder_falls_back_to_the_capped_count(self, large, rf):
        request = request_for(User(username="root", is_superuser=True), rf)
        counted = count_rows(large, cap=100, request=request)
        assert (counted.capped, counted.estimated, counted.display) == (True, False, "100+")


# ── the SQL those paths would run ───────────────────────────────────────────


class TestGeneratedSQL:
    def test_reltuples_reads_the_catalogue_not_the_table(self):
        assert "pg_class" in RELTUPLES_SQL
        assert "reltuples" in RELTUPLES_SQL
        assert "::regclass" in RELTUPLES_SQL

    def test_explain_asks_for_json(self, db):
        sql, _ = explain_sql(LedgerEntry.objects.all())
        assert sql.startswith("EXPLAIN (FORMAT JSON) SELECT ")

    def test_explain_strips_the_ordering(self, db):
        """The sort is irrelevant to how many rows match and is the costliest node."""
        sql, _ = explain_sql(LedgerEntry.objects.order_by("-created_at"))
        assert "ORDER BY" not in sql

    def test_explain_keeps_the_filter_as_bound_parameters(self, db):
        sql, params = explain_sql(LedgerEntry.objects.filter(bucket="b"))
        assert "WHERE" in sql and "%s" in sql
        assert params == ("b",)

    def test_the_timeout_is_transaction_local_and_parameterised(self):
        """``SET LOCAL`` takes no bind parameters; ``set_config(..., true)`` is it."""
        assert TIMEOUT_SQL == "SELECT set_config('statement_timeout', %s, true)"


class TestPostgresPathsAgainstAFakeConnection:
    """Exercises the code Postgres would run, without a Postgres. Read as: the SQL and
    the parsing are covered; the planner's behaviour is not, and cannot be here."""

    def test_reltuples_is_read_for_the_quoted_table(self, db, pretend_postgres):
        connection = pretend_postgres(rows=[(1_249_823.0,)])
        assert table_row_estimate(LedgerEntry) == 1_249_823
        assert connection.last.calls == [(RELTUPLES_SQL, ['"testapp_ledgerentry"'])]

    def test_a_never_vacuumed_relation_reports_unknown_not_empty(self, db, pretend_postgres):
        """Postgres 14+ stores -1 there, and -1 is not zero rows."""
        pretend_postgres(rows=[(-1.0,)])
        assert table_row_estimate(LedgerEntry) is None

    def test_the_estimate_is_cached(self, db, pretend_postgres):
        connection = pretend_postgres(rows=[(500.0,)])
        assert table_row_estimate(LedgerEntry) == 500
        assert table_row_estimate(LedgerEntry) == 500
        assert len(connection.last.calls) == 1

    def test_a_database_error_is_swallowed(self, db, pretend_postgres):
        pretend_postgres(error=DatabaseError("permission denied for table pg_class"))
        assert table_row_estimate(LedgerEntry) is None

    def test_plan_rows_is_read_from_the_plan(self, db, pretend_postgres):
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        assert queryset_row_estimate(LedgerEntry.objects.all()) == 1_249_823

    def test_a_plan_delivered_as_text_parses_too(self, db, pretend_postgres):
        import json

        pretend_postgres(rows=[(json.dumps(POSTGRES_PLAN),)])
        assert queryset_row_estimate(LedgerEntry.objects.all()) == 1_249_823

    def test_an_unrecognisable_plan_is_none_rather_than_an_error(self, db, pretend_postgres):
        pretend_postgres(rows=[({"not": "a plan"},)])
        assert queryset_row_estimate(LedgerEntry.objects.all()) is None

    def test_a_sliced_queryset_is_declined(self, db, pretend_postgres):
        """LIMIT in the plan would make Plan Rows describe the slice."""
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        assert queryset_row_estimate(LedgerEntry.objects.all()[:10]) is None

    def test_the_ladder_prefers_an_estimate_above_the_cap(self, large, rf, pretend_postgres):
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        request = request_for(User(username="root", is_superuser=True), rf)
        counted = count_rows(large, cap=100, request=request)
        assert counted.display == "~1,249,823"
        assert counted.estimated is True

    def test_a_planner_estimate_never_overrides_a_count(self, db, rf, pretend_postgres):
        """Below the cap the number is known, and a guess cannot improve on it."""
        make_entries(30)
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        request = request_for(User(username="root", is_superuser=True), rf)
        assert count_rows(LedgerEntry.objects.all(), cap=100, request=request) == RowCount(30)


# ── the permission gate ─────────────────────────────────────────────────────


class TestIsUnrestricted:
    def test_the_default_permission_layer_narrows_nothing(self, db, rf):
        request = request_for(User(username="staff"), rf)
        assert is_unrestricted(request, LedgerEntry, PanelPermissions()) is True

    def test_a_row_scoped_user_is_restricted(self, db, rf):
        request = request_for(User(username="agent"), rf)
        assert is_unrestricted(request, LedgerEntry, OwnRowsOnly()) is False

    def test_a_user_who_may_see_nothing_is_restricted(self, db, rf):
        """``.none()`` does not compile at all, and must not read as "unchanged"."""
        request = request_for(User(username="agent"), rf)
        assert is_unrestricted(request, LedgerEntry, Nothing()) is False

    def test_an_equivalent_queryset_is_not_a_restriction(self, db, rf):
        """Compared by compiled SQL, not by object identity."""
        request = request_for(User(username="staff"), rf)
        assert is_unrestricted(request, LedgerEntry, Rebuilt()) is True

    def test_an_anonymous_user_is_restricted(self, db, rf):
        assert is_unrestricted(request_for(AnonymousUser(), rf), LedgerEntry) is False

    def test_a_request_without_a_user_is_restricted(self, db, rf):
        assert is_unrestricted(rf.get("/"), LedgerEntry, PanelPermissions()) is False

    def test_no_request_at_all_is_restricted(self, db):
        assert is_unrestricted(None, LedgerEntry, PanelPermissions()) is False


class TestTheLeak:
    """The named test. A restricted user is never told the size of the whole table."""

    def test_a_permission_restricted_user_never_receives_a_whole_table_estimate(
        self, large, rf, pretend_postgres, monkeypatch
    ):
        """A support agent scoped to twelve tickets must not be shown "1,249,823".

        The estimate path is available here — Postgres is pretending to answer, and the
        planner would happily return the whole-table figure — and the only thing
        stopping it is the gate. So this asserts both halves: the number the user gets
        is the capped floor over rows they can see, and the planner was not asked.
        """
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        asked = []
        monkeypatch.setattr(
            scale, "queryset_row_estimate", lambda *a, **k: asked.append(1) or 1_249_823
        )
        request = request_for(User(username="agent"), rf)
        permissions = OwnRowsOnly()
        scoped = permissions.filter_queryset(request, large, "view")

        counted = count_rows(scoped, cap=100, request=request, permissions=permissions)

        assert asked == [], "the planner was consulted for a row-restricted user"
        assert counted.estimated is False
        assert counted.value <= 100
        assert "1,249,823" not in counted.display

    def test_the_same_query_for_an_unrestricted_user_does_get_the_estimate(
        self, large, rf, pretend_postgres
    ):
        """The control: the gate is what differs, not the data or the backend."""
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        request = request_for(User(username="root", is_superuser=True), rf)
        counted = count_rows(large, cap=100, request=request, permissions=PanelPermissions())
        assert counted.display == "~1,249,823"

    def test_no_request_means_no_estimate(self, large, pretend_postgres):
        """Nothing to gate on is not a reason to skip the gate."""
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        assert count_rows(large, cap=100).display == "100+"

    def test_estimates_can_be_switched_off_outright(self, large, rf, pretend_postgres):
        pretend_postgres(rows=[(POSTGRES_PLAN,)])
        request = request_for(User(username="root", is_superuser=True), rf)
        assert count_rows(large, cap=100, request=request, estimates=False).display == "100+"


class TestCoversWholeTable:
    def test_an_untouched_queryset_is_the_table(self, db):
        assert covers_whole_table(LedgerEntry.objects.all()) is True

    @pytest.mark.parametrize(
        ("queryset", "why"),
        [
            (lambda: LedgerEntry.objects.filter(bucket="a"), "a predicate"),
            (lambda: LedgerEntry.objects.all()[:10], "a slice"),
            (lambda: LedgerEntry.objects.distinct("bucket"), "distinct"),
            (lambda: Post.objects.filter(author__name="Ada"), "a join"),
            (lambda: Ticket.objects.all(), "a manager that hides the trash"),
        ],
    )
    def test_anything_narrower_is_not(self, db, queryset, why):
        assert covers_whole_table(queryset()) is False, why

    def test_a_plain_list_is_not_a_table(self):
        assert covers_whole_table([1, 2]) is False


# ── the statement timeout ───────────────────────────────────────────────────


class TestStatementTimeout:
    def test_it_is_a_no_op_off_postgres(self, db, django_assert_num_queries):
        with django_assert_num_queries(0), statement_timeout(5000):
            pass

    def test_the_body_still_runs_off_postgres(self, db):
        make_entries(3)
        with statement_timeout(5000):
            assert LedgerEntry.objects.count() == 3

    def test_zero_disables_it(self, db, pretend_postgres):
        connection = pretend_postgres()
        with statement_timeout(0):
            pass
        assert connection.last is None

    def test_it_sets_a_transaction_local_timeout_on_postgres(self, db, pretend_postgres):
        connection = pretend_postgres()
        with statement_timeout(2500):
            pass
        assert connection.last.calls == [(TIMEOUT_SQL, ["2500ms"])]

    def test_it_opens_a_transaction_so_local_has_something_to_scope_to(self, db, pretend_postgres):
        """``SET LOCAL`` outside a transaction is a warning and no timeout at all."""
        from django.db import transaction

        pretend_postgres()
        with statement_timeout(2500):
            assert transaction.get_connection().in_atomic_block is True

    def test_a_timeout_surfaces_as_an_error_rather_than_hanging(self, db, pretend_postgres):
        """What the caller sees when the ceiling is hit: an exception, not a held worker."""
        pretend_postgres(error=OperationalError("canceling statement due to timeout"))
        with pytest.raises(OperationalError), statement_timeout(1):
            pass


# ── settings ────────────────────────────────────────────────────────────────


class TestSettings:
    def test_a_key_the_settings_layer_does_not_know_falls_back(self):
        """These keys are not in ``conf.DEFAULTS`` yet; the module works regardless."""
        assert setting("LIST_COUNT_CAP") == 10_000
        assert setting("LIST_STATEMENT_TIMEOUT_MS") == 10_000
        assert setting("LIST_COUNT_ESTIMATES") is True

    def test_it_picks_up_the_project_value_once_the_key_is_declared(self, monkeypatch):
        """Exactly what happens when ``LIST_COUNT_CAP`` is added to ``conf.DEFAULTS``."""
        monkeypatch.setitem(conf.DEFAULTS, "LIST_COUNT_CAP", 10_000)
        with override_settings(VANTAGE={"LIST_COUNT_CAP": 250}):
            assert setting("LIST_COUNT_CAP") == 250

    def test_a_declared_false_is_honoured_not_defaulted(self, monkeypatch):
        """The bug in a naive ``value or default``: ``False`` is a real answer."""
        monkeypatch.setitem(conf.DEFAULTS, "LIST_COUNT_ESTIMATES", True)
        with override_settings(VANTAGE={"LIST_COUNT_ESTIMATES": False}):
            assert setting("LIST_COUNT_ESTIMATES") is False


def _connection():
    from django.db import connection

    return connection
