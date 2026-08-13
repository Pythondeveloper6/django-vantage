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


def _cell(value: Any) -> str:
    """Flatten a value for a spreadsheet.

    Related objects export as their ``__str__``, which is what a human reading the
    file expects, rather than a primary key they would have to join back themselves.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


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


def _rows(queryset, columns: list[str], headers: list[str]) -> Iterator[list[str]]:
    yield headers
    for obj in queryset.iterator(chunk_size=CHUNK_SIZE):
        yield [_cell(_resolve(obj, column)) for column in columns]


def stream_export(fmt: str, queryset, columns, headers, filename):
    """Dispatch to the writer for ``fmt``."""
    return {"csv": stream_csv, "tsv": stream_tsv, "json": stream_json}[fmt](
        queryset, columns, headers, filename
    )


def _attach(response, filename: str, extension: str):
    response["Content-Disposition"] = (
        f'attachment; filename="{slugify(filename)}.{extension}"'
    )
    # Nothing downstream should cache a per-user, permission-scoped extract.
    response["Cache-Control"] = "no-store"
    return response


def stream_json(queryset, columns: list[str], headers: list[str], filename: str):
    """Newline-delimited JSON: one object per line.

    NDJSON rather than one big array because a streamed array cannot be closed if
    the queryset raises halfway, leaving a file that is silently truncated and
    invalid. Every line here is independently parseable, so a partial download is
    obviously partial rather than quietly wrong.
    """

    def lines():
        for obj in queryset.iterator(chunk_size=CHUNK_SIZE):
            row = {
                header: _cell(_resolve(obj, column))
                for header, column in zip(headers, columns, strict=True)
            }
            yield json.dumps(row, ensure_ascii=False) + "\n"

    response = StreamingHttpResponse(lines(), content_type="application/x-ndjson")
    return _attach(response, filename, "ndjson")


def stream_tsv(queryset, columns: list[str], headers: list[str], filename: str):
    """Tab-separated, for pasting straight into a spreadsheet."""
    writer = csv.writer(_Echo(), delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in _rows(queryset, columns, headers)),
        content_type="text/tab-separated-values; charset=utf-8",
    )
    return _attach(response, filename, "tsv")


def stream_csv(queryset, columns: list[str], headers: list[str], filename: str):
    """A ``StreamingHttpResponse`` of ``queryset`` as CSV."""
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in _rows(queryset, columns, headers)),
        content_type="text/csv; charset=utf-8",
    )
    return _attach(response, filename, "csv")
