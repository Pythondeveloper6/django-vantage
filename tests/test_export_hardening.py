# ruff: noqa: F811
"""Row cap, cell limit, spool lifetime and regression checks for xlsx and pdf export."""

import io
import re
import xml.etree.ElementTree as ET
import zipfile

import pytest
from django.test import override_settings
from django.urls import reverse

from tests.test_export import InMemoryRows, body_of, client_in, read, sheet_cells, text_of  # noqa: F401
from tests.testapp.models import Author, Post
from vantage import export
from vantage.export import stream_export

LIST = "vantage:testapp-post-list"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@pytest.fixture
def author(db):
    return Author.objects.create(name="Ada", email="ada@example.com")


def make_posts(author, n):
    Post.objects.bulk_create(
        [Post(title=f"Row {i}", slug=f"s{i}", body="b", author=author) for i in range(n)]
    )


def cap(n):
    return override_settings(VANTAGE={"EXPORT_BINARY_MAX_ROWS": n})


def sheet_xml(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read("xl/worksheets/sheet1.xml")


class TestRowCap:
    def test_the_default_is_100000(self):
        from vantage.conf import DEFAULTS

        assert DEFAULTS["EXPORT_BINARY_MAX_ROWS"] == 100_000

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_over_the_cap_is_refused_with_413(self, client_in, author, fmt):
        make_posts(author, 4)
        with cap(3):
            response = client_in.get(reverse(LIST), {"export": fmt})
        assert response.status_code == 413
        assert response["Content-Type"].startswith("text/plain")
        text = response.content.decode()
        assert "4 rows" in text
        assert "EXPORT_BINARY_MAX_ROWS" in text and "CSV" in text

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_exactly_the_cap_succeeds(self, client_in, author, fmt):
        make_posts(author, 3)
        with cap(3):
            response = client_in.get(reverse(LIST), {"export": fmt})
        assert response.status_code == 200
        assert "Row 2" in text_of(response, fmt)

    @pytest.mark.parametrize("fmt", ["csv", "tsv", "json"])
    def test_streaming_formats_ignore_the_cap(self, client_in, author, fmt):
        make_posts(author, 4)
        with cap(1):
            response = client_in.get(reverse(LIST), {"export": fmt})
        assert response.status_code == 200
        assert body_of(response).count(b"Row ") == 4

    def test_the_excel_ceiling_is_reported_not_truncated(self, client_in, author, monkeypatch):
        make_posts(author, 3)
        monkeypatch.setattr(export, "_XLSX_MAX_ROWS", 3)  # header + 2 data rows
        with cap(1000):
            response = client_in.get(reverse(LIST), {"export": "xlsx"})
            pdf = client_in.get(reverse(LIST), {"export": "pdf"})
        assert response.status_code == 413
        assert "Excel" in response.content.decode() and "CSV" in response.content.decode()
        assert pdf.status_code == 200  # the worksheet ceiling is not a PDF limit

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_the_api_answers_in_its_own_error_format(self, client_in, author, fmt):
        from tests.test_api_extras import url as api_url
        from tests.testapp.models import Promo

        for i in range(4):
            Promo.objects.create(headline=f"p{i}")
        with cap(3):
            response = client_in.get(api_url("api-export"), {"format": fmt})
        assert response.status_code == 413
        body = response.json()
        assert "4 rows" in body["detail"] and "EXPORT_BINARY_MAX_ROWS" in body["detail"]
        assert body["rows"] == 4 and body["limit"] == 3

    def test_the_api_csv_is_not_capped(self, client_in, db):
        from tests.test_api_extras import url as api_url
        from tests.testapp.models import Promo

        for i in range(4):
            Promo.objects.create(headline=f"p{i}")
        with cap(1):
            response = client_in.get(api_url("api-export"), {"format": "csv"})
        assert response.status_code == 200

    def test_a_non_positive_cap_is_rejected(self):
        from django.core.exceptions import ImproperlyConfigured

        from vantage.conf import vantage_settings

        with override_settings(VANTAGE={"EXPORT_BINARY_MAX_ROWS": 0}):
            with pytest.raises(ImproperlyConfigured):
                vantage_settings.EXPORT_BINARY_MAX_ROWS  # noqa: B018


class TestCellLimit:
    def test_a_cell_over_32767_characters_is_cut_within_the_limit(self):
        rows = sheet_cells(
            body_of(
                stream_export(
                    "xlsx", InMemoryRows(Post(title="x" * 40_000)), ["title"], ["Title"], "f"
                )
            )
        )
        cell = rows[1][0]
        assert len(cell) == 32_767 and cell.endswith("…")

    def test_a_cell_at_the_limit_is_untouched(self):
        rows = sheet_cells(
            body_of(
                stream_export(
                    "xlsx", InMemoryRows(Post(title="y" * 32_767)), ["title"], ["Title"], "f"
                )
            )
        )
        assert rows[1][0] == "y" * 32_767


class TestSpoolLifetime:
    class Spy:
        def __init__(self):
            self.closed = False
            self.made = None

    @pytest.fixture
    def spied(self, monkeypatch):
        made = []
        real = export.tempfile.SpooledTemporaryFile

        def factory(*args, **kwargs):
            spool = real(*args, **kwargs)
            made.append(spool)
            return spool

        monkeypatch.setattr(export.tempfile, "SpooledTemporaryFile", factory)
        return made

    class Exploding:
        """A row source that yields one row and then fails."""

        def count(self):
            return 2

        def iterator(self, chunk_size=None):
            yield Post(title="ok")
            raise RuntimeError("database went away")

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_a_failed_build_closes_the_spool(self, spied, fmt):
        with pytest.raises(RuntimeError):
            stream_export(fmt, self.Exploding(), ["title"], ["Title"], "f")
        assert len(spied) == 1 and spied[0].closed

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_closing_an_unstarted_response_closes_the_spool(self, spied, fmt, db):
        response = stream_export(fmt, Post.objects.all(), ["title"], ["Title"], "f")
        assert not spied[0].closed
        response.close()
        assert spied[0].closed

    @pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
    def test_finishing_the_stream_closes_the_spool(self, spied, fmt, db):
        response = stream_export(fmt, Post.objects.all(), ["title"], ["Title"], "f")
        body_of(response)
        assert spied[0].closed


class TestWorkbookRegressions:
    def test_no_formula_elements_and_every_cell_is_an_inline_string(self, client_in, author):
        for i, lead in enumerate("=+-@"):
            Post.objects.create(title=f"{lead}SUM(A1)", slug=f"f{i}", body="b", author=author)
        data = sheet_xml(body_of(client_in.get(reverse(LIST), {"export": "xlsx"})))
        root = ET.fromstring(data)  # parses as XML
        assert list(root.iter(f"{NS}f")) == []
        cells = list(root.iter(f"{NS}c"))
        assert cells and all(c.get("t") == "inlineStr" for c in cells)
        assert b"<f>" not in data and b"<f " not in data

    def test_xml_illegal_characters_are_stripped(self):
        # Built in memory: PostgreSQL cannot store NUL in a text column.
        title = "a\x00b\x08c\x0bd\x0ce\x1ff￾g￿h"
        data = body_of(
            stream_export("xlsx", InMemoryRows(Post(title=title)), ["title"], ["Title"], "f")
        )
        ET.fromstring(sheet_xml(data))
        assert not re.search("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", sheet_xml(data).decode())
        assert sheet_cells(data)[1][0] == "abcdefgh"


def content_stream(data: bytes) -> bytes:
    return re.search(rb"stream\n(.*?)\nendstream", data, re.S).group(1)


class TestPdfRegressions:
    def test_an_operator_lookalike_stays_inside_one_string_literal(self, author):
        Post.objects.create(title=") Tj ET (", slug="x", body="b", author=author)
        response = stream_export("pdf", Post.objects.all(), ["title"], ["Title"], "f")
        stream = content_stream(body_of(response))
        assert b"(\\) Tj ET \\()" in stream
        # Strip string literals (honouring backslash escapes); what is left are operators.
        bare = re.sub(rb"\((?:\\.|[^\\()])*\)", b"()", stream)
        # Header + one row, one cell each: two Tm and two Tj, one text object.
        assert bare.count(b"Tj") == 2 and bare.count(b"Tm") == 2
        assert bare.count(b"BT") == 1 and bare.count(b"ET") == 1
        assert bare.count(b"Tf") == 2

    def test_backslash_parentheses_newlines_and_nul_are_neutralised(self):
        out = export._pdf_text("a\\b(c)d\r\ne\nf\x00g\th")
        assert out == b"a\\\\b\\(c\\)d e f g h"
        assert b"\x00" not in out and b"\r" not in out and b"\n" not in out
        # Every paren in the output is escaped, so the literal cannot be closed early.
        assert not re.search(rb"(?<!\\)[()]", out.replace(b"\\\\", b""))


class TestContentDisposition:
    @pytest.mark.parametrize("fmt", ["csv", "xlsx", "pdf"])
    def test_a_hostile_name_cannot_split_the_header(self, db, fmt):
        name = 'evil"\r\nSet-Cookie: x=1; filename="..\\..\\a'
        response = stream_export(fmt, Post.objects.all(), ["title"], ["Title"], name)
        value = response["Content-Disposition"]
        assert "\r" not in value and "\n" not in value
        assert value.count('"') == 2
        assert ".." not in value and "\\" not in value and "/" not in value
