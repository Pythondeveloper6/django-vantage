"""Agent identities: tokens, guardrails, confinement to the API, the audit trail."""

from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, Post
from vantage import agents
from vantage.models import AgentIdentity, AgentRun, VantageLogEntry

pytestmark = pytest.mark.django_db

ON = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "write"}


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.VANTAGE = dict(ON)
    cache.clear()


def api_url(model="post"):
    return reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": model})


def detail(obj):
    return reverse(
        "vantage:api-detail",
        kwargs={"app_label": "testapp", "model_name": type(obj)._meta.model_name, "pk": obj.pk},
    )


def grant(user, *codenames):
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(codename=codename))


@pytest.fixture
def svc(db):
    user = User.objects.create_user("svc", is_staff=True)
    user.set_unusable_password()
    user.save()
    grant(user, "view_post", "add_post", "change_post", "delete_post", "view_author")
    return user


@pytest.fixture
def make(svc):
    def build(**kwargs):
        kwargs.setdefault("name", "bot")
        return agents.create_identity(user=svc, **kwargs)

    return build


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type="application/json", **extra)


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada", email="ada@example.com")


def new_post(author, slug="p"):
    return Post.objects.create(title="t", slug=slug, body="b", author=author)


class TestTokens:
    def test_shown_once_and_only_a_hash_is_stored(self, make):
        identity, token = make()
        assert token.startswith("vta_") and len(token) == 4 + 8 + 1 + 43
        assert token not in (identity.token_hash, identity.token_prefix)
        assert token.split("_", 2)[2] not in identity.token_hash
        assert len(identity.token_hash) == 64

    def test_a_valid_token_lists_records(self, make, author):
        _, token = make()
        new_post(author)
        response = Client().get(api_url(), **bearer(token))
        assert response.status_code == 200

    def test_superuser_service_user_refused_on_creation(self, db):
        boss = User.objects.create_superuser("boss", "b@example.com", "x")
        with pytest.raises(agents.AgentError):
            agents.create_identity("evil", boss)

    def test_superuser_service_user_refused_on_use(self, make):
        identity, token = make()
        User.objects.filter(pk=identity.user_id).update(is_superuser=True)
        assert Client().get(api_url(), **bearer(token)).status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Bearer", "Bearer nonsense", "Bearer vta_short_x", "Bearer vta_AAAAAAAA_wrong"],
    )
    def test_malformed_or_unknown_is_401(self, make, header):
        make()
        response = Client().get(api_url(), HTTP_AUTHORIZATION=header)
        assert response.status_code == 401

    def test_wrong_secret_is_401(self, make):
        _, token = make()
        assert Client().get(api_url(), **bearer(token[:-3] + "xyz")).status_code == 401

    def test_expired_revoked_inactive(self, make, svc):
        identity, token = make()
        identity.expires_at = timezone.now() - timedelta(seconds=1)
        identity.save()
        assert Client().get(api_url(), **bearer(token)).status_code == 401
        identity.expires_at = None
        identity.revoked_at = timezone.now()
        identity.save()
        assert Client().get(api_url(), **bearer(token)).status_code == 401
        identity.revoked_at = None
        identity.save()
        assert Client().get(api_url(), **bearer(token)).status_code == 200
        User.objects.filter(pk=svc.pk).update(is_active=False)
        assert Client().get(api_url(), **bearer(token)).status_code == 401

    def test_feature_off_is_401(self, make, settings):
        _, token = make()
        settings.VANTAGE = {"AGENTS": False}
        assert Client().get(api_url(), **bearer(token)).status_code == 401

    def test_bad_bearer_never_falls_back_to_the_session(self, make):
        make()
        client = Client()
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        assert client.get(api_url()).status_code == 200
        assert client.get(api_url(), HTTP_AUTHORIZATION="Bearer junk").status_code == 401

    def test_bearer_wins_over_a_session_cookie(self, make, author):
        _, token = make()
        client = Client()
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        post = new_post(author)
        response = client.get(detail(post), **bearer(token))
        assert response.status_code == 200
        # The service user (not boss) is who is acting: it may not view tags.
        assert client.get(api_url("tag"), **bearer(token)).status_code == 403

    def test_last_used_is_not_written_on_every_request(self, make):
        identity, token = make()
        client = Client()
        client.get(api_url(), **bearer(token))
        first = AgentIdentity.objects.get(pk=identity.pk).last_used_at
        assert first is not None
        client.get(api_url(), **bearer(token))
        assert AgentIdentity.objects.get(pk=identity.pk).last_used_at == first

    def test_rotate_kills_the_old_token(self, make):
        identity, old = make()
        new = agents.rotate_token(identity)
        client = Client()
        assert client.get(api_url(), **bearer(old)).status_code == 401
        assert client.get(api_url(), **bearer(new)).status_code == 200


class TestCsrf:
    def test_token_without_cookie_needs_no_csrf(self, make, author):
        _, token = make(access="write")
        client = Client(enforce_csrf_checks=True)
        with override_settings(MIDDLEWARE=_csrf_middleware()):
            response = post_json(
                client,
                api_url(),
                {"fields": {"title": "t", "slug": "s", "body": "b", "author": author.pk}},
                **bearer(token),
            )
        assert response.status_code == 201

    def test_token_with_a_session_cookie_keeps_csrf(self, make, author):
        _, token = make(access="write")
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        with override_settings(MIDDLEWARE=_csrf_middleware()):
            response = post_json(
                client,
                api_url(),
                {"fields": {"title": "t", "slug": "s", "body": "b", "author": author.pk}},
                **bearer(token),
            )
        assert response.status_code == 403
        assert not Post.objects.exists()

    def test_session_requests_still_need_csrf(self, author):
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        with override_settings(MIDDLEWARE=_csrf_middleware()):
            response = post_json(client, api_url(), {"fields": {}})
        assert response.status_code == 403


def _csrf_middleware():
    from django.conf import settings

    return [*settings.MIDDLEWARE, "django.middleware.csrf.CsrfViewMiddleware"]


class TestGuardrails:
    def test_read_only_blocks_writes(self, make, author):
        _, token = make(access="read")
        post = new_post(author)
        client = Client()
        body = {"fields": {"title": "x"}}
        assert post_json(client, api_url(), body, **bearer(token)).status_code == 403
        assert (
            client.patch(
                detail(post), json.dumps(body), content_type="application/json", **bearer(token)
            ).status_code
            == 403
        )
        assert client.delete(detail(post), **bearer(token)).status_code == 403
        assert Post.objects.filter(pk=post.pk, title="t").exists()

    def test_delete_needs_allow_delete(self, make, author):
        _, token = make(access="write")
        post = new_post(author)
        assert Client().delete(detail(post), **bearer(token)).status_code == 403
        assert Post.objects.filter(pk=post.pk).exists()
        AgentIdentity.objects.update(allow_delete=True)
        assert Client().delete(detail(post), **bearer(token)).status_code == 204

    def test_model_outside_allowed_models_is_404_and_absent_from_schema(self, make, author):
        _, token = make(allowed_models=["testapp.post"])
        client = Client()
        assert client.get(api_url("author"), **bearer(token)).status_code == 404
        assert client.get(api_url("post"), **bearer(token)).status_code == 200
        schema = client.get(reverse("vantage:api-schema"), **bearer(token)).json()["models"]
        assert list(schema) == ["testapp.post"]
        spec = client.get(reverse("vantage:api-openapi"), **bearer(token)).content.decode()
        assert "/testapp/author/" not in spec

    def test_hidden_model_write_is_404_not_403(self, make):
        _, token = make(access="read", allowed_models=["testapp.post"])
        response = post_json(Client(), api_url("author"), {"fields": {}}, **bearer(token))
        assert response.status_code == 404

    def test_service_user_permissions_still_apply(self, make, svc, author):
        _, token = make(access="write")
        svc.user_permissions.clear()
        new_post(author)
        assert Client().get(api_url(), **bearer(token)).status_code == 403

    def test_rate_limit(self, make):
        _, token = make(rate_limit_per_minute=2)
        client = Client()
        assert client.get(api_url(), **bearer(token)).status_code == 200
        assert client.get(api_url(), **bearer(token)).status_code == 200
        response = client.get(api_url(), **bearer(token))
        assert response.status_code == 429
        assert int(response["Retry-After"]) >= 1

    def test_write_budget_per_run(self, make, author):
        identity, token = make(access="write", max_writes_per_run=2)
        client = Client()

        def create(slug, run):
            return post_json(
                client,
                api_url(),
                {"fields": {"title": "t", "slug": slug, "body": "b", "author": author.pk}},
                HTTP_X_VANTAGE_RUN=run,
                **bearer(token),
            )

        assert create("a", "r1").status_code == 201
        assert create("b", "r1").status_code == 201
        assert create("c", "r1").status_code == 403
        assert create("c", "r2").status_code == 201
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        assert (run.writes, run.rows_changed) == (2, 2)
        assert Post.objects.count() == 3


class TestAudit:
    def test_entry_carries_agent_and_run(self, make, author, svc):
        owner = User.objects.create_user("alice")
        identity, token = agents.create_identity("sync", svc, owner, access="write")
        response = post_json(
            Client(),
            api_url(),
            {"fields": {"title": "t", "slug": "s", "body": "b", "author": author.pk}},
            HTTP_X_VANTAGE_RUN="nightly",
            **bearer(token),
        )
        assert response.status_code == 201
        entry = VantageLogEntry.objects.get()
        assert entry.agent == identity
        assert entry.agent_run.run_key == "nightly"
        assert entry.actor == svc
        assert entry.actor_repr == "Agent sync (for alice)"
        assert "Agent sync (for alice)" in str(entry)


class TestConfinement:
    @pytest.mark.parametrize(
        "name",
        [
            "vantage:dashboard",
            "vantage:settings",
            "vantage:role-create",
            "vantage:break-glass-grant",
        ],
    )
    def test_bearer_cannot_open_html_pages(self, make, name):
        _, token = make()
        try:
            url = reverse(name)
        except Exception:
            pytest.skip(f"{name} is not mounted")
        assert Client().get(url, **bearer(token)).status_code == 403

    def test_every_non_api_route_is_confined(self, make):
        from django.urls import get_resolver

        make()
        from vantage.api import ApiView

        resolver = get_resolver()
        seen = 0

        def walk(patterns):
            nonlocal seen
            for p in patterns:
                nested = getattr(p, "url_patterns", None)
                if nested is not None:
                    walk(nested)
                    continue
                cb = p.callback
                module = getattr(cb, "__module__", "")
                view_class = getattr(cb, "view_class", None)
                if not module.startswith("vantage") and not (
                    view_class and view_class.__module__.startswith("vantage")
                ):
                    continue
                seen += 1
                is_api = isinstance(view_class, type) and issubclass(view_class, ApiView)
                assert getattr(cb, "agent_confined", False), p.name
                if is_api:
                    assert cb.csrf_exempt, p.name
                else:
                    assert hasattr(cb, "__wrapped__"), p.name

        walk(resolver.url_patterns)
        assert seen > 20

    def test_bearer_html_refusal_when_session_also_present(self, make):
        _, token = make()
        client = Client()
        client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
        assert client.get(reverse("vantage:dashboard"), **bearer(token)).status_code == 403


def token_in(text):
    return next(line for line in text.splitlines() if line.startswith("vta_"))


class TestCommand:
    def run(self, *args):
        out = io.StringIO()
        call_command("vantage_agent", *args, stdout=out)
        return out.getvalue()

    def test_create_makes_a_non_superuser_service_user_and_prints_token_once(self):
        out = self.run(
            "create", "--name", "bot", "--user", "svc-bot", "--access", "write",
            "--models", "testapp.post", "--allow-delete", "--expires-in", "30",
            "--rate", "5", "--max-writes", "9",
        )  # fmt: skip
        user = User.objects.get(username="svc-bot")
        assert user.is_staff and not user.is_superuser and not user.has_usable_password()
        token = token_in(out)
        identity = AgentIdentity.objects.get(name="bot")
        assert identity.allowed_models == ["testapp.post"]
        assert identity.rate_limit_per_minute == 5 and identity.max_writes_per_run == 9
        assert identity.expires_at is not None and identity.allow_delete
        assert token not in self.run("list")
        assert "bot" in self.run("list")

    def test_create_refuses_a_superuser(self):
        User.objects.create_superuser("root", "r@example.com", "x")
        with pytest.raises(CommandError):
            self.run("create", "--name", "bot", "--user", "root")

    def test_revoke_and_rotate(self):
        out = self.run("create", "--name", "bot", "--user", "svc-bot")
        old = token_in(out)
        new_out = self.run("rotate", "bot")
        new = token_in(new_out)
        client = Client()
        assert client.get(api_url(), **bearer(old)).status_code == 401
        assert client.get(api_url(), **bearer(new)).status_code in (200, 403)
        self.run("revoke", "bot")
        assert client.get(api_url(), **bearer(new)).status_code == 401
        with pytest.raises(CommandError):
            self.run("rotate", "bot")


def test_new_tables_are_not_in_the_panel(db):
    from vantage.registry import DEFAULT_EXCLUDE

    assert {"vantage.agentidentity", "vantage.agentrun"} <= DEFAULT_EXCLUDE
