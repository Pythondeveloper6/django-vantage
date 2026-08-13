"""Export must hand back exactly what the page is showing, and must stream."""

import csv
import io

import pytest
from django.contrib.auth.models import User
from django.http import StreamingHttpResponse
from django.urls import reverse

from tests.testapp.models import Author, Post

LIST = "vantage:testapp-post-list"


@pytest.fixture
def client_in(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


@pytest.fixture
def posts(db):
    ada = Author.objects.create(name="Ada", email="ada@example.com")
    grace = Author.objects.create(name="Grace", email="grace@example.com")
    Post.objects.create(title="Alpha", slug="a", body="b", author=ada, status="published")
    Post.objects.create(title="Beta", slug="b", body="b", author=grace, status="draft")
    Post.objects.create(title="Gamma", slug="g", body="b", author=ada, status="draft")


def read(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode()
    return list(csv.reader(io.StringIO(body)))


class TestExport:
    def test_returns_a_streaming_response(self, client_in, posts):
        """A 400,000-row export must not be assembled in memory first."""
        response = client_in.get(reverse(LIST), {"export": "csv"})
        assert isinstance(response, StreamingHttpResponse)

    def test_is_offered_as_a_download(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "csv"})
        assert "attachment" in response["Content-Disposition"]
        assert ".csv" in response["Content-Disposition"]

    def test_is_not_cached(self, client_in, posts):
        """A permission-scoped extract must not sit in a shared cache."""
        response = client_in.get(reverse(LIST), {"export": "csv"})
        assert response["Cache-Control"] == "no-store"

    def test_first_row_is_the_column_headers(self, client_in, posts):
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        assert rows[0][0] == "Title"

    def test_columns_match_the_list_page(self, client_in, posts):
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        page = client_in.get(reverse(LIST))
        assert rows[0] == [c["label"] for c in page.context["columns"]]

    def test_exports_every_row_not_just_the_current_page(self, client_in, db):
        author = Author.objects.create(name="A", email="a@example.com")
        for i in range(60):
            Post.objects.create(title=f"P{i}", slug=f"p{i}", body="b", author=author)
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        assert len(rows) == 61  # 60 records plus the header

    def test_relations_export_as_their_label_not_a_primary_key(self, client_in, posts):
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        assert any("Ada" in row for row in rows[1:])


class TestExportRespectsTheView:
    def test_honours_the_active_search(self, client_in, posts):
        """An export that ignores your filters is worse than no export."""
        rows = read(client_in.get(reverse(LIST), {"export": "csv", "q": "Alpha"}))
        assert len(rows) == 2
        assert "Alpha" in rows[1]

    def test_honours_the_active_sort(self, client_in, posts):
        rows = read(client_in.get(reverse(LIST), {"export": "csv", "sort": "-title"}))
        assert [row[0] for row in rows[1:]] == ["Gamma", "Beta", "Alpha"]

    def test_honours_row_level_permissions(self, client_in, posts, monkeypatch):
        from vantage.permissions import PanelPermissions

        monkeypatch.setattr(
            PanelPermissions,
            "filter_queryset",
            lambda self, request, qs, action="view": qs.filter(status="published"),
        )
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        assert len(rows) == 2
        assert "Alpha" in rows[1]


class TestExportGuards:
    def test_an_unknown_format_renders_the_page_instead(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "xlsx"})
        assert response.status_code == 200
        assert not isinstance(response, StreamingHttpResponse)

    def test_export_requires_view_permission(self, client, db):
        staff = User.objects.create_user("plain", password="x", is_staff=True)
        client.force_login(staff)
        assert client.get(reverse(LIST), {"export": "csv"}).status_code == 403

    def test_empty_result_still_produces_headers(self, client_in, db):
        rows = read(client_in.get(reverse(LIST), {"export": "csv"}))
        assert len(rows) == 1
        assert rows[0][0] == "Title"

    def test_booleans_are_written_readably(self, client_in, posts):
        rows = read(client_in.get(reverse("vantage:testapp-author-list"), {"export": "csv"}))
        assert any("true" in row for row in rows[1:])


class TestOtherFormats:
    def test_json_streams_one_object_per_line(self, client_in, posts):
        """NDJSON, not one array: a streamed array cannot be closed if the queryset
        raises halfway, leaving a file that is silently truncated but parses as
        nothing. Every line here is independently parseable."""
        import json as jsonlib

        response = client_in.get(reverse(LIST), {"export": "json"})
        lines = b"".join(response.streaming_content).decode().strip().splitlines()
        assert len(lines) == 3
        assert jsonlib.loads(lines[0])["Title"] in {"Alpha", "Beta", "Gamma"}

    def test_json_is_offered_as_ndjson(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "json"})
        assert ".ndjson" in response["Content-Disposition"]
        assert response["Content-Type"] == "application/x-ndjson"

    def test_tsv_uses_tabs(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "tsv"})
        body = b"".join(response.streaming_content).decode()
        assert "\t" in body.splitlines()[0]

    def test_every_format_honours_the_active_search(self, client_in, posts):
        for fmt in ("csv", "tsv", "json"):
            response = client_in.get(reverse(LIST), {"export": fmt, "q": "Alpha"})
            body = b"".join(response.streaming_content).decode()
            assert "Alpha" in body
            assert "Gamma" not in body, fmt

    def test_every_format_is_uncached(self, client_in, posts):
        for fmt in ("csv", "tsv", "json"):
            response = client_in.get(reverse(LIST), {"export": fmt})
            assert response["Cache-Control"] == "no-store", fmt
