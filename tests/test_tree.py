"""Hierarchy rendering on the list page.

The interesting cases are all about when NOT to draw a tree: a tree built from a
subset shows children whose parents are absent, which reads as corruption rather than
as a filtered view.
"""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Category

LIST = "vantage:testapp-category-list"


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def tree(db):
    """Two roots, one with children and a grandchild."""
    food = Category.objects.create(name="Food", slug="food")
    fruit = Category.objects.create(name="Fruit", slug="fruit", parent=food)
    Category.objects.create(name="Apples", slug="apples", parent=fruit)
    Category.objects.create(name="Drink", slug="drink")
    return food


def depths(response):
    return {row["cells"][0]["value"]: row.get("depth") for row in response.context["rows"]}


class TestDetection:
    def test_a_self_referential_model_is_detected_as_a_tree(self, boss, tree):
        assert boss.get(reverse(LIST)).context["patterns"].tree is not None

    def test_tree_mode_is_on_by_default_for_such_a_model(self, boss, tree):
        assert boss.get(reverse(LIST)).context["showing_tree"] is True

    def test_a_flat_model_never_enters_tree_mode(self, boss, db):
        assert boss.get(reverse("vantage:testapp-author-list")).context["showing_tree"] is False


class TestIndentation:
    def test_depth_reflects_the_hierarchy(self, boss, tree):
        result = depths(boss.get(reverse(LIST)))
        assert result["Food"] == 0
        assert result["Fruit"] == 1
        assert result["Apples"] == 2

    def test_a_second_root_is_also_at_zero(self, boss, tree):
        assert depths(boss.get(reverse(LIST)))["Drink"] == 0

    def test_depth_costs_no_extra_queries(self, boss, tree, django_assert_num_queries):
        """Walking the parent ids already on the page, not a lookup per row."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as first:
            boss.get(reverse(LIST))

        root = Category.objects.get(slug="food")
        for i in range(30):
            Category.objects.create(name=f"Extra {i}", slug=f"x{i}", parent=root)

        with CaptureQueriesContext(connection) as second:
            boss.get(reverse(LIST))

        assert len(second) == len(first)

    def test_a_cycle_cannot_hang_the_page(self, boss, db):
        """Bad data should render oddly, not spin forever."""
        a = Category.objects.create(name="A", slug="a")
        b = Category.objects.create(name="B", slug="b", parent=a)
        a.parent = b
        a.save()
        assert boss.get(reverse(LIST)).status_code == 200


class TestWhenNotToDrawATree:
    def test_searching_falls_back_to_a_flat_list(self, boss, tree):
        """A tree of search hits shows children whose parents are missing."""
        assert boss.get(reverse(LIST), {"q": "Fruit"}).context["showing_tree"] is False

    def test_sorting_falls_back_to_a_flat_list(self, boss, tree):
        assert boss.get(reverse(LIST), {"sort": "name"}).context["showing_tree"] is False

    def test_filtering_falls_back_to_a_flat_list(self, boss, tree):
        root = Category.objects.get(slug="food")
        response = boss.get(reverse(LIST), {"parent": str(root.pk)})
        assert response.context["showing_tree"] is False

    def test_an_explicit_flat_request_is_honoured(self, boss, tree):
        assert boss.get(reverse(LIST), {"flat": "1"}).context["showing_tree"] is False

    def test_paging_does_not_leave_tree_mode(self, boss, tree):
        """Page two of a hierarchy is still a hierarchy."""
        assert boss.get(reverse(LIST), {"page": "1"}).context["showing_tree"] is True


class TestOrdering:
    def test_the_tree_supplies_its_own_order(self, boss, tree):
        names = [row["cells"][0]["value"] for row in boss.get(reverse(LIST)).context["rows"]]
        assert names.index("Food") < names.index("Fruit") < names.index("Apples")

    def test_an_explicit_sort_wins_over_the_tree_order(self, boss, tree):
        """Sorting is a deliberate request to see the rows flat."""
        response = boss.get(reverse(LIST), {"sort": "-name"})
        names = [row["cells"][0]["value"] for row in response.context["rows"]]
        assert names == sorted(names, reverse=True)
