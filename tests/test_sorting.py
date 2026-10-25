"""Column sorting. The sort parameter reaches order_by, so it is never trusted."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import Author, Post

LIST = "vantage:testapp-post-list"


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def posts(db):
    author = Author.objects.create(name="Ada", email="ada@example.com")
    for title, status in [("Beta", "draft"), ("Alpha", "published"), ("Gamma", "draft")]:
        Post.objects.create(title=title, slug=title.lower(), body="b", author=author, status=status)
    return Post.objects.all()


def titles(response):
    body = response.content.decode()
    return sorted(
        ("Alpha", "Beta", "Gamma"),
        key=lambda t: body.index(f">{t}<") if f">{t}<" in body else 10**9,
    )


class TestSorting:
    def test_ascending(self, client_in, posts):
        assert titles(client_in.get(reverse(LIST), {"sort": "title"})) == [
            "Alpha",
            "Beta",
            "Gamma",
        ]

    def test_descending(self, client_in, posts):
        assert titles(client_in.get(reverse(LIST), {"sort": "-title"})) == [
            "Gamma",
            "Beta",
            "Alpha",
        ]

    def test_the_header_advertises_the_sort_to_assistive_tech(self, client_in, posts):
        body = client_in.get(reverse(LIST), {"sort": "title"}).content.decode()
        assert 'aria-sort="ascending"' in body
        body = client_in.get(reverse(LIST), {"sort": "-title"}).content.decode()
        assert 'aria-sort="descending"' in body

    def test_clicking_an_ascending_column_flips_it(self, client_in, posts):
        body = client_in.get(reverse(LIST), {"sort": "title"}).content.decode()
        assert "sort=-title" in body

    def test_sorting_by_a_related_column_works(self, client_in, posts):
        assert client_in.get(reverse(LIST), {"sort": "author"}).status_code == 200


class TestRejectedSorts:
    """Anything that does not resolve to a concrete single-valued column is dropped."""

    def test_an_unknown_field_is_ignored(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"sort": "nonsense"})
        assert response.status_code == 200
        assert response.context["sort"] is None

    def test_a_field_not_in_list_display_is_ignored(self, client_in, posts):
        """Otherwise the sort parameter is an oracle for unexposed columns."""
        response = client_in.get(reverse(LIST), {"sort": "body"})
        assert response.context["sort"] is None

    def test_an_injection_attempt_is_ignored(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"sort": "title; DROP TABLE"})
        assert response.status_code == 200
        assert response.context["sort"] is None

    def test_a_traversal_into_a_multivalued_relation_is_ignored(self, client_in, posts):
        """Sorting across a reverse FK silently duplicates rows."""
        response = client_in.get(reverse(LIST), {"sort": "comments"})
        assert response.context["sort"] is None

    def test_an_empty_sort_is_ignored(self, client_in, posts):
        assert client_in.get(reverse(LIST), {"sort": ""}).context["sort"] is None


class TestStability:
    def test_a_tiebreaker_is_appended(self, client_in, posts):
        """Duplicate values in the sort column would otherwise make paging unstable."""
        response = client_in.get(reverse(LIST), {"sort": "status"})
        assert response.context["sort"]["order_by"] == ["status", "-pk"]

    def test_sorting_resets_the_page(self, client_in, db):
        """Staying on page 7 after re-sorting shows an arbitrary slice."""
        author = Author.objects.create(name="A", email="a@example.com")
        for i in range(60):
            Post.objects.create(title=f"P{i:02}", slug=f"p{i}", body="b", author=author)
        body = client_in.get(reverse(LIST), {"page": "2"}).content.decode()
        assert "sort=title&amp;" not in body or "page=2" not in body.split("sort=title")[1][:40]

    def test_sorting_preserves_the_search(self, client_in, posts):
        body = client_in.get(reverse(LIST), {"q": "a", "sort": "title"}).content.decode()
        assert "q=a" in body
