"""The relation search endpoint.

An autocomplete is a read API over arbitrary models, so most of these tests are about
what it refuses. The naive version happily enumerates a table the caller cannot open.
"""

import json

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage.autocomplete import PAGE_SIZE

URL = "vantage:autocomplete"


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def authors(db):
    return [
        Author.objects.create(name=name, email=f"{name.lower()}@example.com")
        for name in ("Ada Lovelace", "Grace Hopper", "Barbara Liskov")
    ]


def body(response):
    return json.loads(response.content)


class TestResults:
    def test_returns_matching_records(self, boss, authors):
        data = body(boss.get(reverse(URL), {"model": "testapp.Author", "q": "Ada"}))
        assert [r["text"] for r in data["results"]] == ["Ada Lovelace"]

    def test_a_result_carries_an_id_and_a_label(self, boss, authors):
        result = body(boss.get(reverse(URL), {"model": "testapp.Author", "q": "Ada"}))["results"][0]
        assert set(result) == {"id", "text"}

    def test_an_empty_term_lists_the_first_page(self, boss, authors):
        assert len(body(boss.get(reverse(URL), {"model": "testapp.Author"}))["results"]) == 3

    def test_the_page_is_capped(self, boss, db):
        for i in range(PAGE_SIZE + 10):
            Author.objects.create(name=f"Person {i}", email=f"p{i}@example.com")
        data = body(boss.get(reverse(URL), {"model": "testapp.Author"}))
        assert len(data["results"]) == PAGE_SIZE

    def test_it_says_when_there_are_more(self, boss, db):
        for i in range(PAGE_SIZE + 1):
            Author.objects.create(name=f"Person {i}", email=f"p{i}@example.com")
        assert body(boss.get(reverse(URL), {"model": "testapp.Author"}))["more"] is True

    def test_more_is_false_when_everything_fits(self, boss, authors):
        assert body(boss.get(reverse(URL), {"model": "testapp.Author"}))["more"] is False

    def test_more_is_answered_without_counting_the_table(self, boss, db, django_assert_num_queries):
        """One extra row, not a COUNT over a table that may be enormous."""
        for i in range(PAGE_SIZE + 5):
            Author.objects.create(name=f"Person {i}", email=f"p{i}@example.com")
        with django_assert_num_queries(3):  # session, user, the one options query
            boss.get(reverse(URL), {"model": "testapp.Author"})

    def test_it_searches_the_panel_search_fields(self, boss, authors):
        data = body(boss.get(reverse(URL), {"model": "testapp.Author", "q": "hopper@"}))
        assert [r["text"] for r in data["results"]] == ["Grace Hopper"]

    def test_the_response_is_not_cached(self, boss, authors):
        response = boss.get(reverse(URL), {"model": "testapp.Author"})
        assert response["Cache-Control"] == "no-store"


class TestWhatItRefuses:
    def test_an_anonymous_caller_is_refused(self, client, authors):
        assert client.get(reverse(URL), {"model": "testapp.Author"}).status_code in (302, 403)

    def test_a_non_staff_user_is_refused(self, client, db):
        client.force_login(User.objects.create_user("plain", password="x"))
        assert client.get(reverse(URL), {"model": "testapp.Author"}).status_code == 403

    def test_a_model_the_user_cannot_view_is_refused(self, client, authors):
        """The check is against the target model, not the page the request came from."""
        staff = User.objects.create_user("staffer", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Post)
        staff.user_permissions.add(Permission.objects.get(content_type=ct, codename="view_post"))
        client.force_login(User.objects.get(pk=staff.pk))
        assert client.get(reverse(URL), {"model": "testapp.Author"}).status_code == 403

    def test_a_model_the_user_may_view_is_allowed(self, client, db):
        staff = User.objects.create_user("staffer2", password="x", is_staff=True)
        ct = ContentType.objects.get_for_model(Author)
        staff.user_permissions.add(Permission.objects.get(content_type=ct, codename="view_author"))
        client.force_login(User.objects.get(pk=staff.pk))
        assert client.get(reverse(URL), {"model": "testapp.Author"}).status_code == 200

    def test_an_unregistered_model_is_refused(self, boss, db):
        """Going through the registry is what stops it reaching arbitrary tables."""
        assert boss.get(reverse(URL), {"model": "sessions.Session"}).status_code == 403

    def test_a_nonexistent_model_is_refused(self, boss, db):
        assert boss.get(reverse(URL), {"model": "nope.Nope"}).status_code == 403

    def test_a_malformed_label_is_refused_not_crashed(self, boss, db):
        assert boss.get(reverse(URL), {"model": "../../etc/passwd"}).status_code == 403

    def test_a_missing_model_parameter_is_refused(self, boss, db):
        assert boss.get(reverse(URL)).status_code == 403

    def test_row_level_permissions_narrow_the_options(self, boss, authors, monkeypatch):
        from vantage.permissions import PanelPermissions

        monkeypatch.setattr(
            PanelPermissions,
            "filter_queryset",
            lambda self, request, qs, action="view": qs.filter(name__startswith="Ada"),
        )
        data = body(boss.get(reverse(URL), {"model": "testapp.Author"}))
        assert [r["text"] for r in data["results"]] == ["Ada Lovelace"]
