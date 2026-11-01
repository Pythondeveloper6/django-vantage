"""The roles & permissions screen.

Most of these are security tests wearing a UI's clothes. The screen decides what
everyone else may do, so the cases that matter are the refusals: the non-superuser who
tries to grant what they lack, the forged id, the stale tab that would clobber a
colleague, the read-only deployment.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.conftest import not_mysql
from vantage import roles, site
from vantage.conf import DEFAULTS
from vantage.models import VantageLogEntry
from vantage.palette import build_commands
from vantage.siteconfig import UI_EDITABLE


def perm(label: str) -> Permission:
    app_label, codename = label.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


def token_for(group: Group) -> str:
    return roles.fingerprint(group, roles.current_permission_ids(group))


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def client_in(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def editors(db):
    group = Group.objects.create(name="Editors")
    group.permissions.add(perm("auth.view_user"))
    return group


@pytest.fixture
def manager(db):
    """Staff, not superuser, allowed in by ROLES_PERMISSION, holding a few perms."""
    user = User.objects.create_user("manager", "m@example.com", "x", is_staff=True)
    user.user_permissions.add(
        perm("auth.change_group"),
        perm("auth.add_group"),
        perm("auth.delete_group"),
        perm("auth.view_user"),
        perm("auth.view_group"),
    )
    return User.objects.get(pk=user.pk)


WIDENED = {"ROLES_PERMISSION": "auth.change_group"}


def edit_url(group: Group) -> str:
    return reverse("vantage:role-edit", args=[group.pk])


def save(client, group, *, add=(), remove=(), token=None, **extra):
    data = {
        "token": token if token is not None else token_for(group),
        "mode": "diff",
        "add": ",".join(str(p.pk) for p in add),
        "remove": ",".join(str(p.pk) for p in remove),
        **extra,
    }
    return client.post(edit_url(group), data)


# ── registration, nav and palette ──────────────────────────────────────────


class TestWiring:
    def test_the_page_is_registered_and_routed(self, client_in):
        response = client_in.get(reverse("vantage:page-roles"))
        assert response.status_code == 200
        assert reverse("vantage:page-roles") == "/admin/_/roles/"

    def test_it_is_in_the_sidebar_for_a_superuser(self, client_in):
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert f'href="{reverse("vantage:page-roles")}"' in body

    def test_it_is_in_the_command_palette(self, rf, boss):
        request = rf.get("/")
        request.user = boss
        ids = {c.id for c in build_commands(site, request)}
        assert "go:page-roles" in ids

    def test_it_is_hidden_from_staff_who_are_not_let_in(self, rf, client, manager):
        request = rf.get("/")
        request.user = manager
        assert "go:page-roles" not in {c.id for c in build_commands(site, request)}
        client.force_login(manager)
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert reverse("vantage:page-roles") not in body

    def test_the_setting_is_known_and_never_editable_from_the_ui(self):
        """Editable, it would let whoever holds it hand the same power to anyone."""
        assert "ROLES_PERMISSION" in DEFAULTS
        assert "ROLES_PERMISSION" not in UI_EDITABLE


# ── who may get in ─────────────────────────────────────────────────────────


class TestAccess:
    def test_superuser_only_by_default(self, client, manager, editors):
        client.force_login(manager)
        assert client.get(reverse("vantage:page-roles")).status_code == 403
        assert client.get(edit_url(editors)).status_code == 403
        assert save(client, editors, add=[perm("auth.view_group")]).status_code == 403
        assert not editors.permissions.filter(codename="view_group").exists()

    def test_anonymous_is_sent_to_login(self, client, editors):
        response = client.get(edit_url(editors))
        assert response.status_code == 302

    def test_a_permission_string_widens_it(self, client, manager, editors, settings):
        settings.VANTAGE = WIDENED
        client.force_login(manager)
        assert client.get(reverse("vantage:page-roles")).status_code == 200
        assert client.get(edit_url(editors)).status_code == 200

    def test_a_callable_widens_it(self, client, manager, editors, settings):
        settings.VANTAGE = {"ROLES_PERMISSION": lambda request: request.user.username == "manager"}
        client.force_login(manager)
        assert client.get(edit_url(editors)).status_code == 200

    def test_csrf_is_enforced_even_without_the_middleware(self, boss, editors):
        """The test settings have no CsrfViewMiddleware: the view must refuse anyway."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(boss)
        response = save(client, editors, add=[perm("auth.view_group")])
        assert response.status_code == 403
        assert not editors.permissions.filter(codename="view_group").exists()


# ── the list ───────────────────────────────────────────────────────────────


class TestList:
    def test_lists_roles_with_counts(self, client_in, editors, boss):
        boss.groups.add(editors)
        body = client_in.get(reverse("vantage:page-roles")).content.decode()
        assert "Editors" in body
        assert re.search(r'v-roles-num">\s*1\s*<', body)

    def test_the_list_costs_the_same_for_one_role_or_forty(self, client_in, editors):
        url = reverse("vantage:page-roles")
        client_in.get(url)
        with CaptureQueriesContext(connection) as small:
            client_in.get(url)
        for i in range(40):
            group = Group.objects.create(name=f"Role {i}")
            group.permissions.add(perm("auth.view_user"))
        with CaptureQueriesContext(connection) as large:
            client_in.get(url)
        assert len(small) == len(large)

    def test_create(self, client_in):
        response = client_in.post(reverse("vantage:role-create"), {"name": "  Support  agents "})
        group = Group.objects.get(name="Support agents")
        assert response.url == edit_url(group)
        entry = VantageLogEntry.objects.get(action="create")
        assert entry.object_repr == "Support agents"

    def test_create_refuses_a_case_insensitive_duplicate(self, client_in, editors):
        client_in.post(reverse("vantage:role-create"), {"name": "editors"})
        assert Group.objects.filter(name__iexact="editors").count() == 1

    def test_create_refuses_an_empty_name(self, client_in):
        client_in.post(reverse("vantage:role-create"), {"name": "   "})
        assert not Group.objects.exists()

    def test_duplicate_copies_permissions_but_not_members(self, client_in, editors, boss):
        boss.groups.add(editors)
        client_in.post(reverse("vantage:role-duplicate", args=[editors.pk]))
        copy = Group.objects.get(name="Copy of Editors")
        assert set(copy.permissions.all()) == set(editors.permissions.all())
        assert not copy.user_set.exists()
        client_in.post(reverse("vantage:role-duplicate", args=[editors.pk]))
        assert Group.objects.filter(name="Copy of Editors (2)").exists()

    def test_rename(self, client_in, editors):
        client_in.post(
            reverse("vantage:role-rename", args=[editors.pk]),
            {"name": "Writers", "token": token_for(editors)},
        )
        editors.refresh_from_db()
        assert editors.name == "Writers"
        entry = VantageLogEntry.objects.get(action="update")
        assert entry.changes["name"] == {"from": "Editors", "to": "Writers"}

    def test_rename_with_a_stale_token_is_refused(self, client_in, editors):
        stale = token_for(editors)
        editors.permissions.add(perm("auth.view_group"))
        client_in.post(
            reverse("vantage:role-rename", args=[editors.pk]), {"name": "Writers", "token": stale}
        )
        editors.refresh_from_db()
        assert editors.name == "Editors"


# ── the matrix ─────────────────────────────────────────────────────────────


class TestMatrix:
    def test_renders_registered_apps_and_other_models(self, client_in, editors):
        body = client_in.get(edit_url(editors)).content.decode()
        assert "data-roles-grid" in body
        assert "Other models" in body
        view_user = perm("auth.view_user")
        assert re.search(rf'value="{view_user.pk}"[^>]*checked[^>]*data-base', body)

    def test_standard_verbs_are_columns_and_custom_ones_are_other(self, boss):
        ct = ContentType.objects.get_for_model(Group)
        custom = Permission.objects.create(
            content_type=ct, codename="approve_group", name="Can approve"
        )
        catalogue = roles.load_catalogue()
        sections = roles.build_matrix(site, catalogue, set(), roles.reach_for(boss))
        row = next(r for s in sections for r in s.rows if r.key == "auth.group")
        assert row.cells["change"].codename == "change_group"
        assert [c.id for c in row.other] == [custom.pk]

    def test_unregistered_models_go_to_the_other_section(self, boss):
        sections = roles.build_matrix(site, roles.load_catalogue(), set(), roles.reach_for(boss))
        other = next(s for s in sections if s.unregistered)
        assert "auth.permission" in {r.key for r in other.rows}
        assert all(not r.registered for r in other.rows)
        registered = [r for s in sections if not s.unregistered for r in s.rows]
        assert registered and all(r.registered for r in registered)

    def test_a_stale_content_type_still_shows_its_permissions(self, boss):
        ct = ContentType.objects.create(app_label="gone", model="ghost")
        Permission.objects.create(content_type=ct, codename="view_ghost", name="Can view ghost")
        sections = roles.build_matrix(site, roles.load_catalogue(), set(), roles.reach_for(boss))
        row = next(r for s in sections for r in s.rows if r.key == "gone.ghost")
        assert row.cells["view"] is not None

    def test_save_applies_the_diff_atomically_and_audits_it(self, client_in, editors):
        response = save(
            client_in,
            editors,
            add=[perm("auth.add_group"), perm("auth.change_group")],
            remove=[perm("auth.view_user")],
        )
        assert response.status_code == 302
        labels = {roles.perm_label(p) for p in editors.permissions.select_related("content_type")}
        assert labels == {"auth.add_group", "auth.change_group"}
        entry = VantageLogEntry.objects.get(action_name="role.permissions")
        assert entry.changes["Permissions added"]["to"] == "auth.add_group, auth.change_group"
        assert entry.changes["Permissions removed"]["from"] == "auth.view_user"
        assert entry.object_repr == "Editors"

    def test_the_no_script_form_is_diffed_against_its_base(self, client_in, editors):
        view_user, add_group = perm("auth.view_user"), perm("auth.add_group")
        client_in.post(
            edit_url(editors),
            {"token": token_for(editors), "base": str(view_user.pk), "perm": [add_group.pk]},
        )
        assert set(editors.permissions.all()) == {add_group}

    def test_unknown_and_malformed_ids_are_ignored(self, client_in, editors):
        response = client_in.post(
            edit_url(editors),
            {"token": token_for(editors), "mode": "diff", "add": "999999,abc,-3,", "remove": "0"},
        )
        assert response.status_code == 302
        assert set(editors.permissions.all()) == {perm("auth.view_user")}
        assert not VantageLogEntry.objects.filter(action_name="role.permissions").exists()

    def test_a_stale_token_conflicts_instead_of_clobbering(self, client_in, editors):
        stale = token_for(editors)
        # A colleague grants something in the meantime.
        editors.permissions.add(perm("auth.view_group"))
        response = save(client_in, editors, add=[perm("auth.add_group")], token=stale, base="")
        assert response.status_code == 409
        assert set(editors.permissions.values_list("codename", flat=True)) == {
            "view_user",
            "view_group",
        }
        body = response.content.decode()
        assert "Someone else changed this role" in body
        assert "auth.view_group" in body
        # The actor's own edit comes back pending, on top of the new state.
        add_group = perm("auth.add_group")
        assert re.search(rf'value="{add_group.pk}"[^>]*checked', body)
        assert not re.search(rf'value="{add_group.pk}"[^>]*data-base', body)

    def test_a_missing_token_conflicts(self, client_in, editors):
        response = save(client_in, editors, add=[perm("auth.add_group")], token="")
        assert response.status_code == 409

    def test_the_lock_is_the_compare(self, editors, boss):
        """The service itself refuses, whatever the view does."""
        catalogue = roles.load_catalogue()
        with pytest.raises(roles.RoleConflict):
            roles.save_permissions(
                editors,
                token="nope",
                add={perm("auth.add_group").pk},
                remove=set(),
                reach=roles.reach_for(boss),
                catalogue=catalogue,
            )

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_read_only_mode_refuses_every_write(self, client_in, editors):
        assert client_in.get(edit_url(editors)).status_code == 200
        assert save(client_in, editors, add=[perm("auth.add_group")]).status_code == 403
        assert client_in.post(reverse("vantage:role-create"), {"name": "X"}).status_code == 403
        assert (
            client_in.post(
                reverse("vantage:role-delete", args=[editors.pk]), {"token": token_for(editors)}
            ).status_code
            == 403
        )
        assert (
            client_in.post(
                reverse("vantage:role-members", args=[editors.pk]), {"add": ["1"]}
            ).status_code
            == 403
        )
        assert set(editors.permissions.all()) == {perm("auth.view_user")}
        body = client_in.get(edit_url(editors)).content.decode()
        assert "data-roles-review" not in body


class TestQueryCount:
    @not_mysql
    def test_the_matrix_costs_the_same_at_any_number_of_models(
        self, client_in, editors, django_assert_num_queries
    ):
        url = edit_url(editors)
        client_in.get(url)
        with CaptureQueriesContext(connection) as baseline:
            client_in.get(url)

        # Three hundred more models' worth of permissions, the role holding half.
        cts = ContentType.objects.bulk_create(
            [ContentType(app_label="bulk", model=f"model{i}") for i in range(300)]
        )
        created = Permission.objects.bulk_create(
            [
                Permission(
                    content_type=ct, codename=f"{verb}_{ct.model}", name=f"Can {verb} {ct.model}"
                )
                for ct in cts
                for verb in roles.ACTIONS
            ]
        )
        editors.permissions.add(*created[::2])

        with django_assert_num_queries(len(baseline)):
            response = client_in.get(url)
        assert response.content.decode().count('class="v-roles-perm"') >= 1200

    def test_saving_costs_the_same_for_one_change_or_three_hundred(self, client_in, editors):
        few = list(Permission.objects.exclude(group=editors)[:1])
        # Warm the per-process caches (content types and the like) on a save that is
        # then undone. Without it the first save in a fresh process pays for them and
        # the comparison passes or fails on whichever test happened to run before.
        save(client_in, editors, add=few)
        save(client_in, editors, remove=few)
        with CaptureQueriesContext(connection) as small:
            save(client_in, editors, add=few)
        many = list(Permission.objects.exclude(group=editors)[:300])
        with CaptureQueriesContext(connection) as large:
            save(client_in, editors, add=many)
        assert len(small) == len(large)


# ── escalation ─────────────────────────────────────────────────────────────


class TestEscalation:
    @pytest.fixture(autouse=True)
    def widened(self, settings):
        settings.VANTAGE = WIDENED

    @pytest.fixture
    def manager_in(self, client, manager):
        client.force_login(manager)
        return client

    def test_may_grant_only_what_they_hold(self, manager_in, editors):
        save(manager_in, editors, add=[perm("auth.view_group"), perm("auth.delete_user")])
        codenames = set(editors.permissions.values_list("codename", flat=True))
        assert "view_group" in codenames
        assert "delete_user" not in codenames

    def test_may_not_revoke_what_they_do_not_hold(self, manager_in, editors):
        editors.permissions.add(perm("auth.delete_user"))
        save(manager_in, editors, remove=[perm("auth.delete_user"), perm("auth.view_user")])
        codenames = set(editors.permissions.values_list("codename", flat=True))
        assert codenames == {"delete_user"}

    def test_cells_they_do_not_hold_render_locked(self, manager_in, editors):
        body = manager_in.get(edit_url(editors)).content.decode()
        delete_user = perm("auth.delete_user")
        assert re.search(rf'value="{delete_user.pk}"[^>]*disabled data-locked', body)
        view_user = perm("auth.view_user")
        assert not re.search(rf'value="{view_user.pk}"[^>]*disabled', body)

    def test_the_no_script_form_keeps_a_locked_grant(self, manager_in, editors):
        """A disabled box is not submitted; without the hidden twin, saving would
        look like revoking it — and the server would rightly refuse, noisily."""
        delete_user = perm("auth.delete_user")
        editors.permissions.add(delete_user)
        body = manager_in.get(edit_url(editors)).content.decode()
        assert re.search(rf'type="hidden" name="perm" value="{delete_user.pk}"', body)

    def test_cannot_add_members_to_a_role_beyond_their_reach(self, manager_in, manager, editors):
        """The one-click escalation: put yourself in a more powerful role."""
        editors.permissions.add(perm("auth.delete_user"))
        response = manager_in.post(
            reverse("vantage:role-members", args=[editors.pk]), {"add": [manager.pk]}
        )
        assert response.status_code == 403
        assert not manager.groups.exists()

    def test_cannot_rename_duplicate_or_delete_beyond_reach(self, manager_in, editors):
        editors.permissions.add(perm("auth.delete_user"))
        token = token_for(editors)
        assert (
            manager_in.post(
                reverse("vantage:role-rename", args=[editors.pk]), {"name": "X", "token": token}
            ).status_code
            == 403
        )
        assert (
            manager_in.post(reverse("vantage:role-duplicate", args=[editors.pk])).status_code == 403
        )
        assert (
            manager_in.post(
                reverse("vantage:role-delete", args=[editors.pk]), {"token": token}
            ).status_code
            == 403
        )
        assert Group.objects.filter(pk=editors.pk, name="Editors").exists()
        assert Group.objects.count() == 1

    def test_may_manage_members_within_reach(self, manager_in, manager, editors):
        # Someone else: adding yourself is refused, see TestOwnRoles.
        colleague = User.objects.create_user("colleague", "c@example.com", "x", is_staff=True)
        response = manager_in.post(
            reverse("vantage:role-members", args=[editors.pk]), {"add": [colleague.pk]}
        )
        assert response.status_code == 302
        assert colleague.groups.filter(pk=editors.pk).exists()

    def test_needs_the_group_verbs_for_each_write(self, client, editors, settings):
        settings.VANTAGE = {"ROLES_PERMISSION": lambda request: True}
        viewer = User.objects.create_user("viewer", "v@example.com", "x", is_staff=True)
        client.force_login(viewer)
        assert client.get(edit_url(editors)).status_code == 200
        assert client.post(reverse("vantage:role-create"), {"name": "X"}).status_code == 403
        assert save(client, editors, add=[]).status_code == 403

    def test_never_shows_staff_or_superuser_flags(self, manager_in, editors, boss):
        boss.groups.add(editors)
        body = manager_in.get(reverse("vantage:role-members", args=[editors.pk])).content.decode()
        assert "boss" in body
        assert "superuser" not in body.lower()
        body = manager_in.get(reverse("vantage:role-user", args=[boss.pk])).content.decode()
        assert "superuser" not in body.lower()


# ── members ────────────────────────────────────────────────────────────────


class TestMembers:
    def test_add_and_remove_are_audited(self, client_in, editors):
        ada = User.objects.create_user("ada")
        bob = User.objects.create_user("bob")
        url = reverse("vantage:role-members", args=[editors.pk])
        client_in.post(url, {"add": [ada.pk, bob.pk]})
        assert set(editors.user_set.all()) == {ada, bob}
        client_in.post(url, {"remove": [bob.pk]})
        assert set(editors.user_set.all()) == {ada}
        entries = VantageLogEntry.objects.filter(action_name="role.members").order_by("pk")
        assert entries[0].changes["Members added"]["to"] == "ada, bob"
        assert entries[1].changes["Members removed"]["from"] == "bob"

    def test_re_adding_a_member_records_nothing(self, client_in, editors):
        ada = User.objects.create_user("ada")
        ada.groups.add(editors)
        client_in.post(reverse("vantage:role-members", args=[editors.pk]), {"add": [ada.pk]})
        assert not VantageLogEntry.objects.filter(action_name="role.members").exists()

    def test_junk_ids_are_ignored(self, client_in, editors):
        response = client_in.post(
            reverse("vantage:role-members", args=[editors.pk]), {"add": ["abc", "99999"]}
        )
        assert response.status_code == 302
        assert not editors.user_set.exists()

    def test_the_picker_endpoint_searches_users(self, client_in):
        User.objects.create_user("ada", first_name="Ada", last_name="Lovelace")
        User.objects.create_user("bob")
        data = client_in.get(reverse("vantage:role-users"), {"q": "lovelace"}).json()
        assert [r["text"] for r in data["results"]] == ["ada — Ada Lovelace"]
        assert data["more"] is False

    def test_the_picker_endpoint_is_gated(self, client, manager):
        client.force_login(manager)
        assert client.get(reverse("vantage:role-users")).status_code == 403

    def test_members_page_renders_the_picker(self, client_in, editors):
        body = client_in.get(reverse("vantage:role-members", args=[editors.pk])).content.decode()
        assert "data-vantage-combobox" in body
        assert reverse("vantage:role-users") in body


class TestEffectivePermissions:
    def test_union_of_roles_and_direct_with_sources(self, client_in, editors):
        ada = User.objects.create_user("ada", is_staff=True)
        support = Group.objects.create(name="Support")
        support.permissions.add(perm("auth.view_user"), perm("auth.view_group"))
        ada.groups.add(editors, support)
        ada.user_permissions.add(perm("auth.view_user"), perm("auth.add_user"))

        grouped = dict(roles.effective_permissions(ada))
        rows = {r.label: r for items in grouped.values() for r in items}
        assert set(rows) == {"auth.view_user", "auth.view_group", "auth.add_user"}
        assert rows["auth.view_user"].direct
        assert [r["name"] for r in rows["auth.view_user"].roles] == ["Editors", "Support"]
        assert rows["auth.add_user"].roles == []

        body = client_in.get(reverse("vantage:role-user", args=[ada.pk])).content.decode()
        assert "Direct grant" in body
        assert "Support" in body

    def test_query_count_does_not_grow_with_roles(self, client_in, editors):
        ada = User.objects.create_user("ada")
        ada.groups.add(editors)
        url = reverse("vantage:role-user", args=[ada.pk])
        client_in.get(url)
        with CaptureQueriesContext(connection) as small:
            client_in.get(url)
        for i in range(10):
            group = Group.objects.create(name=f"R{i}")
            group.permissions.add(perm("auth.add_user"))
            ada.groups.add(group)
        with CaptureQueriesContext(connection) as large:
            client_in.get(url)
        assert len(small) == len(large)

    def test_a_superuser_viewer_is_told_the_target_is_one(self, client_in, boss):
        body = client_in.get(reverse("vantage:role-user", args=[boss.pk])).content.decode()
        assert "superuser" in body.lower()

    def test_an_unknown_user_is_a_404(self, client_in):
        assert client_in.get(reverse("vantage:role-user", args=["nope"])).status_code == 404


class TestDelete:
    def test_preview_counts_only_what_people_would_actually_lose(self, client_in, editors):
        ada = User.objects.create_user("ada")
        bob = User.objects.create_user("bob")
        ada.groups.add(editors)
        bob.groups.add(editors)
        # Bob also holds the permission another way, so he loses nothing.
        other = Group.objects.create(name="Other")
        other.permissions.add(perm("auth.view_user"))
        bob.groups.add(other)

        impact = roles.delete_impact(editors)
        assert impact.members == 2
        assert impact.affected_total == 1
        assert impact.affected[0][0] == ada

        body = client_in.get(reverse("vantage:role-delete", args=[editors.pk])).content.decode()
        assert "1 person will lose access" in body

    def test_delete_is_token_checked_and_audited(self, client_in, editors):
        url = reverse("vantage:role-delete", args=[editors.pk])
        stale = token_for(editors)
        editors.permissions.add(perm("auth.add_group"))
        client_in.post(url, {"token": stale})
        assert Group.objects.filter(pk=editors.pk).exists()

        pk = editors.pk
        client_in.post(url, {"token": token_for(editors)})
        assert not Group.objects.filter(pk=pk).exists()
        entry = VantageLogEntry.objects.get(action="delete")
        assert entry.object_id == str(pk)
        assert entry.object_repr == "Editors"
        assert "auth.add_group" in entry.changes["Permissions removed"]["from"]


class TestCustomUserModel:
    def test_a_user_model_without_groups_disables_membership(self, monkeypatch, client_in, editors):
        monkeypatch.setattr(roles, "_membership_field", lambda: None)
        assert not roles.supports_membership()
        assert list(roles.members_of(editors)) == []
        body = client_in.get(reverse("vantage:page-roles")).content.decode()
        assert "Members" not in body
        members = reverse("vantage:role-members", args=[editors.pk])
        assert client_in.get(members).status_code == 404
        assert (
            client_in.post(
                reverse("vantage:role-members", args=[editors.pk]), {"add": ["1"]}
            ).status_code
            == 404
        )

    def test_membership_is_found_through_the_user_model(self):
        field = roles._membership_field()
        assert field is not None and field.related_model is Group


# ── a role manager and their own roles ──────────────────────────────────────


class TestOwnRoles:
    """Somebody trusted with the roles screen still cannot use it on themselves.

    Field rules combine most-permissively, so joining any role that grants the same
    model without the rule lifts it; editing a role they hold changes their own access.
    Both are refused for anyone but a superuser.
    """

    @pytest.fixture
    def setup(self, db, settings):
        from tests.test_field_permissions import perm, restrict
        from tests.testapp.models import Order

        settings.VANTAGE = {"ROLES_PERMISSION": "auth.change_group"}
        support = Group.objects.create(name="Support")
        support.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        restrict(support, Order, total="hidden")
        managers = Group.objects.create(name="Managers")
        managers.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        agent = User.objects.create_user("agent", "a@example.com", "x", is_staff=True)
        agent.groups.add(support)
        agent.user_permissions.add(
            perm("auth.change_group"), perm("auth.add_group"), perm("auth.view_group")
        )
        return support, managers, User.objects.get(pk=agent.pk)

    def test_joining_a_role_yourself_is_refused(self, client, setup):
        _support, managers, agent = setup
        client.force_login(agent)
        response = client.post(
            reverse("vantage:role-members", args=[managers.pk]), {"add": [str(agent.pk)]}
        )
        assert response.status_code == 403
        assert not agent.groups.filter(pk=managers.pk).exists()

    def test_adding_someone_else_still_works(self, client, setup):
        _support, managers, agent = setup
        other = User.objects.create_user("other", "o@example.com", "x", is_staff=True)
        client.force_login(agent)
        response = client.post(
            reverse("vantage:role-members", args=[managers.pk]), {"add": [str(other.pk)]}
        )
        assert response.status_code == 302
        assert other.groups.filter(pk=managers.pk).exists()

    def test_editing_a_role_you_hold_is_refused(self, client, setup):
        from tests.test_field_permissions import ct_id
        from tests.testapp.models import Order
        from vantage.models import FieldPermission

        support, _managers, agent = setup
        client.force_login(agent)
        response = client.post(
            reverse("vantage:role-edit", args=[support.pk]),
            {
                "token": roles.fingerprint(support, roles.current_permission_ids(support)),
                "mode": "diff",
                "fields": [f"{ct_id(Order)}:total:"],
            },
        )
        assert response.status_code == 403
        assert FieldPermission.objects.filter(group=support, field_name="total").exists()

    def test_a_superuser_may_still_manage_their_own_roles(self, client, setup):
        support, managers, _agent = setup
        boss = User.objects.create_superuser("boss2", "b2@example.com", "x")
        boss.groups.add(managers)
        client.force_login(boss)
        response = client.post(
            reverse("vantage:role-members", args=[support.pk]), {"add": [str(boss.pk)]}
        )
        assert response.status_code == 302


class TestRenamingAReferencedRole:
    """A role named in code — field rules or a two-factor requirement — keeps its name.

    Both bind by name, so a rename would silently drop the rule or the requirement for
    every member while leaving them every permission the role grants.
    """

    def _rename(self, client, group, name):
        return client.post(
            reverse("vantage:role-rename", args=[group.pk]),
            {"name": name, "token": roles.fingerprint(group, roles.current_permission_ids(group))},
        )

    def test_a_role_with_code_field_rules_cannot_be_renamed(self, client, db, monkeypatch):
        from tests.testapp.models import Order
        from vantage import site

        panel = site.get_panel(Order)
        monkeypatch.setattr(
            panel, "field_permissions", {"Coders": {"number": "hidden"}}, raising=False
        )
        coders = Group.objects.create(name="Coders")
        client.force_login(User.objects.create_superuser("boss3", "b3@example.com", "x"))
        self._rename(client, coders, "Coders2")
        assert Group.objects.get(pk=coders.pk).name == "Coders"

    def test_a_role_that_requires_two_factor_cannot_be_renamed(self, client, db, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        finance = Group.objects.create(name="Finance")
        client.force_login(User.objects.create_superuser("boss4", "b4@example.com", "x"))
        self._rename(client, finance, "Finance (old)")
        assert Group.objects.get(pk=finance.pk).name == "Finance"

    def test_an_unreferenced_role_renames_as_before(self, client, db):
        plain = Group.objects.create(name="Plain")
        client.force_login(User.objects.create_superuser("boss5", "b5@example.com", "x"))
        self._rename(client, plain, "Plainer")
        assert Group.objects.get(pk=plain.pk).name == "Plainer"
