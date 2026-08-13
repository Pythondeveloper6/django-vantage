"""Read-only mode and impersonation.

Both are guardrails for an admin pointed at production, so most of these tests are
about the guardrail holding rather than the feature working.
"""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage.permissions import PanelPermissions
from vantage.safety import (
    ImpersonationError,
    can_impersonate,
    read_only_reason,
    start_impersonation,
    stop_impersonation,
)


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "b@example.com", "x")


@pytest.fixture
def staffer(db):
    return User.objects.create_user("staffer", password="x", is_staff=True)


class TestReadOnlyResolution:
    def test_off_by_default(self):
        assert read_only_reason() is None

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_true_gives_a_generic_reason(self):
        assert "read-only" in read_only_reason()

    @override_settings(VANTAGE={"READ_ONLY": "Frozen during the migration."})
    def test_a_string_doubles_as_the_explanation(self):
        """Better than a bare 'permission denied' for a deliberate operational choice."""
        assert read_only_reason() == "Frozen during the migration."


class TestReadOnlyEnforcement:
    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_writes_are_refused_even_for_a_superuser(self, boss, rf):
        """It is a statement about the deployment, not about who is asking."""
        request = rf.get("/")
        request.user = boss
        perms = PanelPermissions()
        assert perms.has_model_perm(request, Post, "add") is False
        assert perms.has_model_perm(request, Post, "change") is False
        assert perms.has_model_perm(request, Post, "delete") is False

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_reading_still_works(self, boss, rf):
        request = rf.get("/")
        request.user = boss
        assert PanelPermissions().has_model_perm(request, Post, "view") is True

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_object_level_writes_are_refused_too(self, boss, rf, db):
        author = Author.objects.create(name="A", email="a@example.com")
        post = Post.objects.create(title="T", slug="t", body="b", author=author)
        request = rf.get("/")
        request.user = boss
        assert PanelPermissions().has_object_perm(request, Post, "change", post) is False

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_allowed_actions_reports_view_only(self, boss, rf):
        request = rf.get("/")
        request.user = boss
        assert PanelPermissions().allowed_actions(request, Post) == {"view"}

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_the_create_route_refuses(self, client, boss):
        """Enforced in the permission layer, so every write path inherits it."""
        client.force_login(boss)
        assert client.get(reverse("vantage:testapp-post-add")).status_code == 403

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_a_bulk_action_refuses(self, client, boss, db):
        from django.utils import timezone

        from tests.testapp.models import Archive

        archive = Archive.objects.create(title="A", deleted_at=timezone.now())
        client.force_login(boss)
        response = client.post(
            reverse("vantage:testapp-archive-list"),
            {"action": "restore", "selected": [str(archive.pk)]},
        )
        assert response.status_code == 403
        archive.refresh_from_db()
        assert archive.deleted_at is not None

    @override_settings(VANTAGE={"READ_ONLY": "Frozen for maintenance."})
    def test_the_banner_explains_why(self, client, boss):
        client.force_login(boss)
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert "Frozen for maintenance." in body

    @override_settings(VANTAGE={"READ_ONLY": True})
    def test_write_buttons_are_not_offered(self, client, boss, db):
        client.force_login(boss)
        body = client.get(reverse("vantage:testapp-post-list")).content.decode()
        assert reverse("vantage:testapp-post-add") not in body


class TestImpersonationGuards:
    def test_only_superusers_may_impersonate(self, staffer):
        assert can_impersonate(staffer) is False

    def test_a_superuser_may(self, boss):
        assert can_impersonate(boss) is True

    def test_an_anonymous_user_may_not(self):
        from django.contrib.auth.models import AnonymousUser

        assert can_impersonate(AnonymousUser()) is False


@pytest.mark.django_db
class TestImpersonationThroughTheView:
    def url(self):
        return reverse("vantage:impersonate")

    def test_a_superuser_becomes_the_target(self, client, boss, staffer):
        client.force_login(boss)
        client.post(self.url(), {"user": staffer.pk})
        assert client.session["_auth_user_id"] == str(staffer.pk)

    def test_the_banner_says_so(self, client, boss, staffer):
        client.force_login(boss)
        client.post(self.url(), {"user": staffer.pk})
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert "staffer" in body
        assert "boss" in body  # who to return to

    def test_returning_restores_the_original_account(self, client, boss, staffer):
        client.force_login(boss)
        client.post(self.url(), {"user": staffer.pk})
        client.post(self.url(), {})
        assert client.session["_auth_user_id"] == str(boss.pk)

    def test_a_non_superuser_cannot_start_one(self, client, staffer, db):
        other = User.objects.create_user("other", password="x", is_staff=True)
        client.force_login(staffer)
        client.post(self.url(), {"user": other.pk})
        assert client.session["_auth_user_id"] == str(staffer.pk)

    def test_a_superuser_cannot_be_impersonated(self, client, boss, db):
        """Otherwise it is a lateral move that only muddies the audit trail."""
        other = User.objects.create_superuser("other", "o@example.com", "x")
        client.force_login(boss)
        client.post(self.url(), {"user": other.pk})
        assert client.session["_auth_user_id"] == str(boss.pk)

    def test_get_is_rejected(self, client, boss):
        client.force_login(boss)
        assert client.get(self.url()).status_code == 405

    def test_an_anonymous_request_is_redirected(self, client, staffer):
        assert client.post(self.url(), {"user": staffer.pk}).status_code == 302


@pytest.mark.django_db
class TestImpersonationRules:
    def test_an_inactive_account_is_refused(self, rf, boss, db):
        target = User.objects.create_user("inactive", password="x", is_active=False)
        request = rf.post("/")
        request.user = boss
        request.session = {}
        with pytest.raises(ImpersonationError, match="inactive"):
            start_impersonation(request, target)

    def test_stopping_when_not_impersonating_is_an_error(self, rf, boss):
        request = rf.post("/")
        request.user = boss
        request.session = {}
        with pytest.raises(ImpersonationError, match="Not currently"):
            stop_impersonation(request)
