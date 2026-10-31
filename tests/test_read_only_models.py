"""Per-model read-only mode with an editable allowlist.

The case: a panel over tables another system owns. Nobody — superusers included — may
add or delete rows there, and only the fields on ``editable_fields`` may be changed.
"""

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory, override_settings

from tests.test_forms import add_url, change_url, delete_url, post_payload
from tests.testapp.models import Author, Post
from vantage import fieldperms, site
from vantage.panels import ModelPanel
from vantage.permissions import _reset, get_permissions
from vantage.safety import model_read_only

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh_resolver():
    _reset()
    yield
    _reset()


@pytest.fixture
def boss():
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def post():
    author = Author.objects.create(name="Ada", email="ada@example.com")
    return Post.objects.create(title="Original", slug="original", body="b", author=author)


@pytest.fixture
def panel():
    return site.get_panel(Post)


def request_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


def perms(user, obj=None):
    resolver = get_permissions()
    request = request_for(user)
    if obj is None:
        return {
            a
            for a in ("view", "add", "change", "delete")
            if resolver.has_model_perm(request, Post, a)
        }
    return {
        a
        for a in ("view", "add", "change", "delete")
        if resolver.has_object_perm(request, Post, a, obj)
    }


class TestPermissions:
    def test_a_read_only_panel_refuses_every_write_even_to_a_superuser(
        self, boss, panel, monkeypatch
    ):
        monkeypatch.setattr(panel, "read_only", True)
        assert perms(boss) == {"view"}

    def test_an_allowlist_permits_change_but_never_add_or_delete(
        self, boss, post, panel, monkeypatch
    ):
        monkeypatch.setattr(panel, "read_only", True)
        monkeypatch.setattr(panel, "editable_fields", ["status"])
        assert perms(boss) == {"view", "change"}
        assert perms(boss, post) == {"view", "change"}

    def test_only_allowlisted_fields_are_changeable(self, boss, panel, monkeypatch):
        monkeypatch.setattr(panel, "read_only", True)
        monkeypatch.setattr(panel, "editable_fields", ["status"])
        access = fieldperms.access(request_for(boss), Post)
        assert access.can_change("status")
        assert not access.can_change("title")
        assert access.can_view("title")

    def test_an_ordinary_model_is_untouched(self, boss):
        assert model_read_only(Post) is None
        assert perms(boss) == {"view", "add", "change", "delete"}
        assert not fieldperms.access(request_for(boss), Post)


class TestUnmanagedModels:
    @pytest.fixture
    def unmanaged(self, monkeypatch):
        monkeypatch.setattr(Post._meta, "managed", False)

    @override_settings(VANTAGE={"UNMANAGED_READ_ONLY": True})
    def test_unmanaged_models_are_read_only_when_asked(self, boss, unmanaged):
        assert perms(boss) == {"view"}

    def test_off_by_default(self, boss, unmanaged):
        assert perms(boss) == {"view", "add", "change", "delete"}

    @override_settings(VANTAGE={"UNMANAGED_READ_ONLY": True})
    def test_the_panel_can_opt_back_in_to_writes(self, boss, unmanaged, panel, monkeypatch):
        monkeypatch.setattr(panel, "read_only", False)
        assert perms(boss) == {"view", "add", "change", "delete"}


class TestViews:
    @pytest.fixture
    def client_in(self, client, boss):
        client.force_login(boss)
        return client

    def test_add_and_delete_pages_are_refused(self, client_in, post, panel, monkeypatch):
        monkeypatch.setattr(panel, "read_only", True)
        assert client_in.get(add_url(Post)).status_code == 403
        assert client_in.post(delete_url(post)).status_code == 403
        assert Post.objects.filter(pk=post.pk).exists()

    def test_a_posted_change_only_writes_allowlisted_fields(
        self, client_in, post, panel, monkeypatch
    ):
        monkeypatch.setattr(panel, "read_only", True)
        monkeypatch.setattr(panel, "editable_fields", ["status"])
        payload = post_payload(
            post.author, title="Tampered", slug="original", status=Post.Status.REVIEW
        )
        client_in.post(change_url(post), payload)
        post.refresh_from_db()
        assert post.title == "Original"
        assert post.status == Post.Status.REVIEW


class TestChecks:
    def ids(self, **attrs):
        return sorted(e.id for e in type("P", (ModelPanel,), attrs)(Post).check())

    def test_valid(self):
        assert self.ids(read_only=True, editable_fields=["status"]) == []

    def test_read_only_must_be_a_bool(self):
        assert self.ids(read_only="yes") == ["vantage.E122"]

    def test_editable_fields_must_name_fields(self):
        assert self.ids(read_only=True, editable_fields=["nope"]) == ["vantage.E123"]
        assert self.ids(read_only=True, editable_fields="status") == ["vantage.E123"]
