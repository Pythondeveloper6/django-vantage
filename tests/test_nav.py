"""Sidebar composition, derived and declared."""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from vantage.nav import build_nav, check_nav
from vantage.sites import site


def labels(groups):
    return [g.label for g in groups]


def items(groups, group_label):
    group = next(g for g in groups if g.label == group_label)
    return [i.label for i in group.items]


def flat(groups):
    return [i.label for g in groups for i in g.items]


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


class TestDerivedNav:
    def test_groups_by_app(self):
        groups = build_nav(site)
        assert "Test App" in labels(groups)

    def test_registering_a_model_is_enough(self):
        assert "Posts" in flat(build_nav(site))

    def test_items_are_alphabetical_within_a_group(self):
        entries = items(build_nav(site), "Test App")
        assert entries == sorted(entries, key=str.lower)


class TestDeclaredNav:
    @override_settings(
        VANTAGE={
            "NAV": [
                {"group": "Content", "items": ["testapp.Post", "testapp.Author"]},
                {"group": "People", "items": ["auth.User"]},
            ],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_groups_and_their_order_are_declared(self):
        assert labels(build_nav(site)) == ["Content", "People"]

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post", "testapp.Author"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_item_order_follows_the_declaration_not_the_alphabet(self):
        assert items(build_nav(site), "Content") == ["Posts", "Authors"]

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": [{"model": "testapp.Post", "label": "Articles"}]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_an_item_can_be_renamed(self):
        assert items(build_nav(site), "Content") == ["Articles"]

    @override_settings(
        VANTAGE={
            "NAV": [
                {
                    "group": "Content",
                    "items": [{"model": "testapp.Post", "icon": "trending-up", "badge": "New"}],
                }
            ],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_icon_and_badge_can_be_set(self):
        item = build_nav(site)[0].items[0]
        assert item.icon == "trending-up"
        assert item.badge == "New"

    @override_settings(
        VANTAGE={
            "NAV": [
                {
                    "group": "Links",
                    "items": [
                        {"url": "https://example.com/status", "label": "Status", "icon": "shield"}
                    ],
                }
            ],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_an_arbitrary_external_link_can_be_added(self):
        item = build_nav(site)[0].items[0]
        assert item.label == "Status"
        assert item.url == "https://example.com/status"
        assert item.external is True

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Links", "items": [{"url": "/help/", "label": "Help"}]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_relative_link_is_not_treated_as_external(self):
        assert build_nav(site)[0].items[0].external is False

    @override_settings(
        VANTAGE={
            "NAV": [{"route": "vantage:dashboard", "label": "Home", "icon": "layout-dashboard"}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_named_route_resolves(self):
        item = build_nav(site)[0].items[0]
        assert item.label == "Home"
        assert item.url == reverse("vantage:dashboard")

    @override_settings(
        VANTAGE={
            "NAV": [{"route": "vantage:dashboard", "label": "Home"}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_bare_top_level_item_gets_an_unlabelled_group(self):
        """The usual shape for a Home link that needs no heading above it."""
        assert build_nav(site)[0].label == ""

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_hide_drops_everything_unmentioned(self):
        assert flat(build_nav(site)) == ["Posts"]

    @override_settings(VANTAGE={"NAV": [{"group": "Content", "items": ["testapp.Post"]}]})
    def test_append_is_the_default_so_new_models_never_vanish(self):
        entries = flat(build_nav(site))
        assert entries[0] == "Posts"
        assert "Authors" in entries

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post", "nosuch.Model"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_an_unresolvable_model_is_skipped_not_fatal(self):
        assert flat(build_nav(site)) == ["Posts"]

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Empty", "items": ["nosuch.Model"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_group_that_resolves_to_nothing_is_not_rendered(self):
        assert build_nav(site) == []


class TestDeclaredNavCannotGrantAccess:
    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post", "testapp.Author"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_permissions_still_filter_a_declared_nav(self, client, db):
        """Naming a model in NAV must not make it reachable to someone without rights."""
        from django.contrib.contenttypes.models import ContentType
        from django.contrib.auth.models import Permission

        from tests.testapp.models import Post

        staff = User.objects.create_user("plain", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(Permission.objects.get(content_type=ct, codename="view_post"))
        client.force_login(User.objects.get(pk=staff.pk))

        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "Posts" in body
        assert reverse("vantage:testapp-author-list") not in body


class TestRendering:
    @override_settings(
        VANTAGE={
            "NAV": [
                {
                    "group": "Ops",
                    "icon": "shield",
                    "items": [
                        {"model": "testapp.Post", "label": "Articles"},
                        {"url": "https://example.com", "label": "Docs"},
                    ],
                }
            ],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_the_sidebar_renders_the_declaration(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Ops" in body
        assert "Articles" in body
        assert 'href="https://example.com"' in body
        assert 'rel="noopener noreferrer"' in body


class TestChecks:
    def test_no_declaration_means_no_messages(self):
        assert check_nav(None) == []

    @override_settings(VANTAGE={"NAV": [{"group": "G", "items": [{"label": "Orphan"}]}]})
    def test_an_item_with_no_target_is_an_error(self):
        assert [m.id for m in check_nav(None)] == ["vantage.E021"]

    @override_settings(VANTAGE={"NAV": [{"group": "G", "items": [123]}]})
    def test_a_non_dict_item_is_an_error(self):
        assert [m.id for m in check_nav(None)] == ["vantage.E020"]

    @override_settings(VANTAGE={"NAV": [{"group": "G", "items": ["nosuch.Model"]}]})
    def test_an_unknown_model_is_a_warning_not_an_error(self):
        """Almost always a typo, but it must not stop the project booting."""
        assert [m.id for m in check_nav(None)] == ["vantage.W020"]

    @override_settings(VANTAGE={"NAV": [{"group": "G", "items": ["testapp.Post"]}]})
    def test_a_valid_declaration_is_silent(self):
        assert check_nav(None) == []


class TestSettingsValidation:
    @override_settings(VANTAGE={"NAV_UNLISTED": "nonsense"})
    def test_an_invalid_mode_is_rejected(self):
        from django.core.exceptions import ImproperlyConfigured

        from vantage.conf import vantage_settings

        with pytest.raises(ImproperlyConfigured, match="NAV_UNLISTED"):
            _ = vantage_settings.NAV_UNLISTED
