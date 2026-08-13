"""The stats engine is measured by its query count as much as by its numbers.

Every test that computes anything asserts how many queries it took, because the whole
point of the module is that a stat row costs one round trip rather than one per card.
"""

import datetime as dt

import pytest
from django.db.models import Avg, Count, Q, Sum

from tests.testapp.models import Author, Order, Post
from vantage import stats as stats_module
from vantage.stats import (
    Budget,
    Chart,
    Stat,
    compute_charts,
    compute_panel,
    compute_stats,
    format_value,
    plan_queries,
)

BASE = dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.UTC)


def make_author(name="Ada"):
    return Author.objects.create(name=name, email=f"{name.lower()}@example.com")


def make_order(customer, *, days_ago, total, status="paid", number=None):
    """``created_at`` is ``auto_now_add``, so it can only be set after the fact."""
    order = Order.objects.create(
        number=number or f"O-{Order.objects.count() + 1:04d}",
        customer=customer,
        status=status,
        total=total,
    )
    Order.objects.filter(pk=order.pk).update(created_at=BASE - dt.timedelta(days=days_ago))
    order.refresh_from_db()
    return order


@pytest.fixture
def orders(db):
    """Ten orders over ten days: 100 each in the recent half, 50 each in the older."""
    author = make_author()
    for day in range(1, 6):
        make_order(author, days_ago=day, total=100)
    for day in range(6, 11):
        make_order(author, days_ago=day, total=50, status="refunded")
    return Order.objects.all()


# ── formatting ──────────────────────────────────────────────────────────────


class TestFormatValue:
    def test_number_drops_a_pointless_decimal(self):
        assert format_value(1234.0) == "1,234"

    def test_number_keeps_a_real_one(self):
        assert format_value(1234.5) == "1,234.50"

    def test_currency_puts_the_sign_outside_the_symbol(self):
        assert format_value(-1234.5, "currency") == "-$1,234.50"

    def test_percent_reads_its_input_as_a_fraction(self):
        assert format_value(0.4237, "percent") == "42.4%"

    def test_duration_picks_the_two_largest_useful_units(self):
        assert format_value(3 * 3600 + 25 * 60, "duration") == "3h 25m"
        assert format_value(90061, "duration") == "1d 1h"
        assert format_value(45, "duration") == "45s"

    def test_signed_marks_a_positive_delta(self):
        assert format_value(12, "number", signed=True) == "+12"
        assert format_value(-12, "number", signed=True) == "-12"

    def test_none_formats_as_zero(self):
        assert format_value(None, "currency") == "$0.00"

    def test_an_unknown_format_is_rejected_at_declaration(self):
        with pytest.raises(ValueError, match="format"):
            Stat("Bad", Count("id"), format="gauge")


# ── the single-query guarantee ──────────────────────────────────────────────


@pytest.mark.django_db
class TestOneQuery:
    def test_every_stat_shares_one_aggregate(self, orders, django_assert_num_queries):
        declared = [
            Stat("Revenue", Sum("total"), format="currency"),
            Stat("Orders", Count("id")),
            Stat("Avg order", Avg("total"), format="currency"),
            Stat("Refunded", Count("id", filter=Q(status="refunded")), tone="destructive"),
        ]
        with django_assert_num_queries(1):
            result = compute_stats(orders, declared)

        assert [s.value for s in result] == [750.0, 10, 75.0, 5]
        assert result[0].display == "$750.00"

    def test_trends_add_exactly_one_query_however_many_trend(
        self, orders, django_assert_num_queries
    ):
        declared = [
            Stat("Revenue", Sum("total"), trend="created_at"),
            Stat("Orders", Count("id"), trend="created_at"),
            Stat("Avg order", Avg("total"), trend="created_at"),
        ]
        with django_assert_num_queries(2):
            compute_stats(orders, declared)

    def test_a_callable_costs_its_own_query(self, orders, django_assert_num_queries):
        declared = [
            Stat("Orders", Count("id")),
            Stat("Paid", lambda qs: qs.filter(status="paid").count()),
        ]
        with django_assert_num_queries(2):
            result = compute_stats(orders, declared)
        assert result[1].value == 5

    def test_plan_matches_what_is_actually_run(self, orders, django_assert_num_queries):
        declared = [
            Stat("Revenue", Sum("total"), trend="created_at"),
            Stat("Orders", Count("id")),
        ]
        charts = [
            Chart.timeseries("Over time", date="created_at", value=Sum("total")),
            Chart.breakdown("By status", group_by="status", value=Count("id")),
        ]
        planned = plan_queries(Order, declared, charts)
        with django_assert_num_queries(planned):
            compute_panel(orders, stats=declared, charts=charts)


# ── correctness against the filtered queryset ───────────────────────────────


@pytest.mark.django_db
class TestFilteredQueryset:
    def test_stats_follow_the_filter(self, orders):
        result = compute_stats(
            orders.filter(status="refunded"),
            [Stat("Revenue", Sum("total")), Stat("Orders", Count("id"))],
        )
        assert [s.value for s in result] == [250.0, 5]

    def test_a_sliced_queryset_is_refused(self, orders):
        """Aggregating a page would silently answer a different question."""
        with pytest.raises(ValueError, match="before pagination"):
            compute_stats(orders[:2], [Stat("Orders", Count("id"))])

    def test_an_empty_result_is_zero_not_none(self, orders, django_assert_num_queries):
        empty = orders.filter(status="shipped")
        with django_assert_num_queries(1):
            result = compute_stats(
                empty,
                [
                    Stat("Revenue", Sum("total"), format="currency"),
                    Stat("Orders", Count("id")),
                    Stat("Avg", Avg("total")),
                ],
            )
        assert [s.value for s in result] == [0, 0, 0]
        assert result[0].display == "$0.00"

    def test_an_empty_result_still_reports_a_flat_trend(self, orders):
        empty = orders.filter(status="shipped")
        result = compute_stats(empty, [Stat("Revenue", Sum("total"), trend="created_at")])
        assert result[0].value == 0
        assert result[0].trend is None

    def test_results_are_json_serialisable(self, orders):
        import json

        panel = compute_panel(
            orders,
            stats=[Stat("Revenue", Sum("total"), format="currency", trend="created_at")],
            charts=[Chart.timeseries("Over time", date="created_at", value=Sum("total"))],
        )
        assert json.loads(json.dumps(panel)) == panel


# ── trends ──────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTrend:
    def test_up_with_the_delta_maths(self, orders):
        """Recent five days: 5 x 100. The equal-length window before it: 4 x 50."""
        recent = orders.filter(created_at__gte=BASE - dt.timedelta(days=5, hours=12))
        result = compute_stats(
            recent,
            [Stat("Revenue", Sum("total"), format="currency", trend="created_at")],
            baseline=Order.objects.all(),
        )
        trend = result[0].trend
        assert result[0].value == 500.0
        assert trend.previous == 200.0
        assert trend.delta == 300.0
        assert trend.direction == "up"
        assert trend.percent == 1.5
        assert trend.delta_display == "+$300.00"
        assert trend.percent_display == "+150.0%"

    def test_down(self, orders):
        author = Author.objects.get()
        for day in range(11, 15):
            make_order(author, days_ago=day, total=500)
        older = orders.filter(
            created_at__gte=BASE - dt.timedelta(days=10, hours=12),
            created_at__lt=BASE - dt.timedelta(days=5, hours=12),
        )
        result = compute_stats(
            older,
            [Stat("Revenue", Sum("total"), trend="created_at")],
            baseline=Order.objects.all(),
        )
        trend = result[0].trend
        assert result[0].value == 250.0
        assert trend.previous == 2000.0
        assert trend.delta == -1750.0
        assert trend.direction == "down"

    def test_flat(self, db):
        author = make_author("Level")
        for day in (1, 2, 3):
            make_order(author, days_ago=day, total=10)
        for day in (4, 5):
            make_order(author, days_ago=day, total=15)
        recent = Order.objects.filter(created_at__gte=BASE - dt.timedelta(days=3, hours=12))
        result = compute_stats(
            recent,
            [Stat("Revenue", Sum("total"), trend="created_at")],
            baseline=Order.objects.all(),
        )
        trend = result[0].trend
        assert result[0].value == 30.0
        assert trend.previous == 30.0
        assert trend.delta == 0
        assert trend.direction == "flat"
        assert trend.percent == 0.0

    def test_a_stat_filter_survives_into_the_previous_period(self, orders):
        """The window is folded into the aggregate's own FILTER clause, not replacing it."""
        recent = orders.filter(created_at__gte=BASE - dt.timedelta(days=10, hours=12))
        result = compute_stats(
            recent,
            [Stat("Refunded", Count("id", filter=Q(status="refunded")), trend="created_at")],
            baseline=Order.objects.all(),
        )
        assert result[0].value == 5
        assert result[0].trend.previous == 0

    def test_percent_is_none_against_a_zero_baseline(self, orders):
        result = compute_stats(orders, [Stat("Revenue", Sum("total"), trend="created_at")])
        assert result[0].trend.previous == 0
        assert result[0].trend.percent is None
        assert result[0].trend.percent_display is None


# ── the callable escape hatch ───────────────────────────────────────────────


@pytest.mark.django_db
class TestCallableStat:
    def test_a_callable_receives_the_filtered_queryset(self, orders):
        result = compute_stats(
            orders.filter(status="paid"),
            [Stat("Conversion", lambda qs: qs.count() / 10, format="percent")],
        )
        assert result[0].value == 0.5
        assert result[0].display == "50.0%"

    def test_a_callable_can_trend(self, orders, django_assert_num_queries):
        recent = orders.filter(created_at__gte=BASE - dt.timedelta(days=5, hours=12))
        with django_assert_num_queries(3):
            result = compute_stats(
                recent,
                [Stat("Orders", lambda qs: qs.count(), trend="created_at")],
                baseline=Order.objects.all(),
            )
        assert result[0].value == 5
        assert result[0].trend.previous == 4
        assert result[0].trend.direction == "up"

    def test_a_failing_callable_does_not_take_the_page_down(self, orders):
        def broken(qs):
            raise RuntimeError("no such manager method")

        result = compute_stats(orders, [Stat("Broken", broken), Stat("Orders", Count("id"))])
        assert result[0].error == "no such manager method"
        assert result[0].display == "—"
        assert result[1].value == 10


# ── Django #10060: two joins in one aggregate ───────────────────────────────


@pytest.mark.django_db
class TestFanoutSafety:
    @pytest.fixture
    def author(self):
        author = make_author("Grace")
        for i in range(2):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="x", author=author)
        for day in range(1, 4):
            make_order(author, days_ago=day, total=10)
        return author

    def test_two_multi_valued_relations_do_not_multiply(self, author):
        """Chained annotation would report 6 and 6 here. One aggregate per join reports 2 and 3."""
        result = compute_stats(
            Author.objects.all(),
            [Stat("Posts", Count("posts")), Stat("Orders", Count("orders"))],
        )
        assert [s.value for s in result] == [2, 3]

    def test_each_join_costs_its_own_query_and_local_stats_share_one(
        self, author, django_assert_num_queries
    ):
        declared = [
            Stat("Authors", Count("id")),
            Stat("Active", Count("id", filter=Q(is_active=True))),
            Stat("Posts", Count("posts")),
            Stat("Orders", Count("orders")),
        ]
        with django_assert_num_queries(3):
            result = compute_stats(Author.objects.all(), declared)
        assert [s.value for s in result] == [1, 1, 2, 3]

    def test_one_stat_spanning_two_relations_is_refused_with_the_fix(self, author):
        with pytest.raises(ValueError, match="Subquery"):
            compute_stats(
                Author.objects.all(),
                [Stat("Bad", Count("posts", filter=Q(orders__status="paid")))],
            )


# ── charts ──────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTimeseries:
    def test_one_grouped_query_plus_one_shared_span_probe(
        self, orders, django_assert_num_queries
    ):
        charts = [
            Chart.timeseries("Revenue", date="created_at", value=Sum("total")),
            Chart.timeseries("Volume", date="created_at", value=Count("id")),
        ]
        with django_assert_num_queries(3):
            result = compute_charts(orders, charts)
        assert len(result) == 2

    def test_a_pinned_bucket_skips_the_probe(self, orders, django_assert_num_queries):
        with django_assert_num_queries(1):
            compute_charts(
                orders,
                [Chart.timeseries("Revenue", date="created_at", value=Sum("total"),
                                  bucket="month")],
            )

    def test_a_short_span_buckets_by_day(self, orders):
        result = compute_charts(
            orders, [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))]
        )
        chart = result[0]
        assert chart.bucket == "day"
        assert len(chart.labels) == 10
        assert chart.labels[0] == "2026-06-05"
        assert chart.series == [{"name": "Revenue", "data": [50.0] * 5 + [100.0] * 5}]

    def test_a_long_span_buckets_by_week_then_month(self, db):
        author = make_author("Span")
        for day in (0, 200):
            make_order(author, days_ago=day, total=10)
        weekly = compute_charts(
            Order.objects.all(),
            [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))],
        )
        assert weekly[0].bucket == "week"

        make_order(author, days_ago=1500, total=10)
        monthly = compute_charts(
            Order.objects.all(),
            [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))],
        )
        assert monthly[0].bucket == "month"

    def test_gaps_are_filled_with_zero(self, db):
        author = make_author("Gappy")
        make_order(author, days_ago=1, total=10)
        make_order(author, days_ago=4, total=20)
        chart = compute_charts(
            Order.objects.all(),
            [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))],
        )[0]
        assert chart.labels == ["2026-06-11", "2026-06-12", "2026-06-13", "2026-06-14"]
        assert chart.series[0]["data"] == [20.0, 0, 0, 10.0]

    def test_an_empty_queryset_yields_an_empty_series(self, orders):
        chart = compute_charts(
            orders.filter(status="shipped"),
            [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))],
        )[0]
        assert chart.empty is True
        assert chart.labels == []
        assert chart.series == [{"name": "Revenue", "data": []}]

    def test_charts_follow_the_filter(self, orders):
        chart = compute_charts(
            orders.filter(status="refunded"),
            [Chart.timeseries("Revenue", date="created_at", value=Sum("total"))],
        )[0]
        assert sum(chart.series[0]["data"]) == 250.0


@pytest.mark.django_db
class TestBreakdown:
    def test_one_query_grouped_and_labelled_from_choices(self, orders, django_assert_num_queries):
        with django_assert_num_queries(1):
            chart = compute_charts(
                orders,
                [Chart.breakdown("By status", group_by="status", value=Count("id"),
                                 kind="donut")],
            )[0]
        assert chart.kind == "donut"
        assert chart.labels == ["Paid", "Refunded"]
        assert chart.series[0]["data"] == [5, 5]
        assert chart.truncated is False

    def test_segments_are_capped_and_the_rest_become_other(self, db):
        for i in range(25):
            author = make_author(f"A{i:02d}")
            for _ in range(25 - i):
                make_order(author, days_ago=1, total=1)
        chart = compute_charts(
            Order.objects.all(),
            [Chart.breakdown("By customer", group_by="customer", value=Count("id"))],
        )[0]
        assert len(chart.labels) == 21
        assert chart.labels[-1] == "Other"
        # Authors 20..24 contribute 5 + 4 + 3 + 2 + 1 orders.
        assert chart.series[0]["data"][-1] == 15
        assert chart.truncated is True

    def test_a_non_additive_aggregate_gets_no_other_bucket(self, db):
        for i in range(22):
            author = make_author(f"B{i:02d}")
            make_order(author, days_ago=1, total=i + 1)
        chart = compute_charts(
            Order.objects.all(),
            [Chart.breakdown("Avg by customer", group_by="customer", value=Avg("total"))],
        )[0]
        assert "Other" not in chart.labels
        assert len(chart.labels) == 20
        assert chart.truncated is True

    def test_a_unique_field_is_refused(self, orders):
        chart = compute_charts(
            orders, [Chart.breakdown("By number", group_by="number", value=Count("id"))]
        )[0]
        assert "unique" in chart.error
        assert chart.labels == []

    def test_high_cardinality_is_refused(self, orders, monkeypatch):
        monkeypatch.setattr(stats_module, "MAX_GROUPS_SCANNED", 1)
        chart = compute_charts(
            orders, [Chart.breakdown("By status", group_by="status", value=Count("id"))]
        )[0]
        assert "distinct values" in chart.error

    def test_high_cardinality_can_be_forced(self, orders, monkeypatch):
        monkeypatch.setattr(stats_module, "MAX_GROUPS_SCANNED", 1)
        chart = compute_charts(
            orders,
            [Chart.breakdown("By status", group_by="status", value=Count("id"),
                             allow_high_cardinality=True)],
        )[0]
        assert chart.error is None

    def test_nulls_get_a_label_rather_than_disappearing(self, db):
        author = make_author("Null")
        Post.objects.create(title="T", slug="t", body="x", author=author, category=None)
        chart = compute_charts(
            Post.objects.all(),
            [Chart.breakdown("By category", group_by="category", value=Count("id"))],
        )[0]
        assert chart.labels == ["—"]

    def test_a_bad_field_reports_an_error_instead_of_crashing(self, orders):
        chart = compute_charts(
            orders, [Chart.breakdown("Nope", group_by="nonexistent", value=Count("id"))]
        )[0]
        assert chart.error is not None
        assert chart.empty is True


# ── the cost budget ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestBudget:
    def test_over_budget_defers_without_running_anything(
        self, orders, django_assert_num_queries
    ):
        declared = [Stat("Revenue", Sum("total"), trend="created_at")]
        with django_assert_num_queries(0):
            result = compute_stats(orders, declared, budget=Budget(max_queries=1))
        assert result.deferred is True
        assert "exceeds the budget" in result.reason
        assert list(result) == []

    def test_disabled_defers_everything(self, orders, django_assert_num_queries):
        with django_assert_num_queries(0):
            panel = compute_panel(
                orders,
                stats=[Stat("Orders", Count("id"))],
                budget=Budget(enabled=False),
            )
        assert panel["deferred"] is True
        assert panel["reason"] == "disabled"

    def test_a_row_estimate_over_the_cap_defers(self, orders, django_assert_num_queries):
        with django_assert_num_queries(0):
            result = compute_stats(
                orders,
                [Stat("Orders", Count("id"))],
                budget=Budget(max_rows=1000),
                estimated_rows=2_000_000,
            )
        assert result.deferred is True
        assert "rows exceeds" in result.reason

    def test_the_budget_covers_stats_and_charts_together(
        self, orders, django_assert_num_queries
    ):
        """Splitting the cost across two declarations must not slip past the cap."""
        with django_assert_num_queries(0):
            panel = compute_panel(
                orders,
                stats=[Stat("Orders", Count("id"))],
                charts=[Chart.breakdown("By status", group_by="status", value=Count("id"))],
                budget=Budget(max_queries=1),
            )
        assert panel["deferred"] is True

    def test_within_budget_computes(self, orders):
        panel = compute_panel(
            orders,
            stats=[Stat("Orders", Count("id"))],
            charts=[Chart.breakdown("By status", group_by="status", value=Count("id"))],
            budget=Budget(max_queries=2),
        )
        assert panel["deferred"] is False
        assert panel["stats"][0]["value"] == 10
        assert len(panel["charts"][0]["labels"]) == 2


# ── declaration ergonomics ──────────────────────────────────────────────────


class TestDeclaration:
    def test_keys_are_derived_from_labels(self):
        assert Stat("Avg order value", Count("id")).key == "avg-order-value"
        assert Chart.breakdown("By status", group_by="s", value=Count("id")).key == "by-status"

    def test_an_explicit_key_wins(self):
        assert Stat("Revenue", Count("id"), key="rev").key == "rev"

    def test_an_unknown_bucket_is_rejected(self):
        with pytest.raises(ValueError, match="bucket"):
            Chart.timeseries("X", date="created_at", value=Count("id"), bucket="fortnight")

    def test_tone_passes_through_untouched(self):
        """Presentation is the template's job; this module never picks a colour."""
        stat = Stat("Refunded", Count("id"), tone="destructive")
        assert stat.tone == "destructive"

    def test_nothing_declared_costs_nothing(self, db, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert compute_panel(Order.objects.all()) == {
                "stats": [],
                "charts": [],
                "deferred": False,
                "reason": None,
            }
