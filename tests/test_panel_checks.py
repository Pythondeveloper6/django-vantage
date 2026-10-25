"""Panel configuration errors must fail `manage.py check`, not a request."""

from tests.testapp.models import Post
from vantage.panels import ModelPanel


def ids(errors):
    return sorted(e.id for e in errors)


def panel(**attrs):
    cls = type("TestPanel", (ModelPanel,), attrs)
    return cls(Post)


class TestListDisplay:
    def test_valid_field_names_pass(self):
        assert panel(list_display=["title", "author", "status"]).check() == []

    def test_related_path_passes(self):
        assert panel(list_display=["title", "author__name"]).check() == []

    def test_unknown_name_is_an_error(self):
        assert ids(panel(list_display=["nope"]).check()) == ["vantage.E010"]

    def test_a_panel_method_is_acceptable(self):
        """list_display is rendered in Python, so a method is a legitimate column."""

        def byline(self, obj):
            return obj.title

        assert panel(list_display=["byline"], byline=byline).check() == []

    def test_a_model_property_is_acceptable(self):
        assert panel(list_display=["pk"]).check() == []

    def test_inference_default_is_not_checked(self):
        assert panel().check() == []


class TestFilterAndSearch:
    def test_valid_paths_pass(self):
        assert panel(list_filter=["status"], list_search=["title", "author__name"]).check() == []

    def test_a_panel_method_is_not_acceptable_for_a_filter(self):
        """Filters are resolved in SQL, so a Python method cannot serve."""

        def computed(self, obj):
            return 1

        errors = panel(list_filter=["computed"], computed=computed).check()
        assert ids(errors) == ["vantage.E011"]

    def test_bad_search_path(self):
        assert ids(panel(list_search=["nope"]).check()) == ["vantage.E012"]

    def test_search_prefix_operators_are_stripped_before_resolving(self):
        assert panel(list_search=["^title", "=slug", "@body"]).check() == []


class TestOrdering:
    def test_valid(self):
        assert panel(ordering=["-published_at", "title"]).check() == []

    def test_random_ordering_is_allowed(self):
        assert panel(ordering=["?"]).check() == []

    def test_bad_path(self):
        assert ids(panel(ordering=["-nope"]).check()) == ["vantage.E013"]


class TestListLink:
    def test_link_must_be_a_displayed_column(self):
        errors = panel(list_display=["title", "status"], list_link="slug").check()
        assert ids(errors) == ["vantage.E014"]

    def test_link_in_display_passes(self):
        assert panel(list_display=["title"], list_link="title").check() == []


class TestListEditable:
    def test_valid(self):
        assert panel(list_editable=["status"]).check() == []

    def test_unknown_field(self):
        assert ids(panel(list_editable=["nope"]).check()) == ["vantage.E015"]

    def test_a_column_cannot_be_both_editable_and_the_link(self):
        errors = panel(list_display=["title"], list_link="title", list_editable=["title"]).check()
        assert ids(errors) == ["vantage.E016"]


def test_multiple_problems_are_all_reported():
    errors = panel(list_display=["nope"], ordering=["-alsonope"]).check()
    assert ids(errors) == ["vantage.E010", "vantage.E013"]
