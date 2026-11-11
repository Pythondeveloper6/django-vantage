"""Agent writes: dry run, approvals for agents, undo by run, the delete guard."""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import post_save
from django.test import Client
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage import agents
from vantage.actions import Action, delete_selected
from vantage.models import AgentRun, ChangeRequest, VantageLogEntry
from vantage.sites import site

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "write"}
    cache.clear()


def api_url(model="post"):
    return reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": model})


def detail(obj):
    return reverse(
        "vantage:api-detail",
        kwargs={"app_label": "testapp", "model_name": type(obj)._meta.model_name, "pk": obj.pk},
    )


def action_url(name, model="post"):
    return reverse(
        "vantage:api-action-run",
        kwargs={"app_label": "testapp", "model_name": model, "action_name": name},
    )


def undo_url(key):
    return reverse("vantage:api-agent-run-undo", kwargs={"run_key": key})


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
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


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


def bearer(token, run=None):
    extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    if run:
        extra["HTTP_X_VANTAGE_RUN"] = run
    return extra


def send(client, method, url, data=None, **extra):
    return getattr(client, method)(
        url,
        data=json.dumps(data if data is not None else {}),
        content_type="application/json",
        **extra,
    )


def new_post(author, slug="p", **kwargs):
    kwargs.setdefault("title", "t")
    return Post.objects.create(slug=slug, body="b", author=author, **kwargs)


def create_body(author, slug="n", **fields):
    return {"fields": {"title": "t", "slug": slug, "body": "b", "author": author.pk, **fields}}


@pytest.fixture
def delete_action(monkeypatch):
    monkeypatch.setattr(site.get_panel(Post), "actions", [delete_selected], raising=False)


# ── dry run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_create_validates_and_writes_nothing(self, make, author):
        _identity, token = make()
        seen = []

        def receiver(**kwargs):
            seen.append(kwargs)

        post_save.connect(receiver, sender=Post, weak=False)
        try:
            response = send(
                Client(), "post", api_url() + "?dry_run=1", create_body(author), **bearer(token)
            )
        finally:
            post_save.disconnect(receiver, sender=Post)
        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True and body["would"] == "create" and body["valid"] is True
        assert body["errors"] == {} and body["cascade"] == {}
        assert {"field": "slug", "from": None, "to": "n"} in body["diff"]
        assert Post.objects.count() == 0 and not seen
        assert not VantageLogEntry.objects.exists()

    def test_the_header_works_too_and_invalid_input_is_reported_not_raised(self, make, author):
        _, token = make()
        data = create_body(author)
        del data["fields"]["title"]
        response = send(
            Client(), "post", api_url(), data, HTTP_X_VANTAGE_DRY_RUN="1", **bearer(token)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False and "title" in body["errors"] and body["diff"] == []

    def test_update_shows_the_diff_and_leaves_the_row(self, make, author):
        _, token = make()
        post = new_post(author)
        response = send(
            Client(),
            "patch",
            detail(post) + "?dry_run=true",
            {"fields": {"title": "new"}},
            **bearer(token),
        )
        body = response.json()
        assert response.status_code == 200 and body["would"] == "update"
        assert body["diff"] == [{"field": "title", "from": "t", "to": "new"}]
        post.refresh_from_db()
        assert post.title == "t"
        assert not VantageLogEntry.objects.exists()

    def test_a_sensitive_field_cannot_be_named_by_an_agent(self, make, author, monkeypatch):
        monkeypatch.setattr(site.get_panel(Post), "sensitive_fields", ["summary"], raising=False)
        _, token = make()
        post = new_post(author, summary="old-secret")
        response = send(
            Client(),
            "patch",
            detail(post) + "?dry_run=1",
            {"fields": {"summary": "new-secret", "title": "x"}},
            **bearer(token),
        )
        text = response.content.decode()
        assert response.status_code == 422 and "summary" in response.json()["errors"]
        assert "old-secret" not in text and "new-secret" not in text

    def test_a_stale_version_is_refused_as_the_real_write_would(self, make, author):
        _, token = make()
        post = new_post(author)
        response = send(
            Client(),
            "patch",
            detail(post) + "?dry_run=1",
            {"fields": {"title": "x"}, "version": "not-the-version"},
            **bearer(token),
        )
        assert response.status_code in (200, 409)
        real = send(
            Client(),
            "patch",
            detail(post),
            {"fields": {"title": "x"}, "version": "not-the-version"},
            **bearer(token),
        )
        assert response.status_code == real.status_code

    def test_delete_returns_the_cascade_preview(self, make, svc, author):
        grant(svc, "delete_author")
        _, token = make(allow_delete=True)
        new_post(author, "a")
        new_post(author, "b")
        response = Client().delete(detail(author) + "?dry_run=1", **bearer(token))
        body = response.json()
        assert response.status_code == 200 and body["would"] == "delete"
        assert body["valid"] is True
        assert {"label": "Posts", "count": 2} in body["cascade"]["groups"]
        assert Author.objects.filter(pk=author.pk).exists() and Post.objects.count() == 2

    def test_dry_run_refuses_what_the_real_call_refuses(self, make, svc, author):
        _, token = make(access="read", name="reader")
        client = Client()
        assert (
            send(client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(token))
        ).status_code == 403
        _, nodel = make(name="nodelete")
        post = new_post(author)
        assert client.delete(detail(post) + "?dry_run=1", **bearer(nodel)).status_code == 403
        _, narrow = make(name="narrow", allowed_models=["testapp.author"])
        assert (
            send(client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(narrow))
        ).status_code == 404
        svc.user_permissions.remove(Permission.objects.get(codename="add_post"))
        assert (
            send(client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(nodel))
        ).status_code == 403

    def test_unknown_fields_are_refused_in_a_dry_run_too(self, make, author):
        _, token = make()
        data = create_body(author, nonsense=1)
        response = send(Client(), "post", api_url() + "?dry_run=1", data, **bearer(token))
        assert response.status_code == 400

    def test_a_dry_run_spends_no_write_budget_but_is_rate_limited(self, make, author):
        identity, token = make(max_writes_per_run=1, rate_limit_per_minute=5)
        client = Client()
        for _ in range(3):
            response = send(
                client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(token, "r")
            )
            assert response.status_code == 200
        run = AgentRun.objects.get(identity=identity, run_key="r")
        assert run.writes == 0
        assert (
            send(client, "post", api_url(), create_body(author), **bearer(token, "r")).status_code
            == 201
        )
        assert (
            send(
                client, "post", api_url(), create_body(author, "m"), **bearer(token, "r")
            ).status_code
            == 403
        )
        # 5 requests so far in the minute: the 6th is limited, dry run or not.
        limited = send(
            client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(token, "r")
        )
        assert limited.status_code == 429

    def test_any_signed_in_caller_can_dry_run(self, boss, author):
        client = Client()
        client.force_login(boss)
        response = send(client, "post", api_url() + "?dry_run=1", create_body(author))
        assert response.status_code == 200 and response.json()["would"] == "create"
        assert Post.objects.count() == 0

    def test_a_bulk_action_reports_how_many_rows_it_would_touch(self, make, author, delete_action):
        _, token = make(allow_delete=True)
        posts = [new_post(author, f"p{i}") for i in range(3)]
        response = send(
            Client(),
            "post",
            action_url("delete_selected") + "?dry_run=1",
            {"ids": [p.pk for p in posts]},
            **bearer(token),
        )
        body = response.json()
        assert response.status_code == 200 and body["would"] == "run_action"
        assert body["count"] == 3 and body["valid"] is True and body["dry_run"] is True
        assert Post.objects.count() == 3

    def test_a_dry_run_bulk_delete_is_refused_without_allow_delete(
        self, make, author, delete_action
    ):
        _, token = make()
        post = new_post(author)
        response = send(
            Client(),
            "post",
            action_url("delete_selected") + "?dry_run=1",
            {"ids": [post.pk]},
            **bearer(token),
        )
        assert response.status_code == 403


# ── the delete guard ────────────────────────────────────────────────────────


class TestDeleteGuard:
    def test_bulk_delete_needs_allow_delete(self, make, author, delete_action):
        _identity, token = make()
        posts = [new_post(author, f"p{i}") for i in range(2)]
        body = {"ids": [p.pk for p in posts], "confirmed": True}
        response = send(Client(), "post", action_url("delete_selected"), body, **bearer(token))
        assert response.status_code == 403
        assert Post.objects.count() == 2

    def test_with_allow_delete_it_runs_and_is_counted(self, make, author, delete_action):
        identity, token = make(allow_delete=True)
        posts = [new_post(author, f"p{i}") for i in range(2)]
        body = {"ids": [p.pk for p in posts], "confirmed": True}
        response = send(
            Client(), "post", action_url("delete_selected"), body, **bearer(token, "r1")
        )
        assert response.status_code == 200, response.content
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        assert run.deletes == 2 and run.rows_changed == 2 and run.writes == 1

    def test_a_destructive_flag_marks_a_custom_action(self, make, author, monkeypatch):
        def purge(panel, request, queryset):
            return queryset.update(summary="")

        custom = Action(func=purge, name="purge", label="Purge", destructive=True)
        plain = Action(func=purge, name="tidy", label="Tidy")
        monkeypatch.setattr(site.get_panel(Post), "actions", [custom, plain], raising=False)
        _, token = make()
        post = new_post(author, summary="x")
        body = {"ids": [post.pk], "confirmed": True}
        assert send(Client(), "post", action_url("purge"), body, **bearer(token)).status_code == 403
        assert send(Client(), "post", action_url("tidy"), body, **bearer(token)).status_code == 200


# ── approvals for agents ────────────────────────────────────────────────────


class TestApprovals:
    def test_a_patch_becomes_a_change_request(self, make, author):
        identity, token = make(require_approval=True)
        post = new_post(author)
        response = send(
            Client(),
            "patch",
            detail(post),
            {"fields": {"title": "new", "summary": "s"}, "note": "please"},
            **bearer(token, "r1"),
        )
        assert response.status_code == 202, response.content
        body = response.json()
        assert body["status"] == "pending_approval" and body["url"]
        request = ChangeRequest.objects.get(pk=body["change_request"])
        assert sorted(request.field_names) == ["summary", "title"] and request.is_pending
        assert request.note == "please" and request.requested_by_repr.startswith("Agent bot")
        post.refresh_from_db()
        assert post.title == "t" and post.summary == ""
        entry = VantageLogEntry.objects.get(action="action")
        assert entry.agent == identity and entry.agent_run.run_key == "r1"
        assert not VantageLogEntry.objects.filter(action="update").exists()

    def test_put_cannot_bypass_it(self, make, author):
        _, token = make(require_approval=True)
        post = new_post(author)
        tag_free = {
            "title": "via put",
            "slug": "p",
            "body": "b",
            "author": author.pk,
            "status": "draft",
            "view_count": 0,
        }
        response = send(Client(), "put", detail(post), {"fields": tag_free}, **bearer(token))
        assert response.status_code == 202, response.content
        post.refresh_from_db()
        assert post.title == "t"
        pending = ChangeRequest.objects.get()
        assert "title" in pending.field_names and "tags" not in pending.field_names

    def test_omitting_fields_cannot_bypass_it(self, make, author):
        _, token = make(require_approval=True)
        post = new_post(author)
        # A PATCH naming only some fields still holds those.
        response = send(
            Client(), "patch", detail(post), {"fields": {"status": "review"}}, **bearer(token)
        )
        assert response.status_code == 202
        post.refresh_from_db()
        assert post.status == "draft"

    def test_a_relation_that_cannot_wait_is_refused(self, make, author):
        _, token = make(require_approval=True)
        post = new_post(author)
        response = send(Client(), "patch", detail(post), {"fields": {"tags": []}}, **bearer(token))
        assert response.status_code == 403
        assert response.json()["code"] == "approval_not_supported"
        assert not ChangeRequest.objects.exists()

    def test_an_unchanged_value_writes_and_asks_for_nothing(self, make, author):
        _, token = make(require_approval=True)
        post = new_post(author)
        response = send(
            Client(), "patch", detail(post), {"fields": {"title": "t"}}, **bearer(token)
        )
        assert response.status_code == 200
        assert not ChangeRequest.objects.exists() and not VantageLogEntry.objects.exists()

    def test_create_delete_bulk_import_and_saved_views_are_refused(
        self, make, svc, author, delete_action
    ):
        grant(svc, "delete_author")
        identity, token = make(require_approval=True, allow_delete=True)
        post = new_post(author)
        client = Client()
        refused = [
            send(client, "post", api_url(), create_body(author), **bearer(token)),
            client.delete(detail(post), **bearer(token)),
            send(
                client,
                "post",
                action_url("delete_selected"),
                {"ids": [post.pk], "confirmed": True},
                **bearer(token),
            ),
            client.post(
                reverse(
                    "vantage:api-import", kwargs={"app_label": "testapp", "model_name": "post"}
                ),
                {
                    "file": SimpleUploadedFile("a.csv", b"title,slug,body\nx,x,x\n"),
                    "dry_run": "false",
                },
                **bearer(token),
            ),
            send(
                client,
                "post",
                reverse("vantage:api-views", kwargs={"app_label": "testapp", "model_name": "post"}),
                {"name": "v"},
                **bearer(token),
            ),
        ]
        for response in refused:
            assert response.status_code == 403, response.content
            assert response.json()["code"] == "approval_not_supported"
            assert "requires approval" in response.json()["detail"]
        assert Post.objects.count() == 1 and Post.objects.get().pk == post.pk
        assert not AgentRun.objects.filter(identity=identity, writes__gt=0).exists()

    def test_dry_runs_still_work_for_such_an_identity(self, make, author, delete_action):
        _, token = make(require_approval=True, allow_delete=True)
        post = new_post(author)
        client = Client()
        create = send(
            client, "post", api_url() + "?dry_run=1", create_body(author), **bearer(token)
        )
        assert create.status_code == 200 and create.json()["valid"] is True
        update = send(
            client,
            "patch",
            detail(post) + "?dry_run=1",
            {"fields": {"title": "z"}},
            **bearer(token),
        )
        assert update.status_code == 200
        assert update.json()["requires_approval"] == ["title"]
        assert client.delete(detail(post) + "?dry_run=1", **bearer(token)).status_code == 200
        bulk = send(
            client,
            "post",
            action_url("delete_selected") + "?dry_run=1",
            {"ids": [post.pk]},
            **bearer(token),
        )
        assert bulk.status_code == 200 and bulk.json()["count"] == 1
        assert Post.objects.count() == 1 and not ChangeRequest.objects.exists()

    def test_an_ordinary_agent_is_unaffected(self, make, author):
        _, token = make()
        post = new_post(author)
        response = send(
            Client(), "patch", detail(post), {"fields": {"title": "n"}}, **bearer(token)
        )
        assert response.status_code == 200
        post.refresh_from_db()
        assert post.title == "n"

    def test_a_person_approves_and_the_audit_entry_names_the_agent(self, make, author, boss):
        identity, token = make(require_approval=True)
        post = new_post(author)
        response = send(
            Client(), "patch", detail(post), {"fields": {"title": "new"}}, **bearer(token)
        )
        pending = ChangeRequest.objects.get(pk=response.json()["change_request"])
        client = Client()
        client.force_login(boss)
        decided = client.post(
            reverse("vantage:approval-review", args=[pending.pk]), {"decision": "approve"}
        )
        assert decided.status_code == 302
        post.refresh_from_db()
        assert post.title == "new"
        entry = VantageLogEntry.objects.filter(action="update").get()
        assert entry.actor == boss
        assert entry.action_name.startswith("approved change to title")
        assert f"proposed by agent {identity.name}" in entry.action_name

    def test_an_agent_token_can_neither_approve_nor_reach_the_page(self, make, svc, author):
        grant(svc, "change_post")
        _, token = make(require_approval=True)
        post = new_post(author)
        response = send(
            Client(), "patch", detail(post), {"fields": {"title": "new"}}, **bearer(token)
        )
        pending = ChangeRequest.objects.get(pk=response.json()["change_request"])
        url = reverse("vantage:approval-review", args=[pending.pk])
        assert Client().post(url, {"decision": "approve"}, **bearer(token)).status_code == 403
        assert Client().get(url, **bearer(token)).status_code == 403
        pending.refresh_from_db()
        assert pending.is_pending


# ── undo by run ─────────────────────────────────────────────────────────────


def run_agent(token, key, method, url, data=None):
    return send(Client(), method, url, data, **bearer(token, key))


class TestUndoRun:
    def test_it_reverts_creates_and_updates(self, make, owner, author):
        identity, token = make()
        existing = new_post(author, "old", title="before")
        created = run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        assert created.status_code == 201
        run_agent(
            token,
            "r1",
            "patch",
            detail(existing),
            {"fields": {"title": "after", "status": "review"}},
        )
        run = AgentRun.objects.get(identity=identity, run_key="r1")

        report = agents.undo_run(run, owner)
        assert report["conflicts"] == [] and report["skipped"] == []
        actions = sorted((item["action"], sorted(item["fields"])) for item in report["reverted"])
        assert actions == [("deleted", []), ("restored", ["status", "title"])]
        existing.refresh_from_db()
        assert (existing.title, existing.status) == ("before", "draft")
        assert not Post.objects.filter(slug="fresh").exists()
        undo_entries = VantageLogEntry.objects.filter(action_name="undo of run r1")
        assert undo_entries.count() == 2
        assert {e.actor for e in undo_entries} == {owner} and not any(e.agent for e in undo_entries)

    def test_dry_run_changes_nothing(self, make, owner, author):
        identity, token = make()
        existing = new_post(author, "old", title="before")
        run_agent(token, "r1", "patch", detail(existing), {"fields": {"title": "after"}})
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, owner, dry_run=True)
        assert report["dry_run"] is True and len(report["reverted"]) == 1
        existing.refresh_from_db()
        assert existing.title == "after"

    def test_a_row_changed_afterwards_is_a_conflict_and_left_alone(self, make, owner, author, boss):
        identity, token = make()
        existing = new_post(author, "old", title="before")
        run_agent(token, "r1", "patch", detail(existing), {"fields": {"title": "after"}})
        human = Client()
        human.force_login(boss)
        assert (
            send(human, "patch", detail(existing), {"fields": {"title": "by a person"}}).status_code
            == 200
        )
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, owner)
        assert len(report["conflicts"]) == 1 and report["reverted"] == []
        existing.refresh_from_db()
        assert existing.title == "by a person"

    def test_a_write_the_log_did_not_see_is_a_conflict(self, make, owner, author):
        identity, token = make()
        created = run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        Post.objects.filter(pk=created.json()["pk"]).update(title="changed by a script")
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, owner)
        assert len(report["conflicts"]) == 1
        assert Post.objects.filter(slug="fresh").exists()

    def test_deleted_rows_are_reported_not_rebuilt(self, make, owner, author):
        identity, token = make(allow_delete=True)
        post = new_post(author)
        assert Client().delete(detail(post), **bearer(token, "r1")).status_code == 204
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, owner)
        assert len(report["skipped"]) == 1 and "cannot be rebuilt" in report["skipped"][0]["reason"]
        assert not Post.objects.exists()

    def test_running_it_twice_is_safe(self, make, owner, author):
        identity, token = make()
        existing = new_post(author, "old", title="before")
        run_agent(token, "r1", "patch", detail(existing), {"fields": {"title": "after"}})
        run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        agents.undo_run(run, owner)
        again = agents.undo_run(run, owner)
        assert again["conflicts"] == [] and again["reverted"] == []
        assert len(again["skipped"]) == 2

    def test_the_by_users_own_permissions_apply(self, make, owner, author):
        identity, token = make()
        existing = new_post(author, "old", title="before")
        run_agent(token, "r1", "patch", detail(existing), {"fields": {"title": "after"}})
        owner.user_permissions.remove(Permission.objects.get(codename="change_post"))
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, User.objects.get(pk=owner.pk))
        assert report["reverted"] == [] and len(report["skipped"]) == 1
        existing.refresh_from_db()
        assert existing.title == "after"

    def test_a_sensitive_field_is_never_in_the_report(self, make, owner, author, monkeypatch):
        identity, token = make()
        existing = new_post(author, "old", summary="hunter2")
        run_agent(token, "r1", "patch", detail(existing), {"fields": {"summary": "s3cret-new"}})
        # Declared sensitive after the run: the report must still never show the value.
        monkeypatch.setattr(site.get_panel(Post), "sensitive_fields", ["summary"], raising=False)
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        report = agents.undo_run(run, owner)
        text = json.dumps(report)
        assert "hunter2" not in text and "s3cret-new" not in text
        assert len(report["skipped"]) == 1 and report["reverted"] == []
        existing.refresh_from_db()
        assert existing.summary == "s3cret-new"

    def test_only_a_superuser_or_the_owner_may_undo(self, make, svc, owner, author, boss):
        identity, token = make()
        run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        run = AgentRun.objects.get(identity=identity, run_key="r1")
        stranger = User.objects.create_user("carl", is_staff=True)
        with pytest.raises(agents.UndoNotAllowed):
            agents.undo_run(run, stranger)
        with pytest.raises(agents.UndoNotAllowed):
            agents.undo_run(run, svc)
        assert agents.undo_run(run, boss, dry_run=True)["reverted"]

    def test_the_api_is_for_people_with_a_session(self, make, owner, svc, author, boss):
        _identity, token = make()
        run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        client = Client()
        # An agent token, even the run's own, is refused outright.
        assert send(client, "post", undo_url("r1"), {}, **bearer(token)).status_code == 403
        assert (
            send(client, "post", undo_url("r1"), {}, HTTP_AUTHORIZATION="Bearer junk").status_code
            == 403
        )
        assert Post.objects.filter(slug="fresh").exists()
        # A staff member who is neither superuser nor owner.
        stranger = User.objects.create_user("carl", is_staff=True)
        client.force_login(stranger)
        assert send(client, "post", undo_url("r1")).status_code == 403
        # The owner, with ?dry_run=1 first.
        client.force_login(owner)
        preview = send(client, "post", undo_url("r1") + "?dry_run=1")
        assert preview.status_code == 200 and preview.json()["dry_run"] is True
        assert Post.objects.filter(slug="fresh").exists()
        done = send(client, "post", undo_url("r1"))
        assert done.status_code == 200 and len(done.json()["reverted"]) == 1
        assert not Post.objects.filter(slug="fresh").exists()
        assert send(client, "post", undo_url("missing")).status_code == 404

    def test_the_command(self, make, owner, author):
        _identity, token = make()
        run_agent(token, "r1", "post", api_url(), create_body(author, "fresh"))
        out = io.StringIO()
        call_command("vantage_agent", "undo-run", "bot", "r1", "--dry-run", stdout=out)
        assert "Would revert 1" in out.getvalue() and Post.objects.filter(slug="fresh").exists()
        out = io.StringIO()
        call_command("vantage_agent", "undo-run", "bot", "r1", stdout=out)
        assert "Reverted 1" in out.getvalue() and not Post.objects.filter(slug="fresh").exists()
        with pytest.raises(CommandError):
            call_command("vantage_agent", "undo-run", "bot", "nope", stdout=out)
        with pytest.raises(CommandError):
            call_command("vantage_agent", "undo-run", "bot", "r1", "--as", "svc", stdout=out)
