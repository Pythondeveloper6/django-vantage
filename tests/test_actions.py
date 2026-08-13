"""Bulk actions, and the selection semantics that make them safe at scale."""

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from tests.testapp.models import Archive, Author, Post
from vantage.actions import Action, Selection, action, resolve_actions
from vantage.panels import ModelPanel

LIST = "vantage:testapp-archive-list"


class PostActions(ModelPanel):
    @action("Publish", icon="check")
    def publish(self, request, queryset):
        return f"Published {queryset.update(status='published')}."

    @action("Wipe", destructive=True, permission="delete", max_rows=3)
    def wipe(self, request, queryset):
        deleted, _ = queryset.delete()
        return f"Deleted {deleted}."

    def undecorated(self, request, queryset):
        return "ran"


@pytest.fixture
def panel():
    return PostActions(Post)


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def posts(db):
    author = Author.objects.create(name="Ada", email="ada@example.com")
    return [
        Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b", author=author)
        for i in range(5)
    ]


@pytest.fixture
def archives(db):
    from django.utils import timezone

    return [
        Archive.objects.create(title=f"A{i}", deleted_at=timezone.now()) for i in range(5)
    ]


class TestDeclaration:
    def test_decorated_methods_are_discovered_without_being_listed(self, panel):
        """Decorating is already an explicit statement of intent."""
        names = {a.name for a in resolve_actions(panel)}
        assert "publish" in names
        assert "wipe" in names

    def test_an_undecorated_method_is_not_picked_up(self, panel):
        assert "undecorated" not in {a.name for a in resolve_actions(panel)}

    def test_an_explicit_actions_list_wins(self):
        p = type("P", (PostActions,), {"actions": ["publish"]})(Post)
        assert [a.name for a in resolve_actions(p)] == ["publish"]

    def test_metadata_survives(self, panel):
        act = next(a for a in resolve_actions(panel) if a.name == "publish")
        assert act.label == "Publish"
        assert act.icon == "check"

    def test_the_bare_decorator_form_works(self):
        class Bare(ModelPanel):
            @action
            def touch(self, request, queryset):
                return "ok"

        act = resolve_actions(Bare(Post))[0]
        assert act.label == "Touch"

    def test_a_label_defaults_from_the_method_name(self):
        class Named(ModelPanel):
            @action
            def mark_as_read(self, request, queryset):
                return "ok"

        assert resolve_actions(Named(Post))[0].label == "Mark as read"


class TestPermissionFiltering:
    def test_actions_are_filtered_by_permission(self, panel, rf, db):
        request = rf.get("/")
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(
            Permission.objects.get(content_type=ct, codename="change_post")
        )
        request.user = User.objects.get(pk=staff.pk)

        names = {a.name for a in resolve_actions(panel, request)}
        assert "publish" in names
        assert "wipe" not in names  # needs delete

    def test_a_superuser_sees_all(self, panel, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        assert len(resolve_actions(panel, request)) == 2


class TestSelection:
    def test_explicit_ids(self, posts):
        sel = Selection(select_all=False, ids=[str(posts[0].pk)], excluded=[])
        assert list(sel.apply(Post.objects.all())) == [posts[0]]

    def test_select_all_takes_the_whole_filtered_set(self, posts):
        sel = Selection(select_all=True, ids=[], excluded=[])
        assert sel.apply(Post.objects.all()).count() == 5

    def test_select_all_honours_exclusions(self, posts):
        """Select-all then untick a handful is the common case, not an edge one."""
        sel = Selection(select_all=True, ids=[], excluded=[str(posts[0].pk)])
        rows = sel.apply(Post.objects.all())
        assert rows.count() == 4
        assert posts[0] not in rows

    def test_select_all_respects_the_filters_already_applied(self, posts):
        posts[0].status = "published"
        posts[0].save()
        sel = Selection(select_all=True, ids=[], excluded=[])
        assert sel.apply(Post.objects.filter(status="published")).count() == 1

    def test_empty_selection_is_detected(self):
        assert Selection(select_all=False, ids=[], excluded=[]).is_empty is True

    def test_the_resolved_scope_is_described_in_words(self):
        """Silent scope reduction on a destructive action is the worst failure here."""
        assert "5 selected records" in Selection(False, ["1"] * 5, []).describe(5)
        assert "all 40 matching" in Selection(True, [], []).describe(40)
        assert "2 excluded" in Selection(True, [], ["1", "2"]).describe(38)


@pytest.mark.django_db
class TestRunningThroughTheView:
    def test_an_action_runs_over_the_selected_rows(self, boss, archives):
        ids = [str(a.pk) for a in archives[:2]]
        response = boss.post(reverse(LIST), {"action": "restore", "selected": ids}, follow=True)
        assert response.status_code == 200
        assert Archive.objects.filter(deleted_at=None).count() == 2

    def test_an_action_leaves_unselected_rows_alone(self, boss, archives):
        ids = [str(archives[0].pk)]
        boss.post(reverse(LIST), {"action": "restore", "selected": ids}, follow=True)
        assert Archive.objects.exclude(deleted_at=None).count() == 4

    def test_select_all_reaches_every_matching_row(self, boss, archives):
        boss.post(reverse(LIST), {"action": "restore", "select_all": "1"}, follow=True)
        assert Archive.objects.filter(deleted_at=None).count() == 5

    def test_select_all_honours_exclusions_through_the_view(self, boss, archives):
        boss.post(
            reverse(LIST),
            {"action": "restore", "select_all": "1", "excluded": [str(archives[0].pk)]},
            follow=True,
        )
        assert Archive.objects.filter(deleted_at=None).count() == 4

    def test_the_resolved_scope_is_reported_back(self, boss, archives):
        """So a silently narrowed scope is impossible to miss."""
        response = boss.post(
            reverse(LIST), {"action": "restore", "select_all": "1"}, follow=True
        )
        assert "all 5 matching" in response.content.decode()

    def test_a_destructive_action_confirms_before_running(self, boss, archives):
        ids = [str(a.pk) for a in archives[:2]]
        response = boss.post(reverse(LIST), {"action": "purge", "selected": ids})
        assert response.status_code == 200
        assert "cannot be undone" in response.content.decode()
        assert Archive.objects.count() == 5  # nothing destroyed yet

    def test_a_confirmed_destructive_action_runs(self, boss, archives):
        ids = [str(a.pk) for a in archives[:2]]
        boss.post(
            reverse(LIST),
            {"action": "purge", "selected": ids, "confirmed": "1"},
            follow=True,
        )
        assert Archive.objects.count() == 3

    def test_max_rows_refuses_an_oversized_selection(self, boss, archives):
        """A guard against one mis-click wiping a table."""
        response = boss.post(
            reverse(LIST), {"action": "purge", "select_all": "1", "confirmed": "1"}, follow=True
        )
        assert "limited to 3" in response.content.decode()
        assert Archive.objects.count() == 5

    def test_an_action_the_user_may_not_run_is_refused(self, client, archives):
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Archive)
        for codename in ("view_archive", "change_archive"):
            staff.user_permissions.add(Permission.objects.get(content_type=ct, codename=codename))
        client.force_login(User.objects.get(pk=staff.pk))
        ids = [str(archives[0].pk)]
        response = client.post(reverse(LIST), {"action": "purge", "selected": ids})
        assert response.status_code == 403
        assert Archive.objects.count() == 5

    def test_an_unknown_action_is_refused(self, boss, archives):
        response = boss.post(reverse(LIST), {"action": "nonsense", "selected": ["1"]})
        assert response.status_code == 403

    def test_an_empty_selection_does_nothing(self, boss, archives):
        response = boss.post(reverse(LIST), {"action": "restore"}, follow=True)
        assert response.status_code == 200
        assert "Nothing was selected" in response.content.decode()


class TestConfirmationText:
    def test_a_custom_confirm_message_interpolates_the_count(self):
        act = Action(func=lambda *a: None, name="x", label="X", confirm="Delete {count}?")
        assert act.confirmation_text(7) == "Delete 7?"

    def test_the_default_message_names_the_action_and_count(self):
        act = Action(func=lambda *a: None, name="x", label="Archive", destructive=True)
        text = act.confirmation_text(3)
        assert "Archive" in text
        assert "3 records" in text

    def test_singular_reads_correctly(self):
        act = Action(func=lambda *a: None, name="x", label="Archive", destructive=True)
        assert "1 record." in act.confirmation_text(1)

    def test_destructive_actions_always_confirm(self):
        act = Action(func=lambda *a: None, name="x", label="X", destructive=True)
        assert act.needs_confirmation()

    def test_ordinary_actions_do_not(self):
        assert not Action(func=lambda *a: None, name="x", label="X").needs_confirmation()
