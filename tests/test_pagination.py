"""Paging a table that is too big to count and too deep to offset into.

The two failure modes under test are the ones that only appear at scale and are
invisible at 200 rows: a count that scans the whole relation on every render, and an
``OFFSET`` that produces and discards half a million rows to return fifty.
"""

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import F
from django.db.models.functions import Lower
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.testapp.models import Author, LedgerEntry, Post, Tag
from vantage.pagination import (
    CappedPaginator,
    InvalidCursor,
    KeysetPaginator,
    KeysetUnsupported,
    decode_cursor,
    encode_cursor,
    is_totally_ordered,
    keyset_predicate,
    keyset_warranted,
    ordering_terms,
    with_total_ordering,
)


def make_entries(count, *, bucket="a", start=0):
    now = timezone.now()
    LedgerEntry.objects.bulk_create(
        [
            LedgerEntry(
                reference=f"REF-{start + index:07d}",
                # Ties on the sort key on purpose: a keyset window over a
                # non-unique column is where rows go missing.
                bucket=f"{bucket}{index % 5}",
                amount=index % 7,
                created_at=now - dt.timedelta(seconds=start + index),
            )
            for index in range(count)
        ],
        batch_size=2000,
    )


@pytest.fixture
def large(db):
    make_entries(10_000)
    return LedgerEntry.objects.all()


@pytest.fixture
def small(db):
    make_entries(120)
    return LedgerEntry.objects.all()


def superuser_request(rf):
    request = rf.get("/")
    request.user = User(username="root", is_superuser=True)
    return request


def _connection():
    from django.db import connection

    return connection


# ── the capped paginator ────────────────────────────────────────────────────


class TestCappedPaginator:
    def test_below_the_cap_the_count_is_exact(self, small):
        paginator = CappedPaginator(small, 25, cap=1000)
        assert paginator.count == 120
        assert paginator.row_count.display == "120"
        assert paginator.has_more is False

    def test_above_the_cap_it_reports_a_floor(self, large):
        paginator = CappedPaginator(large, 50, cap=1000)
        assert paginator.row_count.display == "1,000+"
        assert paginator.has_more is True

    def test_the_cap_rounds_up_to_a_whole_page(self, large):
        """A half-full final page at the boundary would read as the end of the data."""
        paginator = CappedPaginator(large, 50, cap=1020)
        assert paginator.cap == 1050
        assert paginator.num_pages == 21

    def test_the_pager_window_is_bounded_by_the_cap(self, large):
        paginator = CappedPaginator(large, 50, cap=1000)
        assert paginator.num_pages == 20

    def test_counting_costs_one_query_and_a_bounded_number_of_rows(self, large):
        paginator = CappedPaginator(large, 50, cap=1000)
        with CaptureQueriesContext(_connection()) as captured:
            assert paginator.count == 1000
        assert len(captured) == 1
        assert "LIMIT 1001" in captured[0]["sql"]

    def test_the_cost_does_not_grow_with_the_table(self, db):
        """The same statement at 200 rows and at 10,000. That is the entire point."""
        make_entries(200)
        with CaptureQueriesContext(_connection()) as small_run:
            assert CappedPaginator(LedgerEntry.objects.all(), 50, cap=1000).count == 200
        make_entries(9_800, start=1_000_000)
        with CaptureQueriesContext(_connection()) as large_run:
            assert CappedPaginator(LedgerEntry.objects.all(), 50, cap=1000).count == 1000
        assert small_run[0]["sql"] == large_run[0]["sql"]

    def test_the_last_page_is_full_and_correct(self, small):
        paginator = CappedPaginator(small, 25, cap=1000)
        page = paginator.page(5)
        assert len(page.object_list) == 20
        assert page.has_next() is False
        assert page.has_previous() is True

    def test_the_last_page_at_the_cap_is_full(self, large):
        paginator = CappedPaginator(large, 50, cap=1000)
        page = paginator.page(20)
        assert len(page.object_list) == 50
        assert page.has_more is True

    def test_a_page_out_of_range_falls_back_to_the_last_page(self, small):
        paginator = CappedPaginator(small, 25, cap=1000)
        assert paginator.get_page(9999).number == 5

    def test_a_page_past_the_cap_falls_back_to_the_last_page_in_the_window(self, large):
        paginator = CappedPaginator(large, 50, cap=1000)
        assert paginator.get_page(5000).number == 20

    def test_a_page_that_is_not_a_number_falls_back_to_the_first(self, small):
        assert CappedPaginator(small, 25, cap=1000).get_page("nonsense").number == 1

    def test_a_page_below_one_lands_on_the_last_page_as_it_always_has(self, small):
        """Stock ``get_page`` semantics, unchanged: only a non-integer means page 1."""
        assert CappedPaginator(small, 25, cap=1000).get_page(-3).number == 5

    def test_an_empty_table_still_renders_a_first_page(self, db):
        paginator = CappedPaginator(LedgerEntry.objects.all(), 25, cap=1000)
        assert paginator.count == 0
        assert list(paginator.get_page(1).object_list) == []

    def test_it_paginates_a_plain_list_like_the_stock_paginator(self):
        paginator = CappedPaginator(list(range(30)), 10, cap=1000)
        assert (paginator.count, paginator.num_pages) == (30, 3)

    def test_it_agrees_with_the_stock_paginator_below_the_cap(self, small):
        stock = Paginator(small, 25)
        capped = CappedPaginator(small, 25, cap=1000)
        assert capped.count == stock.count
        assert capped.num_pages == stock.num_pages
        assert list(capped.page(3)) == list(stock.page(3))

    def test_without_a_request_no_estimate_is_ever_attempted(self, large, monkeypatch):
        """No request means no way to prove the user is unrestricted."""
        from vantage import scale

        monkeypatch.setattr(
            scale, "queryset_row_estimate", lambda *a, **k: pytest.fail("estimated")
        )
        assert CappedPaginator(large, 50, cap=1000).row_count.display == "1,000+"

    def test_the_pager_never_does_arithmetic_on_an_estimate(self, large, rf, monkeypatch):
        """A "last page" link built from a guess would OFFSET into invented rows."""
        from vantage import scale

        monkeypatch.setattr(scale, "supports_estimates", lambda *a, **k: True)
        monkeypatch.setattr(scale, "queryset_row_estimate", lambda *a, **k: 2_400_000)
        paginator = CappedPaginator(large, 50, cap=1000, request=superuser_request(rf))
        assert paginator.row_count.display == "~2,400,000"
        assert paginator.count == 1000
        assert paginator.num_pages == 20


# ── reading an ordering ─────────────────────────────────────────────────────


class TestOrderingTerms:
    def test_it_reads_direction_off_each_term(self, db):
        terms = ordering_terms(LedgerEntry.objects.order_by("bucket", "-pk"))
        assert [(t.name, t.descending) for t in terms] == [("bucket", False), ("pk", True)]

    def test_it_falls_back_to_meta_ordering(self, db):
        terms = ordering_terms(LedgerEntry.objects.all())
        assert [t.name for t in terms] == ["created_at", "pk"]

    def test_it_reads_an_orderby_expression(self, db):
        terms = ordering_terms(LedgerEntry.objects.order_by(F("amount").desc(), "pk"))
        assert [(t.name, t.descending) for t in terms] == [("amount", True), ("pk", False)]

    @pytest.mark.parametrize(
        ("ordering", "why"),
        [
            (("?",), "random ordering has no successor relation"),
            (("author__name",), "a related path needs the join to be 1:1"),
            ((Lower("title"),), "an expression is not a stored value to compare"),
            ((F("title").asc(nulls_last=True),), "explicit null placement"),
            (("author",), "expands to the related model's own ordering"),
        ],
    )
    def test_it_refuses_what_it_cannot_page_safely(self, db, ordering, why):
        with pytest.raises(KeysetUnsupported):
            ordering_terms(Post.objects.order_by(*ordering))

    def test_a_name_that_is_not_a_field_is_refused(self, db):
        """``order_by()`` normally rejects this first; nothing here relies on that."""
        queryset = LedgerEntry.objects.order_by("bucket")
        queryset.query.order_by = ("nope",)
        with pytest.raises(KeysetUnsupported, match="not a field"):
            ordering_terms(queryset)

    def test_a_nullable_key_is_refused(self, db):
        """``(a, pk) > (NULL, 5)`` is NULL, matches nothing, and empties the page."""
        with pytest.raises(KeysetUnsupported, match="nullable"):
            ordering_terms(LedgerEntry.objects.order_by("settled_on", "pk"))

    def test_no_ordering_at_all_is_refused(self, db):
        with pytest.raises(KeysetUnsupported):
            ordering_terms(Tag.objects.all())


class TestTotalOrdering:
    def test_the_primary_key_is_a_total_order(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("pk")) is True

    def test_a_unique_non_null_column_is_a_total_order(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("reference")) is True

    def test_a_repeating_column_is_not(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("bucket")) is False

    def test_an_unordered_queryset_is_not(self, db):
        assert is_totally_ordered(Tag.objects.all()) is False

    def test_meta_ordering_counts(self, db):
        assert is_totally_ordered(LedgerEntry.objects.all()) is True

    def test_appending_the_key_makes_it_total(self, db):
        fixed = with_total_ordering(LedgerEntry.objects.order_by("bucket"))
        assert list(fixed.query.order_by) == ["bucket", "pk"]
        assert is_totally_ordered(fixed) is True

    def test_the_tiebreak_follows_the_last_terms_direction(self, db):
        fixed = with_total_ordering(LedgerEntry.objects.order_by("-bucket"))
        assert list(fixed.query.order_by) == ["-bucket", "-pk"]

    def test_an_already_total_ordering_is_left_alone(self, db):
        queryset = LedgerEntry.objects.order_by("bucket", "pk")
        assert with_total_ordering(queryset) is queryset


class TestTotalOrderingBeforeDjango61:
    """The 5.2 fallback, exercised by hiding the 6.1 property it defers to.

    Its mistakes are all false negatives, and a false negative is a refusal — so these
    assert that it agrees with 6.1 on the cases that matter and never over-claims.
    """

    @pytest.fixture(autouse=True)
    def without_the_builtin(self, monkeypatch):
        from django.db.models import QuerySet

        monkeypatch.delattr(QuerySet, "totally_ordered", raising=False)

    def test_the_property_really_is_gone(self, db):
        assert hasattr(LedgerEntry.objects.all(), "totally_ordered") is False

    def test_the_primary_key_is_a_total_order(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("-pk")) is True

    def test_the_primary_key_under_its_own_name_counts(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("id")) is True

    def test_a_unique_non_null_column_is_a_total_order(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("reference")) is True

    def test_a_repeating_column_is_not(self, db):
        assert is_totally_ordered(LedgerEntry.objects.order_by("bucket")) is False

    def test_an_ordering_it_cannot_read_is_not(self, db):
        assert is_totally_ordered(Post.objects.order_by("?")) is False

    def test_meta_ordering_counts(self, db):
        assert is_totally_ordered(LedgerEntry.objects.all()) is True

    def test_the_paginator_still_refuses_on_the_old_path(self, db):
        with pytest.raises(KeysetUnsupported):
            KeysetPaginator(LedgerEntry.objects.order_by("bucket"), 5)


# ── the SQL a cursor window generates ───────────────────────────────────────


class TestKeysetSQL:
    def test_a_uniform_ordering_uses_a_row_constructor(self, db):
        """``WHERE (sort_key, pk) < (%s, %s)`` — one index range scan."""
        queryset = LedgerEntry.objects.order_by("-created_at", "-pk")
        terms = ordering_terms(queryset)
        moment = timezone.now()
        sql, params = (
            queryset.filter(keyset_predicate(terms, [moment, 7]))
            .query.get_compiler(using="default")
            .as_sql()
        )
        sql = sql.replace("`", '"')  # MySQL quotes with backticks
        assert '("testapp_ledgerentry"."created_at", "testapp_ledgerentry"."id") < (%s, %s)' in sql
        assert params[-1] == 7

    def test_an_ascending_ordering_compares_the_other_way(self, db):
        queryset = LedgerEntry.objects.order_by("created_at", "pk")
        terms = ordering_terms(queryset)
        sql, _ = (
            queryset.filter(keyset_predicate(terms, [timezone.now(), 7]))
            .query.get_compiler(using="default")
            .as_sql()
        )
        assert ") > (%s, %s)" in sql

    def test_walking_backwards_mirrors_every_comparison(self, db):
        queryset = LedgerEntry.objects.order_by("-created_at", "-pk")
        terms = ordering_terms(queryset)
        sql, _ = (
            queryset.filter(keyset_predicate(terms, [timezone.now(), 7], backwards=True))
            .query.get_compiler(using="default")
            .as_sql()
        )
        assert ") > (%s, %s)" in sql

    def test_a_mixed_ordering_expands_lexicographically(self, db):
        """Row constructors compare in one direction only, so this cannot use one."""
        queryset = LedgerEntry.objects.order_by("bucket", "-pk")
        terms = ordering_terms(queryset)
        sql, _ = (
            queryset.filter(keyset_predicate(terms, ["a1", 7]))
            .query.get_compiler(using="default")
            .as_sql()
        )
        sql = sql.replace("`", '"')  # MySQL quotes with backticks
        assert " OR " in sql
        assert '"bucket" > %s' in sql
        assert '"id" < %s' in sql

    def test_an_empty_ordering_has_no_window(self):
        with pytest.raises(KeysetUnsupported):
            keyset_predicate([], [])


class TestCursors:
    def test_a_cursor_round_trips_through_the_field(self, db):
        terms = ordering_terms(LedgerEntry.objects.order_by("created_at", "pk"))
        moment = timezone.now().replace(microsecond=0)
        values, backwards = decode_cursor(encode_cursor([moment, 42]), terms)
        assert values == [moment, 42]
        assert backwards is False

    def test_a_backwards_cursor_says_so(self, db):
        terms = ordering_terms(LedgerEntry.objects.order_by("amount", "pk"))
        _, backwards = decode_cursor(encode_cursor([3, 42], backwards=True), terms)
        assert backwards is True

    def test_the_token_is_url_safe(self, db):
        token = encode_cursor([timezone.now(), 42])
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    @pytest.mark.parametrize("token", ["", "!!!!", "eyJ2IjogW10sICJkIjogIm5leHQifQ"])
    def test_a_malformed_cursor_is_refused_not_crashed_on(self, db, token):
        terms = ordering_terms(LedgerEntry.objects.order_by("amount", "pk"))
        with pytest.raises(InvalidCursor):
            decode_cursor(token or "x", terms)

    def test_a_cursor_from_a_different_ordering_is_refused(self, db):
        terms = ordering_terms(LedgerEntry.objects.order_by("amount", "pk"))
        with pytest.raises(InvalidCursor):
            decode_cursor(encode_cursor([1, 2, 3]), terms)

    def test_a_tampered_value_is_refused_by_the_field_not_by_the_database(self, db):
        """The cursor never reaches SQL uncoerced; ``to_python`` is the gate."""
        terms = ordering_terms(LedgerEntry.objects.order_by("amount", "pk"))
        with pytest.raises(InvalidCursor):
            decode_cursor(encode_cursor(["1 OR 1=1", 2]), terms)


# ── the keyset paginator ────────────────────────────────────────────────────


class TestKeysetPaginator:
    def test_it_refuses_a_non_total_ordering(self, db):
        """The requirement: refuse, rather than silently skip rows."""
        make_entries(10)
        with pytest.raises(KeysetUnsupported, match="totally ordered"):
            KeysetPaginator(LedgerEntry.objects.order_by("bucket"), 5)

    def test_the_refusal_names_the_fix(self, db):
        with pytest.raises(KeysetUnsupported, match="with_total_ordering"):
            KeysetPaginator(LedgerEntry.objects.order_by("bucket"), 5)

    def test_it_refuses_an_unordered_queryset(self, db):
        with pytest.raises(KeysetUnsupported):
            KeysetPaginator(Tag.objects.all(), 5)

    def test_it_refuses_something_that_is_not_a_queryset(self):
        with pytest.raises(KeysetUnsupported):
            KeysetPaginator([1, 2, 3], 5)

    def test_it_returns_the_same_rows_as_offset_paging(self, small):
        """The correctness test for the whole feature, walked end to end."""
        queryset = LedgerEntry.objects.order_by("-created_at", "-pk")
        offset = Paginator(queryset, 25)
        keyset = KeysetPaginator(queryset, 25)

        token, walked = None, []
        for number in offset.page_range:
            page = keyset.page(token)
            assert [row.pk for row in page] == [row.pk for row in offset.page(number)]
            walked += [row.pk for row in page]
            token = page.next_cursor
        assert token is None
        assert walked == [row.pk for row in queryset]

    def test_it_matches_offset_paging_on_a_tied_sort_key(self, small):
        """``bucket`` repeats every five rows: exactly where a cursor loses rows."""
        queryset = with_total_ordering(LedgerEntry.objects.order_by("bucket"))
        offset = Paginator(queryset, 25)
        keyset = KeysetPaginator(queryset, 25)

        token, walked = None, []
        for number in offset.page_range:
            page = keyset.page(token)
            assert [row.pk for row in page] == [row.pk for row in offset.page(number)]
            walked += [row.pk for row in page]
            token = page.next_cursor
        assert len(set(walked)) == len(walked) == 120

    def test_it_matches_offset_paging_on_a_mixed_direction_ordering(self, small):
        queryset = LedgerEntry.objects.order_by("bucket", "-pk")
        offset = Paginator(queryset, 40)
        keyset = KeysetPaginator(queryset, 40)

        token, walked = None, []
        for number in offset.page_range:
            page = keyset.page(token)
            assert [row.pk for row in page] == [row.pk for row in offset.page(number)]
            walked += [row.pk for row in page]
            token = page.next_cursor
        assert len(walked) == 120

    def test_the_first_page_has_no_previous(self, small):
        page = KeysetPaginator(LedgerEntry.objects.order_by("pk"), 25).page()
        assert (page.has_previous, page.previous_cursor) == (False, None)
        assert page.has_next is True

    def test_the_final_page_has_no_next(self, small):
        keyset = KeysetPaginator(LedgerEntry.objects.order_by("pk"), 100)
        final = keyset.page(keyset.page().next_cursor)
        assert (final.has_next, final.next_cursor) == (False, None)
        assert len(final) == 20

    def test_walking_back_returns_the_previous_page(self, small):
        queryset = LedgerEntry.objects.order_by("-created_at", "-pk")
        keyset = KeysetPaginator(queryset, 25)
        first = keyset.page()
        second = keyset.page(first.next_cursor)
        back = keyset.page(second.previous_cursor)
        assert [row.pk for row in back] == [row.pk for row in first]

    def test_a_page_reads_one_row_beyond_itself_and_no_more(self, large):
        """ "Is there a next page?" costs one extra row, not a count."""
        keyset = KeysetPaginator(LedgerEntry.objects.order_by("pk"), 50)
        with CaptureQueriesContext(_connection()) as captured:
            keyset.page()
        assert len(captured) == 1
        assert "LIMIT 51" in captured[0]["sql"]

    def test_page_cost_does_not_grow_with_depth(self, large):
        """No OFFSET anywhere: page 200 issues the same shape of statement as page 1."""
        keyset = KeysetPaginator(LedgerEntry.objects.order_by("pk"), 50)
        token = keyset.page().next_cursor
        for _ in range(5):
            token = keyset.page(token).next_cursor
        with CaptureQueriesContext(_connection()) as captured:
            keyset.page(token)
        assert "OFFSET" not in captured[0]["sql"]
        assert "LIMIT 51" in captured[0]["sql"]

    def test_an_empty_table_gives_an_empty_page(self, db):
        page = KeysetPaginator(LedgerEntry.objects.order_by("pk"), 25).page()
        assert (bool(page), page.has_next, page.next_cursor) == (False, False, None)

    def test_it_pages_a_filtered_queryset(self, small):
        queryset = LedgerEntry.objects.filter(bucket="a0").order_by("pk")
        keyset = KeysetPaginator(queryset, 10)
        assert [row.bucket for row in keyset.page()] == ["a0"] * 10


class TestKeysetWarranted:
    def test_a_shallow_page_on_a_small_table_does_not_need_it(self, small):
        assert keyset_warranted(small, page_number=2, per_page=50, threshold=50_000) is False

    def test_a_deep_page_does(self, small):
        """Page 1,001 at 50 a page means discarding 50,000 rows to show fifty."""
        assert keyset_warranted(small, page_number=1_001, per_page=50, threshold=50_000) is True

    def test_a_threshold_of_zero_switches_it_off(self, small):
        assert keyset_warranted(small, page_number=9_999, per_page=50, threshold=0) is False

    def test_without_a_table_estimate_only_depth_can_trigger_it(self, large):
        """On SQLite there is no ``reltuples``, so the size signal is simply absent."""
        assert keyset_warranted(large, page_number=1, per_page=50, threshold=100) is False

    def test_a_large_table_estimate_triggers_it(self, large, monkeypatch):
        from vantage import pagination

        monkeypatch.setattr(pagination, "table_row_estimate", lambda *a, **k: 2_000_000)
        assert keyset_warranted(large, page_number=1, per_page=50, threshold=50_000) is True

    def test_it_needs_no_permission_check_because_it_shows_nobody_a_number(
        self, large, rf, monkeypatch
    ):
        """A gate leaks nothing; only a rendered estimate does."""
        from vantage import pagination

        monkeypatch.setattr(pagination, "table_row_estimate", lambda *a, **k: 2_000_000)
        assert keyset_warranted(large, threshold=1) is True


class TestAgainstRealRelations:
    """A quick check that none of this is specific to the one fixture model."""

    def test_it_pages_a_model_with_a_foreign_key(self, db):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        for index in range(30):
            Post.objects.create(title=f"P{index}", slug=f"p{index}", body="x", author=author)
        queryset = Post.objects.filter(author=author).order_by("pk")
        keyset = KeysetPaginator(queryset, 10)
        page = keyset.page()
        assert len(page) == 10
        assert [p.pk for p in keyset.page(page.next_cursor)] == [
            p.pk for p in Paginator(queryset, 10).page(2)
        ]
