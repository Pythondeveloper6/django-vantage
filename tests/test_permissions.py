"""Access control: three separate concerns that must not be conflated."""

import pytest
from django.contrib.auth.models import Permission, User
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage.permissions import PanelPermissions, get_permissions


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


def grant(user, model, action):
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(
        Permission.objects.get(content_type=ct, codename=f"{action}_{model._meta.model_name}")
    )
    return User.objects.get(pk=user.pk)  # drop the cached permission set


@pytest.fixture
def staff(db):
    return User.objects.create_user("staffer", password="x", is_staff=True)


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


class TestPermName:
    def test_builds_a_dotted_codename(self):
        assert PanelPermissions().perm_name(Post, "change") == "testapp.change_post"

    def test_covers_every_default_action(self):
        perms = PanelPermissions()
        assert perms.perm_name(Post, "view") == "testapp.view_post"
        assert perms.perm_name(Post, "delete") == "testapp.delete_post"


class TestModelPermission:
    def test_denied_without_a_grant(self, staff, rf):
        request = rf.get("/")
        request.user = staff
        assert PanelPermissions().has_model_perm(request, Post, "view") is False

    def test_allowed_with_a_grant(self, staff, rf):
        request = rf.get("/")
        request.user = grant(staff, Post, "view")
        assert PanelPermissions().has_model_perm(request, Post, "view") is True

    def test_a_grant_on_one_model_does_not_leak_to_another(self, staff, rf):
        request = rf.get("/")
        request.user = grant(staff, Post, "view")
        assert PanelPermissions().has_model_perm(request, Author, "view") is False

    def test_a_view_grant_does_not_imply_change(self, staff, rf):
        request = rf.get("/")
        request.user = grant(staff, Post, "view")
        assert PanelPermissions().has_model_perm(request, Post, "change") is False

    def test_superusers_bypass(self, boss, rf):
        request = rf.get("/")
        request.user = boss
        assert PanelPermissions().has_model_perm(request, Post, "delete") is True

    def test_anonymous_is_denied(self, rf, db):
        from django.contrib.auth.models import AnonymousUser

        request = rf.get("/")
        request.user = AnonymousUser()
        assert PanelPermissions().has_model_perm(request, Post, "view") is False


class TestObjectPermission:
    def test_falls_back_to_the_model_answer_with_only_the_stock_backend(self, staff, rf, db):
        """ModelBackend returns an empty set whenever obj is not None.

        Without this fallback every object check would be False, and no row would
        ever be actionable on a stock Django project.
        """
        request = rf.get("/")
        request.user = grant(staff, Post, "change")
        author = Author.objects.create(name="A", email="a@example.com")
        post = Post.objects.create(title="T", slug="t", body="b", author=author)
        assert PanelPermissions().has_object_perm(request, Post, "change", post) is True

    def test_object_permissions_are_reported_unsupported_on_a_stock_project(self):
        assert PanelPermissions().supports_object_permissions is False


class TestQuerysetFiltering:
    def test_the_default_does_not_narrow(self, staff, rf, db):
        request = rf.get("/")
        request.user = staff
        qs = Post.objects.all()
        assert PanelPermissions().filter_queryset(request, qs).query.where is qs.query.where

    def test_a_subclass_can_scope_rows(self, staff, rf, db):
        author = Author.objects.create(name="A", email="a@example.com")
        Post.objects.create(title="Draft", slug="d", body="b", author=author, status="draft")
        Post.objects.create(title="Live", slug="l", body="b", author=author, status="published")

        class OnlyPublished(PanelPermissions):
            def filter_queryset(self, request, queryset, action="view"):
                return queryset.filter(status="published")

        request = rf.get("/")
        request.user = staff
        rows = OnlyPublished().filter_queryset(request, Post.objects.all())
        assert [p.title for p in rows] == ["Live"]


class TestAllowedActions:
    def test_reports_only_what_was_granted(self, staff, rf):
        request = rf.get("/")
        request.user = grant(grant(staff, Post, "view"), Post, "change")
        assert PanelPermissions().allowed_actions(request, Post) == {"view", "change"}

    def test_superusers_get_everything(self, boss, rf):
        request = rf.get("/")
        request.user = boss
        assert PanelPermissions().allowed_actions(request, Post) == {
            "view",
            "add",
            "change",
            "delete",
        }


class TestResolverConfiguration:
    def test_defaults_to_the_base_class(self):
        assert type(get_permissions()) is PanelPermissions

    def test_is_cached(self):
        assert get_permissions() is get_permissions()

    @override_settings(VANTAGE={"PERMISSIONS": "tests.test_permissions.DenyAll"})
    def test_a_dotted_path_is_honoured(self):
        assert type(get_permissions()) is DenyAll


class DenyAll(PanelPermissions):
    """Used by the settings test above; must be importable by path."""

    def has_model_perm(self, request, model, action):
        return False


@pytest.mark.django_db
class TestEnforcedInViews:
    def test_a_staff_user_without_grants_is_forbidden(self, client, staff):
        client.force_login(staff)
        assert client.get(reverse("vantage:testapp-post-list")).status_code == 403

    def test_a_granted_user_gets_in(self, client, staff):
        client.force_login(grant(staff, Post, "view"))
        assert client.get(reverse("vantage:testapp-post-list")).status_code == 200

    def test_the_detail_page_is_gated_too(self, client, staff):
        author = Author.objects.create(name="A", email="a@example.com")
        post = Post.objects.create(title="T", slug="t", body="b", author=author)
        client.force_login(staff)
        url = reverse("vantage:testapp-post-detail", args=[post.pk])
        assert client.get(url).status_code == 403

    def test_the_sidebar_hides_what_the_user_cannot_open(self, client, staff):
        """Offering a link that 403s is worse than not offering it."""
        client.force_login(grant(staff, Post, "view"))
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert "Posts" in body
        assert reverse("vantage:testapp-author-list") not in body

    def test_a_superuser_sees_everything(self, client, boss):
        client.force_login(boss)
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert reverse("vantage:testapp-post-list") in body
        assert reverse("vantage:testapp-author-list") in body
