"""The command registry.

A palette is read as a list of what you are allowed to do, so the permission tests
here are the load-bearing ones: an entry the user cannot act on is not clutter, it is
a lie the user will click on.
"""

import json

import pytest
from django.contrib.auth.models import AnonymousUser, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings
from django.urls import include, path, reverse

from tests.testapp.models import Author, Post
from vantage import palette
from vantage.conf import vantage_settings
from vantage.palette import Command, build_commands, register_command, registry_for
from vantage.sites import VantageSite, site

_patterns, _app, _instance = site.urls
urlpatterns = [
    path("admin/", include((list(_patterns) + palette.build_urls(site), _app), namespace=_instance))
]

pytestmark = pytest.mark.urls(__name__)


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def editor(db):
    """Staff, may view posts and nothing else. Cannot add anything."""
    user = User.objects.create_user("editor", "editor@example.com", "x", is_staff=True)
    user.user_permissions.add(_perm("view_post"))
    return user


@pytest.fixture
def author_maker(db):
    user = User.objects.create_user("maker", "maker@example.com", "x", is_staff=True)
    user.user_permissions.add(_perm("view_author", Author), _perm("add_author", Author))
    return user


@pytest.fixture
def scratch_site():
    """A throwaway site, so registering a command cannot leak into another test."""
    return VantageSite(name="scratch")


def _perm(codename, model=Post):
    return Permission.objects.get(
        codename=codename, content_type=ContentType.objects.get_for_model(model)
    )


def commands_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return build_commands(site, request)


def ids(commands):
    return [command.id for command in commands]


class TestCommand:
    def test_a_command_needs_something_to_do(self):
        with pytest.raises(ValueError, match="exactly one"):
            Command(id="dead", label="Dead end")

    def test_a_command_cannot_be_two_things_at_once(self):
        with pytest.raises(ValueError, match="exactly one"):
            Command(id="both", label="Both", url="/a/", callback="b")

    def test_keywords_are_normalised_to_a_tuple(self):
        command = Command(id="a", label="A", url="/a/", keywords=["x", "y"])
        assert command.keywords == ("x", "y")

    def test_it_serialises_for_the_browser(self):
        payload = Command(id="a", label="A", url="/a/", keywords=["x"], icon="tag").as_dict()
        assert payload["keywords"] == ["x"]
        assert payload["callback"] is None
        assert "permission" not in payload


class TestDerivedCommands:
    def test_every_visible_list_page_is_a_destination(self, boss):
        panel = site.get_panel(Post)
        assert f"go:{panel.slug}" in ids(commands_for(boss))

    def test_the_dashboard_is_always_there(self, boss):
        command = next(c for c in commands_for(boss) if c.id == "go:dashboard")
        assert command.url == reverse("vantage:dashboard")

    def test_every_custom_page_is_a_destination(self, boss):
        assert "go:page-revenue" in ids(commands_for(boss))

    def test_a_creatable_model_gets_a_create_command(self, boss):
        panel = site.get_panel(Post)
        command = next(c for c in commands_for(boss) if c.id == f"add:{panel.slug}")
        assert command.url == reverse(f"vantage:{panel.slug}-add")
        assert command.section == "Create"

    def test_sections_come_in_reading_order(self, boss):
        seen = []
        for command in commands_for(boss):
            if command.section not in seen:
                seen.append(command.section)
        assert seen[:3] == ["Go to", "Create", "Actions"]

    @override_settings(VANTAGE={"THEME_TOGGLE": True})
    def test_the_theme_toggle_is_a_command(self, boss):
        command = next(c for c in commands_for(boss) if c.id == "theme:toggle")
        assert command.callback == "theme.toggle"
        assert command.url is None

    @override_settings(VANTAGE={"THEME_TOGGLE": False})
    def test_no_theme_command_when_the_toggle_is_off(self, boss):
        assert "theme:toggle" not in ids(commands_for(boss))

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_a_read_only_panel_offers_nothing_to_create(self, boss):
        assert not [c for c in commands_for(boss) if c.section == "Create"]


class TestPermissions:
    def test_a_destination_the_user_cannot_open_is_not_listed(self, editor):
        listed = ids(commands_for(editor))
        assert f"go:{site.get_panel(Post).slug}" in listed
        assert f"go:{site.get_panel(Author).slug}" not in listed

    def test_an_action_the_user_cannot_take_is_not_listed(self, editor):
        assert not [c for c in ids(commands_for(editor)) if c.startswith("add:")]

    def test_add_appears_only_for_the_model_it_was_granted_on(self, author_maker):
        listed = [c for c in ids(commands_for(author_maker)) if c.startswith("add:")]
        assert listed == [f"add:{site.get_panel(Author).slug}"]

    def test_a_gated_page_is_hidden_from_whoever_it_gates(self, db):
        user = User.objects.create_user("nobody", "n@example.com", "x", is_staff=True)
        assert "go:page-gated" not in ids(commands_for(user))

    def test_a_gated_page_appears_for_whoever_holds_the_permission(self, editor):
        assert "go:page-gated" in ids(commands_for(editor))

    def test_an_anonymous_request_gets_no_destinations(self, db):
        listed = ids(commands_for(AnonymousUser()))
        assert not [c for c in listed if c.startswith(("go:testapp", "add:"))]

    def test_a_custom_command_honours_its_permission_string(self, editor, scratch_site):
        register_command(
            scratch_site,
            id="gated",
            label="Gated",
            url="/x/",
            permission="testapp.delete_post",
        )
        request = RequestFactory().get("/")
        request.user = editor
        assert "gated" not in ids(build_commands(scratch_site, request))

    def test_a_custom_command_honours_a_callable_permission(self, boss, scratch_site):
        register_command(
            scratch_site,
            id="callable",
            label="Callable",
            url="/x/",
            permission=lambda request: request.user.username == "nobody",
        )
        request = RequestFactory().get("/")
        request.user = boss
        assert "callable" not in ids(build_commands(scratch_site, request))


class TestRegistry:
    def test_keyword_form(self, scratch_site):
        command = register_command(scratch_site, id="docs", label="Docs", url="/docs/")
        assert list(registry_for(scratch_site)) == [command]

    def test_instance_form(self, scratch_site):
        command = Command(id="docs", label="Docs", url="/docs/")
        register_command(scratch_site, command)
        assert list(registry_for(scratch_site)) == [command]

    def test_decorator_form_hands_the_class_back(self, scratch_site):
        class Flush:
            id = "flush"
            label = "Flush the cache"
            url = "/ops/flush/"

        # Exactly what ``@site.register_command`` does to the class it decorates.
        assert register_command(scratch_site, Flush) is Flush
        assert [c.id for c in registry_for(scratch_site)] == ["flush"]

    def test_a_class_falls_back_to_its_own_name(self, scratch_site):
        class Whatever:
            url = "/w/"

        register_command(scratch_site, Whatever)
        assert [c.label for c in registry_for(scratch_site)] == ["Whatever"]

    def test_re_registering_an_id_replaces_it(self, scratch_site):
        register_command(scratch_site, id="docs", label="Old", url="/a/")
        register_command(scratch_site, id="docs", label="New", url="/b/")
        assert [c.label for c in registry_for(scratch_site)] == ["New"]

    def test_unregister(self, scratch_site):
        register_command(scratch_site, id="docs", label="Docs", url="/docs/")
        registry_for(scratch_site).unregister("docs")
        assert len(registry_for(scratch_site)) == 0

    def test_nonsense_is_refused(self, scratch_site):
        with pytest.raises(TypeError):
            register_command(scratch_site, 42)

    def test_two_sites_do_not_share_a_registry(self, scratch_site):
        other = VantageSite(name="other")
        register_command(scratch_site, id="docs", label="Docs", url="/docs/")
        assert len(registry_for(other)) == 0

    def test_a_registered_command_reaches_the_palette(self, boss, scratch_site):
        register_command(scratch_site, id="docs", label="Docs", url="/docs/")
        request = RequestFactory().get("/")
        request.user = boss
        assert "docs" in ids(build_commands(scratch_site, request))


class TestIcons:
    def test_only_known_names_come_back(self):
        assert palette.icon_paths(["search", "not-an-icon"]).keys() == {"search"}

    def test_the_payload_carries_the_paths_its_rows_need(self, client, boss):
        from vantage.templatetags.vantage import ICONS

        client.force_login(boss)
        payload = json.loads(client.get(reverse("vantage:commands")).content)
        used = {command["icon"] for command in payload["commands"]}
        # Every icon the browser has to draw, and nothing it will not.
        assert set(payload["icons"]) == used & set(ICONS)


class TestEndpoint:
    def test_anonymous_is_redirected(self, client, db):
        assert client.get(reverse("vantage:commands")).status_code == 302

    def test_the_payload_has_the_documented_shape(self, client, boss):
        client.force_login(boss)
        response = client.get(reverse("vantage:commands"))
        assert response.status_code == 200
        payload = json.loads(response.content)
        assert set(payload) == {"commands", "sections", "icons"}
        assert payload["sections"] == ["Go to", "Create", "Actions"]
        assert set(payload["commands"][0]) == {
            "id",
            "label",
            "hint",
            "icon",
            "url",
            "callback",
            "keywords",
            "section",
        }

    def test_it_is_never_cached_by_a_shared_cache(self, client, boss):
        client.force_login(boss)
        assert client.get(reverse("vantage:commands"))["Cache-Control"] == "no-store"

    def test_the_endpoint_obeys_the_same_permission_rules(self, client, editor):
        client.force_login(editor)
        payload = json.loads(client.get(reverse("vantage:commands")).content)
        listed = [command["id"] for command in payload["commands"]]
        assert f"go:{site.get_panel(Author).slug}" not in listed


class TestPartial:
    """The markup the palette needs, and the fallback that works without it."""

    def render(self):
        return render_to_string("vantage/partials/palette.html", {"settings": vantage_settings})

    def test_the_header_form_submits_to_the_results_page(self):
        assert f'action="{reverse("vantage:search")}"' in self.render()

    def test_the_form_is_a_plain_get(self):
        assert 'method="get"' in self.render()
        assert 'name="q"' in self.render()

    def test_the_dialog_is_wired_to_both_endpoints(self):
        markup = self.render()
        assert f'data-search-url="{reverse("vantage:search-json")}"' in markup
        assert f'data-commands-url="{reverse("vantage:commands")}"' in markup

    def test_the_combobox_is_announced(self):
        markup = self.render()
        assert 'role="combobox"' in markup
        assert 'aria-controls="v-palette-list"' in markup
        assert 'aria-expanded="false"' in markup
        assert 'role="listbox"' in markup
        assert 'aria-live="polite"' in markup

    def test_no_comment_leaks_into_the_page(self):
        # ``{# … #}`` is single-line only; a multi-line one renders as visible text.
        assert "{#" not in self.render()

    @override_settings(VANTAGE={"COMMAND_PALETTE": False})
    def test_switching_the_palette_off_leaves_the_search_box(self):
        markup = self.render()
        assert "<dialog" not in markup
        assert 'name="q"' in markup
