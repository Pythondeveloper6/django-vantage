"""Global search: it has to find things, and it must never find things you can't open.

The second half is the important one. Every leak test below is named so that a failure
reads as a security failure in the pytest output, because that is what it would be.
"""

import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, override_settings
from django.urls import include, path, reverse

from tests.testapp.models import Author, Post, Tag
from vantage import palette
from vantage.permissions import PanelPermissions
from vantage.search import GlobalSearch
from vantage.sites import site

_patterns, _app, _instance = site.urls
urlpatterns = [
    path("admin/", include((list(_patterns) + palette.build_urls(site), _app), namespace=_instance))
]

pytestmark = pytest.mark.urls(__name__)


class PublishedOnly(PanelPermissions):
    """Row scoping, the way a real deployment would express it."""

    def filter_queryset(self, request, queryset, action="view"):
        if queryset.model is Post:
            return queryset.filter(status=Post.Status.PUBLISHED)
        return queryset


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def editor(db):
    """Staff, but allowed to view exactly one model."""
    user = User.objects.create_user("editor", "editor@example.com", "x", is_staff=True)
    user.user_permissions.add(
        Permission.objects.get(
            codename="view_post", content_type=ContentType.objects.get_for_model(Post)
        )
    )
    return user


@pytest.fixture
def content(db):
    ada = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    post = Post.objects.create(
        title="Ada and the analytical engine",
        slug="ada-engine",
        body="Notes on the engine.",
        author=ada,
        status=Post.Status.PUBLISHED,
    )
    Post.objects.create(
        title="Ada, unfinished",
        slug="ada-unfinished",
        body="Draft.",
        author=ada,
        status=Post.Status.DRAFT,
    )
    return {"author": ada, "post": post}


def search_as(user, query, **kwargs):
    request = RequestFactory().get("/")
    request.user = user
    return GlobalSearch(site, request, **kwargs).search(query)


def keys(groups):
    return [group.key for group in groups]


def labels(groups):
    return [hit.label for group in groups for hit in group.hits]


class TestFinding:
    def test_finds_records_across_models(self, boss, content):
        groups = search_as(boss, "ada")
        assert {"testapp.author", "testapp.post"} <= set(keys(groups))

    def test_a_hit_carries_a_detail_url(self, boss, content):
        group = next(g for g in search_as(boss, "ada") if g.key == "testapp.author")
        assert group.hits[0].url == reverse(
            "vantage:testapp-author-detail", args=[content["author"].pk]
        )

    def test_a_group_links_through_to_its_list_page(self, boss, content):
        group = next(g for g in search_as(boss, "ada") if g.key == "testapp.post")
        assert group.url == reverse("vantage:testapp-post-list") + "?q=ada"

    def test_matches_a_field_that_is_not_the_title(self, boss, content):
        Author.objects.create(name="Grace Hopper", email="lovelace@example.com")
        assert "Grace Hopper" in labels(search_as(boss, "lovelace@"))

    def test_a_second_line_tells_two_records_apart(self, boss, db):
        Author.objects.create(name="Ada Lovelace", email="ada@one.example")
        Author.objects.create(name="Ada Lovelace", email="ada@two.example")
        group = next(g for g in search_as(boss, "lovelace") if g.key == "testapp.author")
        assert {hit.sublabel for hit in group.hits} == {"ada@one.example", "ada@two.example"}

    def test_the_second_line_is_the_field_that_matched(self, boss, db):
        author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
        Post.objects.create(
            title="Untitled", slug="engine-notes", body="Long body text", author=author
        )
        group = next(g for g in search_as(boss, "engine") if g.key == "testapp.post")
        assert group.hits[0].sublabel == "engine-notes"

    def test_a_short_query_asks_nothing_of_the_database(self, boss, content):
        assert search_as(boss, "a") == []

    def test_whitespace_is_not_a_query(self, boss, content):
        assert search_as(boss, "   ") == []

    def test_an_exact_title_outranks_a_substring(self, boss, db):
        author = Author.objects.create(name="Zed", email="z@example.com")
        Post.objects.create(title="Engine notes", slug="a", body="b", author=author)
        Post.objects.create(title="engine", slug="c", body="d", author=author)
        group = next(g for g in search_as(boss, "engine") if g.key == "testapp.post")
        assert group.hits[0].label == "engine"


class TestCaps:
    def test_a_model_contributes_no_more_than_its_cap(self, boss, db):
        author = Author.objects.create(name="Zed", email="z@example.com")
        for index in range(12):
            Post.objects.create(
                title=f"Widget {index}", slug=f"widget-{index}", body="x", author=author
            )
        group = next(g for g in search_as(boss, "widget") if g.key == "testapp.post")
        assert len(group.hits) == 5
        assert group.truncated

    def test_the_overall_cap_is_the_last_word(self, boss, db):
        author = Author.objects.create(name="Widget Person", email="w@example.com")
        for index in range(6):
            Post.objects.create(
                title=f"Widget {index}", slug=f"widget-{index}", body="x", author=author
            )
        groups = search_as(boss, "widget", total_limit=3)
        assert sum(len(group.hits) for group in groups) == 3

    def test_a_capped_group_says_so(self, boss, db):
        author = Author.objects.create(name="Zed", email="z@example.com")
        for index in range(4):
            Post.objects.create(
                title=f"Widget {index}", slug=f"widget-{index}", body="x", author=author
            )
        groups = search_as(boss, "widget", total_limit=2)
        assert groups[0].truncated


class TestWeighting:
    def test_a_weighted_model_sorts_first(self, boss, content, monkeypatch):
        posts = site.get_panel(Post)
        authors = site.get_panel(Author)
        monkeypatch.setattr(authors, "search_weight", 0.1, raising=False)
        monkeypatch.setattr(posts, "search_weight", 5.0, raising=False)
        assert keys(search_as(boss, "ada"))[0] == "testapp.post"

    def test_weighing_a_model_zero_takes_it_out_of_global_search(self, boss, content, monkeypatch):
        monkeypatch.setattr(site.get_panel(Author), "search_weight", 0, raising=False)
        assert "testapp.author" not in keys(search_as(boss, "ada"))


class TestPermissions:
    """A result the user cannot open is a leak, not a near miss."""

    def test_search_never_returns_a_model_the_user_cannot_view(self, editor, content):
        groups = search_as(editor, "ada")
        assert keys(groups) == ["testapp.post"]

    def test_search_never_returns_a_row_filter_queryset_excluded(self, editor, content):
        with override_settings(VANTAGE={"PERMISSIONS": "tests.test_search.PublishedOnly"}):
            groups = search_as(editor, "ada")
        assert labels(groups) == ["Ada and the analytical engine"]

    def test_an_anonymous_request_gets_nothing(self, db, content):
        from django.contrib.auth.models import AnonymousUser

        assert search_as(AnonymousUser(), "ada") == []


class TestTargets:
    def test_a_search_path_that_is_not_a_field_is_skipped_not_raised(
        self, boss, content, monkeypatch
    ):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "list_search", ["title", "not_a_field"], raising=False)
        assert "testapp.post" in keys(search_as(boss, "ada"))

    def test_a_model_with_no_searchable_field_is_not_asked(self, boss, content, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "list_search", [], raising=False)
        assert "testapp.post" not in keys(search_as(boss, "ada"))

    def test_a_multivalued_path_does_not_duplicate_a_row(self, boss, db, monkeypatch):
        author = Author.objects.create(name="Zed", email="z@example.com")
        post = Post.objects.create(title="Tagged", slug="tagged", body="x", author=author)
        post.tags.set([Tag.objects.create(name="alpha one"), Tag.objects.create(name="alpha two")])
        monkeypatch.setattr(site.get_panel(Post), "list_search", ["tags__name"], raising=False)
        group = next(g for g in search_as(boss, "alpha") if g.key == "testapp.post")
        assert len(group.hits) == 1


class TestJSONEndpoint:
    def test_anonymous_is_redirected(self, client, db):
        response = client.get(reverse("vantage:search-json"), {"q": "ada"})
        assert response.status_code == 302

    def test_the_payload_has_the_documented_shape(self, client, boss, content):
        client.force_login(boss)
        response = client.get(reverse("vantage:search-json"), {"q": "ada"})
        assert response.status_code == 200
        assert response["Content-Type"] == "application/json"

        payload = json.loads(response.content)
        assert set(payload) == {"query", "min_length", "total", "truncated", "groups", "icons"}
        assert payload["query"] == "ada"
        assert payload["total"] >= 2

        group = payload["groups"][0]
        assert set(group) == {"key", "label", "icon", "url", "truncated", "results"}
        assert set(group["results"][0]) == {"id", "label", "sublabel", "url"}

    def test_results_are_never_cached_by_a_shared_cache(self, client, boss, content):
        client.force_login(boss)
        response = client.get(reverse("vantage:search-json"), {"q": "ada"})
        assert response["Cache-Control"] == "no-store"

    def test_the_endpoint_obeys_the_same_permission_rules(self, client, editor, content):
        client.force_login(editor)
        payload = json.loads(client.get(reverse("vantage:search-json"), {"q": "ada"}).content)
        assert [group["key"] for group in payload["groups"]] == ["testapp.post"]

    def test_a_missing_query_is_an_empty_answer_not_an_error(self, client, boss, content):
        client.force_login(boss)
        payload = json.loads(client.get(reverse("vantage:search-json")).content)
        assert payload["total"] == 0
        assert payload["groups"] == []


class TestFallbackPage:
    def test_it_renders_without_a_query(self, client, boss, content):
        client.force_login(boss)
        response = client.get(reverse("vantage:search"))
        assert response.status_code == 200
        assert "Search everything" in response.content.decode()

    def test_it_renders_results_for_a_query(self, client, boss, content):
        client.force_login(boss)
        body = client.get(reverse("vantage:search"), {"q": "ada"}).content.decode()
        assert "Ada Lovelace" in body
        assert reverse("vantage:testapp-author-detail", args=[content["author"].pk]) in body

    def test_it_says_so_when_nothing_matched(self, client, boss, content):
        client.force_login(boss)
        body = client.get(reverse("vantage:search"), {"q": "zzzznothing"}).content.decode()
        assert "No matches" in body

    def test_it_shows_a_limited_user_only_what_they_may_open(self, client, editor, content):
        client.force_login(editor)
        body = client.get(reverse("vantage:search"), {"q": "ada"}).content.decode()
        assert "Ada and the analytical engine" in body
        assert reverse("vantage:testapp-author-detail", args=[content["author"].pk]) not in body

    def test_anonymous_is_redirected(self, client, db):
        assert client.get(reverse("vantage:search")).status_code == 302

    def test_no_comment_leaks_into_the_page(self, client, boss, content):
        # ``{# … #}`` is single-line only; a multi-line one renders as visible text.
        client.force_login(boss)
        assert "{#" not in client.get(reverse("vantage:search")).content.decode()


class TestCost:
    def test_one_query_per_searched_model(self, boss, content, django_assert_num_queries):
        request = RequestFactory().get("/")
        request.user = boss
        search = GlobalSearch(site, request)
        expected = len(search.targets())
        with django_assert_num_queries(expected):
            search.search("ada")

    def test_a_related_search_path_does_not_go_n_plus_one(
        self, boss, db, monkeypatch, django_assert_num_queries
    ):
        author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
        for index in range(4):
            Post.objects.create(title=f"Post {index}", slug=f"p-{index}", body="b", author=author)
        monkeypatch.setattr(site.get_panel(Post), "list_search", ["author__name"], raising=False)

        request = RequestFactory().get("/")
        request.user = boss
        search = GlobalSearch(site, request)
        expected = len(search.targets())
        with django_assert_num_queries(expected):
            # str(post) never touches the author, but _sublabel walks author__name on
            # every row, which is the shape that would go N+1 without select_related.
            search.search("ada")
