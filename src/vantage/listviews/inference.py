"""Which alternative views a model can support, read from its fields alone.

The same contract as :mod:`vantage.inference`: model in, answer out, no request, no
database, no settings. Every function here is one attribute's default, so a panel that
sets ``kanban_field`` overrides that answer and nothing else — ``calendar_field`` stays
inferred, and so does the list of views itself.

The rules are deliberately conservative, because an inferred view is a button on every
list page that has one. A Kanban board over a field with forty choices is not a board,
it is a horizontal scroll bar; a calendar over ``updated_at`` plots when somebody last
touched a record, which is noise arranged in a grid. Each refusal below exists because
the view it would have produced is worse than not offering one.
"""

from __future__ import annotations

from django.db import models

from vantage.inference import SENSITIVE, infer_list_display, infer_list_link
from vantage.introspection import concrete_fields, resolve_path

__all__ = [
    "KANBAN_MAX_COLUMNS",
    "MAX_CARD_FIELDS",
    "calendar_candidates",
    "infer_calendar_field",
    "infer_card_fields",
    "infer_gallery_field",
    "infer_kanban_field",
    "infer_list_views",
    "infer_map_fields",
    "is_point_field",
    "kanban_candidates",
    "kanban_field_ok",
]

#: A board with more columns than this stops being something you can take in at a
#: glance. A ``country`` field with two hundred choices is a filter, not a workflow.
KANBAN_MAX_COLUMNS = 12

#: Names that mean "where this record is in a process", in preference order. A model
#: with both ``status`` and ``category`` choices gets a board over ``status``.
KANBAN_NAMES = ("status", "state", "stage", "phase", "step", "progress", "priority")

#: Booleans that read as a stage something moves through. Only these become a board:
#: a two-column board over ``is_superuser`` is a privilege toggle you can drag, and one
#: over ``is_deleted`` is the trash wearing a disguise. Neither is a workflow.
KANBAN_BOOLEAN_TOKENS = (
    "published",
    "approved",
    "active",
    "done",
    "complete",
    "completed",
    "resolved",
    "featured",
    "public",
    "live",
    "visible",
    "enabled",
    "paid",
    "shipped",
    "verified",
)

#: Date names in preference order: when something *happens* beats when it was written
#: down. ``created`` is last because it is on nearly every model and says the least.
CALENDAR_TOKENS = (
    "start",
    "starts",
    "begins",
    "date",
    "day",
    "scheduled",
    "due",
    "event",
    "deadline",
    "published",
    "publish",
)

#: Audit timestamps: a calendar of when rows were written. Valid, but the last resort —
#: any other date on the model says more about the record than its insertion time.
CALENDAR_AUDIT_TOKENS = ("created", "joined", "added", "timestamp")

#: Date fields that are never a calendar's anchor. A soft-delete stamp or a "last
#: modified" column plots bookkeeping, not events.
CALENDAR_REFUSED_TOKENS = ("deleted", "archived", "updated", "modified", "last", "expires")

#: File fields worth a picture even though they are not ``ImageField`` — a project
#: that stores images through a plain ``FileField`` usually names it like one.
GALLERY_TOKENS = ("image", "photo", "picture", "cover", "thumbnail", "avatar", "logo", "banner")

LATITUDE_NAMES = ("latitude", "lat")
LONGITUDE_NAMES = ("longitude", "lng", "lon", "long")

#: Cards show the title and then this many fields. More turns a card into a row.
MAX_CARD_FIELDS = 3


def _sensitive(field: models.Field) -> bool:
    return bool(SENSITIVE.search(field.name))


def _tokens(name: str) -> list[str]:
    return name.lower().split("_")


# ── kanban ──────────────────────────────────────────────────────────────────


def kanban_field_ok(field: models.Field) -> bool:
    """Whether ``field`` can be a board: a short list of choices, or a boolean.

    Public because :mod:`vantage.panel_checks` asks the same question of a declared
    ``kanban_field`` — a rule restated in two places is two rules.
    """
    if field.primary_key or field.is_relation:
        return False
    if isinstance(field, models.BooleanField):
        return True
    choices = getattr(field, "flatchoices", None) or ()
    return 2 <= len(choices) <= KANBAN_MAX_COLUMNS


def kanban_candidates(model: type[models.Model]) -> list[str]:
    """Every field a board could be grouped by, best first.

    Status-like choices fields (:data:`KANBAN_NAMES` order), then any other short
    choices field, then booleans named like a stage. The first is the inferred
    ``kanban_field``; the rest are what the board's "Group by" picker offers.
    """
    candidates = [
        field
        for field in concrete_fields(model)
        if getattr(field, "editable", False) and not _sensitive(field) and kanban_field_ok(field)
    ]
    with_choices = [field for field in candidates if getattr(field, "flatchoices", None)]
    ranked: list[str] = []
    for wanted in KANBAN_NAMES:
        for field in with_choices:
            if (field.name == wanted or wanted in _tokens(field.name)) and field.name not in ranked:
                ranked.append(field.name)
    ranked.extend(field.name for field in with_choices if field.name not in ranked)
    ranked.extend(
        field.name
        for field in candidates
        if isinstance(field, models.BooleanField)
        and any(token in KANBAN_BOOLEAN_TOKENS for token in _tokens(field.name))
    )
    return ranked


def infer_kanban_field(model: type[models.Model]) -> str | None:
    """A status-like choices field, else any short choices field, else a boolean.

    Booleans come last on purpose, and only the ones named like a stage (see
    :data:`KANBAN_BOOLEAN_TOKENS`): a two-column board is a weak view, offered only
    when nothing that looks more like a workflow exists.
    """
    candidates = kanban_candidates(model)
    return candidates[0] if candidates else None


# ── calendar ────────────────────────────────────────────────────────────────


def _date_candidates(model: type[models.Model]) -> list[models.Field]:
    found = []
    for field in concrete_fields(model):
        if not isinstance(field, models.DateField):  # DateTimeField subclasses it
            continue
        if _sensitive(field) or getattr(field, "auto_now", False):
            continue
        if any(token in CALENDAR_REFUSED_TOKENS for token in _tokens(field.name)):
            continue
        found.append(field)
    return found


def calendar_candidates(model: type[models.Model]) -> list[str]:
    """Every date a calendar could be placed by, best first.

    By name first (``start``, ``date``, ``scheduled``, ``due``, ``published`` …), then
    any other date, and audit stamps like ``created_at`` last. The first is the
    inferred ``calendar_field``; the rest are what the "Dates from" picker offers.
    """
    candidates = _date_candidates(model)
    ranked: list[str] = []
    for wanted in CALENDAR_TOKENS:
        for field in candidates:
            if wanted in _tokens(field.name) and field.name not in ranked:
                ranked.append(field.name)
    ranked.extend(
        field.name
        for field in candidates
        if field.name not in ranked
        and not any(token in CALENDAR_AUDIT_TOKENS for token in _tokens(field.name))
    )
    ranked.extend(field.name for field in candidates if field.name not in ranked)
    return ranked


def infer_calendar_field(model: type[models.Model]) -> str | None:
    """The date that best says *when this happens*: the first of the candidates.

    An audit stamp like ``created_at`` — on nearly every model — is chosen only when
    there is nothing else, and is still a fair calendar of activity when it is all
    there is.
    """
    candidates = calendar_candidates(model)
    return candidates[0] if candidates else None


# ── gallery ─────────────────────────────────────────────────────────────────


def infer_gallery_field(model: type[models.Model]) -> str | None:
    """An ``ImageField``, else a ``FileField`` named like a picture."""
    files = [
        field
        for field in concrete_fields(model)
        if isinstance(field, models.FileField) and not _sensitive(field)
    ]
    for field in files:
        if isinstance(field, models.ImageField):
            return field.name
    for field in files:
        if any(token in GALLERY_TOKENS for token in _tokens(field.name)):
            return field.name
    return None


# ── map ─────────────────────────────────────────────────────────────────────


def is_point_field(field: models.Field) -> bool:
    """A GeoDjango ``PointField``, recognised without importing GeoDjango.

    Importing ``django.contrib.gis`` needs GDAL on the machine, and a package that
    merely *asks* whether a field is a point must not require a C library to answer.
    """
    return type(field).__name__ == "PointField" and "gis" in type(field).__module__


_COORDINATE = (models.FloatField, models.DecimalField)


def infer_map_fields(model: type[models.Model]) -> str | tuple[str, str] | None:
    """A ``PointField`` name, or a ``(latitude, longitude)`` pair of number fields.

    A pair must share a prefix — ``home_lat`` with ``home_lng``, never ``home_lat``
    with ``work_lng`` — so a model with two locations gets the first consistent one
    rather than a plot of one coordinate from each.
    """
    fields = concrete_fields(model)
    for field in fields:
        if is_point_field(field):
            return field.name

    numbers = {f.name: f for f in fields if isinstance(f, _COORDINATE) and not f.primary_key}
    for name in numbers:
        for lat in LATITUDE_NAMES:
            if name != lat and not name.endswith(f"_{lat}"):
                continue
            prefix = name[: -len(lat)]
            for lng in LONGITUDE_NAMES:
                partner = f"{prefix}{lng}"
                if partner in numbers:
                    return (name, partner)
    return None


# ── cards ───────────────────────────────────────────────────────────────────


def infer_card_fields(
    model: type[models.Model],
    columns: list[str] | None = None,
    exclude: tuple[str, ...] = (),
    link: str | None = None,
) -> list[str]:
    """Two or three fields a card shows under its title.

    Drawn from the list columns, which are already the fields inference judged worth
    reading at a glance, minus the title (the card says it already) and minus the
    field the view itself is arranged by (the column header or the date says it). A
    multi-valued path is dropped: a card cannot afford a prefetch per field.
    """
    columns = list(columns) if columns is not None else infer_list_display(model)
    link = link or infer_list_link(model)
    chosen: list[str] = []
    for name in columns:
        if name == link or name in exclude:
            continue
        hops = resolve_path(model, name)
        if hops and any(hop.is_multivalued for hop in hops):
            continue
        if hops and isinstance(hops[-1].field, models.FileField):
            continue
        chosen.append(name)
        if len(chosen) == MAX_CARD_FIELDS:
            break
    return chosen


# ── which views ─────────────────────────────────────────────────────────────


def infer_list_views(model: type[models.Model]) -> list[str]:
    """The table first, then every view the model's fields can support."""
    views = ["table"]
    if infer_kanban_field(model):
        views.append("kanban")
    if infer_calendar_field(model):
        views.append("calendar")
    if infer_gallery_field(model):
        views.append("gallery")
    if infer_map_fields(model):
        views.append("map")
    return views
