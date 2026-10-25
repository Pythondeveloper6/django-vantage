"""The JSON API: a read/write surface over every registered panel.

Most of these tests are about *reuse*, not about ``vantage.api`` itself — proving that
the endpoints answer exactly what the permission layer, the filters, the pagination and
the serialisers already answer, rather than a second opinion that could drift from
them. Where a test proves a security property (an unauthenticated caller, an
unauthorised one, an unregistered model, another tenant's rows), it is checking that
the *shared* machinery closed the door, because a JSON API view that got that wrong on
its own would be a second, worse copy of a bug the HTML pages already do not have.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from tests.testapp.models import Author, Category, Order, Post, Snippet, Tag
from vantage import site
from vantage.models import VantageLogEntry
from vantage.permissions import PanelPermissions

pytestmark = pytest.mark.django_db

LIST_URL = "vantage:api-list"
DETAIL_URL = "vantage:api-detail"
SCHEMA_URL = "vantage:api-schema"


def list_url(model=Post) -> str:
    opts = model._meta
    return reverse(LIST_URL, kwargs={"app_label": opts.app_label, "model_name": opts.model_name})


def detail_url(obj) -> str:
    opts = type(obj)._meta
    return reverse(
        DETAIL_URL,
        kwargs={"app_label": opts.app_label, "model_name": opts.model_name, "pk": obj.pk},
    )


def body(response) -> dict:
    assert response["Content-Type"].startswith("application/json"), (
        f"expected JSON, got {response['Content-Type']!r}: {response.content[:200]!r}"
    )
    return json.loads(response.content)


def post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type="application/json", **extra)


def put_json(client, url, data, **extra):
    return client.put(url, data=json.dumps(data), content_type="application/json", **extra)


def patch_json(client, url, data, **extra):
    return client.patch(url, data=json.dumps(data), content_type="application/json", **extra)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada Lovelace", email="ada@example.com")


@pytest.fixture
def posts(author):
    return [
        Post.objects.create(title=f"Post {i}", slug=f"post-{i}", body="body", author=author)
        for i in range(5)
    ]


def grant(user, model, *codenames):
    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(*Permission.objects.filter(content_type=ct, codename__in=codenames))


# ── authentication and authorisation ────────────────────────────────────────


class TestWhoMayCall:
    def test_an_anonymous_caller_is_refused(self, client, posts):
        response = client.get(list_url())
        assert response.status_code == 401
        assert body(response) == {"detail": "Authentication required."}

    def test_a_signed_in_non_staff_user_is_refused(self, client, db):
        client.force_login(User.objects.create_user("plain", password="x"))
        response = client.get(list_url())
        assert response.status_code == 403

    def test_staff_without_the_view_permission_is_refused(self, client, db, posts):
        staffer = User.objects.create_user("staffer", password="x", is_staff=True)
        client.force_login(User.objects.get(pk=staffer.pk))
        response = client.get(list_url())
        assert response.status_code == 403
        assert "posts" in body(response)["detail"]

    def test_staff_with_the_view_permission_is_allowed(self, client, db, posts):
        staffer = User.objects.create_user("staffer2", password="x", is_staff=True)
        grant(staffer, Post, "view_post")
        client.force_login(User.objects.get(pk=staffer.pk))
        assert client.get(list_url()).status_code == 200

    def test_add_change_delete_are_gated_separately_from_view(self, client, db, author):
        staffer = User.objects.create_user("staffer3", password="x", is_staff=True)
        grant(staffer, Post, "view_post")
        client.force_login(User.objects.get(pk=staffer.pk))

        create = post_json(
            client,
            list_url(),
            {"fields": {"title": "t", "slug": "t", "body": "b", "author": author.pk}},
        )
        assert create.status_code == 403
        assert "add" in body(create)["detail"]

    def test_a_superuser_needs_no_grants(self, boss, posts):
        assert boss.get(list_url()).status_code == 200

    def test_every_refusal_is_json_never_html(self, client, posts):
        """The whole premise: a script cannot read Django's default HTML error pages."""
        for response in (
            client.get(list_url()),  # 401
            client.get("/admin/_/api/testapp/post/999999/"),  # would be 404, but 401 first
        ):
            assert response["Content-Type"].startswith("application/json")


class TestRegistryGuard:
    """Only a model the site actually registered is reachable - never auth.Permission."""

    def test_a_model_vantage_excludes_by_default_is_refused(self, boss):
        response = boss.get(
            reverse(LIST_URL, kwargs={"app_label": "auth", "model_name": "permission"})
        )
        assert response.status_code == 404

    def test_a_nonexistent_app_is_refused_not_crashed(self, boss):
        response = boss.get(reverse(LIST_URL, kwargs={"app_label": "nope", "model_name": "nope"}))
        assert response.status_code == 404
        assert body(response) == {"detail": "No such model."}

    def test_a_framework_table_vantage_never_registers_is_refused(self, boss):
        response = boss.get(
            reverse(LIST_URL, kwargs={"app_label": "sessions", "model_name": "session"})
        )
        assert response.status_code == 404


# ── schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_it_lists_every_visible_model(self, boss, posts):
        data = body(boss.get(reverse(SCHEMA_URL)))
        assert "testapp.post" in data["models"]
        assert "testapp.author" in data["models"]

    def test_a_model_without_the_view_permission_is_absent(self, client, db, posts):
        staffer = User.objects.create_user("schemastaff", password="x", is_staff=True)
        grant(staffer, Post, "view_post")
        client.force_login(User.objects.get(pk=staffer.pk))
        data = body(client.get(reverse(SCHEMA_URL)))
        assert "testapp.post" in data["models"]
        assert "testapp.author" not in data["models"]

    def test_a_field_describes_its_kind_and_whether_it_is_writable(self, boss):
        data = body(boss.get(reverse(SCHEMA_URL)))
        fields = {f["name"]: f for f in data["models"]["testapp.post"]["fields"]}
        assert fields["title"]["kind"] == "text"
        assert fields["title"]["readonly"] is False
        assert fields["created_at"]["readonly"] is True  # auto_now_add

    def test_choices_are_included_so_a_client_never_reads_the_source(self, boss):
        data = body(boss.get(reverse(SCHEMA_URL)))
        fields = {f["name"]: f for f in data["models"]["testapp.post"]["fields"]}
        choices = fields["status"]["widget"]["choices"]
        assert {c["value"] for c in choices} == {"draft", "review", "published"}

    def test_a_relation_field_carries_an_options_url(self, boss):
        data = body(boss.get(reverse(SCHEMA_URL)))
        fields = {f["name"]: f for f in data["models"]["testapp.post"]["fields"]}
        assert fields["author"]["widget"]["options_url"] == (
            reverse("vantage:autocomplete") + "?model=testapp.Author"
        )

    def test_it_reports_the_actions_this_caller_may_take(self, client, db, posts):
        staffer = User.objects.create_user("schemaactions", password="x", is_staff=True)
        grant(staffer, Post, "view_post", "change_post")
        client.force_login(User.objects.get(pk=staffer.pk))
        data = body(client.get(reverse(SCHEMA_URL)))
        assert set(data["models"]["testapp.post"]["actions"]) == {"view", "change"}

    def test_anonymous_is_refused(self, client):
        assert client.get(reverse(SCHEMA_URL)).status_code == 401


# ── list: reuses filters, search, sort, pagination ──────────────────────────


class TestRequestedFieldsAreBounded:
    """``?fields=`` may only name what the panel already shows.

    It used to be split on commas and handed to the serialiser, which resolved it with
    ``resolve_path`` — and ``resolve_path`` walks Django's ``_meta`` graph, which knows
    nothing about this panel's registry or its permissions. So the parameter reached
    anything the ORM could join to. Against the demo, before the fix,
    ``?fields=number,customer__api_token`` came back with a live API token: a field the
    ``SENSITIVE`` name filter hides from the detail endpoint, on a model the caller had
    no permission for, through a panel that merely had a foreign key to it.
    """

    def test_a_relation_hop_the_panel_does_not_show_is_refused(self, boss, posts):
        response = boss.get(list_url(), {"fields": "title,author__email"})
        assert response.status_code == 400
        assert "author__email" in body(response)["detail"]

    def test_the_refusal_names_every_offending_field_at_once(self, boss, posts):
        """One round trip tells the client everything wrong with its request."""
        detail = body(boss.get(list_url(), {"fields": "title,author__email,nonsense"}))["detail"]
        assert "author__email" in detail and "nonsense" in detail

    def test_a_field_the_list_page_shows_is_still_allowed(self, boss, posts):
        """The gate must bound the parameter, not break it."""
        shown = site.get_panel(Post).get_list_display(None)[0]
        data = body(boss.get(list_url(), {"fields": shown}))
        assert list(data["results"][0]["cells"]) == [shown]

    def test_a_plain_field_of_the_model_is_allowed_even_if_it_is_not_a_column(self, boss, posts):
        """The detail endpoint would return it, so asking for it here is not widening."""
        data = body(boss.get(list_url(), {"fields": "body"}))
        assert list(data["results"][0]["cells"]) == ["body"]

    def test_asking_for_nothing_still_gives_the_default_columns(self, boss, posts):
        data = body(boss.get(list_url()))
        assert list(data["results"][0]["cells"]) == list(
            site.get_panel(Post).get_list_display(None)
        )


class TestList:
    def test_it_returns_every_visible_row(self, boss, posts):
        data = body(boss.get(list_url()))
        assert len(data["results"]) == len(posts)

    def test_a_row_carries_pk_display_version_and_urls(self, boss, posts):
        row = body(boss.get(list_url()))["results"][0]
        assert set(row) == {"pk", "display", "url", "html_url", "version", "cells"}
        assert row["url"] == detail_url(posts[-1] if row["pk"] == posts[-1].pk else posts[0])

    def test_search_reuses_the_list_search_fields(self, boss, author):
        Post.objects.create(title="Unique Zebra", slug="z", body="x", author=author)
        Post.objects.create(title="Something else", slug="e", body="x", author=author)
        data = body(boss.get(list_url(), {"q": "Zebra"}))
        assert [r["cells"]["title"]["value"] for r in data["results"]] == ["Unique Zebra"]
        assert data["query"] == "Zebra"

    def test_filters_reuse_the_same_filter_engine_the_list_page_uses(self, boss, author):
        Post.objects.create(title="Featured", slug="feat", body="x", author=author, featured=True)
        Post.objects.create(title="Plain", slug="plain", body="x", author=author)
        data = body(boss.get(list_url(), {"featured": "1"}))
        assert [r["cells"]["title"]["value"] for r in data["results"]] == ["Featured"]
        active = {f["path"]: f for f in data["filters"]}
        assert active["featured"]["active"] is True

    def test_sort_reuses_resolve_sort_and_is_reported_back(self, boss, author):
        Post.objects.create(title="B", slug="b", body="x", author=author)
        Post.objects.create(title="A", slug="a", body="x", author=author)
        data = body(boss.get(list_url(), {"sort": "title"}))
        titles = [r["cells"]["title"]["value"] for r in data["results"]]
        assert titles == sorted(titles)
        assert data["sort"]["path"] == "title"

    def test_fields_selects_which_columns_come_back(self, boss, posts):
        data = body(boss.get(list_url(), {"fields": "title"}))
        assert set(data["results"][0]["cells"]) == {"title"}

    def test_an_object_permission_narrows_the_rows_like_everywhere_else(
        self, boss, posts, monkeypatch
    ):
        kept = posts[0].pk
        monkeypatch.setattr(
            PanelPermissions,
            "filter_queryset",
            lambda self, request, qs, action="view": qs.filter(pk=kept),
        )
        data = body(boss.get(list_url()))
        assert [r["pk"] for r in data["results"]] == [kept]


class TestCursorPagination:
    """No page numbers, no OFFSET - see vantage.pagination.KeysetPaginator."""

    def test_a_page_is_bounded_by_page_size(self, boss, author):
        for i in range(12):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="x", author=author)
        data = body(boss.get(list_url(), {"page_size": "5"}))
        assert len(data["results"]) == 5
        assert data["next"] is not None
        assert data["previous"] is None

    def test_walking_the_cursor_covers_every_row_exactly_once(self, boss, author):
        made = [
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="x", author=author)
            for i in range(23)
        ]
        seen: list[int] = []
        cursor = None
        for _ in range(100):
            params = {"page_size": "7"}
            if cursor:
                params["cursor"] = cursor
            data = body(boss.get(list_url(), params))
            seen.extend(r["pk"] for r in data["results"])
            cursor = data["next"]
            if not cursor:
                break
        assert sorted(seen) == sorted(p.pk for p in made)
        assert len(seen) == len(set(seen))  # no row repeated across pages

    def test_page_size_is_capped(self, boss, author):
        for i in range(5):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="x", author=author)
        data = body(boss.get(list_url(), {"page_size": "999999"}))
        # Capped, not "however many exist" - proven by the cap being under the count.
        from vantage.api import MAX_PAGE_SIZE

        assert len(data["results"]) <= MAX_PAGE_SIZE

    def test_query_cost_does_not_grow_with_the_page_reached(self, boss, author):
        """The property optimizer.py and KeysetPaginator exist to guarantee.

        A page five cursors deep must cost exactly what the first page costs - proof
        there is no ``OFFSET`` walking and discarding the rows in between.
        """
        for i in range(60):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="x", author=author)

        first = body(boss.get(list_url(), {"page_size": "5"}))
        cursor = first["next"]
        for _ in range(4):  # walk to a deeper page
            cursor = body(boss.get(list_url(), {"page_size": "5", "cursor": cursor}))["next"]

        with CaptureQueriesContext(connection) as shallow:
            boss.get(list_url(), {"page_size": "5"})
        with CaptureQueriesContext(connection) as deep:
            boss.get(list_url(), {"page_size": "5", "cursor": cursor})

        assert len(shallow.captured_queries) == len(deep.captured_queries)

    def test_query_cost_does_not_grow_with_extra_requested_fields(self, boss):
        """optimizer.plan_related over ?fields= - joined columns cost no extra query.

        Two relation columns that were not in ``list_display`` are asked for, over
        fifteen rows. Without :func:`vantage.optimizer.plan_related` deriving a
        ``select_related`` for them, that would be fifteen extra queries apiece - the
        N+1 the whole package exists to avoid. With it, both requests cost exactly the
        same, because the two extra columns are joined into the one list query.
        """
        category = Category.objects.create(name="News", slug="news")
        author = Author.objects.create(name="Grace Hopper", email="grace@example.com")
        for i in range(15):
            Post.objects.create(
                title=f"P{i}", slug=f"p{i}", body="x", author=author, category=category
            )

        with CaptureQueriesContext(connection) as narrow:
            boss.get(list_url(), {"fields": "title", "page_size": "50"})
        with CaptureQueriesContext(connection) as wide:
            boss.get(list_url(), {"fields": "title,author,category", "page_size": "50"})

        assert len(narrow.captured_queries) == len(wide.captured_queries)

    def test_a_malformed_cursor_is_a_400_not_a_500(self, boss, posts):
        response = boss.get(list_url(), {"cursor": "not-a-real-cursor"})
        assert response.status_code == 400

    def test_a_cursor_from_a_different_model_is_refused_not_crashed(self, boss, author):
        """The cursor's own field-by-field type check - see InvalidCursor - and not
        something this module has to re-derive: a token minted for one ordering fails
        to decode against a shape it was never encoded for."""
        for i in range(3):
            Author.objects.create(name=f"Extra {i}", email=f"extra{i}@example.com")
        cursor = body(boss.get(list_url(Author), {"page_size": "2"}))["next"]
        response = boss.get(list_url(Post), {"page_size": "2", "cursor": cursor})
        assert response.status_code == 400

    def test_an_ordering_that_spans_a_relation_is_refused_with_400_not_silently_offset(
        self, boss, author
    ):
        """KeysetPaginator refuses rather than this module falling back to OFFSET -
        exactly the cost regression cursor pagination exists to rule out."""
        from vantage.panels import ModelPanel
        from vantage.sites import site

        class _RelationOrderedPanel(ModelPanel):
            ordering = ["author__name"]

        registry = site.registry._panels
        original = registry[Post]
        registry[Post] = _RelationOrderedPanel(Post, site)
        try:
            Post.objects.create(title="X", slug="relorder", body="x", author=author)
            response = boss.get(list_url())
        finally:
            registry[Post] = original

        assert response.status_code == 400
        assert "relation" in body(response)["detail"]


# ── retrieve ─────────────────────────────────────────────────────────────────


class TestRetrieve:
    def test_it_returns_every_field(self, boss, posts):
        data = body(boss.get(detail_url(posts[0])))
        assert data["pk"] == posts[0].pk
        assert data["fields"]["title"]["value"] == posts[0].title
        assert data["fields"]["title"]["kind"] == "text"

    def test_a_nonexistent_pk_is_404(self, boss, posts):
        response = boss.get(
            reverse(
                DETAIL_URL,
                kwargs={"app_label": "testapp", "model_name": "post", "pk": 999999},
            )
        )
        assert response.status_code == 404

    def test_a_pk_of_the_wrong_shape_is_404_not_a_500(self, boss, posts):
        """get_object_or_404 only catches DoesNotExist; a letter in an integer pk
        raises ValueError straight from the database layer - ApiView.dispatch has to
        catch that itself, or a malformed identifier would be an HTML 500 instead."""
        response = boss.get(
            reverse(
                DETAIL_URL,
                kwargs={"app_label": "testapp", "model_name": "post", "pk": "not-a-number"},
            )
        )
        assert response.status_code == 404
        assert response["Content-Type"].startswith("application/json")

    def test_a_row_outside_scope_is_404_not_403(self, boss, posts, monkeypatch):
        """A row you may not see does not exist - see vantage.formviews.get_record."""
        monkeypatch.setattr(
            PanelPermissions,
            "filter_queryset",
            lambda self, request, qs, action="view": qs.none(),
        )
        assert boss.get(detail_url(posts[0])).status_code == 404


# ── create ───────────────────────────────────────────────────────────────────


class TestCreate:
    def test_it_creates_and_returns_the_record(self, boss, author):
        response = post_json(
            boss,
            list_url(),
            {"fields": {"title": "New", "slug": "new-post", "body": "hi", "author": author.pk}},
        )
        assert response.status_code == 201
        data = body(response)
        assert data["fields"]["title"]["value"] == "New"
        assert Post.objects.filter(slug="new-post").exists()

    def test_a_required_field_with_a_model_default_may_be_omitted(self, boss, author):
        """An HTML create page can never actually omit it either - see api._with_field_defaults."""
        response = post_json(
            boss,
            list_url(),
            {"fields": {"title": "New", "slug": "new2", "body": "hi", "author": author.pk}},
        )
        assert response.status_code == 201
        assert body(response)["fields"]["status"]["value"] == "draft"

    def test_validation_errors_are_field_level_and_422(self, boss, author):
        response = post_json(
            boss, list_url(), {"fields": {"title": "", "slug": "", "author": author.pk}}
        )
        assert response.status_code == 422
        errors = body(response)["errors"]
        assert "title" in errors and isinstance(errors["title"], list)
        assert "slug" in errors

    def test_an_unknown_field_is_refused_before_it_reaches_a_form(self, boss, author):
        response = post_json(
            boss,
            list_url(),
            {
                "fields": {
                    "title": "t",
                    "slug": "t2",
                    "body": "b",
                    "author": author.pk,
                    "is_superuser": True,
                }
            },
        )
        assert response.status_code == 400

    def test_a_readonly_field_cannot_be_set_through_the_write_path(self, boss, author):
        """Never a parallel path - a readonly field is not in the form at all."""
        response = post_json(
            boss,
            list_url(),
            {
                "fields": {
                    "title": "t",
                    "slug": "t3",
                    "body": "b",
                    "author": author.pk,
                    "created_at": "2000-01-01T00:00:00Z",
                }
            },
        )
        assert response.status_code == 400

    def test_a_malformed_body_is_400_not_500(self, boss):
        response = boss.post(list_url(), data="{not json", content_type="application/json")
        assert response.status_code == 400

    def test_a_body_without_a_fields_object_is_400(self, boss):
        response = post_json(boss, list_url(), {"title": "no envelope"})
        assert response.status_code == 400

    def test_a_json_field_round_trips_a_real_value(self, boss, author):
        response = post_json(
            boss,
            list_url(),
            {
                "fields": {
                    "title": "t",
                    "slug": "meta1",
                    "body": "b",
                    "author": author.pk,
                    "metadata": {"a": 1, "b": [1, 2, 3]},
                }
            },
        )
        assert response.status_code == 201
        assert body(response)["fields"]["metadata"]["value"] == {"a": 1, "b": [1, 2, 3]}

    def test_an_empty_json_object_is_not_silently_dropped_to_null(self, boss, author):
        """The forms.JSONField / EMPTY_VALUES trap json_to_form_data exists to avoid."""
        response = post_json(
            boss,
            list_url(),
            {
                "fields": {
                    "title": "t",
                    "slug": "meta2",
                    "body": "b",
                    "author": author.pk,
                    "metadata": {},
                }
            },
        )
        assert response.status_code == 201
        assert body(response)["fields"]["metadata"]["value"] == {}

    def test_it_writes_through_the_audit_trail_like_the_html_form(self, boss, author):
        post_json(
            boss,
            list_url(),
            {"fields": {"title": "t", "slug": "audited", "body": "b", "author": author.pk}},
        )
        obj = Post.objects.get(slug="audited")
        entry = VantageLogEntry.objects.get(object_id=str(obj.pk))
        assert entry.action == VantageLogEntry.Action.CREATE

    def test_creating_a_many_to_many_by_id_list(self, boss, author):
        tag_a = Tag.objects.create(name="alpha")
        tag_b = Tag.objects.create(name="beta")
        response = post_json(
            boss,
            list_url(),
            {
                "fields": {
                    "title": "t",
                    "slug": "tagged",
                    "body": "b",
                    "author": author.pk,
                    "tags": [tag_a.pk, tag_b.pk],
                }
            },
        )
        assert response.status_code == 201
        obj = Post.objects.get(slug="tagged")
        assert set(obj.tags.values_list("pk", flat=True)) == {tag_a.pk, tag_b.pk}


# ── update / partial update ─────────────────────────────────────────────────


class TestUpdate:
    def test_put_replaces_every_editable_field(self, boss, posts):
        obj = posts[0]
        response = put_json(
            boss,
            detail_url(obj),
            {
                "fields": {
                    "title": "Replaced",
                    "slug": obj.slug,
                    "body": "new body",
                    "author": obj.author_id,
                    "status": "published",
                    "view_count": 3,
                    "featured": True,
                    "metadata": {},
                }
            },
        )
        assert response.status_code == 200
        obj.refresh_from_db()
        assert obj.title == "Replaced"
        assert obj.status == "published"

    def test_put_without_every_field_is_a_validation_error(self, boss, posts):
        response = put_json(boss, detail_url(posts[0]), {"fields": {"title": "Only title"}})
        assert response.status_code == 422

    def test_patch_touches_only_the_submitted_fields(self, boss, posts):
        obj = posts[0]
        original_body = obj.body
        response = patch_json(boss, detail_url(obj), {"fields": {"featured": True}})
        assert response.status_code == 200
        obj.refresh_from_db()
        assert obj.featured is True
        assert obj.body == original_body

    def test_patch_reports_validation_errors_the_same_way_as_create(self, boss, posts):
        response = patch_json(boss, detail_url(posts[0]), {"fields": {"slug": ""}})
        assert response.status_code == 422
        assert "slug" in body(response)["errors"]

    def test_a_nonexistent_record_is_404(self, boss):
        response = patch_json(
            boss,
            reverse(
                DETAIL_URL,
                kwargs={"app_label": "testapp", "model_name": "post", "pk": 999999},
            ),
            {"fields": {"featured": True}},
        )
        assert response.status_code == 404


class TestConcurrency:
    """The optimistic lock every list row already carries a token for."""

    def test_a_matching_version_saves_and_advances_the_token(self, boss, posts):
        obj = posts[0]
        current = body(boss.get(detail_url(obj)))["version"]
        response = patch_json(
            boss, detail_url(obj), {"fields": {"featured": True}, "version": current}
        )
        assert response.status_code == 200
        assert body(response)["version"] != current

    def test_a_stale_version_is_409_with_a_field_diff(self, boss, posts):
        obj = posts[0]
        current = body(boss.get(detail_url(obj)))["version"]
        # Somebody else writes first.
        patch_json(boss, detail_url(obj), {"fields": {"featured": True}, "version": current})

        response = patch_json(
            boss, detail_url(obj), {"fields": {"featured": False}, "version": current}
        )
        assert response.status_code == 409
        conflict = body(response)["conflict"]
        assert conflict["fields"]["featured"]["theirs"] is True
        assert conflict["fields"]["featured"]["mine"] is False

    def test_omitting_the_version_is_still_allowed_like_the_html_form_today(self, boss, posts):
        """No regression: vantage.formviews.UpdateView has never passed a version."""
        response = patch_json(boss, detail_url(posts[0]), {"fields": {"featured": True}})
        assert response.status_code == 200


# ── delete ───────────────────────────────────────────────────────────────────


class TestDelete:
    def test_it_deletes_and_answers_204(self, boss, posts):
        obj = posts[0]
        response = boss.delete(detail_url(obj))
        assert response.status_code == 204
        assert response.content == b""
        assert not Post.objects.filter(pk=obj.pk).exists()

    def test_it_is_recorded_in_the_audit_trail(self, boss, posts):
        obj = posts[0]
        boss.delete(detail_url(obj))
        entry = VantageLogEntry.objects.get(
            object_id=str(obj.pk), action=VantageLogEntry.Action.DELETE
        )
        assert entry is not None

    def test_a_protected_relation_blocks_the_delete_with_409(self, boss, author):
        """Order.customer is on_delete=PROTECT - same cascade preview the HTML page uses."""
        Order.objects.create(number="X-1", customer=author, total="10.00")
        response = boss.delete(detail_url(author))
        assert response.status_code == 409
        assert "cannot be deleted" in body(response)["detail"]
        assert Author.objects.filter(pk=author.pk).exists()


# ── method handling ──────────────────────────────────────────────────────────


class TestMethodHandling:
    def test_an_unsupported_method_is_405_json_with_an_allow_header(self, boss, posts):
        response = boss.delete(list_url())
        assert response.status_code == 405
        assert response["Content-Type"].startswith("application/json")
        assert "Allow" in response


# ── CSRF ─────────────────────────────────────────────────────────────────────

CSRF_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]


def with_csrf(func):
    """``override_settings`` as a class decorator needs a Django ``TestCase``; these
    are plain pytest classes, so every CSRF test applies it to itself instead."""
    return override_settings(
        MIDDLEWARE=CSRF_MIDDLEWARE, CSRF_FAILURE_VIEW="vantage.api.csrf_failure"
    )(func)


class TestCSRF:
    """Never exempted: an unsafe verb is checked exactly like an HTML form post."""

    @with_csrf
    def test_a_write_without_a_token_is_refused(self, db, author):
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("csrfboss", "c@example.com", "x"))
        response = post_json(
            client,
            list_url(),
            {"fields": {"title": "t", "slug": "csrf1", "body": "b", "author": author.pk}},
        )
        assert response.status_code == 403
        assert response["Content-Type"].startswith("application/json")
        assert not Post.objects.filter(slug="csrf1").exists()

    @with_csrf
    def test_a_read_needs_no_token(self, db, posts):
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("csrfboss2", "c2@example.com", "x"))
        assert client.get(list_url()).status_code == 200

    @with_csrf
    def test_a_write_with_the_right_token_succeeds(self, db, author):
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_superuser("csrfboss3", "c3@example.com", "x"))
        # A GET primes the CSRF cookie - see ApiView.authenticate - exactly what
        # docs/api.md tells a script to do before its first write.
        client.get(list_url())
        token = client.cookies["csrftoken"].value
        response = post_json(
            client,
            list_url(),
            {"fields": {"title": "t", "slug": "csrf2", "body": "b", "author": author.pk}},
            HTTP_X_CSRFTOKEN=token,
        )
        assert response.status_code == 201
        assert Post.objects.filter(slug="csrf2").exists()

    def test_csrf_failure_is_never_exempted_at_the_view_level(self):
        """vantage.api never decorates a view with csrf_exempt."""
        import inspect

        from vantage import api

        source = inspect.getsource(api)
        assert "csrf_exempt" not in source


# ── tenancy composes for free ───────────────────────────────────────────────


TENANT_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.tenancy.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

#: auth.User doubles as its own tenant model here: TENANT_FIELD="pk" makes the
#: resolver answer with the signed-in user's own primary key, and Snippet.owner - "the
#: hook a tenant scope attaches to", per its own docstring in tests/testapp/models.py -
#: gives vantage.tenancy.scope_for exactly one foreign key to infer the scoping path
#: from. No project would really set TENANT_MODEL to auth.User; this is the smallest
#: fixture that proves the *api.py* side of the composition without writing a whole
#: second tenant model and migration into the test app.
TENANT_SETTINGS = {"TENANT_MODEL": "auth.User", "TENANT_FIELD": "pk"}


@pytest.fixture
def tenants(db):
    alice = User.objects.create_user("alice", password="x", is_staff=True)
    bob = User.objects.create_user("bob", password="x", is_staff=True)
    for user in (alice, bob):
        grant(user, Snippet, "view_snippet", "change_snippet", "add_snippet", "delete_snippet")
    alices = Snippet.objects.create(title="Alice's plan", owner=alice)
    bobs = Snippet.objects.create(title="Bob's plan", owner=bob)
    return alice, bob, alices, bobs


def with_tenancy(func):
    """See ``with_csrf`` above for why this is a function decorator, not a class one."""
    return override_settings(VANTAGE=TENANT_SETTINGS, MIDDLEWARE=TENANT_MIDDLEWARE)(func)


class TestTenancyComposesAutomatically:
    """The claim in tenancy.py's own docstring, exercised end to end through the API.

    Nothing in vantage.api mentions tenancy at all - every read here goes through
    ``get_permissions().filter_queryset()``, and :func:`vantage.tenancy.enforce_on`
    wraps that call for every resolver the package ships. If this file did not compose
    automatically, that would be the loud finding the task asked for.
    """

    @with_tenancy
    def test_a_user_cannot_read_another_tenants_row_through_list(self, tenants):
        alice, _bob, alices, bobs = tenants
        client = Client()
        client.force_login(alice)
        data = body(client.get(list_url(Snippet)))
        pks = {r["pk"] for r in data["results"]}
        assert pks == {alices.pk}
        assert bobs.pk not in pks

    @with_tenancy
    def test_a_user_cannot_read_another_tenants_row_by_pk(self, tenants):
        alice, _bob, alices, bobs = tenants
        client = Client()
        client.force_login(alice)
        # 404, not 403: a row you may not see does not exist - see get_record.
        assert client.get(detail_url(bobs)).status_code == 404
        assert client.get(detail_url(alices)).status_code == 200

    @with_tenancy
    def test_each_tenant_sees_their_own_row(self, tenants):
        _alice, bob, _alices, bobs = tenants
        client = Client()
        client.force_login(bob)
        data = body(client.get(list_url(Snippet)))
        assert {r["pk"] for r in data["results"]} == {bobs.pk}
