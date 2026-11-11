"""Streaming exports of whatever the list page is currently showing.

Two properties matter more than format support. First, the export must honour the
active search and sort, because an export that silently ignores your filters and hands
back the whole table is worse than no export — you only notice after sending it to
someone. Second, it must stream: an admin exporting 400,000 rows should not put a
400,000-row list in a worker's memory, and should start downloading immediately rather
than appearing to hang.

``StreamingHttpResponse`` plus ``QuerySet.iterator()`` gives both. The cost is that a
streamed response cannot report an error once the first byte is out, which is why the
queryset is validated before the generator starts.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from typing import IO, Any

from django.http import HttpResponse, StreamingHttpResponse
from django.utils.text import slugify

__all__ = [
    "FORMATS",
    "ExportTooLarge",
    "stream_csv",
    "stream_export",
    "stream_json",
    "stream_pdf",
    "stream_tsv",
    "stream_xlsx",
]

FORMATS = ("csv", "json", "tsv", "xlsx", "pdf")

#: Rows per database round trip. Large enough that the query overhead disappears,
#: small enough that a wide model does not spike memory.
CHUNK_SIZE = 2000

#: Values that are data, never text a stranger typed: left as they are by the formula
#: rule, so a negative number or a negative duration stays one. Everything else is
#: treated as text, including types this module has never heard of.
_PLAIN_TYPES = (int, float, Decimal, dt.date, dt.time, dt.timedelta, uuid.UUID)


class _Echo:
    """A file-like object that returns what it is given.

    ``csv.writer`` insists on writing to something; this lets it hand each formatted
    row straight back to the generator instead of accumulating a buffer.
    """

    def write(self, value: str) -> str:
        return value


def _cell(value: Any, *, armoured: bool = False) -> str:
    """Flatten a value for a spreadsheet.

    Related objects export as their ``__str__``, which is what a human reading the
    file expects, rather than a primary key they would have to join back themselves.

    ``armoured`` is for the delimited-text writers. A value that starts with ``=``,
    ``+``, ``-``, ``@``, a tab or a carriage return is a formula to Excel, LibreOffice
    and Sheets, and a stranger can type one into any free-text field, so those get a
    leading quote (OWASP's guidance). It is decided on the Python type, before
    flattening: a negative number, decimal or duration is data and must stay one, which
    a check on the finished string could not tell apart. The exemption is an
    allowlist (:data:`_PLAIN_TYPES`), not a check for ``str``: a lazy translation, a
    property returning a value object or a third-party field type is not a ``str``
    either, and prints whatever text it holds.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "_meta"):
        # A related record whose __str__ prints one of its own secrets exports as
        # "<Model> #<pk>" instead. See vantage.sensitive.safe_str.
        from vantage.sensitive import safe_str

        text = safe_str(value)
        return _defuse(text) if armoured else text
    text = str(value)
    if armoured and not isinstance(value, _PLAIN_TYPES):
        return _defuse(text)
    return text


def _defuse(text: str) -> str:
    """The same rule the import error report applies, from the same place."""
    from vantage.importer import armour

    return armour(text)


def _value(obj: Any, column: str, masked: frozenset[str], *, armoured: bool = False) -> str:
    """One exported cell: masked when the column is sensitive, never the raw value.

    An export is the surface a masked value is most likely to escape through — it is
    a file, it gets emailed — so a sensitive column exports as the same bullets the
    table shows. Empty stays empty, exactly as on the page.
    """
    value = _resolve(obj, column)
    if column in masked:
        if value is None or value == "":
            return ""
        from vantage.sensitive import mask

        return mask(value, column)
    return _cell(value, armoured=armoured)


def _resolve(obj: Any, path: str) -> Any:
    from vantage.introspection import resolve_path

    hops = resolve_path(type(obj), path)
    if not hops:
        return getattr(obj, path, None)
    value = obj
    for hop in hops:
        if value is None:
            return None
        value = getattr(value, hop.name, None)
    return value


def _rows(
    queryset, columns: list[str], headers: list[str], masked: frozenset[str] = frozenset()
) -> Iterator[list[str]]:
    # Headers too: a column label can come from a stored list preference.
    yield [_defuse(header) for header in headers]
    for obj in queryset.iterator(chunk_size=CHUNK_SIZE):
        yield [_value(obj, column, masked, armoured=True) for column in columns]


#: Rows (header included) one worksheet can hold.
_XLSX_MAX_ROWS = 1_048_576

#: Formats that are built completely before the first byte is sent.
_BUFFERED = ("xlsx", "pdf")


class ExportTooLarge(Exception):
    """A buffered format (xlsx, pdf) was asked for more rows than it may carry."""

    def __init__(self, fmt: str, rows: int, cap: int, *, hard_limit: bool = False) -> None:
        self.fmt, self.rows, self.cap, self.hard_limit = fmt, rows, cap, hard_limit
        super().__init__(self.message())

    def message(self) -> str:
        if self.hard_limit:
            reason = f"An Excel worksheet holds at most {self.cap} data rows."
        else:
            reason = (
                f"The {self.fmt} format is limited to {self.cap} rows "
                "(VANTAGE['EXPORT_BINARY_MAX_ROWS'])."
            )
        return (
            f"{self.rows} rows match this export. {reason} "
            "Narrow the filter, or export as CSV, which has no row limit."
        )


def binary_row_cap(fmt: str) -> int:
    """The most data rows ``fmt`` may carry: the setting, and Excel's own ceiling."""
    from vantage.conf import vantage_settings

    cap = int(vantage_settings.EXPORT_BINARY_MAX_ROWS)
    return min(cap, _XLSX_MAX_ROWS - 1) if fmt == "xlsx" else cap


def check_binary_cap(fmt: str, queryset) -> None:
    """Refuse, before building anything, a xlsx or pdf export over the row cap."""
    if fmt not in _BUFFERED:
        return
    from vantage.conf import vantage_settings

    cap = binary_row_cap(fmt)
    rows = queryset.count()
    if rows > cap:
        hard = fmt == "xlsx" and cap < int(vantage_settings.EXPORT_BINARY_MAX_ROWS)
        raise ExportTooLarge(fmt, rows, cap, hard_limit=hard)


def stream_export(
    fmt: str,
    queryset,
    columns,
    headers,
    filename,
    *,
    masked=frozenset(),
    raise_too_large: bool = False,
):
    """Dispatch to the writer for ``fmt``. ``masked`` names the sensitive columns.

    An xlsx or pdf export over ``EXPORT_BINARY_MAX_ROWS`` is refused before anything is
    built: a 413 plain-text response, or :class:`ExportTooLarge` when ``raise_too_large``
    is set (the JSON API turns it into its own error body).
    """
    try:
        check_binary_cap(fmt, queryset)
    except ExportTooLarge as exc:
        if raise_too_large:
            raise
        response = HttpResponse(
            exc.message() + "\n", status=413, content_type="text/plain; charset=utf-8"
        )
        response["Cache-Control"] = "no-store"
        return response
    writers = {
        "csv": stream_csv,
        "tsv": stream_tsv,
        "json": stream_json,
        "xlsx": stream_xlsx,
        "pdf": stream_pdf,
    }
    return writers[fmt](queryset, columns, headers, filename, masked=frozenset(masked))


def _attach(response, filename: str, extension: str):
    response["Content-Disposition"] = f'attachment; filename="{slugify(filename)}.{extension}"'
    # Nothing downstream should cache a per-user, permission-scoped extract.
    response["Cache-Control"] = "no-store"
    return response


def stream_json(
    queryset, columns: list[str], headers: list[str], filename: str, *, masked=frozenset()
):
    """Newline-delimited JSON: one object per line.

    NDJSON rather than one big array because a streamed array cannot be closed if
    the queryset raises halfway, leaving a file that is silently truncated and
    invalid. Every line here is independently parseable, so a partial download is
    obviously partial rather than quietly wrong.
    """

    def lines():
        for obj in queryset.iterator(chunk_size=CHUNK_SIZE):
            row = {
                header: _value(obj, column, masked)
                for header, column in zip(headers, columns, strict=True)
            }
            yield json.dumps(row, ensure_ascii=False) + "\n"

    response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
    return _attach(response, filename, "ndjson")


def stream_tsv(
    queryset, columns: list[str], headers: list[str], filename: str, *, masked=frozenset()
):
    """Tab-separated, for pasting straight into a spreadsheet."""
    writer = csv.writer(_Echo(), delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in _rows(queryset, columns, headers, masked)),
        content_type="text/tab-separated-values; charset=utf-8",
    )
    return _attach(response, filename, "tsv")


def stream_csv(
    queryset, columns: list[str], headers: list[str], filename: str, *, masked=frozenset()
):
    """A ``StreamingHttpResponse`` of ``queryset`` as CSV."""
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in _rows(queryset, columns, headers, masked)),
        content_type="text/csv; charset=utf-8",
    )
    return _attach(response, filename, "csv")


# -- Excel and PDF ---------------------------------------------------------------
#
# Both are binary containers that need a trailer (a zip directory, a PDF xref table), so
# they cannot be streamed byte-for-byte the way CSV is. They go through the same
# ``_rows`` generator, which is what carries the permission-stripped columns, the masking
# of sensitive columns and the formula quoting, and are spooled to a temporary file that
# is read back in chunks. Memory stays bounded by the chunk size, not the row count.

#: Characters XML 1.0 cannot carry at all, even escaped.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")

_READ_CHUNK = 64 * 1024


class _SpoolChunks:
    """Iterate a finished temporary file, closing it however the response ends.

    A class rather than a generator because Django calls ``close()`` on the iterator
    even when the client went away before the first chunk, and a generator that never
    started would not run its ``finally``.
    """

    def __init__(self, spool: IO[bytes]) -> None:
        spool.seek(0)
        self._spool = spool

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        try:
            block = self._spool.read(_READ_CHUNK)
        except BaseException:
            self.close()
            raise
        if not block:
            self.close()
            raise StopIteration
        return block

    def close(self) -> None:
        self._spool.close()


def _spool_response(spool: IO[bytes], content_type: str):
    """Stream a finished temporary file, closing it when the response is done."""
    return StreamingHttpResponse(_SpoolChunks(spool), content_type=content_type)


def _xml_text(text: str) -> str:
    text = _XML_ILLEGAL.sub("", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


_OOXML = "http://schemas.openxmlformats.org"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_SHEET_NS = f"{_OOXML}/spreadsheetml/2006/main"
_REL_NS = f"{_OOXML}/officeDocument/2006/relationships"
_SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml"

_XLSX_CONTENT_TYPES = (
    f'{_XML_DECL}<Types xmlns="{_OOXML}/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/xl/workbook.xml" ContentType="{_SHEET_TYPE}.sheet.main+xml"/>'
    f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_SHEET_TYPE}.worksheet+xml"/>'
    f'<Override PartName="/xl/styles.xml" ContentType="{_SHEET_TYPE}.styles+xml"/>'
    "</Types>"
)
_XLSX_RELS = (
    f'{_XML_DECL}<Relationships xmlns="{_OOXML}/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_XLSX_WORKBOOK = (
    f'{_XML_DECL}<workbook xmlns="{_SHEET_NS}" xmlns:r="{_REL_NS}">'
    '<sheets><sheet name="Export" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_XLSX_WORKBOOK_RELS = (
    f'{_XML_DECL}<Relationships xmlns="{_OOXML}/package/2006/relationships">'
    f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_REL_NS}/styles" Target="styles.xml"/>'
    "</Relationships>"
)
# Style 0 is the default, style 1 is bold (the header row).
_XLSX_STYLES = (
    f'{_XML_DECL}<styleSheet xmlns="{_SHEET_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    "</styleSheet>"
)

#: Excel's limit on the characters in one cell; more makes it offer to repair the file.
_XLSX_MAX_CELL = 32_767


def _clip(text: str) -> str:
    if len(text) <= _XLSX_MAX_CELL:
        return text
    return text[: _XLSX_MAX_CELL - 1] + "\u2026"


def _sheet_rows(rows: Iterator[list[str]]) -> Iterator[bytes]:
    yield (f'{_XML_DECL}<worksheet xmlns="{_SHEET_NS}"><sheetData>'.encode())
    for number, row in enumerate(rows, start=1):
        style = ' s="1"' if number == 1 else ""
        cells = "".join(
            f'<c r="{_column_name(i)}{number}" t="inlineStr"{style}>'
            f'<is><t xml:space="preserve">{_xml_text(_clip(cell))}</t></is></c>'
            for i, cell in enumerate(row)
        )
        yield f'<row r="{number}">{cells}</row>'.encode()
    yield b"</sheetData></worksheet>"


def stream_xlsx(
    queryset, columns: list[str], headers: list[str], filename: str, *, masked=frozenset()
):
    """The list as an ``.xlsx`` workbook, written with the standard library only.

    Every cell is an inline string. Cells are text on purpose: they pass through the same
    ``_rows`` as CSV, so a masked column stays masked and a cell starting with ``=``,
    ``+``, ``-`` or ``@`` is quoted rather than becoming a formula.
    """
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES)
            archive.writestr("_rels/.rels", _XLSX_RELS)
            archive.writestr("xl/workbook.xml", _XLSX_WORKBOOK)
            archive.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS)
            archive.writestr("xl/styles.xml", _XLSX_STYLES)
            with archive.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as sheet:
                for chunk in _sheet_rows(_rows(queryset, columns, headers, masked)):
                    sheet.write(chunk)
    except BaseException:
        spool.close()
        raise
    response = _spool_response(
        spool, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return _attach(response, filename, "xlsx")


# A4 landscape, in points.
_PDF_WIDTH, _PDF_HEIGHT = 842, 595
_PDF_MARGIN = 36
_PDF_FONT_SIZE = 8
_PDF_LEADING = 12
#: Helvetica averages a little over half an em per character; slightly high so that
#: truncated text never spills into the next column.
_PDF_CHAR_WIDTH = _PDF_FONT_SIZE * 0.56


def _pdf_text(text: str) -> bytes:
    """A PDF string literal body: WinAnsi bytes, unrenderable characters as ``?``."""
    text = re.sub(r"[\r\n\t]+", " ", text)
    raw = text.encode("cp1252", errors="replace")
    # cp1252 leaves five bytes undefined; anything control-like becomes a space.
    raw = bytes(0x20 if b < 0x20 else b for b in raw)
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _pdf_fit(text: str, chars: int) -> str:
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= chars else text[: max(chars - 3, 0)] + "..."


def _pdf_document(rows: Iterator[list[str]], out: IO[bytes]) -> None:
    offsets: dict[int, int] = {}
    position = 0

    def write(data: bytes) -> None:
        nonlocal position
        out.write(data)
        position += len(data)

    def obj(number: int, body: bytes) -> None:
        offsets[number] = position
        write(b"%d 0 obj\n" % number + body + b"\nendobj\n")

    write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    # 1 catalog, 2 page tree, 3 regular font, 4 bold font; pages follow from 5.
    obj(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    obj(
        4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )

    kids: list[int] = []
    next_number = 5
    header: list[str] | None = None
    column_width = 0.0
    chars = 0
    per_page = max((_PDF_HEIGHT - 2 * _PDF_MARGIN) // _PDF_LEADING, 2)
    lines: list[tuple[list[str], bool]] = []

    def flush() -> None:
        nonlocal next_number
        if not lines:
            return
        ops = [b"BT"]
        y = _PDF_HEIGHT - _PDF_MARGIN
        for cells, bold in lines:
            ops.append(b"/F%d %d Tf" % (2 if bold else 1, _PDF_FONT_SIZE))
            for i, cell in enumerate(cells):
                x = _PDF_MARGIN + i * column_width
                ops.append(b"1 0 0 1 %.2f %.2f Tm" % (x, y))
                ops.append(b"(" + _pdf_text(_pdf_fit(cell, chars)) + b") Tj")
            y -= _PDF_LEADING
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content, page = next_number, next_number + 1
        next_number += 2
        obj(content, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        obj(
            page,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (_PDF_WIDTH, _PDF_HEIGHT, content),
        )
        kids.append(page)
        lines.clear()

    for row in rows:
        if header is None:
            header = row
            column_width = (_PDF_WIDTH - 2 * _PDF_MARGIN) / max(len(row), 1)
            chars = max(int((column_width - 4) / _PDF_CHAR_WIDTH), 1)
            lines.append((header, True))
            continue
        if len(lines) >= per_page:
            flush()
            lines.append((header, True))
        lines.append((row, False))
    if header is None:  # pragma: no cover - _rows always yields a header
        lines.append(([], True))
    flush()  # the last page; with no data rows, one page carrying the header

    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    refs = b" ".join(b"%d 0 R" % k for k in kids)
    obj(2, b"<< /Type /Pages /Kids [" + refs + b"] /Count %d >>" % len(kids))

    xref = position
    total = next_number
    write(b"xref\n0 %d\n" % total)
    write(b"0000000000 65535 f \n")
    for number in range(1, total):
        write(b"%010d 00000 n \n" % offsets[number])
    write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (total, xref))


def stream_pdf(
    queryset, columns: list[str], headers: list[str], filename: str, *, masked=frozenset()
):
    """The list as a plain PDF table: Helvetica, A4 landscape, header repeated per page.

    Text longer than its column is cut with ``...``; characters outside Windows-1252 print
    as ``?``. Like the workbook it is built from ``_rows``, so permissions, masking and
    formula quoting are identical to CSV.
    """
    spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        _pdf_document(_rows(queryset, columns, headers, masked), spool)
    except BaseException:
        spool.close()
        raise
    response = _spool_response(spool, "application/pdf")
    return _attach(response, filename, "pdf")
