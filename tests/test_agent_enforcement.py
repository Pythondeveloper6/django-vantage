"""Panel-level agent rules are enforced by the JSON API for every Bearer request.

``agent_access`` and ``agent_hidden_fields`` are declared on a panel and checked in
``vantage.agents`` (the model gate) and ``vantage.fieldperms.access`` (the field gate).
Each test pairs an agent request with the same request from a signed-in person, who must
see no change at all.
"""

from __future__ import annotations

import io
import json

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.testapp.models import Author, Order, Post
from vantage import agents
from vantage.models import AgentRun
from vantage.sites import site

pytestmark = pytest.mark.django_db

SECRET = "TOPSECRETVALUE"
MODELS = ("post", "author", "order", "comment")


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "write"}
    cache.clear()


def grant(user):
    for model in MODELS:
        for verb in ("view", "add", "change", "delete"):
            user.user_permissions.add(Permission.objects.get(codename=f"{verb}_{model}"))


@pytest.fixture
def svc():
    user = User.objects.create_user("svc", is_staff=True)
    user.set_unusable_password()
    user.save()
    grant(user)
    return user


@pytest.fixture
def owner():
    user = User.objects.create_user("alice", is_staff=True)
    grant(user)
    return user


@pytest.fixture
def human():
    user = User.objects.create_user("henry", is_staff=True)
    grant(user)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def make(svc, owner):
    def build(**kwargs):
        kwargs.setdefault("name", "bot")
        kwargs.setdefault("access", "write")
        kwargs.setdefault("owner", owner)
        _identity, token = agents.create_identity(user=svc, **kwargs)
        return Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    return build


@pytest.fixture
def bot(make):
    return make()


@pytest.fixture
def panel(monkeypatch):
    def configure(model, **attrs):
        target = site.get_panel(model)
        for name, value in attrs.items():
            monkeypatch.setattr(target, name, value, raising=False)
        return target

    return configure


@pytest.fixture
def author():
    return Author.objects.create(name="Ada", email="ada@example.com", bio=SECRET)


def api(model="post", name="api-list", **kwargs):
    return reverse(
        f"vantage:{name}",
        kwargs={"app_label": "testapp", "model_name": model, **kwargs},
    )


def detail(obj):
    return api(type(obj)._meta.model_name, "api-detail", pk=obj.pk)


def send(client, method, url, data=None):
    if method in ("get", "delete"):
        return getattr(client, method)(url)
    return getattr(client, method)(
        url, data=json.dumps(data or {}), content_type="application/json"
    )


def make_post(author, slug="p", **kwargs):
    kwargs.setdefault("title", "t")
    return Post.objects.create(slug=slug, body="b", author=author, **kwargs)


def create_body(author, slug="n", **fields):
    return {"fields": {"title": "t", "slug": slug, "body": "b", "author": author.pk, **fields}}


def text(response):
    return response.content.decode()


# ── rule 3: hidden fields are absent from output ────────────────────────────


class TestHiddenFieldsAreAbsent:
    @pytest.fixture(autouse=True)
    def hide(self, panel):
        panel(Post, agent_hidden_fields=("summary", "status"), list_display=["title", "summary"])

    def test_list_for_an_agent_leaves_the_field_out(self, bot, human, author):
        make_post(author, summary=SECRET)
        assert SECRET not in text(bot.get(api()))
        assert SECRET in text(human.get(api()))

    def test_detail(self, bot, human, author):
        post = make_post(author, summary=SECRET)
        assert SECRET not in text(bot.get(detail(post)))
        assert SECRET in text(human.get(detail(post)))

    def test_create_response(self, bot, human, author):
        response = send(bot, "post", api(), create_body(author))
        assert response.status_code == 201
        assert "summary" not in response.json()["fields"]
        assert response.json()["fields"]["title"]
        assert "summary" in send(human, "post", api(), create_body(author, "m")).json()["fields"]

    def test_update_response(self, bot, human, author):
        post = make_post(author, summary=SECRET)
        response = send(bot, "patch", detail(post), {"fields": {"title": "new"}})
        assert response.status_code == 200
        assert SECRET not in text(response)
        assert SECRET in text(send(human, "patch", detail(post), {"fields": {"title": "x"}}))

    def test_dry_run_diff_omits_it(self, bot, author):
        post = make_post(author, summary=SECRET)
        response = send(bot, "patch", detail(post) + "?dry_run=1", {"fields": {"title": "new"}})
        assert response.status_code == 200
        assert [entry["field"] for entry in response.json()["diff"]] == ["title"]
        assert SECRET not in text(response)

    def test_dry_run_form_diff_drops_hidden_names_defensively(self, bot, author, monkeypatch):
        # Even a form that somehow carried a hidden field must not mention it.
        from vantage import dryrun

        class Form:
            fields = {"title": None, "summary": None}
            changed_data = ["title", "summary"]
            initial = {"title": "a", "summary": SECRET}
            cleaned_data = {"title": "b", "summary": "other"}

        diff = dryrun.form_diff(Form(), creating=False, hidden={"summary"})
        assert [entry["field"] for entry in diff] == ["title"]

    def test_schema(self, bot, human):
        def names(client):
            fields = client.get(reverse("vantage:api-schema")).json()["models"]["testapp.post"]
            return {f["name"] for f in fields["fields"]}

        assert "summary" not in names(bot) and "title" in names(bot)
        assert "summary" in names(human)

    def test_openapi(self, bot, human):
        def dump(client):
            doc = client.get(reverse("vantage:api-openapi")).json()
            return json.dumps(
                {k: v for k, v in doc["components"]["schemas"].items() if "post" in k.lower()}
            )

        assert '"summary"' not in dump(bot)
        assert '"summary"' in dump(human)

    @pytest.mark.parametrize("fmt", ["csv", "tsv", "json"])
    def test_export(self, bot, human, author, fmt):
        make_post(author, summary=SECRET)

        def read(client):
            response = client.get(api(name="api-export"), {"format": fmt})
            assert response.status_code == 200
            return b"".join(response.streaming_content).decode()

        assert SECRET not in read(bot)
        assert SECRET in read(human)

    def test_fields_parameter_is_a_400(self, bot, human, author):
        make_post(author)
        refused = bot.get(api(), {"fields": "title,summary"})
        assert refused.status_code == 400 and "summary" in refused.json()["detail"]
        assert human.get(api(), {"fields": "title,summary"}).status_code == 200

    def test_filter_is_not_an_oracle(self, bot, human, panel, author):
        panel(Post, list_filter=["status"])
        make_post(author, "a", status="draft")
        make_post(author, "b", status="published")

        def count(client):
            return len(client.get(api(), {"status": "published"}).json()["results"])

        assert count(bot) == 2  # the parameter is ignored, never applied
        assert count(human) == 1
        listed = bot.get(api(), {"status": "published"}).json()["filters"]
        assert [f for f in listed if "status" in json.dumps(f)] == []

    def test_search_is_not_an_oracle(self, bot, human, panel, author):
        panel(Post, list_search=["title", "summary"])
        make_post(author, "a", summary=SECRET)
        make_post(author, "b", title="other")

        def count(client):
            return len(client.get(api(), {"q": SECRET}).json()["results"])

        assert count(bot) == 0  # the hidden path is no longer searched at all
        assert count(human) == 1

    def test_ordering_is_not_an_oracle(self, bot, human, author):
        make_post(author, "a", summary="b")
        make_post(author, "b", summary="a")
        assert bot.get(api(), {"sort": "summary"}).json()["sort"] is None
        assert human.get(api(), {"sort": "summary"}).json()["sort"] is not None


class TestRelatedTraversal:
    @pytest.fixture(autouse=True)
    def hide(self, panel):
        panel(Author, agent_hidden_fields=("bio",))
        panel(
            Order, list_display=["number", "customer__bio"], list_search=["number", "customer__bio"]
        )

    def make_order(self, author, number="O1"):
        return Order.objects.create(number=number, customer=author, total=1)

    def test_column_is_dropped_for_an_agent_only(self, bot, human, author):
        self.make_order(author)
        assert SECRET not in text(bot.get(api("order")))
        assert SECRET in text(human.get(api("order")))

    def test_fields_parameter_naming_a_related_hidden_field(self, bot, human, author):
        self.make_order(author)
        refused = bot.get(api("order"), {"fields": "number,customer__bio"})
        assert refused.status_code == 400
        assert human.get(api("order"), {"fields": "number,customer__bio"}).status_code == 200

    def test_search_through_the_relation(self, bot, human, author):
        self.make_order(author, "A1")
        self.make_order(Author.objects.create(name="B", email="b@x.io"), "B1")
        assert len(bot.get(api("order"), {"q": SECRET}).json()["results"]) == 0
        assert len(human.get(api("order"), {"q": SECRET}).json()["results"]) == 1

    def test_sort_through_the_relation(self, bot, human, author):
        self.make_order(author)
        response = bot.get(api("order"), {"sort": "customer__bio"})
        assert response.status_code == 200 and response.json()["sort"] is None


class TestRoleRulesStillApply:
    def test_a_role_rule_on_the_service_user_hides_the_field(self, make, svc, panel, author):
        group = Group.objects.create(name="Bots")
        svc.groups.add(group)
        panel(Post, field_permissions={"Bots": {"summary": "hidden"}})
        bot = make()
        make_post(author, summary=SECRET)
        assert SECRET not in text(bot.get(api()))
        assert send(bot, "post", api(), create_body(author, summary="x")).status_code == 400

    def test_declared_hiding_adds_to_role_rules(self, make, svc, panel, author):
        group = Group.objects.create(name="Bots")
        svc.groups.add(group)
        panel(
            Post, field_permissions={"Bots": {"summary": "hidden"}}, agent_hidden_fields=("title",)
        )
        bot = make()
        post = make_post(author, summary=SECRET, title="TITLEVALUE")
        body = text(bot.get(detail(post)))
        assert SECRET not in body and "TITLEVALUE" not in body


# ── rule 4: writes naming a hidden field are a 422 ──────────────────────────


class TestWritesNamingHiddenFields:
    @pytest.fixture(autouse=True)
    def hide(self, panel):
        panel(Post, agent_hidden_fields=("summary",))

    def test_create(self, bot, human, author):
        response = send(bot, "post", api(), create_body(author, summary="x"))
        assert response.status_code == 422
        assert "summary" in response.json()["errors"]
        assert not Post.objects.exists()
        assert send(human, "post", api(), create_body(author, summary="x")).status_code == 201

    def test_patch(self, bot, human, author):
        post = make_post(author)
        response = send(bot, "patch", detail(post), {"fields": {"summary": "x"}})
        assert response.status_code == 422 and "summary" in response.json()["errors"]
        post.refresh_from_db()
        assert post.summary == ""
        assert send(human, "patch", detail(post), {"fields": {"summary": "x"}}).status_code == 200

    def test_put_naming_it(self, bot, author):
        post = make_post(author)
        fields = {**create_body(author, post.slug)["fields"], "summary": "x"}
        assert send(bot, "put", detail(post), {"fields": fields}).status_code == 422

    def test_dry_run_refuses_the_same_way(self, bot, human, author):
        post = make_post(author)
        for method, url, body in (
            ("post", api() + "?dry_run=1", create_body(author, "z", summary="x")),
            ("patch", detail(post) + "?dry_run=1", {"fields": {"summary": "x"}}),
        ):
            response = send(bot, method, url, body)
            assert response.status_code == 422 and "summary" in response.json()["errors"]
            assert send(human, method, url, body).status_code == 200

    def test_import_mapping_target(self, bot, human):
        def upload(client, **extra):
            csv = SimpleUploadedFile("p.csv", b"t,s\nTITLE,x\n", content_type="text/csv")
            data = {"file": csv, "mapping": json.dumps({"t": "title", "s": "summary"}), **extra}
            return client.post(api(name="api-import"), data)

        response = upload(bot, dry_run="false")
        assert response.status_code == 422 and "summary" in response.json()["errors"]
        assert upload(human).status_code == 200

    def test_import_inferred_column_is_ignored(self, bot):
        csv = SimpleUploadedFile("p.csv", b"title,summary\nT,x\n", content_type="text/csv")
        response = bot.post(api(name="api-import"), {"file": csv})
        assert response.status_code == 200
        assert "summary" not in response.json()["mapping"]["columns"].values()
        assert "title" in response.json()["mapping"]["columns"].values()


# ── rule 1: access "none" ───────────────────────────────────────────────────


class TestAccessNone:
    @pytest.fixture(autouse=True)
    def none(self, panel):
        panel(Post, agent_access="none")

    def test_every_endpoint_is_a_404(self, bot, human, author):
        post = make_post(author)
        assert human.get(api()).status_code == 200
        checks = [
            ("get", api(), None),
            ("post", api(), create_body(author)),
            ("get", detail(post), None),
            ("patch", detail(post), {"fields": {"title": "x"}}),
            ("delete", detail(post), None),
            ("get", api(name="api-actions"), None),
            ("post", api(name="api-action-run", action_name="delete_selected"), {"ids": [1]}),
            ("get", api(name="api-export"), None),
            ("post", api(name="api-import"), {}),
            ("get", api(name="api-views"), None),
            ("post", api(name="api-views"), {"name": "v"}),
        ]
        for method, url, body in checks:
            assert send(bot, method, url, body).status_code == 404, (method, url)

    def test_absent_from_schema_and_openapi(self, bot, human):
        schema = bot.get(reverse("vantage:api-schema")).json()["models"]
        assert "testapp.post" not in schema and "testapp.author" in schema
        assert "testapp.post" in human.get(reverse("vantage:api-schema")).json()["models"]
        doc = bot.get(reverse("vantage:api-openapi")).json()
        assert api() not in doc["paths"]
        assert api() in human.get(reverse("vantage:api-openapi")).json()["paths"]

    def test_default_setting_none(self, settings, bot, author):
        settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "none"}
        assert bot.get(api("author")).status_code == 404


# ── rule 2: access "read", and the minimum of the two ───────────────────────


class TestAccessRead:
    def test_writes_are_403_though_the_identity_may_write(self, bot, human, panel, author):
        panel(Post, agent_access="read")
        post = make_post(author)
        assert bot.get(api()).status_code == 200
        assert bot.get(detail(post)).status_code == 200
        csv = SimpleUploadedFile("p.csv", b"title\nx\n", content_type="text/csv")
        for method, url, body in (
            ("post", api(), create_body(author)),
            ("put", detail(post), create_body(author)),
            ("patch", detail(post), {"fields": {"title": "x"}}),
            ("delete", detail(post), None),
            ("post", api(name="api-action-run", action_name="delete_selected"), {"ids": [post.pk]}),
        ):
            assert send(bot, method, url, body).status_code == 403, (method, url)
        assert (
            bot.post(api(name="api-import"), {"file": csv, "dry_run": "false"}).status_code == 403
        )
        assert Post.objects.count() == 1
        assert send(human, "patch", detail(post), {"fields": {"title": "x"}}).status_code == 200

    def test_a_panel_set_to_write_never_grants_more_than_the_identity(self, make, panel, author):
        panel(Post, agent_access="write")
        reader = make(name="reader", access="read")
        assert reader.get(api()).status_code == 200
        assert send(reader, "post", api(), create_body(author)).status_code == 403

    def test_both_write_allows_it(self, bot, panel, author):
        panel(Post, agent_access="write")
        assert send(bot, "post", api(), create_body(author)).status_code == 201

    def test_default_setting_read_blocks_writes(self, settings, bot, author):
        settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "read"}
        assert send(bot, "post", api(), create_body(author)).status_code == 403
        assert bot.get(api()).status_code == 200


# ── rule 6: performance ─────────────────────────────────────────────────────


def test_hidden_fields_cost_no_extra_queries(bot, panel, author):
    for i in range(5):
        make_post(author, f"s{i}", summary="x")
    panel(Post, list_display=["title", "summary", "author"])
    bot.get(api())  # warm the content-type and permission caches

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert bot.get(api()).status_code == 200
        return len(ctx)

    baseline = count()
    panel(Post, agent_hidden_fields=("summary", "view_count"))
    assert count() == baseline


def test_a_person_gets_no_agent_work(human, panel, author):
    panel(
        Post,
        agent_hidden_fields=("summary",),
        agent_access="none",
        list_display=["title", "summary"],
    )
    make_post(author, summary=SECRET)
    assert SECRET in text(human.get(api()))
    assert not AgentRun.objects.exists()


# ── rule 7: audit log and undo output ───────────────────────────────────────


class TestAuditOutputs:
    def test_activity_export_omits_a_hidden_field(self, bot, panel, author):
        post = make_post(author)
        assert send(bot, "patch", detail(post), {"fields": {"summary": SECRET}}).status_code == 200
        out = io.StringIO()
        call_command("vantage_agent_activity", stdout=out)
        assert SECRET in out.getvalue()  # recorded, and visible while not hidden
        panel(Post, agent_hidden_fields=("summary",))
        out = io.StringIO()
        call_command("vantage_agent_activity", stdout=out)
        assert SECRET not in out.getvalue()
        from vantage.models import VantageLogEntry

        assert SECRET in json.dumps(list(VantageLogEntry.objects.values_list("changes", flat=True)))

    def test_undo_report_leaves_out_hidden_field_names(self, bot, panel, author, owner):
        post = make_post(author)
        send(bot, "patch", detail(post), {"fields": {"summary": "x", "title": "changed"}})
        run = AgentRun.objects.get()
        panel(Post, agent_hidden_fields=("summary",))
        report = agents.undo_run(run, owner, dry_run=True)
        fields = [f for item in report["reverted"] for f in item["fields"]]
        assert "summary" not in fields
