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
import json
from collections.abc import Iterator
from typing import Any

from django.http import StreamingHttpResponse
from django.utils.text import slugify

__all__ = ["FORMATS", "stream_csv", "stream_export", "stream_json", "stream_tsv"]

FORMATS = ("csv", "json", "tsv")

#: Rows per database round trip. Large enough that the query overhead disappears,
#: small enough that a wide model does not spike memory.
CHUNK_SIZE = 2000


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

    ``armoured`` is for the delimited-text writers. A *text* value that starts with
    ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is a formula to Excel,
    LibreOffice and Sheets, and a stranger can type one into any free-text field, so
    those get a leading quote (OWASP's guidance). It is decided on the Python type,
    before flattening: a negative number, decimal or duration is data and must stay
    one, which a check on the finished string could not tell apart.
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
    if armoured and isinstance(value, str):
        return _defuse(value)
    return str(value)


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
    yield headers
    for obj in queryset.iterator(chunk_size=CHUNK_SIZE):
        yield [_value(obj, column, masked, armoured=True) for column in columns]


def stream_export(fmt: str, queryset, columns, headers, filename, *, masked=frozenset()):
    """Dispatch to the writer for ``fmt``. ``masked`` names the sensitive columns."""
    return {"csv": stream_csv, "tsv": stream_tsv, "json": stream_json}[fmt](
        queryset, columns, headers, filename, masked=frozenset(masked)
    )


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
