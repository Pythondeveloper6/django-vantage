"""Sidebar composition: derived, declared, and arranged from the UI."""

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from django.urls import reverse

from vantage.models import NavConfiguration
from vantage.nav import (
    apply_edits,
    build_nav,
    check_nav,
    clean_declaration,
    clean_url,
    code_declaration,
    editor_context,
    invalidate_nav,
    reset_declaration,
    save_declaration,
    stored_declaration,
)
from vantage.sites import site


def labels(groups):
    return [g.label for g in groups]


def items(groups, group_label):
    group = next(g for g in groups if g.label == group_label)
    return [i.label for i in group.items]


def flat(groups):
    return [i.label for g in groups for i in g.items]


@pytest.fixture(autouse=True)
def clean_nav_cache():
    """The stored arrangement is cached per process, exactly as the settings row is.

    Without this a test that saves one would leak it into the next, because the cache
    is dropped by ``request_started`` and a test that calls ``build_nav`` directly
    never starts a request.
    """
    invalidate_nav()
    yield
    invalidate_nav()


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


def item(kind, target, **extra):
    """One entry in the stored shape, with the parts a test does not care about."""
    return {"kind": kind, "target": target, **extra}


def group(label, *entries, icon=""):
    return {"label": label, "icon": icon, "items": list(entries)}


def store(*groups):
    save_declaration(clean_declaration(list(groups)))
    invalidate_nav()


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
            "NAV": [
                {"group": "Content", "items": [{"model": "testapp.Post", "label": "Articles"}]}
            ],
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
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

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


# ── the UI arrangement ──────────────────────────────────────────────────────


class TestLinkSafety:
    """The one place a sidebar can become an XSS vector, and the one guard against it."""

    @pytest.mark.parametrize(
        "value",
        [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            # A browser strips control characters inside a scheme before deciding what
            # it is, so a check that reads the raw string reads a different URL from
            # the one that navigates.
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "\x01javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "",
            "   ",
        ],
    )
    def test_refused(self, value):
        assert clean_url(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://example.com/status", "https://example.com/status"),
            ("http://example.com", "http://example.com"),
            ("mailto:ops@example.com", "mailto:ops@example.com"),
            ("/admin/orders/", "/admin/orders/"),
            # A hostname typed without a scheme is a website, not a path on this site.
            ("example.com/status", "https://example.com/status"),
            # Protocol-relative is pinned rather than left to inherit http.
            ("//example.com", "https://example.com"),
            ("  https://example.com  ", "https://example.com"),
        ],
    )
    def test_accepted(self, value, expected):
        assert clean_url(value) == expected

    def test_a_url_with_a_space_in_it_is_refused(self):
        assert clean_url("https://example.com/a b") is None

    def test_an_absurdly_long_url_is_refused(self):
        assert clean_url("https://example.com/" + "a" * 600) is None

    def test_a_refused_url_takes_its_whole_item_with_it(self):
        assert clean_declaration(
            [group("Links", item("url", "javascript:alert(1)", label="Tempting"))]
        ) == [group("Links")]


class TestCleaning:
    def test_a_row_that_is_not_a_list_is_no_opinion_at_all(self):
        assert clean_declaration({"nope": True}) is None
        assert clean_declaration(None) is None

    def test_an_emptied_sidebar_is_a_real_answer(self):
        """Distinct from ``None``: with NAV_UNLISTED = append it is the plain list."""
        assert clean_declaration([]) == []

    def test_an_unknown_icon_falls_back_rather_than_being_stored(self):
        stored = clean_declaration([group("G", item("model", "testapp.post", icon="nope"))])
        assert stored[0]["items"][0]["icon"] == ""

    def test_a_bundled_icon_is_kept(self):
        stored = clean_declaration([group("G", item("model", "testapp.post", icon="users"))])
        assert stored[0]["items"][0]["icon"] == "users"

    def test_a_malformed_permission_is_dropped(self):
        stored = clean_declaration([group("G", item("model", "testapp.post", permission="nope"))])
        assert stored[0]["items"][0]["permission"] == ""

    def test_an_item_with_no_recognised_kind_is_dropped(self):
        assert clean_declaration([group("G", {"kind": "shell", "target": "rm -rf /"})]) == [
            group("G")
        ]

    def test_the_tree_is_bounded(self):
        huge = [group(f"G{n}", item("model", "testapp.post")) for n in range(200)]
        assert len(clean_declaration(huge)) == 40

    def test_labels_are_bounded(self):
        stored = clean_declaration([group("x" * 500, item("model", "testapp.post"))])
        assert len(stored[0]["label"]) == 60

    def test_an_empty_group_survives_so_it_can_be_filled(self):
        assert clean_declaration([group("Later")]) == [group("Later")]


class TestArrangedNav:
    def test_no_row_means_the_code_decides(self, db):
        assert stored_declaration() is None
        assert "Test App" in labels(build_nav(site))

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_the_stored_arrangement_wins(self, db):
        store(group("Everything", item("model", "testapp.post", label="Articles")))
        assert labels(build_nav(site)) == ["Everything"]
        assert flat(build_nav(site)) == ["Articles"]

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_it_wins_over_a_python_declaration_too(self, db):
        store(group("Mine", item("model", "testapp.author")))
        assert labels(build_nav(site)) == ["Mine"]

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Content", "items": ["testapp.Post"]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_reset_gives_the_declaration_back(self, db):
        store(group("Mine", item("model", "testapp.author")))
        reset_declaration()
        invalidate_nav()
        assert labels(build_nav(site)) == ["Content"]

    def test_reset_does_not_freeze_todays_answer(self, db):
        """The point of Reset: the code becomes the sidebar again *and stays* it."""
        store(group("Mine", item("model", "testapp.author")))
        reset_declaration()
        invalidate_nav()
        assert NavConfiguration.objects.count() == 0
        assert stored_declaration() is None

    def test_an_item_can_be_renamed_and_re_iconed(self, db):
        store(group("G", item("model", "testapp.post", label="Articles", icon="users")))
        entry = build_nav(site)[0].items[0]
        assert (entry.label, entry.icon) == ("Articles", "users")

    def test_an_external_link_is_marked_external(self, db):
        store(group("Links", item("url", "https://example.com/status", label="Status")))
        entry = next(i for g in build_nav(site) for i in g.items if i.label == "Status")
        assert entry.external is True

    def test_a_site_path_is_not_external(self, db):
        store(group("Links", item("url", "/admin/", label="Home")))
        entry = next(i for g in build_nav(site) for i in g.items if i.label == "Home")
        assert entry.external is False

    def test_a_group_that_resolves_to_nothing_is_not_rendered(self, db):
        store(group("Ghosts", item("model", "nosuch.model")))
        assert "Ghosts" not in labels(build_nav(site))

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_hidden_item_does_not_render(self, db):
        store(group("G", item("model", "testapp.post", hidden=True)))
        assert flat(build_nav(site)) == []

    def test_a_hidden_item_does_not_come_back_through_the_leftovers(self, db):
        """Otherwise "hide this" would silently mean "move this to the bottom"."""
        store(group("G", item("model", "testapp.post", hidden=True)))
        assert "Posts" not in flat(build_nav(site))

    def test_a_removed_model_reappears_because_nothing_claims_it(self, db):
        """Remove and Hide are different words for different things, and this is why."""
        store(group("G", item("model", "testapp.author")))
        assert "Posts" in flat(build_nav(site))

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_hide_mode_drops_everything_unarranged(self, db):
        store(group("G", item("model", "testapp.author")))
        assert flat(build_nav(site)) == ["Authors"]

    def test_a_model_registered_after_the_save_still_appears(self, db):
        """The failure mode this whole layer has to not have."""
        store(group("G", item("model", "testapp.author")))
        entries = flat(build_nav(site))
        assert "Authors" == entries[0]
        assert "Posts" in entries

    def test_an_emptied_arrangement_still_lists_the_models(self, db):
        save_declaration([])
        invalidate_nav()
        assert "Posts" in flat(build_nav(site))

    def test_a_route_item_resolves(self, db):
        store(group("G", item("route", "vantage:dashboard", label="Home")))
        entry = next(i for g in build_nav(site) for i in g.items if i.label == "Home")
        assert entry.url == reverse("vantage:dashboard")

    def test_a_page_item_resolves(self, db):
        store(group("G", item("page", "revenue")))
        assert "Revenue" in flat(build_nav(site))


class TestArrangementCannotGrantAccess:
    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_stored_item_for_an_invisible_model_does_not_render(self, client, db):
        """Naming a model in the arrangement must not disclose that it exists."""
        from tests.testapp.models import Post

        store(
            group(
                "Everything",
                item("model", "testapp.post"),
                item("model", "testapp.author", label="Secret authors"),
            )
        )
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        content_type = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(
            Permission.objects.get(content_type=content_type, codename="view_post")
        )
        client.force_login(User.objects.get(pk=staff.pk))

        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "Posts" in body
        assert "Secret authors" not in body
        assert reverse("vantage:testapp-author-list") not in body


class TestPerItemAudience:
    def _staff(self):
        return User.objects.create_user("plain", password="x", is_staff=True)

    def _see_everything(self, user):
        user.user_permissions.add(*Permission.objects.filter(codename__startswith="view_"))
        return User.objects.get(pk=user.pk)

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_permission_gate_hides_an_item_from_someone_without_it(self, client, db):
        store(group("G", item("model", "testapp.post", permission="testapp.delete_post")))
        client.force_login(self._see_everything(self._staff()))
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "v-nav-item" not in body

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_the_same_item_appears_for_someone_with_it(self, client, db):
        store(group("G", item("model", "testapp.post", permission="testapp.delete_post")))
        user = self._see_everything(self._staff())
        user.user_permissions.add(Permission.objects.get(codename="delete_post"))
        client.force_login(User.objects.get(pk=user.pk))
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "v-nav-item" in body

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_group_gate_hides_an_item_from_someone_outside_it(self, client, db):
        Group.objects.create(name="Finance")
        store(group("G", item("model", "testapp.post", audiences=["Finance"])))
        client.force_login(self._see_everything(self._staff()))
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "v-nav-item" not in body

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_member_of_the_group_sees_it(self, client, db):
        finance = Group.objects.create(name="Finance")
        store(group("G", item("model", "testapp.post", audiences=["Finance"])))
        user = self._see_everything(self._staff())
        user.groups.add(finance)
        client.force_login(User.objects.get(pk=user.pk))
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "v-nav-item" in body

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_gate_never_widens(self, client, db):
        """Gating on a permission the viewer holds still cannot show them a model they
        may not view."""
        store(group("G", item("model", "testapp.author", permission="testapp.view_post")))
        user = self._staff()
        user.user_permissions.add(Permission.objects.get(codename="view_post"))
        client.force_login(User.objects.get(pk=user.pk))
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert reverse("vantage:testapp-author-list") not in body

    @override_settings(VANTAGE={"NAV_UNLISTED": "hide"})
    def test_a_superuser_is_never_gated_out_of_their_own_sidebar(self, client_in, db):
        store(group("G", item("model", "testapp.post", audiences=["Nobody"])))
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "v-nav-item" in body

    def test_no_request_means_nothing_to_gate_on(self, db):
        store(group("G", item("model", "testapp.post", audiences=["Finance"])))
        assert "Posts" in flat(build_nav(site))


class TestExternalRendering:
    def test_the_sidebar_marks_an_arranged_external_link(self, client_in, db):
        store(
            group(
                "Elsewhere",
                item("url", "https://docs.djangoproject.com/", label="Django docs"),
            )
        )
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'href="https://docs.djangoproject.com/"' in body
        assert 'rel="noopener noreferrer"' in body
        assert "(opens in a new tab)" in body
        assert "v-nav-external" in body


class TestCodeDeclaration:
    """What the editor is seeded with, and what Reset goes back to. Same answer."""

    def test_the_inferred_sidebar_round_trips(self, db):
        seeded = code_declaration(site)
        assert seeded
        assert all(entry["kind"] in ("model", "page") for g in seeded for entry in g["items"])

    def test_seeding_from_inference_reproduces_the_inferred_sidebar(self, db):
        before = [(g.label, [i.label for i in g.items]) for g in build_nav(site)]
        store(*code_declaration(site))
        after = [(g.label, [i.label for i in g.items]) for g in build_nav(site)]
        assert after == before

    @override_settings(
        VANTAGE={
            "NAV": [
                {"route": "vantage:dashboard", "label": "Overview", "icon": "layout-dashboard"},
                {
                    "group": "Content",
                    "icon": "folder",
                    "items": [
                        {"model": "testapp.Post", "label": "Articles", "badge": "live"},
                        "testapp.Author",
                    ],
                },
                {"group": "Links", "items": [{"url": "https://example.com", "label": "Docs"}]},
            ],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_python_declaration_round_trips_through_the_stored_shape(self, db):
        before = [(g.label, [(i.label, i.icon, i.badge) for i in g.items]) for g in build_nav(site)]
        store(*code_declaration(site))
        after = [(g.label, [(i.label, i.icon, i.badge) for i in g.items]) for g in build_nav(site)]
        assert after == before

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "C", "items": [{"model": "testapp.Post", "badge": "live"}]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_badge_survives_the_first_save(self, db):
        store(*code_declaration(site))
        assert build_nav(site)[0].items[0].badge == "live"


class TestEditorContext:
    def test_it_seeds_from_the_code_when_nothing_is_stored(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        context = editor_context(site, request)
        assert context["overridden"] is False
        assert context["groups"]

    def test_it_reads_the_stored_arrangement_once_there_is_one(self, rf, db):
        store(group("Mine", item("model", "testapp.post", label="Articles")))
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        context = editor_context(site, request)
        assert context["overridden"] is True
        assert [g.label for g in context["groups"]] == ["Mine"]
        assert context["groups"][0].items[0].display == "Articles"

    def test_an_unresolvable_item_is_kept_and_flagged(self, rf, db):
        store(group("Mine", item("model", "gone.model", label="Was here")))
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        entry = editor_context(site, request)["groups"][0].items[0]
        assert entry.available is False
        assert entry.display == "Was here"

    def test_the_icon_menu_is_the_icon_allowlist(self, rf, db):
        from vantage.templatetags.vantage import ICONS

        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        assert set(editor_context(site, request)["icons"]) == set(ICONS)


class TestApplyEdits:
    """The POST parser. Order is a number box and a group select, and always was."""

    def posted(self, **extra):
        data = {
            "nav_group_uid": ["g0", "g1"],
            "nav_group_label__g0": "Content",
            "nav_group_pos__g0": "1",
            "nav_group_label__g1": "Links",
            "nav_group_pos__g1": "2",
            "nav_item_uid": ["i0-0", "i0-1"],
            "nav_item_kind__i0-0": "model",
            "nav_item_target__i0-0": "testapp.post",
            "nav_item_group__i0-0": "g0",
            "nav_item_pos__i0-0": "1",
            "nav_item_kind__i0-1": "model",
            "nav_item_target__i0-1": "testapp.author",
            "nav_item_group__i0-1": "g0",
            "nav_item_pos__i0-1": "2",
        }
        data.update(extra)
        return _querydict(data)

    def test_it_keeps_what_was_posted(self):
        result = apply_edits(self.posted())
        assert [g["label"] for g in result.declaration] == ["Content", "Links"]
        assert [i["target"] for i in result.declaration[0]["items"]] == [
            "testapp.post",
            "testapp.author",
        ]

    def test_the_position_boxes_decide_the_order(self):
        result = apply_edits(self.posted(**{"nav_item_pos__i0-0": "9"}))
        assert [i["target"] for i in result.declaration[0]["items"]] == [
            "testapp.author",
            "testapp.post",
        ]

    def test_the_group_select_moves_an_item_between_groups(self):
        result = apply_edits(self.posted(**{"nav_item_group__i0-1": "g1"}))
        assert [i["target"] for i in result.declaration[1]["items"]] == ["testapp.author"]

    def test_the_group_positions_decide_the_group_order(self):
        result = apply_edits(self.posted(nav_group_pos__g1="0"))
        assert [g["label"] for g in result.declaration] == ["Links", "Content"]

    def test_an_item_can_be_renamed(self):
        result = apply_edits(self.posted(**{"nav_item_label__i0-0": "Articles"}))
        assert result.declaration[0]["items"][0]["label"] == "Articles"

    def test_an_item_can_be_hidden(self):
        result = apply_edits(self.posted(**{"nav_item_hidden__i0-0": "1"}))
        assert result.declaration[0]["items"][0]["hidden"] is True

    def test_an_item_can_be_removed(self):
        result = apply_edits(self.posted(**{"nav_item_remove__i0-0": "1"}))
        assert [i["target"] for i in result.declaration[0]["items"]] == ["testapp.author"]

    def test_a_group_can_be_deleted_and_takes_its_items_with_it(self):
        result = apply_edits(self.posted(nav_group_delete__g0="1"))
        assert [g["label"] for g in result.declaration] == ["Links"]
        assert result.declaration[0]["items"] == []

    def test_a_group_can_be_renamed(self):
        result = apply_edits(self.posted(nav_group_label__g0="Commerce"))
        assert result.declaration[0]["label"] == "Commerce"

    def test_a_group_can_be_created(self):
        result = apply_edits(self.posted(nav_new_group="Operations", nav_new_group_icon="shield"))
        assert result.declaration[-1]["label"] == "Operations"
        assert result.declaration[-1]["icon"] == "shield"

    def test_an_external_link_can_be_added(self):
        result = apply_edits(
            self.posted(
                nav_add_kind="url",
                nav_add_url="https://example.com/status",
                nav_add_label="Status",
                nav_add_icon="shield",
                nav_add_group="g1",
            )
        )
        added = result.declaration[1]["items"][0]
        assert added == {
            "kind": "url",
            "target": "https://example.com/status",
            "label": "Status",
            "icon": "shield",
            "badge": "",
            "hidden": False,
            "permission": "",
            "audiences": [],
            "args": [],
        }
        assert result.errors == []

    def test_a_model_can_be_added(self):
        result = apply_edits(
            self.posted(nav_add_kind="model", nav_add_model="testapp.archive", nav_add_group="g1")
        )
        assert result.declaration[1]["items"][0]["target"] == "testapp.archive"

    def test_an_added_link_lands_in_the_new_group_when_that_is_what_was_chosen(self):
        result = apply_edits(
            self.posted(
                nav_new_group="Operations",
                nav_add_kind="url",
                nav_add_url="/status/",
                nav_add_group="new",
            )
        )
        assert result.declaration[-1]["label"] == "Operations"
        assert result.declaration[-1]["items"][0]["target"] == "/status/"

    def test_a_dangerous_link_is_refused_out_loud(self):
        result = apply_edits(
            self.posted(nav_add_kind="url", nav_add_url="javascript:alert(1)", nav_add_group="g1")
        )
        assert result.declaration[1]["items"] == []
        assert len(result.errors) == 1
        assert "javascript" in str(result.errors[0])

    def test_the_audience_gate_round_trips(self):
        result = apply_edits(
            self.posted(
                **{
                    "nav_item_permission__i0-0": "testapp.delete_post",
                    "nav_item_audience__i0-0": ["Finance", "Ops"],
                }
            )
        )
        entry = result.declaration[0]["items"][0]
        assert entry["permission"] == "testapp.delete_post"
        assert entry["audiences"] == ["Finance", "Ops"]

    def test_a_route_keeps_its_arguments(self):
        result = apply_edits(
            _querydict(
                {
                    "nav_group_uid": ["g0"],
                    "nav_group_label__g0": "G",
                    "nav_item_uid": ["i0-0"],
                    "nav_item_kind__i0-0": "route",
                    "nav_item_target__i0-0": "vantage:testapp-post-detail",
                    "nav_item_arg__i0-0": ["7"],
                    "nav_item_group__i0-0": "g0",
                }
            )
        )
        assert result.declaration[0]["items"][0]["args"] == ["7"]

    def test_an_item_whose_group_vanished_goes_with_it(self):
        result = apply_edits(self.posted(nav_group_delete__g0="1", nav_group_delete__g1="1"))
        assert result.declaration == []

    def test_a_forged_target_does_not_become_a_link(self):
        result = apply_edits(
            self.posted(**{"nav_item_kind__i0-0": "model", "nav_item_target__i0-0": "../../etc"})
        )
        assert [i["target"] for i in result.declaration[0]["items"]] == ["testapp.author"]


def _querydict(data):
    """A ``QueryDict`` built from a dict of values and lists, as a POST would arrive."""
    from django.http import QueryDict

    query = QueryDict(mutable=True)
    for key, value in data.items():
        if isinstance(value, list):
            query.setlist(key, value)
        else:
            query[key] = value
    return query
