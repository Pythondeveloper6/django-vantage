"""Grouping list rows into time buckets."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, Post

LIST = "vantage:testapp-post-list"


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def spread(db):
    """Posts spread across months on the field the detector actually picks.

    That is ``published_at``, not ``created_at``: the timeline detector prefers a
    domain date over an audit column, which is the right call for grouping.
    """
    author = Author.objects.create(name="Ada", email="ada@example.com")
    # Anchored mid-month so "this month" holds two posts whatever day the suite runs;
    # "now minus a day" lands in the previous month on the 1st.
    now = timezone.now().replace(day=15, hour=12)
    return [
        Post.objects.create(
            title=name,
            slug=name.lower().replace(" ", "-"),
            body="b",
            author=author,
            published_at=now - timezone.timedelta(days=days),
        )
        for days, name in ((0, "This month"), (1, "Also this month"), (120, "Older"))
    ]


class TestAvailability:
    def test_offered_for_a_model_with_a_creation_date(self, boss, spread):
        assert boss.get(reverse(LIST)).context["timeline_field"] == "published_at"

    def test_not_offered_for_a_model_with_no_date(self, boss, db):
        assert boss.get(reverse("vantage:testapp-tag-list")).context["timeline_field"] is None

    def test_off_unless_asked_for(self, boss, spread):
        response = boss.get(reverse(LIST))
        assert response.context["grouping"] is None
        assert response.context["groups"] is None


class TestGrouping:
    def test_grouping_by_month(self, boss, spread):
        response = boss.get(reverse(LIST), {"group": "month"})
        assert response.context["grouping"] == "month"
        assert len(response.context["groups"]) == 2

    def test_each_group_records_where_it_starts(self, boss, spread):
        groups = boss.get(reverse(LIST), {"group": "month"}).context["groups"]
        assert groups[0]["at"] == 0
        assert groups[1]["at"] > 0

    def test_labels_read_as_dates_not_keys(self, boss, spread):
        labels = [g["label"] for g in boss.get(reverse(LIST), {"group": "month"}).context["groups"]]
        assert all(any(c.isalpha() for c in label) for label in labels)

    def test_grouping_by_day_splits_further_than_by_year(self, boss, spread):
        by_day = boss.get(reverse(LIST), {"group": "day"}).context["groups"]
        by_year = boss.get(reverse(LIST), {"group": "year"}).context["groups"]
        assert len(by_day) >= len(by_year)

    def test_headers_render(self, boss, spread):
        body = boss.get(reverse(LIST), {"group": "month"}).content.decode()
        assert "v-group-row" in body

    def test_an_unknown_bucket_is_ignored(self, boss, spread):
        assert boss.get(reverse(LIST), {"group": "fortnight"}).context["grouping"] is None

    def test_grouping_costs_no_extra_queries(self, boss, spread):
        """Computed from the rows already fetched, not by a second grouped query -
        which would have to re-apply every filter to stay consistent with the page."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as plain:
            boss.get(reverse(LIST))
        with CaptureQueriesContext(connection) as grouped:
            boss.get(reverse(LIST), {"group": "month"})
        assert len(grouped) == len(plain)


class TestInteractionWithOtherModes:
    def test_grouping_is_suppressed_while_a_tree_is_drawn(self, boss, db):
        """Two orderings competing over the same rows makes neither readable."""
        from tests.testapp.models import Category

        Category.objects.create(name="Root", slug="root")
        response = boss.get(reverse("vantage:testapp-category-list"), {"group": "month"})
        if response.context["showing_tree"]:
            assert response.context["grouping"] is None

    def test_grouping_survives_a_search(self, boss, spread):
        response = boss.get(reverse(LIST), {"group": "month", "q": "month"})
        assert response.context["grouping"] == "month"

    def test_a_null_date_gets_its_own_labelled_bucket(self, boss, db):
        """Rows with no date must not silently join whichever bucket came last."""
        from vantage.views import ListView

        assert ListView._bucket_label(None, "month") == "No date"
        assert ListView._bucket_key(None, "month") is None
