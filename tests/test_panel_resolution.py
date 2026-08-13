"""Per-attribute resolution: overriding one thing must not disable the rest.

This is the package's most important ergonomic guarantee, so it is tested directly
rather than left as an emergent property of the getters.
"""

import pytest
from django.test import override_settings

from tests.testapp.models import Author, Order, Post
from vantage.panels import ModelPanel


def panel(model=Post, **attrs):
    return type("P", (ModelPanel,), attrs)(model)


class TestPartialOverrideKeepsInference:
    def test_setting_list_display_leaves_filters_inferred(self):
        p = panel(list_display=["title"])
        assert p.get_list_display() == ["title"]
        assert p.get_list_filter() == panel().get_list_filter()
        assert p.get_list_filter() != []

    def test_setting_list_display_leaves_search_inferred(self):
        p = panel(list_display=["title"])
        assert p.get_list_search() == panel().get_list_search()

    def test_setting_list_filter_leaves_columns_inferred(self):
        p = panel(list_filter=["status"])
        assert p.get_list_filter() == ["status"]
        assert p.get_list_display() == panel().get_list_display()

    def test_setting_ordering_leaves_everything_else_inferred(self):
        p = panel(ordering=["title"])
        assert p.get_ordering() == ["title"]
        assert p.get_list_display() == panel().get_list_display()
        assert p.get_list_search() == panel().get_list_search()

    def test_an_empty_list_is_an_override_not_a_fallback(self):
        """`list_filter = []` means 'no filters', not 'infer them for me'."""
        assert panel(list_filter=[]).get_list_filter() == []

    def test_none_means_infer(self):
        assert panel(list_filter=None).get_list_filter() != []


class TestListLink:
    def test_inferred_when_unset(self):
        assert panel().get_list_link() == "title"

    def test_explicit_wins(self):
        assert panel(list_display=["title", "slug"], list_link="slug").get_list_link() == "slug"

    def test_falls_back_to_the_first_declared_column(self):
        """The user removed the column inference would have linked, so link what is there."""
        assert panel(list_display=["status", "author"]).get_list_link() == "status"

    def test_uses_the_inferred_link_when_it_survives_in_list_display(self):
        assert panel(list_display=["status", "title"]).get_list_link() == "title"


class TestPerPage:
    def test_defaults_to_the_global_setting(self):
        assert panel().get_per_page() == 50

    @override_settings(VANTAGE={"DEFAULT_PER_PAGE": 25})
    def test_follows_the_global_setting(self):
        assert panel().get_per_page() == 25

    @override_settings(VANTAGE={"DEFAULT_PER_PAGE": 25})
    def test_panel_overrides_the_global_setting(self):
        """Narrowest scope wins."""
        assert panel(list_per_page=10).get_per_page() == 10


class TestSearchFieldsAlias:
    def test_contrib_admin_name_is_accepted(self):
        assert panel(search_fields=["title"]).get_list_search() == ["title"]

    def test_list_search_wins_when_both_are_given(self):
        p = panel(search_fields=["title"], list_search=["slug"])
        assert p.get_list_search() == ["slug"]


@pytest.mark.django_db
class TestListQueryset:
    def test_applies_inferred_select_related(self):
        qs = panel(Post).get_list_queryset(None)
        assert qs.query.select_related == {"author": {}, "category": {}}

    def test_applies_inferred_ordering(self):
        qs = panel(Post).get_list_queryset(None)
        assert qs.query.order_by == ("-created_at",)

    def test_a_custom_column_changes_the_optimisation(self):
        """Optimisation follows what the page renders, so it cannot drift out of date."""
        qs = panel(Order, list_display=["number", "customer"]).get_list_queryset(None)
        assert "customer" in qs.query.select_related

    def test_no_relations_means_no_join(self):
        qs = panel(Author, list_display=["name"], list_search=["name"]).get_list_queryset(None)
        assert qs.query.select_related is False

    def test_respects_a_get_queryset_override(self):
        class Scoped(ModelPanel):
            def get_queryset(self, request):
                return Post.objects.filter(status="published")

        qs = Scoped(Post).get_list_queryset(None)
        assert "published" in str(qs.query)
