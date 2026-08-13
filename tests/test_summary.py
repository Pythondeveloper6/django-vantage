"""The bottom summary bar.

A wrong number in a footer is worse than a blank one, because the reader has no way to
tell it is wrong. Most of these tests are about what the bar declines to compute.
"""

import pytest
from django.contrib.auth.models import User
from django.db.models import Avg, Sum
from django.urls import reverse

from tests.testapp.models import Author, Comment, Order, Post
from vantage.panels import ModelPanel
from vantage.summary import compute_summary, infer_summaries


def panel(model, **attrs):
    return type("P", (ModelPanel,), attrs)(model)


@pytest.fixture
def orders(db):
    customer = Author.objects.create(name="Ada", email="ada@example.com")
    for i in range(4):
        Order.objects.create(
            number=f"ORD-{100 + i}",
            customer=customer,
            status="paid" if i % 2 else "pending",
            total=10 + i,
        )
    return Order.objects.all()


class TestInference:
    def test_money_is_summed(self):
        assert infer_summaries(panel(Order), ["total"]) == {"total": "sum"}

    def test_an_integer_column_is_summed(self):
        assert infer_summaries(panel(Post), ["view_count"]) == {"view_count": "sum"}

    def test_a_rating_is_averaged_not_summed(self):
        """Summing a score produces a number with no meaning."""
        assert infer_summaries(panel(Post), ["rating"]) == {"rating": "avg"}

    def test_an_identifier_stored_as_text_is_skipped(self):
        assert infer_summaries(panel(Order), ["number"]) == {}

    def test_the_primary_key_is_skipped(self):
        assert infer_summaries(panel(Post), ["id"]) == {}

    def test_non_numeric_columns_are_skipped(self):
        assert infer_summaries(panel(Post), ["title", "status", "created_at"]) == {}

    def test_a_relation_column_is_skipped(self):
        """Aggregating across a join multiplies values through the fan-out."""
        assert infer_summaries(panel(Post), ["author"]) == {}

    def test_a_traversed_path_is_skipped(self):
        assert infer_summaries(panel(Comment), ["post__view_count"]) == {}

    def test_mixed_columns_pick_out_only_the_summable_ones(self):
        result = infer_summaries(panel(Post), ["title", "view_count", "rating", "author"])
        assert result == {"view_count": "sum", "rating": "avg"}


class TestExplicitDeclaration:
    def test_a_named_function_wins(self):
        p = panel(Order, list_aggregates={"total": "avg"})
        assert infer_summaries(p, ["total"]) == {"total": "avg"}

    def test_an_aggregate_class_reads_naturally(self):
        """So it sits comfortably beside the list_stats declarations above it."""
        p = panel(Order, list_aggregates={"total": Avg})
        assert infer_summaries(p, ["total"]) == {"total": "avg"}

    def test_none_opts_a_column_out(self):
        p = panel(Order, list_aggregates={"total": None})
        assert infer_summaries(p, ["total"]) == {}

    def test_an_unknown_function_is_ignored_rather_than_raising(self):
        p = panel(Order, list_aggregates={"total": "median"})
        assert infer_summaries(p, ["total"]) == {}

    def test_a_column_not_declared_still_gets_its_default(self):
        p = panel(Post, list_aggregates={"rating": Sum})
        assert infer_summaries(p, ["rating", "view_count"]) == {
            "rating": "sum",
            "view_count": "sum",
        }


@pytest.mark.django_db
class TestComputation:
    def test_values_are_correct(self, orders):
        cells = compute_summary(Order.objects.all(), {"total": "sum"})
        assert cells[0].value == 46  # 10 + 11 + 12 + 13

    def test_the_whole_bar_costs_one_query(self, orders, django_assert_num_queries):
        """However many columns it covers."""
        with django_assert_num_queries(1):
            compute_summary(Order.objects.all(), {"total": "sum", "id": "count"})

    def test_no_summaries_means_no_query(self, orders, django_assert_num_queries):
        with django_assert_num_queries(0):
            assert compute_summary(Order.objects.all(), {}) == []

    def test_it_follows_the_queryset_it_is_given(self, orders):
        cells = compute_summary(Order.objects.filter(status="paid"), {"total": "sum"})
        assert cells[0].value == 24  # 11 + 13

    def test_an_empty_queryset_shows_a_dash_rather_than_none(self, db):
        cells = compute_summary(Order.objects.none(), {"total": "sum"})
        assert cells[0].value is None
        assert cells[0].display == "—"

    def test_whole_numbers_lose_the_decimals(self, orders):
        assert compute_summary(Order.objects.all(), {"total": "sum"})[0].display == "46"

    def test_fractions_keep_two_places(self, orders):
        assert compute_summary(Order.objects.all(), {"total": "avg"})[0].display == "11.50"

    def test_large_numbers_are_grouped(self, db):
        customer = Author.objects.create(name="A", email="a@example.com")
        Order.objects.create(number="X", customer=customer, total=1234567)
        assert compute_summary(Order.objects.all(), {"total": "sum"})[0].display == "1,234,567"

    def test_a_column_named_like_a_field_does_not_collide(self, orders):
        """Aliases are positional so a column name can never clash with the model."""
        cells = compute_summary(Order.objects.all(), {"total": "sum", "id": "max"})
        assert {c.column for c in cells} == {"total", "id"}


@pytest.mark.django_db
class TestThroughTheView:
    def test_the_bar_renders(self, client):
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        author = Author.objects.create(name="A", email="a@example.com")
        Post.objects.create(title="T", slug="t", body="b", author=author, view_count=5)
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "v-table-summary" in body

    def test_it_reflects_the_active_search(self, client, db):
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        author = Author.objects.create(name="A", email="a@example.com")
        Post.objects.create(title="Keep", slug="k", body="b", author=author, view_count=100)
        Post.objects.create(title="Drop", slug="d", body="b", author=author, view_count=7)
        response = client.get(reverse("vantage:testapp-post-list"), {"q": "Keep"})
        assert response.context["summary"]["view_count"].value == 100
