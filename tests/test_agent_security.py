"""Security review of the agent surface: regressions for each finding fixed."""

from __future__ import annotations

import io
import json
import re
import time

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import post_save
from django.test import Client, RequestFactory
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage import agentdata, agents, mcp, site
from vantage.models import AgentRun

pytestmark = pytest.mark.django_db

URL = "/admin/_/mcp/"


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "write"}
    cache.clear()


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
def owner(db):
    user = User.objects.create_user("alice", is_staff=True)
    grant(user, "view_post", "add_post", "change_post", "delete_post", "view_author")
    return user


@pytest.fixture
def make(svc, owner):
    def build(**kwargs):
        kwargs.setdefault("name", "bot")
        kwargs.setdefault("access", "write")
        kwargs.setdefault("owner", owner)
        return agents.create_identity(user=svc, **kwargs)

    return build


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada", email="ada@example.com")


def api_url(model="post"):
    return reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": model})


def undo_url(key):
    return reverse("vantage:api-agent-run-undo", kwargs={"run_key": key})


def bearer(token, run=None):
    extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    if run is not None:
        extra["HTTP_X_VANTAGE_RUN"] = run
    return extra


def send(client, method, url, data=None, **extra):
    return getattr(client, method)(
        url, data=json.dumps(data or {}), content_type="application/json", **extra
    )


# ── undo endpoint ───────────────────────────────────────────────────────────


class TestUndoEndpoint:
    def _run(self, make, author, key="r1", **kwargs):
        identity, token = make(**kwargs)
        body = {"fields": {"title": "t", "slug": "s-" + key, "body": "b", "author": author.pk}}
        assert send(Client(), "post", api_url(), body, **bearer(token, key)).status_code == 201
        return identity, token

    def test_switched_off_with_agents_off(self, make, author, owner, settings):
        self._run(make, author)
        settings.VANTAGE = {"AGENTS": False}
        client = Client()
        client.force_login(owner)
        assert send(client, "post", undo_url("r1")).status_code == 404
        assert Post.objects.filter(slug="s-r1").exists()

    def test_a_stranger_cannot_tell_a_real_run_from_a_missing_one(self, make, author):
        self._run(make, author)
        stranger = User.objects.create_user("carl", is_staff=True)
        client = Client()
        client.force_login(stranger)
        real = send(client, "post", undo_url("r1"))
        missing = send(client, "post", undo_url("nope"))
        assert real.status_code == missing.status_code == 404
        assert real.json() == missing.json()

    def test_an_ambiguous_key_is_not_disclosed_to_a_stranger(self, make, author, svc):
        self._run(make, author)
        other_owner = User.objects.create_user("bob", is_staff=True)
        _other, token = agents.create_identity("bot2", svc, other_owner, access="write")
        send(Client(), "post", api_url(), {"fields": {}}, **bearer(token, "r1"))
        assert AgentRun.objects.filter(run_key="r1").count() == 2
        stranger = User.objects.create_user("carl", is_staff=True)
        client = Client()
        client.force_login(stranger)
        assert send(client, "post", undo_url("r1")).status_code == 404
        boss = User.objects.create_superuser("boss", "b@example.com", "x")
        client.force_login(boss)
        assert send(client, "post", undo_url("r1")).status_code == 400


# ── run keys ────────────────────────────────────────────────────────────────


class TestRunKeys:
    @pytest.mark.parametrize(
        "key",
        ["a\x00b", "has spaces", "café\U0001f600", "x" * 200, "../../etc", "=cmd|' /C calc'!A0"],
    )
    def test_odd_keys_are_normalised_and_never_break_a_request(self, make, key):
        identity, token = make()
        for _ in range(2):
            assert Client().get(api_url(), **bearer(token, key)).status_code == 200
        keys = list(AgentRun.objects.filter(identity=identity).values_list("run_key", flat=True))
        assert len(keys) == 1, "the same raw key is still one run"
        assert re.fullmatch(r"[A-Za-z0-9._:@+=-]{1,64}", keys[0])

    def test_distinct_raw_keys_stay_distinct(self, make):
        identity, token = make()
        for key in ("a b", "a\tb", "x" * 100 + "1", "x" * 100 + "2"):
            Client().get(api_url(), **bearer(token, key))
        assert AgentRun.objects.filter(identity=identity).count() == 4

    def test_a_plain_key_is_kept_as_sent(self, make):
        identity, token = make()
        Client().get(api_url(), **bearer(token, "nightly-2025-01-31"))
        assert AgentRun.objects.get(identity=identity).run_key == "nightly-2025-01-31"


# ── content scrubbing ───────────────────────────────────────────────────────


class TestScrubbing:
    @pytest.mark.parametrize(
        "char",
        [
            "\U000e0100",  # variation selector supplement
            "؜",  # Arabic letter mark
            "⁪",  # deprecated format controls
            "⁯",
            "￹",  # interlinear annotation
            "￻",
            "᠎",
            "ㅤ",  # Hangul filler
            "͏",  # combining grapheme joiner
            "︀",  # variation selector-1
            "\N{LINE SEPARATOR}",
        ],
    )
    def test_more_invisible_characters_are_removed(self, char):
        out = agentdata.scrub_text(f"ab{char}cd")
        assert char not in out
        assert out.replace("\n", "") == "abcd"

    def test_emoji_presentation_selector_survives(self):
        assert agentdata.scrub_text("❤️ ok") == "❤️ ok"

    def test_a_huge_string_is_cut_before_it_is_parsed(self):
        started = time.perf_counter()
        out = agentdata.scrub_text("<a" * 200_000)
        assert time.perf_counter() - started < 3
        assert "truncated" in out

    def test_the_truncation_note_counts_the_original_length(self):
        out = agentdata.scrub_text("y" * 50_000, max_chars=100)
        assert out.endswith("[truncated, 49900 more characters]")

    def test_a_very_deep_value_does_not_crash(self):
        deep: object = "leaf"
        for _ in range(5000):
            deep = [deep]
        agentdata.scrub_value(deep)
        agentdata.wrap_untrusted({"x": deep})


# ── tenancy ─────────────────────────────────────────────────────────────────


class TestTenantCache:
    def test_a_tenant_resolved_for_the_session_user_is_not_kept_for_the_agent(self, make):
        from vantage import tenancy

        _, token = make()
        request = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {token}")
        request.user = User.objects.create_user("human", is_staff=True)
        setattr(request, tenancy.REQUEST_ATTR, "tenant-of-the-human")
        agents.authenticate_bearer(request)
        assert not hasattr(request, tenancy.REQUEST_ATTR)


# ── service users ───────────────────────────────────────────────────────────


class TestServiceUser:
    def test_a_person_with_a_password_cannot_become_a_service_user(self, db):
        person = User.objects.create_user("dana", password="correct horse", is_staff=True)
        with pytest.raises(agents.AgentError):
            agents.create_identity("bot", person)

    def test_the_command_refuses_it_too(self, db):
        User.objects.create_user("dana", password="correct horse", is_staff=True)
        with pytest.raises(CommandError):
            call_command(
                "vantage_agent", "create", "--name", "x", "--user", "dana", stdout=io.StringIO()
            )

    def test_a_service_user_cannot_sign_in_with_a_password(self, make, svc):
        make()
        assert not svc.has_usable_password()
        assert not Client().login(username="svc", password="")
        assert (
            Client().post("/admin/login/", {"username": "svc", "password": ""}).status_code != 302
        )


# ── import dry run ──────────────────────────────────────────────────────────


class TestImportDryRun:
    def _csv(self, author):
        text = f"title,slug,body,author\nT,import-dry,B,{author.pk}\n"
        return SimpleUploadedFile("p.csv", text.encode(), content_type="text/csv")

    def _import(self, client, token, author, **extra):
        url = reverse("vantage:api-import", kwargs={"app_label": "testapp", "model_name": "post"})
        return client.post(url, {"file": self._csv(author), **extra}, **bearer(token))

    def test_an_agent_dry_run_runs_no_save_and_fires_no_signal(self, make, author):
        _, token = make()
        seen = []

        def receiver(sender, **kwargs):
            seen.append(sender)

        post_save.connect(receiver, sender=Post)
        try:
            response = self._import(Client(), token, author)
        finally:
            post_save.disconnect(receiver, sender=Post)
        body = response.json()
        assert response.status_code == 200 and body["dry_run"] is True, body
        assert body["created"] == 1 and body["failed"] == 0
        assert seen == []
        assert not Post.objects.filter(slug="import-dry").exists()

    def test_an_agent_dry_run_still_reports_a_bad_row(self, make, author):
        _, token = make()
        bad = SimpleUploadedFile("p.csv", b"title,slug,body,author\nT,s,B,99999\n")
        url = reverse("vantage:api-import", kwargs={"app_label": "testapp", "model_name": "post"})
        body = Client().post(url, {"file": bad}, **bearer(token)).json()
        assert body["failed"] == 1

    def test_a_real_import_still_writes(self, make, author):
        _, token = make()
        response = self._import(Client(), token, author, dry_run="false")
        assert response.status_code == 200 and response.json()["created"] == 1
        assert Post.objects.filter(slug="import-dry").exists()


# ── MCP transport ───────────────────────────────────────────────────────────


class TestMcpTransport:
    def _post(self, token, body, **extra):
        return Client().post(
            URL, data=json.dumps(body), content_type="application/json", **bearer(token), **extra
        )

    def test_a_non_ascii_digit_content_length_is_413_not_500(self, make):
        _, token = make()
        response = self._post(
            token, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, CONTENT_LENGTH="²"
        )
        assert response.status_code == 413

    def test_origin_scheme_and_port_must_match_or_be_listed(self, make):
        _, token = make()
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for origin in (
            "null",
            "http://testserver:8000",
            "http://evil.test",
            "",
            "http://testserver@evil.test",
        ):
            assert self._post(token, ping, HTTP_ORIGIN=origin).status_code == 403, origin
        assert self._post(token, ping, HTTP_ORIGIN="http://TESTSERVER").status_code == 200

    def test_a_model_label_with_a_trailing_newline_is_refused(self, make):
        _, token = make()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "vantage_list_records", "arguments": {"model": "testapp.post\n"}},
        }
        reply = self._post(token, body).json()
        assert reply["error"]["code"] == mcp.INVALID_PARAMS

    def test_a_filter_name_with_a_newline_is_refused(self, make):
        _, token = make()
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "vantage_list_records",
                "arguments": {"model": "testapp.post", "filters": {"status\n": "x"}},
            },
        }
        assert self._post(token, body).json()["error"]["code"] == mcp.INVALID_PARAMS

    def test_stdio_drops_an_oversized_line(self, make, svc):
        identity, _ = make()
        server = mcp.McpServer(site, mcp.stdio_request(identity))
        line = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "x" * (mcp.MAX_BODY_BYTES + 10)}
        )
        out = io.StringIO()
        mcp.serve_stdio(server, io.StringIO(line + "\n"), out)
        reply = json.loads(out.getvalue())
        assert reply["error"]["code"] == mcp.INVALID_REQUEST


class TestSavedViews:
    def _url(self):
        return reverse("vantage:api-views", kwargs={"app_label": "testapp", "model_name": "post"})

    def test_an_agent_may_not_share_a_view(self, make):
        _, token = make()
        shared = send(
            Client(), "post", self._url(), {"name": "v", "visibility": "shared"}, **bearer(token)
        )
        assert shared.status_code == 403
        private = send(Client(), "post", self._url(), {"name": "v"}, **bearer(token))
        assert private.status_code == 201 and private.json()["visibility"] == "private"
        detail = reverse(
            "vantage:api-view-detail",
            kwargs={"app_label": "testapp", "model_name": "post", "view_pk": private.json()["id"]},
        )
        assert (
            send(Client(), "patch", detail, {"visibility": "shared"}, **bearer(token)).status_code
            == 403
        )
