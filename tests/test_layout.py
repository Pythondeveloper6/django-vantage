"""The form layout DSL.

Most of these are about the two failure modes a layout can cause: silently dropping a
field the form needs, and naming a field that does not exist.
"""

import pytest

from tests.testapp.models import Post
from vantage.layout import Row, Section, Tab, Tabs, check_layout, normalise, resolve_layout
from vantage.panels import ModelPanel


def panel(**attrs):
    return type("P", (ModelPanel,), attrs)(Post)


class TestConstruction:
    def test_a_section_collects_its_fields(self):
        assert list(Section("a", "b").fields()) == ["a", "b"]

    def test_nesting_flattens_in_order(self):
        node = Section(Row("a", "b"), "c", Section("d"))
        assert list(node.fields()) == ["a", "b", "c", "d"]

    def test_every_positional_argument_is_a_field(self):
        """No leading-string-is-the-title guess: it is ambiguous and wrong half the time."""
        node = Section("status", "published_at")
        assert node.title is None
        assert list(node.fields()) == ["status", "published_at"]

    def test_an_explicit_title_is_unambiguous(self):
        node = Section("status", title="Publishing")
        assert node.title == "Publishing"
        assert list(node.fields()) == ["status"]

    def test_tabs_carry_their_labels(self):
        node = Tabs(Tab("Content", "title"), Tab("SEO", "summary"))
        assert [t.label for t in node.children] == ["Content", "SEO"]

    def test_a_tab_slug_is_url_safe(self):
        assert Tab("Search Engine Optimisation!").slug == "search-engine-optimisation"

    def test_tabs_reject_anything_that_is_not_a_tab(self):
        """Otherwise a stray Section renders beside the tab strip with no home."""
        with pytest.raises(TypeError, match="Tabs may only contain Tab"):
            Tabs(Section("title"))

    def test_section_options_are_carried(self):
        node = Section("title", collapsed=True, description="Why this exists")
        assert node.collapsed is True
        assert node.description == "Why this exists"


class TestNormalise:
    def test_a_node_passes_through(self):
        node = Section("a")
        assert normalise(node) is node

    def test_a_bare_string_becomes_a_section(self):
        assert list(normalise("title").fields()) == ["title"]

    def test_a_list_becomes_a_section(self):
        assert list(normalise(["a", "b"]).fields()) == ["a", "b"]

    def test_junk_is_rejected(self):
        with pytest.raises(TypeError):
            normalise(42)


class TestResolve:
    def test_fields_the_form_lacks_are_dropped(self):
        """Usually a field the user may not edit; it should vanish, not raise."""
        resolved = resolve_layout(Section("title", "secret"), ["title"])
        assert list(resolved.fields()) == ["title"]

    def test_fields_the_layout_forgot_are_appended(self):
        """A form missing a required field cannot be saved and says nothing about why."""
        resolved = resolve_layout(Section("title"), ["title", "body"])
        assert set(resolved.fields()) == {"title", "body"}

    def test_forgotten_fields_land_in_their_own_section(self):
        resolved = resolve_layout(Section("title"), ["title", "body"])
        extra = resolved.children[-1]
        assert getattr(extra, "title", None) == "Other"

    def test_forgotten_fields_get_their_own_tab_when_the_root_is_tabs(self):
        resolved = resolve_layout(Tabs(Tab("Main", "title")), ["title", "body"])
        assert [t.label for t in resolved.children] == ["Main", "Other"]
        assert set(resolved.fields()) == {"title", "body"}

    def test_an_emptied_container_is_removed(self):
        resolved = resolve_layout(Section(Section("gone"), "title"), ["title"])
        assert all(list(c.fields()) for c in resolved.children if hasattr(c, "fields"))

    def test_declaration_order_is_preserved(self):
        resolved = resolve_layout(Section("body", "title"), ["title", "body"])
        assert list(resolved.fields()) == ["body", "title"]

    def test_nothing_is_appended_when_the_layout_is_complete(self):
        resolved = resolve_layout(Section("title", "body"), ["title", "body"])
        assert len(resolved.children) == 2


class TestChecks:
    def test_a_valid_layout_is_silent(self):
        p = panel(layout=Section("title", "body"))
        assert check_layout(p, ["title", "body"]) == []

    def test_an_unknown_field_is_reported(self):
        p = panel(layout=Section("title", "nope"))
        assert check_layout(p, ["title"]) == ["nope"]

    def test_a_real_model_field_absent_from_the_form_is_not_an_error(self):
        """It is probably readonly or permission-hidden, which is not a typo."""
        p = panel(layout=Section("title", "created_at"))
        assert check_layout(p, ["title"]) == []

    def test_no_layout_means_nothing_to_check(self):
        assert check_layout(panel(), ["title"]) == []

    def test_the_panel_check_surfaces_it(self):
        errors = panel(layout=Section("title", "definitely_not_a_field")).check()
        assert [e.id for e in errors] == ["vantage.E017"]

    def test_a_valid_layout_passes_the_panel_check(self):
        assert panel(layout=Tabs(Tab("Main", Row("title", "slug")))).check() == []


def test_the_public_import_path_works():
    """The spec advertises `from vantage import Tabs, Tab, Section, Row`."""
    from vantage import Row as R
    from vantage import Section as S
    from vantage import Tab as T
    from vantage import Tabs as TS

    assert (TS, T, S, R) == (Tabs, Tab, Section, Row)
