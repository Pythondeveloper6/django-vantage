"""The JSON API's bulk actions, saved views, import/export, uploads and OpenAPI document.

As in ``test_api.py`` the point is reuse: each endpoint must answer what the HTML feature
already answers (permissions, tenancy, validators, masking), so these tests prove the
doors are closed by the shared machinery and that the new endpoints are thin enough not
to have opened a second one.
"""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse

from tests.test_api import (
    CSRF_MIDDLEWARE,
    TENANT_MIDDLEWARE,
    TENANT_SETTINGS,
    body,
    post_json,
)
from tests.test_media import HTML, PNG_1PX
from tests.testapp.models import Author, Post, Promo, Snippet
from vantage import jobs, savedviews, site
from vantage.actions import action, delete_selected
from vantage.models import SavedView
from vantage.panels import ModelPanel

pytestmark = pytest.mark.django_db


def url(name: str, model=Promo, **extra) -> str:
    opts = model._meta
    return reverse(
        f"vantage:{name}",
        kwargs={"app_label": opts.app_label, "model_name": opts.model_name, **extra},
    )


def grant(user, model, *codenames):
    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(*Permission.objects.filter(content_type=ct, codename__in=codenames))


def staff(name, model, *codenames) -> Client:
    user = User.objects.create_user(name, password="x", is_staff=True)
    grant(user, model, *codenames)
    client = Client()
    client.force_login(User.objects.get(pk=user.pk))
    return client


@pytest.fixture
def boss(db):
    client = Client()
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def swap_panel():
    swapped = []

    def _swap(model, panel_class):
        swapped.append((model, site.registry._panels.get(model)))
        site.registry._panels[model] = panel_class(model, site)

    yield _swap
    for model, original in reversed(swapped):
        site.registry._panels[model] = original


# ── bulk actions ────────────────────────────────────────────────────────────


class TestActions:
    def promos(self, n=4):
        return [Promo.objects.create(headline=f"promo {i}") for i in range(n)]

    def run(self, client, name, data, query=""):
        return post_json(client, url("api-action-run", action_name=name) + query, data)

    def test_the_list_respects_per_action_permissions(self):
        viewer = staff("viewer", Promo, "view_promo")
        assert body(viewer.get(url("api-actions")))["actions"] == []
        editor = staff("editor", Promo, "view_promo", "change_promo")
        names = {a["name"] for a in body(editor.get(url("api-actions")))["actions"]}
        assert names == {"publish", "unpublish"}

    def test_running_needs_the_actions_own_permission(self):
        rows = self.promos()
        viewer = staff("viewer2", Promo, "view_promo")
        response = self.run(viewer, "publish", {"ids": [rows[0].pk]})
        assert response.status_code == 403
        assert not Promo.objects.filter(is_published=True).exists()

    def test_an_unknown_action_is_the_same_refusal(self, boss):
        assert self.run(boss, "nope", {"ids": [1]}).status_code == 403

    def test_a_run_over_explicit_ids_reports_what_it_touched(self, boss):
        rows = self.promos()
        response = self.run(boss, "publish", {"ids": [rows[0].pk, rows[1].pk]})
        assert response.status_code == 200
        assert body(response)["affected"] == 2
        assert Promo.objects.filter(is_published=True).count() == 2

    def test_select_all_matching_uses_the_list_filters_and_exclusions(self, boss):
        self.promos(3)
        extra = Promo.objects.create(headline="special one")
        Promo.objects.create(headline="special two")
        response = self.run(
            boss, "publish", {"select_all": True, "excluded": [extra.pk]}, "?q=special"
        )
        assert body(response)["affected"] == 1
        assert Promo.objects.get(headline="special two").is_published
        assert not Promo.objects.get(headline="special one").is_published
        assert not Promo.objects.get(headline="promo 0").is_published

    def test_dry_run_counts_without_executing(self, boss):
        self.promos(5)
        data = body(self.run(boss, "publish", {"select_all": True, "dry_run": True}))
        assert data["dry_run"] is True and data["count"] == 5
        assert not Promo.objects.filter(is_published=True).exists()

    def test_an_empty_selection_is_a_bad_request(self, boss):
        assert self.run(boss, "publish", {}).status_code == 400

    def test_a_write_without_a_csrf_token_is_refused(self, db):
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("csrf", "c@example.com", "x"))
        promo = Promo.objects.create(headline="x")
        with override_settings(
            MIDDLEWARE=CSRF_MIDDLEWARE, CSRF_FAILURE_VIEW="vantage.api.csrf_failure"
        ):
            response = self.run(client, "publish", {"ids": [promo.pk]})
        assert response.status_code == 403
        assert not Promo.objects.get(pk=promo.pk).is_published

    def test_a_destructive_action_needs_confirmation(self, boss, swap_panel):
        class Panel(ModelPanel):
            actions = [delete_selected]

        swap_panel(Promo, Panel)
        rows = self.promos(2)
        first = self.run(boss, "delete_selected", {"ids": [r.pk for r in rows]})
        assert first.status_code == 428
        assert body(first)["confirmation_required"] is True and body(first)["count"] == 2
        assert Promo.objects.count() == 2
        done = self.run(boss, "delete_selected", {"ids": [r.pk for r in rows], "confirmed": True})
        assert done.status_code == 200
        assert Promo.objects.count() == 0

    def test_max_rows_is_enforced(self, boss, swap_panel):
        class Panel(ModelPanel):
            @action("Cap", max_rows=2)
            def cap(self, request, queryset):
                return "ok"

        swap_panel(Promo, Panel)
        self.promos(3)
        response = self.run(boss, "cap", {"select_all": True})
        assert response.status_code == 422

    def test_a_big_run_is_handed_to_a_job(self, boss, monkeypatch):
        rows = self.promos(2)
        monkeypatch.setattr(jobs, "should_enqueue", lambda act, count: True)
        monkeypatch.setattr(jobs, "_submit", lambda job: None)  # no worker: stays pending
        response = self.run(boss, "publish", {"ids": [r.pk for r in rows]})
        assert response.status_code == 202
        job = body(response)["job"]
        assert job["status_url"] == reverse("vantage:job-status", args=[job["id"]])
        assert not Promo.objects.filter(is_published=True).exists()
        assert boss.get(job["status_url"]).status_code == 200

    @override_settings(VANTAGE=TENANT_SETTINGS, MIDDLEWARE=TENANT_MIDDLEWARE)
    def test_select_all_stays_inside_the_callers_tenant(self, swap_panel):
        class Panel(ModelPanel):
            @action("Rename", permission="change", bulk_strategy="sql")
            def rename(self, request, queryset):
                return f"{queryset.update(title='renamed')}"

        swap_panel(Snippet, Panel)
        alice = User.objects.create_user("alice", password="x", is_staff=True)
        bob = User.objects.create_user("bob", password="x", is_staff=True)
        grant(alice, Snippet, "view_snippet", "change_snippet")
        mine = Snippet.objects.create(title="a", owner=alice)
        theirs = Snippet.objects.create(title="b", owner=bob)
        client = Client()
        client.force_login(alice)
        response = post_json(
            client, url("api-action-run", Snippet, action_name="rename"), {"select_all": True}
        )
        assert body(response)["affected"] == 1
        assert Snippet.objects.get(pk=mine.pk).title == "renamed"
        assert Snippet.objects.get(pk=theirs.pk).title == "b"
        # And explicit ids from another tenant are simply not in scope.
        again = post_json(
            client,
            url("api-action-run", Snippet, action_name="rename"),
            {"ids": [theirs.pk], "dry_run": True},
        )
        assert body(again)["count"] == 0


# ── saved views ─────────────────────────────────────────────────────────────


class TestSavedViews:
    def make(self, client, **data):
        payload = {"name": "Mine", "query": "q=abc&evil=1", **data}
        return post_json(client, url("api-views", Post), payload)

    def test_create_stores_only_what_a_view_may_remember(self):
        client = staff("sv1", Post, "view_post")
        response = self.make(client)
        assert response.status_code == 201
        state = body(response)["state"]
        assert state["q"] == "abc"
        assert "evil" not in json.dumps(state)
        assert body(client.get(url("api-views", Post)))["views"][0]["name"] == "Mine"

    def test_a_private_view_is_invisible_to_others_not_forbidden(self):
        owner = staff("sv2", Post, "view_post")
        pk = body(self.make(owner))["id"]
        other = staff("sv3", Post, "view_post")
        assert body(other.get(url("api-views", Post)))["views"] == []
        assert other.get(url("api-view-detail", Post, view_pk=pk)).status_code == 404

    def test_a_shared_view_is_visible_but_not_deletable_or_editable_by_others(self):
        owner = staff("sv4", Post, "view_post")
        pk = body(self.make(owner, visibility="shared"))["id"]
        other = staff("sv5", Post, "view_post")
        detail = url("api-view-detail", Post, view_pk=pk)
        assert other.get(detail).status_code == 200
        assert other.delete(detail).status_code == 403
        patched = other.patch(
            detail,
            json.dumps({"query": "q=zzz", "version": 1}),
            content_type="application/json",
        )
        assert patched.status_code == 403
        assert SavedView.objects.get(pk=pk).state["q"] == "abc"

    def test_no_model_permission_means_no_views(self):
        client = staff("sv6", Post)
        assert client.get(url("api-views", Post)).status_code == 403

    def test_update_delete_and_default_by_the_owner(self):
        client = staff("sv7", Post, "view_post")
        pk = body(self.make(client))["id"]
        detail = url("api-view-detail", Post, view_pk=pk)
        patched = client.patch(
            detail,
            json.dumps({"query": "q=new", "name": "Renamed", "default": True}),
            content_type="application/json",
        )
        data = body(patched)
        assert data["state"]["q"] == "new" and data["name"] == "Renamed"
        assert data["is_default"] is True
        assert client.delete(detail).status_code == 204
        assert not SavedView.objects.filter(pk=pk).exists()

    def test_a_stale_version_on_a_shared_view_conflicts(self):
        client = staff("sv8", Post, "view_post")
        pk = body(self.make(client, visibility="shared"))["id"]
        detail = url("api-view-detail", Post, view_pk=pk)
        ok = client.patch(
            detail, json.dumps({"query": "q=1", "version": 1}), content_type="application/json"
        )
        assert ok.status_code == 200
        stale = client.patch(
            detail, json.dumps({"query": "q=2", "version": 1}), content_type="application/json"
        )
        assert stale.status_code == 409

    def test_a_locked_view_cannot_be_overwritten(self):
        client = staff("sv9", Post, "view_post")
        pk = body(self.make(client))["id"]
        SavedView.objects.filter(pk=pk).update(locked=True)
        assert self.make(client).status_code == 403

    def test_the_feature_switch_is_honoured(self, monkeypatch):
        client = staff("sv10", Post, "view_post")
        monkeypatch.setattr(savedviews, "enabled", lambda: False)
        if True:
            assert client.get(url("api-views", Post)).status_code == 403


# ── import / export ─────────────────────────────────────────────────────────


def csv_file(text: str, name="promos.csv"):
    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


class TestImport:
    def send(self, client, text, **data):
        return client.post(url("api-import"), {"file": csv_file(text), **data})

    def test_it_is_a_dry_run_unless_told_otherwise(self, boss):
        response = self.send(boss, "headline,is_published\nA,true\nB,false\n")
        data = body(response)
        assert data["dry_run"] is True and data["created"] == 2
        assert Promo.objects.count() == 0

    def test_a_dry_run_reports_per_row_errors(self, boss):
        data = body(self.send(boss, "headline\n" + "x" * 500 + "\nfine\n"))
        assert data["failed"] == 1
        assert data["errors"][0]["row"] == 2 and data["errors"][0]["column"] == "headline"

    def test_commit_writes(self, boss):
        data = body(self.send(boss, "headline\nA\nB\n", dry_run="false"))
        assert data["dry_run"] is False and data["created"] == 2
        assert Promo.objects.count() == 2

    def test_an_explicit_mapping_is_honoured(self, boss):
        self.send(boss, "Title\nA\n", mapping=json.dumps({"Title": "headline"}), dry_run="false")
        assert Promo.objects.filter(headline="A").exists()

    def test_add_permission_is_required(self):
        client = staff("imp1", Promo, "view_promo")
        assert self.send(client, "headline\nA\n", dry_run="false").status_code == 403

    def test_update_mode_needs_change_too(self):
        client = staff("imp2", Promo, "view_promo", "add_promo")
        response = self.send(client, "id,headline\n1,A\n", mode="upsert", key_field="id")
        assert response.status_code == 403

    def test_a_binary_file_is_rejected(self, boss):
        response = boss.post(
            url("api-import"),
            {"file": SimpleUploadedFile("x.xlsx", b"PK\x03\x04\x00\x00" * 40)},
        )
        assert response.status_code == 422

    def test_the_size_limit_is_enforced(self, boss):
        with override_settings(VANTAGE={"IMPORT_MAX_BYTES": 10}):
            response = self.send(boss, "headline\n" + "a" * 100)
        assert response.status_code == 413

    def test_a_missing_file_is_a_bad_request(self, boss):
        assert boss.post(url("api-import"), {}).status_code == 400


class TestExport:
    def test_it_streams_the_filtered_list_like_the_html_export(self, boss):
        for name in ("alpha", "beta", "alphabet"):
            Promo.objects.create(headline=name)
        api = b"".join(boss.get(url("api-export"), {"q": "alpha"}).streaming_content)
        html = b"".join(
            boss.get(
                reverse(site.get_panel(Promo).list_url_name), {"q": "alpha", "export": "csv"}
            ).streaming_content
        )
        assert api == html
        assert b"beta" not in api and b"alphabet" in api

    def test_json_is_newline_delimited(self, boss):
        Promo.objects.create(headline="one")
        response = boss.get(url("api-export"), {"format": "json"})
        lines = b"".join(response.streaming_content).decode().strip().splitlines()
        assert [next(iter(json.loads(line).values())) for line in lines] == ["one"]

    def test_view_permission_is_required(self):
        assert staff("exp1", Promo).get(url("api-export")).status_code == 403

    def test_an_unknown_format_is_refused(self, boss):
        assert boss.get(url("api-export"), {"format": "xml"}).status_code == 400


# ── uploads ─────────────────────────────────────────────────────────────────


def author_fields(**extra):
    return json.dumps({"name": "Ada", "email": "ada@example.com", **extra})


class TestUploads:
    def png(self, name="ada.png"):
        return SimpleUploadedFile(name, PNG_1PX, content_type="image/png")

    def test_create_with_an_image(self, boss):
        response = boss.post(
            url("api-list", Author), {"fields": author_fields(), "avatar": self.png()}
        )
        assert response.status_code == 201, response.content
        assert Author.objects.get().avatar.name.startswith("avatars/")

    def test_a_disguised_file_is_rejected_by_the_media_validators(self, boss):
        liar = SimpleUploadedFile("ada.png", HTML, content_type="image/png")
        response = boss.post(url("api-list", Author), {"fields": author_fields(), "avatar": liar})
        assert response.status_code == 422
        assert "avatar" in body(response)["errors"]
        assert Author.objects.count() == 0

    def test_patch_replaces_and_null_clears(self, boss):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        detail = reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "author", "pk": author.pk},
        )
        data = encode_multipart(BOUNDARY, {"avatar": self.png()})
        response = boss.patch(detail, data, content_type=MULTIPART_CONTENT)
        assert response.status_code == 200, response.content
        assert Author.objects.get().avatar
        cleared = encode_multipart(BOUNDARY, {"fields": json.dumps({"avatar": None})})
        response = boss.patch(detail, cleared, content_type=MULTIPART_CONTENT)
        assert response.status_code == 200, response.content
        assert not Author.objects.get().avatar

    def test_an_upload_to_a_non_editable_field_is_refused(self, boss):
        response = boss.post(
            url("api-list", Author),
            {"fields": author_fields(), "created_at": self.png()},
        )
        assert response.status_code == 400

    def test_bad_fields_json_is_a_bad_request(self, boss):
        response = boss.post(url("api-list", Author), {"fields": "{nope"})
        assert response.status_code == 400


# ── OpenAPI ─────────────────────────────────────────────────────────────────

HTTP_VERBS = {"get", "put", "post", "delete", "patch", "head", "options"}


def collect_refs(node, found):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                found.append(value)
            else:
                collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, found)


def assert_valid_structure(doc: dict) -> None:
    """A hand-written structural check: no new dependency for a validator."""
    for key in ("openapi", "info", "paths", "components"):
        assert key in doc, key
    assert doc["openapi"].startswith("3.1")
    assert {"title", "version"} <= set(doc["info"])
    schemes = doc["components"]["securitySchemes"]
    operation_ids = []
    for path, item in doc["paths"].items():
        assert path.startswith("/")
        for verb, op in item.items():
            assert verb in HTTP_VERBS, (path, verb)
            assert op["responses"], (path, verb)
            for response in op["responses"].values():
                assert "description" in response
            operation_ids.append(op["operationId"])
            for requirement in op.get("security", []):
                assert set(requirement) <= set(schemes)
            declared = {p["name"] for p in op.get("parameters", []) if p["in"] == "path"}
            templated = {seg[1:-1] for seg in path.split("/") if seg.startswith("{")}
            assert templated <= declared, (path, verb)
    assert len(operation_ids) == len(set(operation_ids))
    for requirement in doc["security"]:
        assert set(requirement) <= set(schemes)
    refs: list[str] = []
    collect_refs(doc, refs)
    assert refs
    for ref in refs:
        assert ref.startswith("#/components/schemas/"), ref
        assert ref.rsplit("/", 1)[1] in doc["components"]["schemas"], ref


class TestOpenAPI:
    def fetch(self, client):
        return body(client.get(reverse("vantage:api-openapi")))

    def test_the_full_document_is_structurally_valid(self, boss):
        doc = self.fetch(boss)
        assert_valid_structure(doc)
        assert "testapp.post.Record" in doc["components"]["schemas"]
        assert doc["components"]["securitySchemes"]["csrfToken"]["name"] == "X-CSRFToken"

    def test_every_documented_path_is_a_real_route(self, boss):
        doc = self.fetch(boss)
        base = reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": "post"})
        assert base in doc["paths"]
        assert f"{base}{{pk}}/" in doc["paths"]
        assert f"{base}_actions/{{action_name}}/" in doc["paths"]

    def test_models_the_caller_cannot_view_do_not_exist_in_it(self):
        doc = self.fetch(staff("oa1", Post, "view_post"))
        assert_valid_structure(doc)
        text = json.dumps(doc)
        assert "testapp.post.Record" in text
        assert "testapp.author" not in text and "/author/" not in text
        assert "testapp.promo" not in text

    def test_write_operations_follow_the_callers_permissions(self):
        doc = self.fetch(staff("oa2", Post, "view_post", "change_post"))
        base = reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": "post"})
        assert "post" not in doc["paths"][base]
        assert "put" in doc["paths"][f"{base}{{pk}}/"]
        assert "delete" not in doc["paths"][f"{base}{{pk}}/"]
        assert f"{base}_import/" not in doc["paths"]

    def test_it_is_staff_only(self, client, db):
        assert client.get(reverse("vantage:api-openapi")).status_code == 401
        client.force_login(User.objects.create_user("plain", password="x"))
        assert client.get(reverse("vantage:api-openapi")).status_code == 403

    def test_the_command_writes_the_full_document(self, tmp_path):
        out = tmp_path / "openapi.json"
        call_command("vantage_openapi", output=str(out))
        doc = json.loads(out.read_text())
        assert_valid_structure(doc)
        assert "testapp.author.Record" in doc["components"]["schemas"]

    def test_the_command_can_describe_one_users_view(self, tmp_path):
        staff("oa3", Post, "view_post")
        out = io.StringIO()
        call_command("vantage_openapi", user="oa3", stdout=out)
        doc = json.loads(out.getvalue())
        assert "testapp.author.Record" not in doc["components"]["schemas"]

    def test_the_command_rejects_an_unknown_user(self):
        with pytest.raises(CommandError):
            call_command("vantage_openapi", user="ghost")
