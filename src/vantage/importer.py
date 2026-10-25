"""Bringing a spreadsheet into the database, in the only order that is safe.

Import is the mirror of :mod:`vantage.export`, and the mirroring is load-bearing: the
columns an export produces are the columns an import accepts, so a file can be pulled
out, edited in a spreadsheet, and pushed back without anyone hand-editing a header.
That round trip is asserted in the test suite, because it is the property that breaks
silently the first time either side changes its column labels.

The engine is four separable steps, each a pure-ish function over the one before:

1. :func:`read_csv` — bytes in, a :class:`ParsedFile` out, or a refusal with a sentence
   a human can act on. Everything that can be wrong with a file is decided here, once,
   before anything else has looked at it.
2. :func:`importable_fields` and :func:`infer_mapping` — which model fields may be
   written at all, and which CSV column probably feeds each one.
3. :func:`resolve_mapping` — the security boundary. The mapping arrives from a form
   and is therefore hostile input; nothing downstream ever sees a field name that was
   not re-derived from the model on this request.
4. :func:`dry_run` / :func:`commit` — the same row loop, differing only in what happens
   to the transaction at the end.

Two design commitments are worth stating outright.

**Validation is Django's, not ours.** Every row goes through a real ``ModelForm`` built
by :func:`~vantage.widgets.build_form_class`, so coercion, ``clean_<field>``,
cross-field ``clean()``, unique checks and ``full_clean`` behave exactly as they do on
the edit page. A CSV importer that writes its own ``int(value)`` coercion is a second,
worse validation layer that will disagree with the form the same data would have gone
through by hand.

**Partial failure is reported, never smoothed over.** Each row saves inside its own
savepoint, so one bad row cannot poison the chunk around it and cannot half-write. The
result carries the exact count of each outcome plus every error keyed to its row and
column, which is what makes "1,204 created, 3 failed" an honest sentence rather than a
guess.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, router, transaction
from django.db.models import Model
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from vantage.inference import infer_list_link
from vantage.widgets import FieldKind, build_form_class, classify, infer_form_fields

__all__ = [
    "CHUNK_SIZE",
    "MODES",
    "FieldOption",
    "ImportRejected",
    "ImportResult",
    "Mapping",
    "ParsedFile",
    "RowChange",
    "RowDiff",
    "RowError",
    "StashExpired",
    "Stashed",
    "commit",
    "discard_stash",
    "dry_run",
    "error_csv_rows",
    "importable_fields",
    "infer_mapping",
    "key_options",
    "read_csv",
    "read_error_csv",
    "read_stash",
    "resolve_mapping",
    "stash_errors",
    "stash_upload",
    "sweep_stash",
]


# ── limits ──────────────────────────────────────────────────────────────────

#: Defaults, deliberately modest. An import is synchronous: every row is a form
#: validation and a save, so a file large enough to be interesting is also large enough
#: to time out a request. Past these numbers the honest answer is a management command,
#: not a spinner. Both are configurable — see :func:`limit`.
MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5_000

#: Rows per transaction on commit. Small enough that a failure late in a large file
#: leaves a resume point, large enough that the commit overhead disappears.
CHUNK_SIZE = 500

#: How many before/after diffs the preview collects. The preview is for judging whether
#: the mapping is right, which a handful of rows answers as well as a thousand.
PREVIEW_DIFF_ROWS = 10

#: Bytes of the file handed to the dialect sniffer, and the window scanned for the
#: binary-content check.
SNIFF_BYTES = 64 * 1024

#: A header row wider than this is a transposed file or a corrupt one, not a table.
MAX_COLUMNS = 200

#: A "column name" longer than this is a sentence — i.e. the first row is data.
MAX_HEADER_LENGTH = 128

#: Tried in this order. ``utf-8-sig`` comes *first* even though it is the rarer file,
#: because it is plain utf-8 plus BOM removal: decoding a BOM-prefixed file as bare
#: utf-8 succeeds and leaves a ``﻿`` glued to the first header, which then matches
#: no field and produces the baffling "your first column mapped to nothing" bug.
#: ``latin-1`` is last and never fails, so it is the terminating fallback.
ENCODINGS = ("utf-8-sig", "utf-8", "latin-1")


def limit(name: str, default: int) -> int:
    """A configurable cap, read through the settings layer when it knows the key.

    ``vantage_settings`` rejects unknown keys by design, so this asks with a fallback
    rather than assuming: the importer works with these defaults today, and picks up
    ``VANTAGE["IMPORT_MAX_BYTES"]`` / ``["IMPORT_MAX_ROWS"]`` the moment those names are
    added to :data:`vantage.conf.DEFAULTS`, with no change here.
    """
    from vantage.conf import vantage_settings

    value = getattr(vantage_settings, name, None)
    return int(value) if value else default


class ImportRejected(Exception):
    """The file, or the mapping over it, cannot be used at all.

    Carries the sentence shown to the user. Every message says what is wrong *and*
    what would be right, because the person holding the file is usually not the person
    who knows the schema.
    """


# ── step 1: read the file ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedFile:
    """A CSV reduced to headers and rows of strings, with how it was read."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    encoding: str
    delimiter: str

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def sample(self, limit: int = 3) -> list[list[str]]:
        """The first few rows, squared off to the header width, for display only.

        Padding here is a rendering convenience and never reaches the database: a row
        whose width disagrees with the header is a hard error at import time, because
        silently padding it would write blanks into real columns.
        """
        width = len(self.headers)
        return [list(row[:width]) + [""] * (width - len(row)) for row in self.rows[:limit]]

    def column(self, header: str) -> int:
        return self.headers.index(header)


def read_csv(raw: bytes, *, max_bytes: int | None = None, max_rows: int | None = None):
    """Parse an uploaded file, or refuse it with a reason.

    Refusal is the common path in practice — people upload the wrong file, an Excel
    workbook, or a PDF — so every rejection is a sentence rather than a stack trace.
    """
    cap_bytes = max_bytes if max_bytes is not None else limit("IMPORT_MAX_BYTES", MAX_BYTES)
    cap_rows = max_rows if max_rows is not None else limit("IMPORT_MAX_ROWS", MAX_ROWS)

    if len(raw) > cap_bytes:
        raise ImportRejected(
            _(
                "That file is %(size)s; the limit is %(limit)s. "
                "Split it, or import it with a management command."
            )
            % {"size": _bytes(len(raw)), "limit": _bytes(cap_bytes)}
        )

    text = _decode(raw)
    dialect = _sniff(text)
    reader = csv.reader(io.StringIO(text, newline=""), dialect)

    try:
        first = next(reader, None)
    except csv.Error as exc:
        raise ImportRejected(
            _("That file is not valid CSV: %(reason)s.") % {"reason": exc}
        ) from exc
    if first is None:
        raise ImportRejected(_("That file is empty."))

    headers = _check_headers(first)

    rows: list[tuple[str, ...]] = []
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                # A trailing blank line is how most editors end a file; it is not a row.
                continue
            rows.append(tuple(row))
            if len(rows) > cap_rows:
                raise ImportRejected(
                    ngettext(
                        "That file has more than %(count)s row, which is the import "
                        "limit. Split it, or import it with a management command.",
                        "That file has more than %(count)s rows, which is the import "
                        "limit. Split it, or import it with a management command.",
                        cap_rows,
                    )
                    % {"count": f"{cap_rows:,}"}
                )
    except csv.Error as exc:
        raise ImportRejected(
            _("That file stopped being valid CSV: %(reason)s.") % {"reason": exc}
        ) from exc

    if not rows:
        raise ImportRejected(_("That file has a header row but no data rows."))

    return ParsedFile(
        headers=headers,
        rows=tuple(rows),
        encoding=_encoding_of(raw),
        delimiter=dialect.delimiter,
    )


def _bytes(count: int) -> str:
    if count >= 1024 * 1024:
        return str(_("%(size)s MB") % {"size": f"{count / (1024 * 1024):.1f}"})
    return str(_("%(size)s KB") % {"size": max(1, count // 1024)})


def _looks_binary(raw: bytes) -> bool:
    """Whether these bytes are not a text file at all.

    Needed because ``latin-1`` decodes literally any byte sequence without error, so
    "did it decode?" cannot answer the question: a PNG decodes cleanly into mojibake
    and would reach the CSV parser as a single enormous line.
    """
    sample = raw[:SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    control = sum(1 for byte in sample if byte < 0x09 or 0x0E <= byte < 0x20 or byte == 0x7F)
    return control > len(sample) * 0.02


def _encoding_of(raw: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return encoding
    return ENCODINGS[-1]  # pragma: no cover - latin-1 cannot fail


def _decode(raw: bytes) -> str:
    if not raw.strip():
        raise ImportRejected(_("That file is empty."))
    if _looks_binary(raw):
        raise ImportRejected(
            _(
                "That does not look like a text file. Export it as CSV — a .xlsx workbook "
                "or a PDF cannot be read here."
            )
        )
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportRejected(  # pragma: no cover - latin-1 cannot fail
        _("That file is not readable as text in utf-8 or latin-1.")
    )


def _sniff(text: str) -> type[csv.Dialect] | csv.Dialect:
    """Guess the delimiter, falling back to a comma.

    The delimiter list is explicit because ``Sniffer`` will otherwise happily conclude
    that a prose column is space-delimited. A single-column file makes it give up
    entirely, which is not an error — it is a one-column file.
    """
    try:
        return csv.Sniffer().sniff(text[:SNIFF_BYTES], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _check_headers(row: list[str]) -> tuple[str, ...]:
    """Accept the first row as column names, or say why it is not one.

    The check exists because the most common import mistake is a file whose header row
    was stripped. Without it, row one is silently consumed as headers, every column maps
    to nothing, and the user is told their file is empty of recognisable fields.
    """
    cells = [cell.strip() for cell in row]
    if not cells or not any(cells):
        raise ImportRejected(_("The first row is blank. It must hold the column names."))
    if len(cells) > MAX_COLUMNS:
        raise ImportRejected(
            ngettext(
                "That file has %(count)s column, more than the %(limit)s allowed.",
                "That file has %(count)s columns, more than the %(limit)s allowed.",
                len(cells),
            )
            % {"count": len(cells), "limit": MAX_COLUMNS}
        )

    blank = [index + 1 for index, cell in enumerate(cells) if not cell]
    if blank:
        listed = ", ".join(str(number) for number in blank)
        raise ImportRejected(
            _(
                "The first row must be column names, but column %(columns)s has no name. "
                "Name every column, or delete the empty ones."
            )
            % {"columns": listed}
        )
    if any(len(cell) > MAX_HEADER_LENGTH for cell in cells):
        raise ImportRejected(
            _(
                "The first row looks like data, not column names — one of its values is "
                "longer than a column name would be. Add a header row."
            )
        )
    if all(_is_number(cell) for cell in cells):
        raise ImportRejected(
            _(
                "The first row is all numbers, so it looks like data rather than column "
                "names. Add a header row."
            )
        )

    seen: dict[str, str] = {}
    for cell in cells:
        folded = cell.casefold()
        if folded in seen:
            raise ImportRejected(
                _(
                    "Two columns are both called “%(column)s”. Column names must be unique, "
                    "or there is no way to say which one feeds a field."
                )
                % {"column": cell}
            )
        seen[folded] = cell
    return tuple(cells)


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


# ── step 2: what may be written, and by which column ────────────────────────

#: Kinds a CSV cell cannot honestly carry. Files and images because a cell is a string
#: and nothing in it can conjure an upload — and pointing a ``FileField`` at a
#: server-side path from user input is a file-disclosure bug, not a feature.
#: Many-to-many because the separator would collide with the file's own, and because
#: it is the case where "did this replace or append?" has no obvious answer.
UNIMPORTABLE_KINDS = frozenset(
    {FieldKind.FILE, FieldKind.IMAGE, FieldKind.RELATION_MANY, FieldKind.BINARY}
)


@dataclass(frozen=True)
class FieldOption:
    """One model field a column may be pointed at."""

    name: str
    label: str
    kind: str
    #: Whether a value in this column is written. False for the primary key, which is
    #: offered so it can identify a row without becoming something an import can set.
    writable: bool
    #: Whether this field can identify an existing record for an upsert.
    unique: bool


def importable_fields(panel, request=None, permissions=None) -> list[FieldOption]:
    """The fields this user may write on this panel, plus the primary key.

    Four exclusions, in order of how badly each would bite:

    * ``readonly_fields`` — the panel author has said this is not editable, and a
      column named after it is ignored rather than obeyed.
    * per-field permissions, when the configured resolver implements them.
    * kinds a cell cannot carry (:data:`UNIMPORTABLE_KINDS`).
    * everything ``infer_form_fields`` already drops: sensitive names, generated
      identity, ``auto_now`` stamps, ``editable=False``.

    The primary key is appended as a non-writable option because an upsert keyed on it
    is the single most common import, and it must be *addressable* without being
    *settable*.
    """
    model = panel.model
    readonly = set(getattr(panel, "readonly_fields", None) or ())
    excluded = set(getattr(panel, "exclude", None) or ())
    declared = getattr(panel, "fields", None)
    names = list(declared) if declared else infer_form_fields(model)

    options: list[FieldOption] = []
    for name in names:
        if name in readonly or name in excluded:
            continue
        field = _model_field(model, name)
        if field is None:
            continue  # a form-only field a panel added; there is nothing to write to
        if classify(field) in UNIMPORTABLE_KINDS:
            continue
        if not _field_allowed(permissions, request, model, name):
            continue
        options.append(
            FieldOption(
                name=name,
                label=_label(field),
                kind=str(classify(field)),
                writable=True,
                unique=bool(field.unique or field.primary_key),
            )
        )

    pk = model._meta.pk
    if pk is not None and not any(option.name == pk.name for option in options):
        options.insert(
            0,
            FieldOption(
                name=pk.name,
                label=_label(pk),
                kind=str(classify(pk)),
                writable=False,
                unique=True,
            ),
        )
    return options


def key_options(options: Iterable[FieldOption]) -> list[FieldOption]:
    """The fields that can identify an existing record: the pk and unique columns."""
    return [option for option in options if option.unique]


def _model_field(model: type[Model], name: str):
    from django.core.exceptions import FieldDoesNotExist

    try:
        return model._meta.get_field(name)
    except FieldDoesNotExist:
        return None


def _label(field) -> str:
    verbose = getattr(field, "verbose_name", None) or getattr(field, "name", "")
    return str(verbose).capitalize()


def _field_allowed(permissions, request, model, name: str) -> bool:
    """Per-field permission, asked of the resolver only if it answers such questions.

    Duck-typed rather than required: ``PanelPermissions`` has no field-level API today,
    and a project that adds one by subclassing should not have to also patch this file.
    """
    check = getattr(permissions, "has_field_perm", None)
    if check is None or request is None:
        return True
    return bool(check(request, model, name, "change"))


def _normalise(text: str) -> str:
    """Fold a header or field name to its comparable core: ``"View count"`` -> ``viewcount``."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def infer_mapping(headers: Iterable[str], options: Iterable[FieldOption]) -> dict[str, str]:
    """Guess which field each column feeds.

    Three passes, most specific first, and each pass runs over *every* header before
    the next begins. Per-header cascading would let a fuzzy match on an early column
    claim the field that a later column matches exactly, which is the failure people
    notice only after the import.

    A field is claimed once. A second column matching the same field is left unmapped
    rather than silently overwriting the first, because two columns feeding one field
    is a question only the user can answer.
    """
    options = list(options)
    exact = {option.name: option.name for option in options}
    folded = {_normalise(option.name): option.name for option in options}
    verbose = {_normalise(option.label): option.name for option in options}

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for lookup, key in ((exact, str), (folded, _normalise), (verbose, _normalise)):
        for header in headers:
            if header in mapping:
                continue
            name = lookup.get(key(header))
            if name is not None and name not in taken:
                mapping[header] = name
                taken.add(name)
    return mapping


# ── step 3: the mapping, re-derived from the model every time ───────────────

#: Import modes and the permission verbs each needs. An upsert needs both, because it
#: is not knowable in advance which rows will insert and which will update.
MODES: dict[str, tuple[str, ...]] = {
    "create": ("add",),
    "update": ("change",),
    "upsert": ("add", "change"),
}

MODE_LABELS: dict[str, str] = {
    "create": _("Create new records only"),
    "update": _("Update existing records only"),
    "upsert": _("Create or update (upsert)"),
}


@dataclass(frozen=True)
class Mapping:
    """A validated column→field plan. Nothing here came from the request unchecked."""

    #: Header -> field name, writable targets only, in header order.
    columns: dict[str, str]
    #: The header whose value identifies an existing record, and the field it looks up.
    key_column: str | None
    key_field: str | None
    mode: str
    #: ``(header, reason)`` for every column that will not be written. Shown to the
    #: user: a column silently dropped is how a readonly field looks like a bug.
    ignored: tuple[tuple[str, str], ...]
    #: ``(name, label)`` for fields the database will refuse to leave empty and that no
    #: column feeds. Rows that create will fail on these; rows that update will not.
    missing_required: tuple[tuple[str, str], ...] = ()

    @property
    def targets(self) -> list[str]:
        return list(self.columns.values())

    @property
    def mode_label(self) -> str:
        return MODE_LABELS.get(self.mode, self.mode)


def resolve_mapping(
    panel,
    parsed: ParsedFile,
    chosen: dict[str, str],
    *,
    key_field: str | None,
    mode: str,
    request=None,
    permissions=None,
) -> Mapping:
    """Turn a submitted mapping into a trusted one, or refuse it.

    ``chosen`` is form data and is treated as such. Every target is looked up in the
    options recomputed from the model *on this request* — never in a list carried
    between steps — so a hand-crafted POST naming ``password_hash``, a readonly column,
    or a field the user may not write cannot reach ``construct_instance``. Such a
    column is dropped and reported, not obeyed and not made into an error page: the
    honest UI outcome of "you may not write this" is "this column was ignored".
    """
    if mode not in MODES:
        raise ImportRejected(_("Choose whether to create, update, or do both."))

    options = {option.name: option for option in importable_fields(panel, request, permissions)}

    columns: dict[str, str] = {}
    ignored: list[tuple[str, str]] = []
    assigned: dict[str, str] = {}  # field name -> header, including non-writable ones
    identifying: list[str] = []  # headers held back because their field is not writable

    for header in parsed.headers:
        target = (chosen.get(header) or "").strip()
        if not target:
            # Translators: why a CSV column was not imported, shown beside its name.
            ignored.append((header, _("skipped")))
            continue
        option = options.get(target)
        if option is None:
            # Either a readonly/excluded field, a field this user may not write, or a
            # name that is not a field at all. All three are the same answer.
            ignored.append((header, _("read-only — nothing is written to it")))
            continue
        if option.name in assigned:
            ignored.append(
                (
                    header,
                    _("“%(column)s” already feeds that field") % {"column": assigned[option.name]},
                )
            )
            continue
        assigned[option.name] = header
        if option.writable:
            columns[header] = option.name
        else:
            identifying.append(header)

    key_field = (key_field or "").strip() or None
    if key_field is not None:
        option = options.get(key_field)
        if option is None or not option.unique:
            raise ImportRejected(
                _("The column that identifies a record must be a unique field or the primary key.")
            )
        if key_field not in assigned:
            raise ImportRejected(
                _("No column is mapped to “%(field)s”, so it cannot identify existing records.")
                % {"field": option.label}
            )

    if mode in ("update", "upsert") and key_field is None:
        raise ImportRejected(
            _("Choose the column that identifies an existing record before updating.")
        )
    if not columns:
        raise ImportRejected(
            _(
                "No column is mapped to a field that can be written, so this import would "
                "do nothing."
            )
        )

    key_column = assigned.get(key_field) if key_field else None
    for header in identifying:
        ignored.append(
            (
                header,
                _("identifies the record; its value is never written")
                if header == key_column
                else _("read-only — nothing is written to it"),
            )
        )

    return Mapping(
        columns=columns,
        key_column=key_column,
        key_field=key_field,
        mode=mode,
        ignored=tuple(ignored),
        missing_required=_missing_required(panel.model, options.values(), set(columns.values())),
    )


def _missing_required(model, options, mapped: set[str]) -> tuple[tuple[str, str], ...]:
    """Fields the database will not let a new row leave empty, that no column feeds.

    The test is deliberately the *database's*, not the form's. A ``CharField`` with
    ``blank=False`` is required on a form but stores ``""`` happily when it is simply
    absent, so calling it missing would be a false alarm on most imports. A foreign key
    with no default is the real case: without it the insert raises a ``NOT NULL``
    constraint error, which is a terrible thing to learn one row at a time.

    One more source counts as supplying a value, and it is asked of tenancy rather than
    guessed at here: the field :func:`~vantage.tenancy.scope_for` names as the model's
    single, direct path to the tenant is exactly the field ``vantage.tenancy.enforce``
    stamps on every create, on this path (``_Runner._save``) the same as on the HTML
    form and the JSON API. A column left unmapped for *that* field is not an omission,
    it is the normal, correct shape of a tenant-owned import — the tenant is not the
    uploader's to fill in. Tenancy being a second opinion here is deliberate: this is
    the one place the two must agree on what "the tenant column" is, and ``scope_for``
    is the only thing allowed to answer that. A multi-hop path (``"parent__tenant"``)
    does *not* qualify: ``enforce`` only verifies a parent's ownership on that shape, it
    never stamps a value, so a missing parent there is still a genuine gap.
    """
    from vantage.tenancy import ScopeKind, scope_for

    scope = scope_for(model)
    stamped_by_tenancy = (
        scope.path
        if scope.kind is ScopeKind.SCOPED and scope.path and "__" not in scope.path
        else None
    )

    missing: list[tuple[str, str]] = []
    for option in options:
        if not option.writable or option.name in mapped:
            continue
        if option.name == stamped_by_tenancy:
            continue
        field = _model_field(model, option.name)
        if field is None:  # pragma: no cover - options came from the model
            continue
        if field.null or field.has_default() or field.empty_strings_allowed:
            continue
        missing.append((option.name, option.label))
    return tuple(missing)


# ── the result ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RowError:
    """One thing wrong with one cell, addressed the way the user sees the file."""

    #: 1-based line of the file, header included, so row 2 is the first data row.
    row: int
    #: The CSV column name, or "" for a whole-row problem.
    column: str
    message: str


@dataclass(frozen=True)
class RowChange:
    label: str
    before: str
    after: str


@dataclass(frozen=True)
class RowDiff:
    row: int
    pk: Any
    label: str
    changes: tuple[RowChange, ...]


@dataclass
class ImportResult:
    """What an import did, or would have done."""

    dry_run: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[RowError] = dataclass_field(default_factory=list)
    diffs: list[RowDiff] = dataclass_field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged + self.failed

    @property
    def wrote_anything(self) -> bool:
        return bool(self.created or self.updated)

    def summary(self) -> str:
        """A sentence that never rounds a failure away.

        Every non-zero outcome is named with its exact count. "Some rows failed" is the
        message this method exists to make impossible to produce.

        Each outcome is its own plural form. English does not inflect "created", but
        the count still selects the form, so a language that agrees its participle with
        the number has somewhere to put it. The clauses are joined rather than written
        out as one sentence per combination, which would be sixteen messages.
        """
        parts = [
            ngettext("%(count)s created", "%(count)s created", self.created)
            % {"count": f"{self.created:,}"}
            if self.created
            else "",
            ngettext("%(count)s updated", "%(count)s updated", self.updated)
            % {"count": f"{self.updated:,}"}
            if self.updated
            else "",
            ngettext("%(count)s unchanged", "%(count)s unchanged", self.unchanged)
            % {"count": f"{self.unchanged:,}"}
            if self.unchanged
            else "",
            ngettext("%(count)s failed", "%(count)s failed", self.failed)
            % {"count": f"{self.failed:,}"}
            if self.failed
            else "",
        ]
        stated = [part for part in parts if part]
        return ", ".join(stated) if stated else str(_("nothing to do"))


# ── step 4: the row loop ────────────────────────────────────────────────────

#: Spreadsheet vocabulary for booleans, folded before the widget sees it.
#: ``CheckboxInput.value_from_datadict`` understands "false" but reads "0" as *True* —
#: it is a non-empty string, and truthiness is the last thing it tries. A CSV that
#: writes 1/0 is normal, so the widget's blind spot has to be closed here.
TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1", "on"})
FALSE_WORDS = frozenset({"false", "f", "no", "n", "0", "off"})


class _Runner:
    """The row loop, shared by the dry run and the commit.

    Held as an object rather than a function so the per-run caches — related-object
    lookups, the form class, the field map — are built once and outlive the loop.
    """

    def __init__(self, panel, parsed: ParsedFile, mapping: Mapping, request, permissions):
        self.panel = panel
        self.model = panel.model
        self.parsed = parsed
        self.mapping = mapping
        self.request = request
        self.permissions = permissions
        self.width = len(parsed.headers)
        self.db = router.db_for_write(self.model)

        self.form_class = build_form_class(panel, mapping.targets)
        self.fields = {name: self.model._meta.get_field(name) for name in mapping.targets}
        self.column_index = [
            (parsed.column(header), name) for header, name in mapping.columns.items()
        ]
        self.header_for = {name: header for header, name in mapping.columns.items()}
        self.key_index = (
            parsed.column(mapping.key_column) if mapping.key_column is not None else None
        )
        self.booleans = [
            name for name, field in self.fields.items() if classify(field) is FieldKind.BOOLEAN
        ]
        self.relations = [
            name for name, field in self.fields.items() if classify(field) is FieldKind.RELATION_ONE
        ]
        self._related: dict[tuple[str, str], tuple[str | None, str | None]] = {}
        self._labels: dict[tuple[str, Any], str] = {}

        # A panel with a ``save_model`` hook may well be the thing that supplies the
        # missing value — stamping the acting user onto an ``author`` field is the
        # canonical example — so the guard steps aside for one rather than pre-empting
        # a write it cannot predict.
        self.guard_required = bool(mapping.missing_required) and not callable(
            getattr(panel, "save_model", None)
        )

    # ── the queryset existing rows are looked up in ─────────────────────────

    @property
    def lookup_queryset(self):
        """Scoped to what this user may see, and nothing narrower.

        ``filter_queryset`` with ``"view"`` matches how the form views scope a single
        record: scoping answers "does this row exist for you", which has one answer per
        user rather than one per verb. A per-row ``has_object_perm`` call is
        deliberately *not* made — that is the N-queries-per-page mistake
        :mod:`vantage.permissions` exists to prevent, and the same reasoning that
        governs bulk actions governs a bulk write.
        """
        queryset = self.panel.get_queryset(self.request)
        if self.permissions is not None and self.request is not None:
            queryset = self.permissions.filter_queryset(self.request, queryset, "view")
        return queryset

    # ── one row ─────────────────────────────────────────────────────────────

    def row(self, number: int, row: tuple[str, ...], result: ImportResult) -> None:
        if len(row) != self.width:
            # Two counts, two plural forms. The header width is pluralised on its own
            # and passed in as a finished noun phrase — the pattern ``django.contrib
            # .admin`` uses with ``model_ngettext`` — because a single ``ngettext`` can
            # only agree with one number, and "the header has 1 columns" is a bug.
            columns = ngettext("%(width)s column", "%(width)s columns", self.width) % {
                "width": self.width
            }
            self._fail(
                result,
                RowError(
                    number,
                    "",
                    ngettext(
                        "This row has %(count)s value but the header has %(columns)s. "
                        "A stray or missing separator shifts every value after it.",
                        "This row has %(count)s values but the header has %(columns)s. "
                        "A stray or missing separator shifts every value after it.",
                        len(row),
                    )
                    % {"count": len(row), "columns": columns},
                ),
            )
            return

        data = {name: row[index] for index, name in self.column_index}
        for name in self.booleans:
            data[name] = _fold_boolean(data[name])

        try:
            existing = self._find(number, row)
        except _RowRejected as exc:
            self._fail(result, exc.error)
            return

        if existing is not None and self.mapping.mode == "create":
            self._fail(
                result,
                RowError(
                    number,
                    self.mapping.key_column or "",
                    _(
                        "A %(model)s with this value already exists (#%(pk)s). "
                        "This import is set to create new records only."
                    )
                    % {"model": self.model._meta.verbose_name, "pk": existing.pk},
                ),
            )
            return
        if existing is None and self.mapping.mode == "update":
            self._fail(
                result,
                RowError(
                    number,
                    self.mapping.key_column or "",
                    _("No existing record matches. This import is set to update only."),
                ),
            )
            return
        if existing is None and self.guard_required:
            # Said once, in the file's own vocabulary. The alternative is a raw
            # "NOT NULL constraint failed: testapp_post.author_id" on every row.
            listed = ", ".join(f"“{label}”" for _name, label in self.mapping.missing_required)
            self._fail(
                result,
                RowError(
                    number,
                    "",
                    ngettext(
                        "%(fields)s must have a value and no column feeds it, so this "
                        "row cannot be created.",
                        "%(fields)s must have values and no column feeds them, so this "
                        "row cannot be created.",
                        len(self.mapping.missing_required),
                    )
                    % {"fields": listed},
                ),
            )
            return

        before = self._snapshot(existing) if existing is not None else None
        label = str(existing) if existing is not None else ""

        # Relations are resolved after the record is found, so a row with an unresolvable
        # foreign key can still be run through the form: one pass then reports everything
        # wrong with the row rather than making the user fix it one error per attempt.
        relation_errors = self._resolve_relations(number, data)

        form = self.form_class(data=data, instance=existing)
        if not form.is_valid() or relation_errors:
            named = {error.column for error in relation_errors}
            rest = [error for error in self._form_errors(number, form) if error.column not in named]
            self._fail(result, *relation_errors, *rest)
            return

        if existing is not None:
            changed = self._changes(before, form)
            if not changed:
                result.unchanged += 1
                return
            if len(result.diffs) < PREVIEW_DIFF_ROWS:
                result.diffs.append(
                    RowDiff(row=number, pk=existing.pk, label=label, changes=changed)
                )

        try:
            self._save(form)
        except (DatabaseError, ValidationError) as exc:
            # The savepoint has already rolled this row back, so the surrounding
            # transaction is still usable and the row left nothing behind.
            self._fail(result, RowError(number, "", _clean_message(exc)))
            return

        if existing is None:
            result.created += 1
        else:
            result.updated += 1

    def _save(self, form) -> None:
        """Save inside a savepoint, through the panel's hook if it has one.

        Nested ``atomic`` is a savepoint, which is what makes per-row failure survivable:
        an ``IntegrityError`` inside one poisons only that savepoint, and the chunk
        around it keeps its already-written rows. Going through ``save_model`` keeps
        parity with the edit form — a panel that stamps ``updated_by`` there must not
        find that an import bypassed it. No ``save_m2m``: many-to-many columns are never
        importable, so there is nothing deferred to write.
        """
        from vantage.tenancy import enforce as enforce_tenant

        hook = getattr(self.panel, "save_model", None)
        with transaction.atomic(using=self.db):
            obj = form.save(commit=False)
            # Same guard the edit form applies, for the same reason: a spreadsheet is
            # just as capable of carrying another tenant's key as a POST body is.
            enforce_tenant(self.request, obj)
            if callable(hook):
                hook(self.request, obj, form, form.instance.pk is not None)
            else:
                obj.save()

    # ── lookups ─────────────────────────────────────────────────────────────

    def _find(self, number: int, row: tuple[str, ...]):
        if self.key_index is None:
            return None
        raw = row[self.key_index].strip()
        if not raw:
            raise _RowRejected(
                RowError(
                    number,
                    self.mapping.key_column or "",
                    _("This column identifies the record, so it cannot be empty."),
                )
            )
        try:
            return self.lookup_queryset.filter(**{self.mapping.key_field: raw}).first()
        except (ValueError, TypeError, ValidationError, DatabaseError):
            raise _RowRejected(
                RowError(
                    number,
                    self.mapping.key_column or "",
                    _("“%(value)s” is not a valid value.") % {"value": raw},
                )
            ) from None

    def _resolve_relations(self, number: int, data: dict[str, str]) -> list[RowError]:
        """Let a foreign key be given as a label, not only as a primary key.

        Export writes a related object as its ``__str__``, so requiring a pk here would
        break the round trip and, worse, would make the obvious hand-written file — a
        column of customer names — fail on every row. The pk is tried first because it
        is unambiguous; the label lookup is only reached when the value is not a pk, and
        an ambiguous label is an error rather than a coin flip.
        """
        errors: list[RowError] = []
        for name in self.relations:
            raw = (data.get(name) or "").strip()
            if not raw:
                continue
            field = self.fields[name]
            cached = self._related.get((name, raw))
            if cached is None:
                # Cached as a pair so a value that was ambiguous the first time keeps
                # saying so on row 900, rather than degrading to "no match".
                cached = self._resolve_one(field.related_model, raw)
                self._related[(name, raw)] = cached
            resolved, error = cached
            if resolved is not None:
                data[name] = resolved
            elif error is not None:
                errors.append(RowError(number, self.header_for[name], error))
        return errors

    def _resolve_one(self, related, raw: str) -> tuple[Any, str | None]:
        # Scoped, so a spreadsheet cannot reach another tenant's record by naming it —
        # an unresolvable name reports "no match", which is exactly what a row that
        # does not exist for this user should say. A no-op unless tenancy is on.
        from vantage.tenancy import scope_queryset

        manager = scope_queryset(related._default_manager.all(), self.request)
        pk_field = related._meta.pk
        try:
            candidate = pk_field.to_python(raw)
        except (ValidationError, ValueError, TypeError):
            candidate = None
        if candidate is not None and manager.filter(pk=candidate).exists():
            return raw, None

        label_field = infer_list_link(related)
        if label_field == pk_field.name:
            return None, _no_match(related, raw)
        matches = list(manager.filter(**{label_field: raw}).values_list("pk", flat=True)[:2])
        if len(matches) == 1:
            return str(matches[0]), None
        if len(matches) > 1:
            return None, (
                _("“%(value)s” matches more than one %(model)s. Use the numeric id instead.")
                % {"value": raw, "model": related._meta.verbose_name}
            )
        return None, _no_match(related, raw)

    # ── change detection ────────────────────────────────────────────────────

    def _snapshot(self, instance) -> dict[str, Any]:
        """Values before the form touches them, read by ``attname`` so a foreign key
        compares as an id and costs no extra query."""
        return {name: getattr(instance, field.attname, None) for name, field in self.fields.items()}

    def _changes(self, before: dict[str, Any], form) -> tuple[RowChange, ...]:
        """What this row would actually alter.

        ``ModelForm._post_clean`` has already written the cleaned values onto
        ``form.instance``, so the comparison is between the snapshot taken beforehand
        and the instance as it now stands — the same values that are about to be saved,
        rather than a re-derivation that could disagree with them.
        """
        changes: list[RowChange] = []
        for name, field in self.fields.items():
            after = getattr(form.instance, field.attname, None)
            if after == before[name]:
                continue
            changes.append(
                RowChange(
                    label=_label(field),
                    before=self._render(field, before[name]),
                    after=self._render_after(name, field, form, after),
                )
            )
        return tuple(changes)

    def _render_after(self, name: str, field, form, value) -> str:
        # cleaned_data already holds the related *instance* for a relation, so the new
        # side of a diff never costs a query.
        cleaned = form.cleaned_data.get(name, _MISSING)
        if cleaned is not _MISSING and field.is_relation:
            return _display(cleaned)
        return self._render(field, value)

    def _render(self, field, value) -> str:
        if value is None or not field.is_relation:
            return _display(value)
        key = (field.name, value)
        if key not in self._labels:
            obj = field.related_model._default_manager.filter(pk=value).first()
            self._labels[key] = _display(obj) if obj is not None else str(value)
        return self._labels[key]

    # ── errors ──────────────────────────────────────────────────────────────

    def _form_errors(self, number: int, form) -> list[RowError]:
        """Every validation failure, keyed to the column the user would go and fix.

        A field error carries the CSV header rather than the model field name, because
        the person reading the report is looking at a spreadsheet, not at ``models.py``.
        """
        errors: list[RowError] = []
        for name, messages in form.errors.items():
            column = self.header_for.get(name, "")
            for message in messages:
                errors.append(RowError(number, column, str(message)))
        return errors

    @staticmethod
    def _fail(result: ImportResult, *errors: RowError) -> None:
        result.failed += 1
        result.errors.extend(errors)


class _RowRejected(Exception):
    def __init__(self, error: RowError) -> None:
        super().__init__(error.message)
        self.error = error


_MISSING = object()


def _no_match(related, raw: str) -> str:
    return _("No %(model)s matches “%(value)s”.") % {
        "model": related._meta.verbose_name,
        "value": raw,
    }


def _fold_boolean(raw: str) -> str:
    text = str(raw).strip().lower()
    if text in TRUE_WORDS:
        return "true"
    if text in FALSE_WORDS:
        return "false"
    return raw


def _display(value: Any) -> str:
    """Flatten a value for a diff cell. Mirrors what the export writes."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _clean_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(exc.messages)
    return str(exc).strip() or type(exc).__name__


def _numbered(parsed: ParsedFile) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Rows paired with their line in the file, header counted as line 1."""
    for index, row in enumerate(parsed.rows):
        yield index + 2, row


def _chunks(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def dry_run(panel, parsed: ParsedFile, mapping: Mapping, request=None, permissions=None):
    """Run the whole import inside a transaction that is rolled back.

    Every row is validated *and saved*, then the transaction is discarded. Simulating
    instead — validating without saving — would miss exactly the failures a preview is
    for: unique collisions between two rows of the same file, database constraints no
    form knows about, and anything a ``pre_save`` signal decides.

    **What this cannot promise.** The rollback covers writes to this database, made
    through this connection, on a backend with working transactions. It does not and
    cannot undo:

    * side effects outside the database — email, HTTP calls, cache writes, queued
      tasks, log lines — performed by a signal receiver or a ``save_model`` hook;
    * writes routed to a *different* database, which is a separate transaction;
    * files written to storage. The importer never writes files itself (file and image
      fields are not importable), but a receiver that generates a thumbnail will leave
      it behind;
    * anything at all on a backend without transactional guarantees (MySQL with MyISAM
      tables, or a router pointing at a non-transactional store).

    It also holds locks for its duration, so a dry run over a large file can block
    concurrent writers on the rows it touches — one more reason the row cap is low.
    """
    result = ImportResult(dry_run=True)
    runner = _Runner(panel, parsed, mapping, request, permissions)
    with transaction.atomic(using=runner.db):
        for number, row in _numbered(parsed):
            runner.row(number, row, result)
        # Explicit, and last: once set, the block refuses further queries, so anything
        # that still needs the database must already have happened.
        transaction.set_rollback(True, using=runner.db)
    return result


def commit(
    panel,
    parsed: ParsedFile,
    mapping: Mapping,
    request=None,
    permissions=None,
    *,
    chunk_size: int = CHUNK_SIZE,
):
    """Write the file for real, one transaction per chunk.

    The atomicity guarantee is stated precisely because "atomic" is often heard as
    something stronger than any chunked writer can offer:

    * a **row** is all-or-nothing — it saves inside its own savepoint, so a failure
      never leaves a half-written record;
    * a **chunk** is one transaction, so a crash between chunks leaves whole chunks
      committed and a clear resume point rather than a torn write;
    * the **file** is not one transaction. The first chunk is durable while a row in
      the fourth is still failing. That is the trade that lets a 5,000-row import report "1,204
      created, 3 failed" instead of throwing away 1,204 good rows for 3 bad ones.

    Callers that need file-level all-or-nothing already have it: run the dry run, and
    refuse to commit unless it reported zero failures.
    """
    result = ImportResult(dry_run=False)
    runner = _Runner(panel, parsed, mapping, request, permissions)
    for chunk in _chunks(_numbered(parsed), chunk_size):
        with transaction.atomic(using=runner.db):
            for number, row in chunk:
                runner.row(number, row, result)
    return result


# ── the error report ────────────────────────────────────────────────────────

#: Characters that make a spreadsheet treat a cell as a formula. A cell beginning with
#: one is prefixed with an apostrophe before it is written.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def armour(value: Any) -> str:
    """Neutralise a cell that a spreadsheet would otherwise execute.

    The error report is the one place this file writes attacker-influenced text back
    out as CSV: a column called ``=cmd|'/c calc'!A1`` arrives in the header row and
    leaves in the report. Excel, LibreOffice and Sheets all evaluate a leading ``=``,
    ``+``, ``-`` or ``@`` on open, so the prefix goes on before the cell is written and
    not, as is often done, only when the value "looks dangerous".
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text[:1] in FORMULA_PREFIXES else text


def error_csv_rows(result: ImportResult) -> list[list[str]]:
    """The error report, as rows ready for ``csv.writer``."""
    # The report is downloaded and read by the person who uploaded the file, so its
    # header row is UI, not a programmatic key: nothing parses it back.
    # Translators: header row of the downloadable CSV listing what went wrong.
    rows = [[str(_("Row")), str(_("Column")), str(_("Error"))]]
    rows.extend(
        [str(error.row), armour(error.column), armour(error.message)] for error in result.errors
    )
    return rows


def error_csv_bytes(result: ImportResult) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(error_csv_rows(result))
    return buffer.getvalue().encode("utf-8")


# ── the stash ───────────────────────────────────────────────────────────────
#
# The uploaded file has to survive three requests: upload, map, preview, commit. It
# must not go in the session (a cookie session cannot hold a megabyte, and a database
# session should not), and it must not go anywhere served by the web server — an
# uploaded extract of production data under MEDIA_URL is a data breach with a URL.
#
# So: a private temp directory, a random token, and a sidecar recording who uploaded
# what. The token alone is not authority; every read re-checks the owner and the model.

#: Long enough that guessing is hopeless, and matched strictly before the value is ever
#: put anywhere near a path — the token arrives from the URL, so this regex is the
#: path-traversal defence.
TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{22,64}\Z")

#: An abandoned upload is swept after this. Long enough to read a preview and think
#: about it, short enough that production data is not left on a disk overnight.
STASH_TTL = 60 * 60


class StashExpired(LookupError):
    """The uploaded file is gone, or was never this user's to read."""


@dataclass(frozen=True)
class Stashed:
    token: str
    filename: str
    raw: bytes


def _stash_dir() -> Path:
    # The uid is in the name because the parent is world-writable: without it, another
    # local account could create the directory first and own everything inside it.
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    path = Path(gettempdir()) / f"vantage-imports{suffix}"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _private_opener(path, flags):
    # O_EXCL means we created this file or the write fails: a symlink planted at a
    # predictable path cannot be followed. 0o600 so the mode is right from creation
    # rather than after a chmod that races.
    return os.open(path, flags | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _paths(token: str) -> tuple[Path, Path, Path]:
    directory = _stash_dir()
    return (
        directory / f"{token}.csv",
        directory / f"{token}.json",
        directory / f"{token}.errors.csv",
    )


def stash_upload(*, user, model_label: str, filename: str, raw: bytes) -> str:
    """Write an upload to the stash and return its token."""
    sweep_stash()
    token = secrets.token_urlsafe(24)
    data_path, meta_path, _errors = _paths(token)
    with open(data_path, "wb", opener=_private_opener) as handle:
        handle.write(raw)
    meta = {
        "user": user.pk,
        "model": model_label,
        "filename": filename,
        "created": time.time(),
    }
    with open(meta_path, "w", encoding="utf-8", opener=_private_opener) as handle:
        json.dump(meta, handle)
    return token


def _meta(token: str, user, model_label: str) -> dict:
    if not TOKEN_RE.match(token or ""):
        raise StashExpired(_("That upload is no longer available."))
    _data, meta_path, _errors = _paths(token)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StashExpired(_("That upload is no longer available.")) from exc
    # Bound to the uploader and to the model. A token is a name, not a capability:
    # the same message for "not yours" and "gone" so it cannot be probed either.
    if meta.get("user") != user.pk or meta.get("model") != model_label:
        raise StashExpired(_("That upload is no longer available."))
    if time.time() - float(meta.get("created") or 0) > STASH_TTL:
        discard_stash(token)
        raise StashExpired(_("That upload has expired. Upload the file again."))
    return meta


def read_stash(*, user, model_label: str, token: str) -> Stashed:
    meta = _meta(token, user, model_label)
    data_path, _meta_path, _errors = _paths(token)
    try:
        raw = data_path.read_bytes()
    except OSError as exc:
        raise StashExpired(_("That upload is no longer available.")) from exc
    return Stashed(token=token, filename=str(meta.get("filename") or "import.csv"), raw=raw)


def stash_errors(*, token: str, result: ImportResult) -> None:
    """Park the error report next to the upload so it can be downloaded afterwards."""
    if not TOKEN_RE.match(token or ""):  # pragma: no cover - callers hold a live token
        return
    _data, _meta_path, error_path = _paths(token)
    error_path.unlink(missing_ok=True)
    with open(error_path, "wb", opener=_private_opener) as handle:
        handle.write(error_csv_bytes(result))


def read_error_csv(*, user, model_label: str, token: str) -> bytes:
    _meta(token, user, model_label)
    _data, _meta_path, error_path = _paths(token)
    try:
        return error_path.read_bytes()
    except OSError as exc:
        raise StashExpired(_("That report is no longer available.")) from exc


def discard_stash(token: str) -> None:
    """Delete the upload. Safe to call twice, and on a token that never existed."""
    if not TOKEN_RE.match(token or ""):
        return
    for path in _paths(token):
        path.unlink(missing_ok=True)


def keep_report_only(token: str) -> None:
    """Drop the uploaded data once it has been committed, keeping the error report.

    The file itself is production data and has no reason to outlive the import. The
    report is a few kilobytes of the user's own error messages, and they have not
    downloaded it yet.
    """
    if not TOKEN_RE.match(token or ""):  # pragma: no cover - callers hold a live token
        return
    data_path, _meta_path, _errors = _paths(token)
    data_path.unlink(missing_ok=True)


def sweep_stash(now: float | None = None) -> int:
    """Delete every stash older than the TTL. Called on each upload.

    Sweeping on write rather than from a cron job keeps the whole feature dependency
    free: the directory only grows when someone is importing, so that is exactly when
    it is worth tidying.
    """
    moment = time.time() if now is None else now
    removed = 0
    try:
        entries = list(_stash_dir().glob("*.json"))
    except OSError:  # pragma: no cover - the directory was just created
        return 0
    for meta_path in entries:
        try:
            created = float(json.loads(meta_path.read_text(encoding="utf-8")).get("created") or 0)
        except (OSError, ValueError):
            created = 0.0
        if moment - created > STASH_TTL:
            discard_stash(meta_path.stem)
            removed += 1
    return removed
