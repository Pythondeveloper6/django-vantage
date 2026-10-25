"""Slots: the named seams a project injects its own HTML into.

Four properties carry the whole feature, and each is here because the alternative is a
bug with no stack trace: contributions render in a predictable order, one that raises
cannot take the page with it, output is escaped unless the author said otherwise, and a
permission on a fragment is actually enforced.
"""

from types import SimpleNamespace

import pytest
from django.template import Context, Template
from django.test import override_settings
from django.utils.safestring import SafeString, mark_safe

from tests.testapp.models import Author, Post
from vantage import slots
from vantage.panels import ModelPanel


@pytest.fixture(autouse=True)
def clean_slots():
    """Registrations are module-global; a leak between tests is a mystery failure."""
    slots.clear()
    yield
    slots.clear()


def request_for(*permissions, **attrs):
    """A request stub. No database: none of this reaches one."""
    user = SimpleNamespace(
        has_perm=lambda perm: perm in permissions,
        is_authenticated=True,
        **attrs,
    )
    return SimpleNamespace(user=user)


class TestRegistration:
    def test_an_unknown_slot_name_is_rejected(self):
        """A typo would otherwise be invisible — the fragment just never appears."""
        with pytest.raises(ValueError, match="not a known Vantage slot"):
            slots.add("before_lst", render=lambda ctx: "x")

    def test_a_declared_name_is_accepted(self):
        slots.declare("my_own_slot")
        slots.add("my_own_slot", render=lambda ctx: "x")
        assert slots.render_slot("my_own_slot") == "x"

    def test_exactly_one_of_template_or_render_is_required(self):
        with pytest.raises(ValueError, match="exactly one"):
            slots.add("before_list")
        with pytest.raises(ValueError, match="exactly one"):
            slots.add("before_list", template="a.html", render=lambda ctx: "x")

    def test_render_must_be_callable(self):
        with pytest.raises(TypeError):
            slots.add("before_list", render="not callable")

    def test_the_decorator_registers(self):
        @slots.slot("after_list")
        def fragment(context):
            return "decorated"

        assert slots.render_slot("after_list") == "decorated"
        # The function is returned unchanged, so it stays callable and testable.
        assert fragment({}) == "decorated"


class TestOrdering:
    def test_contributions_render_in_registration_order(self):
        for letter in "abc":
            slots.add("before_list", render=lambda ctx, x=letter: f"<i>{x}</i>")
        expected = "".join(f"&lt;i&gt;{letter}&lt;/i&gt;" for letter in "abc")
        assert slots.render_slot("before_list") == expected

    def test_order_overrides_registration_order(self):
        slots.add("before_list", render=lambda ctx: "second")
        slots.add("before_list", render=lambda ctx: "first", order=-10)
        assert slots.render_slot("before_list") == "firstsecond"

    def test_equal_order_keeps_registration_order(self):
        slots.add("before_list", render=lambda ctx: "a", order=5)
        slots.add("before_list", render=lambda ctx: "b", order=5)
        assert slots.render_slot("before_list") == "ab"


class TestFailureIsolation:
    def test_a_raising_contribution_is_skipped_and_its_siblings_render(self, caplog):
        slots.add("after_list", render=lambda ctx: "before")

        def explode(context):
            raise RuntimeError("the plugin is broken")

        slots.add("after_list", render=explode)
        slots.add("after_list", render=lambda ctx: "after")

        with caplog.at_level("ERROR", logger="vantage.slots"):
            rendered = slots.render_slot("after_list")

        assert rendered == "beforeafter"
        assert "failed" in caplog.text
        assert "the plugin is broken" in caplog.text

    def test_the_failing_contribution_is_named_in_the_log(self, caplog):
        def broken_banner(context):
            raise ValueError("nope")

        slots.add("before_list", render=broken_banner)
        with caplog.at_level("ERROR", logger="vantage.slots"):
            slots.render_slot("before_list")
        assert "broken_banner" in caplog.text

    def test_a_missing_template_is_skipped_rather_than_fatal(self, caplog):
        slots.add("before_list", template="does/not/exist.html")
        slots.add("before_list", render=lambda ctx: "survivor")
        with caplog.at_level("ERROR", logger="vantage.slots"):
            assert slots.render_slot("before_list") == "survivor"


class TestEscaping:
    def test_a_plain_string_is_escaped(self):
        slots.add("before_list", render=lambda ctx: "<script>alert(1)</script>")
        rendered = slots.render_slot("before_list")
        assert rendered == "&lt;script&gt;alert(1)&lt;/script&gt;"
        assert isinstance(rendered, SafeString)

    def test_marked_safe_output_passes_through(self):
        slots.add("before_list", render=lambda ctx: mark_safe('<b class="x">hi</b>'))
        assert slots.render_slot("before_list") == '<b class="x">hi</b>'

    def test_none_and_empty_contribute_nothing(self):
        slots.add("before_list", render=lambda ctx: None)
        slots.add("before_list", render=lambda ctx: "")
        assert slots.render_slot("before_list") == ""


class TestPermissions:
    def test_a_permission_string_is_enforced(self):
        slots.add("object_actions", render=lambda ctx: "secret", permission="app.view_x")
        allowed = {"request": request_for("app.view_x")}
        denied = {"request": request_for()}
        assert slots.render_slot("object_actions", allowed) == "secret"
        assert slots.render_slot("object_actions", denied) == ""

    def test_a_callable_permission_receives_the_request(self):
        slots.add(
            "object_actions",
            render=lambda ctx: "flagged",
            permission=lambda request: request.user.beta,
        )
        assert slots.render_slot("object_actions", {"request": request_for(beta=True)}) == "flagged"
        assert slots.render_slot("object_actions", {"request": request_for(beta=False)}) == ""

    def test_a_gated_fragment_fails_closed_without_a_request(self):
        """Leaking gated content into a context that forgot the request is not 'safe'."""
        slots.add("before_list", render=lambda ctx: "secret", permission="app.view_x")
        assert slots.render_slot("before_list", {}) == ""

    def test_an_ungated_fragment_renders_without_a_request(self):
        slots.add("before_list", render=lambda ctx: "public")
        assert slots.render_slot("before_list", {}) == "public"

    def test_the_others_still_render_when_one_is_denied(self):
        slots.add("before_list", render=lambda ctx: "public")
        slots.add("before_list", render=lambda ctx: "private", permission="app.secret")
        assert slots.render_slot("before_list", {"request": request_for()}) == "public"


class TestContext:
    def test_the_context_reaches_the_contribution(self):
        slots.add("detail_sidebar", render=lambda ctx: f"id={ctx['object'].pk}")
        obj = SimpleNamespace(pk=7)
        assert slots.render_slot("detail_sidebar", {"object": obj}) == "id=7"

    def test_extra_keyword_arguments_are_merged_over_the_context(self):
        slots.add("row_actions", render=lambda ctx: str(ctx["object"]))
        rendered = slots.render_slot("row_actions", {"object": "page"}, object="row")
        assert rendered == "row"

    def test_a_template_context_is_flattened(self):
        slots.add("before_list", render=lambda ctx: ctx["greeting"])
        assert slots.render_slot("before_list", Context({"greeting": "hello"})) == "hello"


class TestModelScope:
    def test_a_scoped_contribution_only_renders_on_its_own_model(self):
        slots.add("before_list", render=lambda ctx: "posts only", model=Post)
        post_panel = ModelPanel(Post)
        author_panel = ModelPanel(Author)
        assert slots.render_slot("before_list", {"panel": post_panel}) == "posts only"
        assert slots.render_slot("before_list", {"panel": author_panel}) == ""

    def test_an_unscoped_contribution_renders_everywhere(self):
        slots.add("before_list", render=lambda ctx: "everywhere")
        assert slots.render_slot("before_list", {"panel": ModelPanel(Author)}) == "everywhere"


class TestPanelHook:
    """``ModelPanel.slot`` — the per-model version of the same seam."""

    def test_the_panel_hook_renders_first(self):
        class HookedPanel(ModelPanel):
            def slot(self, name, context):
                return mark_safe("<em>panel</em>") if name == "before_list" else None

        slots.add("before_list", render=lambda ctx: "registry")
        rendered = slots.render_slot("before_list", {"panel": HookedPanel(Post)})
        assert rendered == "<em>panel</em>registry"

    def test_the_panel_hook_is_escaped_like_any_other(self):
        class HookedPanel(ModelPanel):
            def slot(self, name, context):
                return "<b>raw</b>"

        rendered = slots.render_slot("before_list", {"panel": HookedPanel(Post)})
        assert rendered == "&lt;b&gt;raw&lt;/b&gt;"

    def test_a_raising_panel_hook_does_not_take_the_page_down(self, caplog):
        class HookedPanel(ModelPanel):
            def slot(self, name, context):
                raise RuntimeError("boom")

        slots.add("before_list", render=lambda ctx: "registry")
        with caplog.at_level("ERROR", logger="vantage.slots"):
            assert slots.render_slot("before_list", {"panel": HookedPanel(Post)}) == "registry"

    def test_a_panel_without_the_hook_is_fine(self):
        assert slots.render_slot("before_list", {"panel": ModelPanel(Post)}) == ""


class TestIsFilled:
    def test_reports_whether_anything_would_render(self):
        assert slots.is_filled("row_actions") is False
        slots.add("row_actions", render=lambda ctx: "x")
        assert slots.is_filled("row_actions") is True

    def test_respects_permissions_without_rendering(self):
        calls = []

        def fragment(context):
            calls.append(1)
            return "x"

        slots.add("row_actions", render=fragment, permission="app.view_x")
        assert slots.is_filled("row_actions", {"request": request_for()}) is False
        assert slots.is_filled("row_actions", {"request": request_for("app.view_x")}) is True
        assert calls == []

    def test_respects_model_scope(self):
        slots.add("row_actions", render=lambda ctx: "x", model=Post)
        assert slots.is_filled("row_actions", {"panel": ModelPanel(Author)}) is False
        assert slots.is_filled("row_actions", {"panel": ModelPanel(Post)}) is True


class TestTemplateTag:
    def render(self, template: str, **context) -> str:
        return Template("{% load vantage_slots %}" + template).render(Context(context))

    def test_the_tag_renders_the_slot(self):
        slots.add("before_list", render=lambda ctx: mark_safe("<hr>"))
        assert self.render('{% slot "before_list" %}') == "<hr>"

    def test_the_tag_does_not_double_escape(self):
        slots.add("before_list", render=lambda ctx: "a & b")
        assert self.render('{% slot "before_list" %}') == "a &amp; b"

    def test_the_tag_passes_keyword_arguments_through(self):
        slots.add("row_actions", render=lambda ctx: str(ctx["object"]))
        assert self.render('{% slot "row_actions" object=row %}', row=42) == "42"

    def test_an_empty_slot_renders_nothing(self):
        assert self.render('{% slot "dashboard_top" %}') == ""

    def test_slot_filled_can_guard_the_wrapper(self):
        template = (
            '{% slot_filled "row_actions" as filled %}'
            '{% if filled %}<td>{% slot "row_actions" %}</td>{% endif %}'
        )
        assert self.render(template) == ""
        slots.add("row_actions", render=lambda ctx: mark_safe("<button>Go</button>"))
        assert self.render(template) == "<td><button>Go</button></td>"


class TestTemplateContributions:
    """A contribution can be a template name, which is the common case in practice."""

    @pytest.fixture
    def template_dir(self, tmp_path, settings):
        (tmp_path / "banner.html").write_text(
            '<div class="banner">Hello {{ name }} &amp; welcome</div>'
        )
        (tmp_path / "unsafe.html").write_text("<p>{{ payload }}</p>")
        engine = {
            **settings.TEMPLATES[0],
            "DIRS": [str(tmp_path)],
        }
        with override_settings(TEMPLATES=[engine]):
            yield tmp_path

    def test_a_template_contribution_renders(self, template_dir):
        slots.add("before_list", template="banner.html")
        rendered = slots.render_slot("before_list", {"name": "Ada"})
        assert rendered == '<div class="banner">Hello Ada &amp; welcome</div>'

    def test_a_template_escapes_its_own_variables(self, template_dir):
        """The engine already did it; the result must not be escaped a second time."""
        slots.add("before_list", template="unsafe.html")
        rendered = slots.render_slot("before_list", {"payload": "<script>x</script>"})
        assert rendered == "<p>&lt;script&gt;x&lt;/script&gt;</p>"

    def test_a_template_contribution_honours_its_permission(self, template_dir):
        slots.add("before_list", template="banner.html", permission="app.view_x")
        assert slots.render_slot("before_list", {"name": "Ada", "request": request_for()}) == ""
