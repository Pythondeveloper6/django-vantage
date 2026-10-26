"""Inferring a good panel configuration from the model alone.

This is the core value of the package. A project that writes no ``panel.py`` at all
must still get columns worth looking at, filters that matter, search that works, and
a sensible order — and the inference has to be *predictable*, because a user who
cannot guess what it will do will simply configure everything by hand and lose the benefit.

Every function here is pure: model in, configuration out. No database access, no
request, no settings. That makes the whole engine table-testable, which is why the
inference tests are the densest in the suite.

Inference is resolved **per attribute**. Setting ``list_display`` on a panel overrides
the columns and nothing else; filters, search and ordering stay inferred. Partial
configuration must never opt a user out of the rest of the intelligence.
"""

from __future__ import annotations

import re
from typing import Any

from django.db import models

from vantage.introspection import concrete_fields

__all__ = [
    "infer_list_display",
    "infer_list_filter",
    "infer_list_link",
    "infer_list_search",
    "infer_ordering",
    "infer_sensitive_fields",
]

#: Columns beyond this are noise; the rest are one click away on the detail page.
MAX_LIST_DISPLAY = 6
MAX_LIST_FILTER = 5
MAX_LIST_SEARCH = 5

#: Names that strongly suggest the human-readable label for a record.
DISPLAY_NAMES = ("name", "title", "label", "headline", "subject", "display_name")

#: The original sensitive-name pattern, kept importable for code written against it.
#: The live rule is :func:`vantage.sensitive.field_is_sensitive`, which adds the
#: settings-extensible vocabulary, the type rules and encrypted field classes; nothing in
#: Vantage reads this regex any more.
SENSITIVE = re.compile(
    r"(^|_)(password|passwd|secret|token|api_key|apikey|private_key|salt|hash|"
    r"signature|otp|pin|cvv|ssn)($|_)",
    re.IGNORECASE,
)

#: A CharField longer than this reads as prose in a table cell, not a value.
SHORT_TEXT_LENGTH = 100
LONG_TEXT_LENGTH = 200

#: Timestamps that belong at the end of a row, not the front.
TRAILING_NAMES = ("created_at", "created", "updated_at", "updated", "modified", "modified_at")

#: Field classes never worth a list column — too long, too big, or unrenderable.
UNCOLUMNABLE = (
    models.TextField,
    models.JSONField,
    models.BinaryField,
    # A file path in a table cell is noise. An ImageField subclasses this and is
    # deliberately promoted before the check reaches here — it renders as a thumbnail,
    # which is the opposite of noise.
    models.FileField,
)

#: Field classes worth searching.
SEARCHABLE = (
    models.CharField,
    models.TextField,
    models.EmailField,
    models.SlugField,
    models.URLField,
)


def _is_sensitive(field: models.Field) -> bool:
    """Never a column, a filter, a search path or the record's link. See vantage.sensitive.

    Wider than what is masked: a field whose *name* matches is kept out of inferred
    configuration even when its type means it is not masked (``secret_token`` with
    ``choices``). Inference is a guess, and leaving a secret-sounding column out of a
    default filter costs nothing; masking it would hide a value nobody asked to hide.
    """
    from vantage.sensitive import field_is_sensitive, name_is_sensitive

    return field_is_sensitive(field) or name_is_sensitive(field.name)


def _column_score(field: models.Field) -> int | None:
    """Rank a field's worth as a list column. ``None`` means never show it.

    Higher is better. The scale is deliberately coarse and gapped so that inserting a
    new category later does not reshuffle existing results.
    """
    if _is_sensitive(field):
        return None
    if isinstance(field, models.ImageField):
        # Ranked just under the record's own name and above everything else. A
        # thumbnail is the fastest cell on the page to read — the eye resolves a
        # picture before it reads a word — and a catalogue with no pictures in it does
        # not look like a catalogue. This outranks the FileField exclusion below on
        # purpose: see vantage.media, which is what makes the cell an image rather
        # than the storage path a column of these used to print.
        return 90
    if isinstance(field, UNCOLUMNABLE):
        return None
    if field.primary_key:
        return None  # the display column links to the record; the pk is redundant
    if field.name in TRAILING_NAMES:
        return 10  # useful, but last

    if field.name in DISPLAY_NAMES:
        return 100
    if field.choices:
        return 80  # status/state/type — the highest-signal column after the name
    if isinstance(field, models.BooleanField):
        return 70
    if field.is_relation and (field.many_to_one or field.one_to_one):
        return 60
    if isinstance(field, models.SlugField):
        return 20  # usually redundant with the title it was derived from
    if isinstance(field, models.DateTimeField | models.DateField):
        return 30
    if isinstance(field, models.IntegerField | models.DecimalField | models.FloatField):
        return 40
    if isinstance(field, models.EmailField | models.URLField):
        return 50
    if isinstance(field, models.CharField):
        # A long CharField is prose in disguise. It wrecks row height well before it
        # earns its place, so it is demoted and eventually dropped.
        max_length = field.max_length or 0
        if max_length > LONG_TEXT_LENGTH:
            return None
        if max_length > SHORT_TEXT_LENGTH:
            return 15
        return 45
    return 35


def infer_list_link(model: type[models.Model]) -> str:
    """The column whose value opens the record.

    Prefers a conventionally-named label field, then any short text field, and falls
    back to the primary key so that every row is always openable.
    """
    fields = concrete_fields(model)
    by_name = {f.name: f for f in fields}

    for candidate in DISPLAY_NAMES:
        if candidate in by_name and not _is_sensitive(by_name[candidate]):
            return candidate

    for field in fields:
        if (
            isinstance(field, models.CharField)
            and not isinstance(field, models.SlugField)
            and not field.choices
            and not _is_sensitive(field)
        ):
            return field.name

    return model._meta.pk.name


def infer_list_display(model: type[models.Model]) -> list[str]:
    """Choose the columns for the list page.

    The linked column always comes first regardless of score — the eye needs a stable
    anchor at the start of every row — and timestamps always come last.
    """
    link = infer_list_link(model)

    scored: list[tuple[int, int, str]] = []
    for index, field in enumerate(concrete_fields(model)):
        if field.name == link:
            continue
        score = _column_score(field)
        if score is None:
            continue
        # Declaration order breaks ties, so the result follows the shape of the model
        # the developer wrote rather than an arbitrary sort.
        scored.append((-score, index, field.name))

    scored.sort()
    columns = [link] + [name for _, _, name in scored]
    return columns[:MAX_LIST_DISPLAY]


def infer_list_filter(model: type[models.Model]) -> list[str]:
    """Choose filters, preferring the ones that are both cheap and high-signal.

    Choice fields and booleans are cheap to facet and partition the data usefully.
    Foreign keys are included but capped, because their cardinality is unknown until
    query time. At most one date filter: a second rarely earns its width.
    """
    filters: list[str] = []
    dates: list[str] = []
    relations: list[str] = []

    for field in concrete_fields(model):
        if _is_sensitive(field) or field.primary_key:
            continue
        if field.choices:
            filters.append(field.name)
        elif isinstance(field, models.BooleanField):
            filters.append(field.name)
        elif isinstance(field, models.DateTimeField | models.DateField):
            dates.append(field.name)
        elif field.is_relation and (field.many_to_one or field.one_to_one):
            relations.append(field.name)

    # Prefer the record's own date over an audit timestamp when both exist.
    if dates:
        preferred = next((d for d in dates if d not in TRAILING_NAMES), dates[0])
        filters.append(preferred)

    filters.extend(relations)
    return filters[:MAX_LIST_FILTER]


def infer_list_search(model: type[models.Model]) -> list[str]:
    """Choose search fields, and reach one hop into foreign keys.

    Searching an order by its customer's name is what a human means by "search", and
    it is the single most common thing people add by hand to a Django admin.
    """
    local: list[tuple[int, int, str]] = []

    for index, field in enumerate(concrete_fields(model)):
        if _is_sensitive(field) or field.primary_key:
            continue
        if not isinstance(field, SEARCHABLE):
            continue
        if field.choices:
            # Substring-matching a choice field is close to useless: the column stores
            # "draft" while the user types "Draft" or "in review". Choice fields belong
            # in list_filter, and inference puts them there.
            continue
        # Indexed and unique fields are both more useful to search and cheaper to.
        rank = 0 if (field.unique or field.db_index) else 1
        if isinstance(field, models.SlugField):
            rank += 1  # a slug duplicates the title it came from
        if isinstance(field, models.TextField):
            rank += 1  # long bodies are the most expensive thing to scan
        # Declaration order breaks ties, so results follow the model as written rather
        # than falling back to alphabetical, which looks arbitrary to the reader.
        local.append((rank, index, field.name))

    local.sort()
    fields = [name for _, _, name in local]

    # One hop into each FK, targeting that model's own display field.
    for field in concrete_fields(model):
        if not (field.is_relation and (field.many_to_one or field.one_to_one)):
            continue
        related = field.related_model
        if related is None or related is model:
            continue
        target = infer_list_link(related)
        if target != related._meta.pk.name:
            fields.append(f"{field.name}__{target}")

    return fields[:MAX_LIST_SEARCH]


def infer_ordering(model: type[models.Model]) -> list[str]:
    """Choose a stable, useful default order.

    ``Meta.ordering`` is the developer's own statement of intent and always wins. Newest
    first is the right default for anything with a creation timestamp. The fallback is
    descending primary key, which is both stable — pagination requires a total order —
    and a decent proxy for recency.
    """
    if model._meta.ordering:
        return list(model._meta.ordering)

    names = {f.name for f in concrete_fields(model)}
    for candidate in ("created_at", "created", "date_joined", "timestamp", "date"):
        if candidate in names:
            return [f"-{candidate}"]

    return [f"-{model._meta.pk.name}"]


def infer_all(model: type[models.Model]) -> dict[str, Any]:
    """Every inferred attribute at once. Used by ``manage.py vantage_scaffold``."""
    return {
        "list_display": infer_list_display(model),
        "list_link": infer_list_link(model),
        "list_filter": infer_list_filter(model),
        "list_search": infer_list_search(model),
        "ordering": infer_ordering(model),
        "sensitive_fields": infer_sensitive_fields(model),
    }


def infer_sensitive_fields(model: type[models.Model]) -> list[str]:
    """The fields masked by default. Delegates to :mod:`vantage.sensitive`."""
    from vantage.sensitive import infer_sensitive_fields as infer

    return infer(model)
