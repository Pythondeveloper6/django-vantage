"""The filter rail and the analytics strip, as rendered HTML.

These are UI tests, so they assert markup: a class name that the stylesheet depends on,
an ``href`` that has to keep carrying the search, an ``svg`` that has to exist because
no JavaScript is going to draw one. The engines underneath have their own suites in
``test_filters.py`` and ``test_stats.py``; nothing here re-tests arithmetic.

The rail is deliberately exercised through the real list view rather than by rendering
the partial: the property under test is that a plain GET form round-trips through the
query string, and only a real request can show that.
"""

import pytest
from django.contrib.auth.models import User
from django.db.models import Count, Sum
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from tests.testapp.models import Author, Category, Post
from vantage import site
from vantage.panels import ModelPanel
from vantage.stats import Chart, ChartResult, Stat, StatResult, Trend
from vantage.views import ListView

LIST_URL = "vantage:testapp-post-list"


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("railer", "rail@example.com", "x")


@pytest.fixture
def client_in(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def posts(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    category = Category.objects.create(name="Essays", slug="essays")
    rows = []
    for index, status in enumerate(
        [Post.Status.DRAFT, Post.Status.DRAFT, Post.Status.REVIEW, Post.Status.PUBLISHED]
    ):
        rows.append(
            Post.objects.create(
                title=f"Post {index}",
                slug=f"post-{index}",
                body="Body",
                status=status,
                author=author,
                category=category,
                view_count=index,
            )
        )
    return rows


def body_of(client, **params) -> str:
    return client.get(reverse(LIST_URL), params).content.decode()


# ── the rail ────────────────────────────────────────────────────────────────


class TestFilterRail:
    def test_the_rail_renders_with_a_fieldset_per_filter(self, client_in, posts):
        body = body_of(client_in)
        assert 'class="v-filters"' in body
        # status, featured, published_at, author, category — all inferred.
        assert body.count("v-filter-legend") == 5

    def test_choices_render_as_options(self, client_in, posts):
        body = body_of(client_in)
        assert 'value="draft"' in body
        assert "In review" in body
        assert "Published" in body

    def test_a_multi_select_filter_renders_checkboxes(self, client_in, posts):
        body = body_of(client_in)
        assert '<input type="checkbox" name="status"' in body

    def test_a_single_select_filter_renders_radios_including_a_way_out(self, client_in, posts):
        """A radio group without an "Any" option is a one-way door."""
        body = body_of(client_in)
        assert '<input type="radio" name="featured"' in body
        assert '<input type="radio" name="featured" value=""' in body

    def test_a_related_filter_lists_its_objects(self, client_in, posts):
        assert "Ada Lovelace" in body_of(client_in)

    def test_the_form_is_a_plain_get_submission(self, client_in, posts):
        """No JavaScript is loaded for filtering, so the form has to stand alone."""
        body = body_of(client_in)
        assert 'class="v-filter-form" method="get"' in body
        assert 'type="submit">Apply filters' in body

    def test_the_form_carries_the_search_and_sort_forward(self, client_in, posts):
        """Submitting replaces the whole query string; anything not restated is lost."""
        body = body_of(client_in, q="Post", sort="-title")
        assert '<input type="hidden" name="q" value="Post">' in body
        assert '<input type="hidden" name="sort" value="-title">' in body

    def test_the_form_does_not_carry_the_page_number(self, client_in, posts):
        """Changing the predicate invalidates which page you were on."""
        body = body_of(client_in, page="1")
        assert 'name="page"' not in body

    def test_the_search_box_carries_the_filters_back(self, client_in, posts):
        """The rail restates the search; the search has to restate the rail."""
        params = {"status": ["draft", "review"], "sort": "title"}
        body = client_in.get(reverse(LIST_URL), params).content.decode()
        # Anchored on the list's own form: the header palette is also a search
        # landmark and appears earlier in the document.
        start = body.index('id="v-list-search"')
        search_form = body[start : body.index("</form>", start)]
        assert '<input type="hidden" name="status" value="draft">' in search_form
        assert '<input type="hidden" name="status" value="review">' in search_form
        assert '<input type="hidden" name="sort" value="title">' in search_form

    def test_no_filters_means_no_rail_and_no_dead_column(self, client_in, db):
        """Tag has nothing filterable; the table should get the whole width."""
        body = client_in.get(reverse("vantage:testapp-tag-list")).content.decode()
        assert 'class="v-filters"' not in body
        assert "v-list-layout-bare" in body


# ── counts are opt-in ───────────────────────────────────────────────────────


class TestFacetCounts:
    def test_counts_are_absent_by_default(self, client_in, posts):
        assert "v-filter-count" not in body_of(client_in)

    def test_counts_appear_when_asked_for(self, client_in, posts):
        body = body_of(client_in, _facets="1")
        assert "v-filter-count" in body
        assert '<span class="v-filter-count">2</span>' in body  # two drafts

    def test_a_zero_count_still_renders(self, client_in, posts):
        """`count is not None` — a bucket that is genuinely empty must say so."""
        Post.objects.filter(status=Post.Status.PUBLISHED).delete()
        assert '<span class="v-filter-count">0</span>' in body_of(client_in, _facets="1")

    def test_the_toggle_offers_counts_and_then_offers_to_hide_them(self, client_in, posts):
        assert "Show counts" in body_of(client_in)
        assert "_facets=1" in body_of(client_in)
        assert "Hide counts" in body_of(client_in, _facets="1")

    def test_the_toggle_survives_a_submission(self, client_in, posts):
        assert '<input type="hidden" name="_facets" value="1">' in body_of(client_in, _facets="1")


# ── chips ───────────────────────────────────────────────────────────────────


class TestActiveFilterChips:
    def test_a_selected_value_shows_a_chip(self, client_in, posts):
        body = body_of(client_in, status="draft")
        assert 'class="v-chip"' in body
        assert "Status:" in body
        assert "Draft" in body

    def test_nothing_selected_means_no_chips(self, client_in, posts):
        assert 'class="v-chip"' not in body_of(client_in)

    def test_a_chip_removes_only_its_own_value(self, client_in, posts):
        """OR within a filter: dropping "draft" must not drop "review" with it."""
        response = client_in.get(reverse(LIST_URL), {"status": ["draft", "review"]})
        body = response.content.decode()
        assert body.count('class="v-chip"') == 2
        assert 'href="?status=review"' in body
        assert 'href="?status=draft"' in body

    def test_a_chip_keeps_the_search_and_the_sort(self, client_in, posts):
        body = body_of(client_in, status="draft", q="Post", sort="title")
        assert "q=Post" in body
        assert "sort=title" in body

    def test_the_selected_choice_is_checked_in_the_rail(self, client_in, posts):
        body = body_of(client_in, status="draft")
        assert '<input type="checkbox" name="status" value="draft"\n' in body or (
            'value="draft"' in body and "checked" in body
        )


# ── clearing ────────────────────────────────────────────────────────────────


class TestClearing:
    def test_clear_all_appears_only_when_something_is_active(self, client_in, posts):
        assert "Clear all filters" not in body_of(client_in)
        assert "Clear all filters" in body_of(client_in, status="draft")

    def test_clear_all_drops_every_filter_and_keeps_the_search(self, client_in, posts):
        body = body_of(client_in, status="draft", featured="1", q="Post", sort="title")
        assert 'href="?q=Post&amp;sort=title">Clear all filters</a>' in body

    def test_following_the_clear_link_restores_the_full_list(self, client_in, posts):
        filtered = client_in.get(reverse(LIST_URL), {"status": "published"})
        assert "Post 0" not in filtered.content.decode()
        assert "Post 3" in filtered.content.decode()
        restored = client_in.get(reverse(LIST_URL), {"q": ""}).content.decode()
        assert "Post 0" in restored
        assert 'class="v-chip"' not in restored


# ── filtering actually filters ──────────────────────────────────────────────


class TestFilteringEndToEnd:
    def test_a_filter_narrows_the_rows(self, client_in, posts):
        body = body_of(client_in, status="published")
        assert "Post 3" in body
        assert "Post 2" not in body

    def test_filtering_composes_with_search_and_sort(self, client_in, posts):
        """The three states are orthogonal; a page that loses one on the others is broken."""
        body = body_of(client_in, status="draft", q="Post", sort="title")
        assert "Post 0" in body
        assert "Post 3" not in body
        assert 'value="Post"' in body  # the search box still holds the query
        assert 'aria-sort="ascending"' in body

    def test_a_filter_that_matches_nothing_gets_the_second_empty_state(self, client_in, posts):
        """Decision 15 state 2: no create CTA, and the way out is removing a filter."""
        Post.objects.filter(status=Post.Status.PUBLISHED).delete()
        body = body_of(client_in, status="published")
        assert "match these filters" in body
        assert "No posts yet" not in body
        assert 'class="v-chip"' in body


# ── analytics strip ─────────────────────────────────────────────────────────


def render_analytics(analytics: dict) -> str:
    request = RequestFactory().get("/admin/testapp/post/")
    return render_to_string(
        "vantage/partials/analytics.html", {"analytics": analytics, "request": request}
    )


def stat(**kwargs) -> dict:
    defaults = {
        "key": "revenue",
        "label": "Revenue",
        "value": 1200,
        "display": "$1,200.00",
        "format": "currency",
    }
    return StatResult(**{**defaults, **kwargs}).as_dict()


class TestStatStrip:
    def test_a_stat_renders_its_display_and_label(self):
        body = render_analytics({"stats": [stat()], "charts": [], "deferred": False})
        assert 'class="v-stat"' in body
        assert "$1,200.00" in body
        assert "Revenue" in body

    def test_a_trend_becomes_a_direction_carrying_delta_chip(self):
        trend = Trend(
            previous=1000,
            delta=200,
            percent=0.2,
            direction="up",
            delta_display="+$200.00",
            percent_display="+20.0%",
        )
        body = render_analytics({"stats": [stat(trend=trend)], "charts": [], "deferred": False})
        assert 'class="v-stat-delta" data-direction="up"' in body
        assert "+$200.00" in body
        assert "+20.0%" in body

    def test_a_falling_trend_says_so_in_the_same_attribute(self):
        trend = Trend(
            previous=1000,
            delta=-200,
            percent=-0.2,
            direction="down",
            delta_display="-$200.00",
            percent_display="-20.0%",
        )
        body = render_analytics({"stats": [stat(trend=trend)], "charts": [], "deferred": False})
        assert 'data-direction="down"' in body

    def test_tone_reaches_the_card_as_data_not_as_colour(self):
        body = render_analytics(
            {"stats": [stat(tone="destructive")], "charts": [], "deferred": False}
        )
        assert 'data-tone="destructive"' in body

    def test_one_failed_stat_does_not_take_the_strip_down(self):
        """Decision 15's sixth state, applied per card."""
        body = render_analytics(
            {
                "stats": [stat(error="boom", display="—"), stat(key="orders", label="Orders")],
                "charts": [],
                "deferred": False,
            }
        )
        assert 'data-error="true"' in body
        assert "Could not be computed" in body
        assert "Orders" in body
        assert "$1,200.00" in body

    def test_the_strip_is_capped_at_one_row(self):
        """Decision 1: pinned cards are opt-in chrome, and one row of them is the cap."""
        stats = [stat(key=f"s{i}", label=f"Stat {i}") for i in range(6)]
        body = render_analytics({"stats": stats, "charts": [], "deferred": False})
        assert body.count('class="v-stat"') == 4
        assert "v-stats-row" in body

    def test_a_deferred_panel_offers_the_work_instead_of_doing_it(self):
        body = render_analytics(
            {"stats": [], "charts": [], "deferred": True, "reason": "12 queries exceeds the budget"}
        )
        assert "Compute" in body
        assert "12 queries exceeds the budget" in body
        assert 'class="v-stat"' not in body


# ── charts ──────────────────────────────────────────────────────────────────


def chart(**kwargs) -> dict:
    defaults = {
        "key": "revenue-over-time",
        "label": "Revenue over time",
        "kind": "line",
        "labels": ["2026-01-01", "2026-01-02", "2026-01-03"],
        "series": [{"name": "Revenue", "data": [10, 40, 25]}],
        "bucket": "day",
        "empty": False,
    }
    return ChartResult(**{**defaults, **kwargs}).as_dict()


class TestCharts:
    def test_a_line_chart_renders_as_inline_svg(self):
        body = render_analytics({"stats": [], "charts": [chart()], "deferred": False})
        assert "<svg" in body
        assert "<polyline" in body
        assert 'viewBox="0 0 320 96"' in body
        assert "chart.js" not in body.lower()

    def test_a_chart_is_readable_without_seeing_it(self):
        body = render_analytics({"stats": [], "charts": [chart()], "deferred": False})
        assert 'role="img"' in body
        assert 'aria-label="Revenue over time: line chart of 3 days' in body
        assert "<title>Revenue over time</title>" in body

    def test_a_chart_takes_its_colour_from_tokens_only(self):
        """A hard-coded hex would survive neither the dark theme nor BRAND_COLOR."""
        body = render_analytics({"stats": [], "charts": [chart()], "deferred": False})
        svg = body[body.index("<svg") : body.index("</svg>")]
        assert "var(--primary)" in svg
        assert "currentColor" in svg
        assert "#" not in svg

    def test_a_bar_chart_renders_rectangles(self):
        body = render_analytics(
            {
                "stats": [],
                "charts": [
                    chart(
                        kind="bar",
                        bucket=None,
                        labels=["Draft", "Review"],
                        series=[{"name": "Posts", "data": [7, 3]}],
                    )
                ],
                "deferred": False,
            }
        )
        assert body.count("<rect") == 2
        assert "v-chart-legend" in body

    def test_a_donut_renders_one_arc_per_segment(self):
        body = render_analytics(
            {
                "stats": [],
                "charts": [
                    chart(
                        kind="donut",
                        bucket=None,
                        labels=["Paid", "Refunded"],
                        series=[{"name": "Orders", "data": [75, 25]}],
                    )
                ],
                "deferred": False,
            }
        )
        assert "v-chart-donut" in body
        assert 'stroke-dasharray="75 25"' in body
        assert 'stroke-dasharray="25 75"' in body

    def test_an_empty_chart_says_so_rather_than_drawing_nothing(self):
        body = render_analytics(
            {"stats": [], "charts": [chart(labels=[], series=[], empty=True)], "deferred": False}
        )
        assert "No data in range." in body
        assert "<svg" not in body

    def test_a_truncated_chart_admits_it(self):
        body = render_analytics({"stats": [], "charts": [chart(truncated=True)], "deferred": False})
        assert "Showing part of the data" in body

    def test_a_failed_chart_renders_its_error_and_no_svg(self):
        body = render_analytics(
            {
                "stats": [],
                "charts": [chart(error="'slug' is unique: one segment per row is not a chart.")],
                "deferred": False,
            }
        )
        assert "one segment per row is not a chart" in body
        assert "<svg" not in body
        assert "v-chart-failed" in body


# ── the strip on a real page ────────────────────────────────────────────────


class StatPostPanel(ModelPanel):
    """A panel that opts into the strip, since the test app's models do not."""

    list_stats = [Stat("Posts", Count("id")), Stat("Views", Sum("view_count"))]
    list_charts = [Chart.breakdown("By status", group_by="status", value=Count("id"))]


class TestStripOnTheListPage:
    def response(self, staff):
        request = RequestFactory().get("/admin/testapp/post/")
        request.user = staff
        view = ListView.as_view(site=site, panel=StatPostPanel(Post, site))
        return view(request).render()

    def test_the_strip_renders_above_the_table(self, staff, posts):
        body = self.response(staff).content.decode()
        assert body.index("v-stats") < body.index("v-table-wrap")
        assert "Posts" in body
        assert "v-chart-svg" in body

    def test_the_numbers_follow_the_filters(self, staff, posts):
        """Stats come from the filter predicate, never from the rendered rows."""
        request = RequestFactory().get("/admin/testapp/post/", {"status": "draft"})
        request.user = staff
        view = ListView.as_view(site=site, panel=StatPostPanel(Post, site))
        body = view(request).render().content.decode()
        assert '<div class="v-stat-value">2</div>' in body
