"""JSON shapes for everything the panel already knows how to render.

This module is to :mod:`vantage.api` what :mod:`vantage.widgets` is to
:mod:`vantage.formviews`: pure data in, pure data out. No request, no permissions, no
database policy — those belong to the view. Keeping the split means every shape below
can be asserted in a table test, and it means the HTML and the JSON views describe the
same record by calling the same functions rather than by agreeing to.

Three rules shape it.

**Every value goes out twice: raw and rendered.** ``{"value": "paid", "display":
"Paid"}``. The raw value is what a client hands back on a write; the display value is
what a person reads, and it is produced by :func:`vantage.render.render_cell` — the
same function the table cell uses — with its markup stripped. A JSON API that returns
only the raw value forces every client to re-implement choice labels, money formatting
and date formats, and they will all do it differently to the panel.

**A serialiser never widens what a page shows.** A sensitive field (see
:mod:`vantage.sensitive`) goes out as ``{"value": null, "display": "••••••••",
"masked": true}`` — the same mask the HTML page draws, decided by the same function, so
the JSON API cannot become the way round it. Revealing one value is a separate,
audited ``POST``; nothing in a ``GET`` response ever carries it.

**Nothing here decides which rows or which fields.** Callers pass the columns, the
fields and the objects. That is what stops this module from becoming a second, subtly
different list view.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html import unescape
from typing import Any
from uuid import UUID

from django.db import models
from django.db.models.fields.files import FieldFile
from django.utils.duration import duration_iso_string
from django.utils.functional import Promise
from django.utils.html import strip_tags
from django.utils.text import capfirst

from vantage import concurrency
from vantage.introspection import displayable_fields, m2m_fields, resolve_path
from vantage.render import render_cell
from vantage.sensitive import safe_str
from vantage.widgets import WidgetSpec, classify, infer_widget

__all__ = [
    "cell",
    "choices_payload",
    "display",
    "field_schema",
    "filter_payload",
    "json_value",
    "object_fields",
    "raw",
    "record_fields",
    "row",
    "summary_payload",
    "widget_payload",
]


# ── values ──────────────────────────────────────────────────────────────────


def json_value(value: Any) -> Any:
    """A JSON-safe stand-in for any value a model field can hold.

    ``Decimal`` becomes a string rather than a float, deliberately. A price is the
    canonical case, and ``json.dumps(Decimal("0.10"))`` through ``float`` is
    ``0.1000000000000000055511151231257827``; every serious payments API sends money as
    a string for exactly this reason. Clients that want arithmetic can parse it; clients
    that want to round-trip it get back what the database holds.

    Durations use ISO-8601 rather than seconds, because that is what Django's own
    ``parse_duration`` accepts on the way back in.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return duration_iso_string(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Promise):  # a lazy translation
        return str(value)
    if isinstance(value, FieldFile):
        return _file_value(value)
    if isinstance(value, models.Model):
        return json_value(value.pk)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_value(item) for item in value]
    return str(value)


def _file_value(value: FieldFile) -> dict[str, Any] | None:
    """A file as ``{"name", "url"}``, or ``None`` when the field is empty.

    ``.url`` raises when the storage backend cannot produce one, which is a
    configuration fact about the deployment and not something an API response should
    500 over.
    """
    if not value:
        return None
    try:
        url = value.url
    except (ValueError, NotImplementedError):  # pragma: no cover - storage dependent
        url = None
    return {"name": value.name, "url": url}


def display(panel, obj, name: str) -> str:
    """What a person reads for one column, from the renderer the table cell uses.

    The markup is stripped rather than sent: a JSON client that wanted
    ``<span class="v-badge">Paid</span>`` would be building HTML from a data API, and a
    client that did not want it would have to strip it anyway. Entities are unescaped
    afterwards because ``render_cell`` escapes for HTML, and ``&amp;`` is not the
    customer's name.
    """
    return unescape(strip_tags(str(render_cell(panel, obj, name)))).strip()


def raw(panel, obj, name: str) -> Any:
    """The stored value behind a column, JSON-safe.

    A relation resolves to its primary key and a multi-valued path to a list of them,
    so a client can filter or write with what it reads. A column backed by a panel
    method has no stored value at all and answers ``None`` — its
    :func:`display` is the whole of it.
    """
    if callable(getattr(panel, name, None)):
        return None

    hops = resolve_path(panel.model, name)
    if not hops:
        return json_value(getattr(obj, name, None))

    value: Any = obj
    for hop in hops:
        if value is None:
            return None
        if hop.is_multivalued:
            manager = getattr(value, hop.name, None)
            if manager is None:
                return None
            return [json_value(item.pk) for item in manager.all()]
        value = getattr(value, hop.name, None)
    return json_value(value)


def cell(panel, obj, name: str) -> dict[str, Any]:
    """One column of one row: what is stored, and what it reads as.

    A sensitive column has no ``value`` at all — ``null``, never the stored one — and
    its ``display`` is the mask. ``masked`` tells a client the null is a refusal rather
    than an empty field.
    """
    from vantage.sensitive import is_sensitive_path

    if is_sensitive_path(panel, name):
        return {"value": None, "display": display(panel, obj, name), "masked": True}
    return {"value": raw(panel, obj, name), "display": display(panel, obj, name)}


# ── rows and records ────────────────────────────────────────────────────────


def row(
    panel,
    obj,
    columns: list[str],
    *,
    url: str | None = None,
    html_url: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """One list row.

    ``version`` is the optimistic-lock token, and it is on every row rather than only on
    the detail response on purpose: a client that edits from a list has already read the
    row it is about to write, and making it fetch the record again to learn the token
    would be a round trip whose only purpose is ceremony.
    """
    payload: dict[str, Any] = {
        "pk": json_value(obj.pk),
        "display": safe_str(obj, getattr(panel, "site", None)),
        "url": url,
        "html_url": html_url,
        "version": concurrency.token_for(obj),
        "cells": {name: cell(panel, obj, name) for name in columns},
    }
    if depth is not None:
        payload["depth"] = depth
    return payload


def record_fields(model: type[models.Model]) -> list[Any]:
    """The fields a record's JSON exposes, in a fixed order.

    :func:`~vantage.introspection.displayable_fields` — the same list the HTML detail
    page shows, sensitive fields included because they go out masked — plus
    many-to-many relations, which the detail page shows as related blocks and an API
    client needs as a list of ids because that is what it has to send back to change
    them.

    Separate from :func:`object_fields` because it is also the *allowlist*: it is the
    answer to "which field names may a caller ask for?", and having one function answer
    both questions is what stops a ``?fields=`` parameter from reaching a column the
    detail page would not show. Asking for a sensitive field is allowed and answers the
    mask; sorting by one is refused by :func:`vantage.listquery.resolve_sort`.
    """
    return [*displayable_fields(model), *m2m_fields(model)]


def object_fields(panel, obj) -> dict[str, dict[str, Any]]:
    """Every field of one record, keyed by name.

    Forward relations are read through ``attname`` (``author_id``), so a record with
    twelve foreign keys costs no queries at all. A many-to-many costs one each, which is
    why the caller is expected to have prefetched them.
    """
    fields: dict[str, dict[str, Any]] = {}
    secret = panel.get_sensitive_fields()

    for field in record_fields(panel.model):
        if field.name in secret:
            fields[field.name] = {
                "label": _label(field),
                "kind": str(classify(field)),
                "value": None,
                "display": display(panel, obj, field.name),
                "masked": True,
            }
            continue
        if field.many_to_many:
            related = list(getattr(obj, field.name).all())
            value: Any = [json_value(item.pk) for item in related]
            rendered = ", ".join(safe_str(item) for item in related)
        else:
            value = json_value(getattr(obj, field.attname, None))
            rendered = display(panel, obj, field.name)
        fields[field.name] = {
            "label": _label(field),
            "kind": str(classify(field)),
            "value": value,
            "display": rendered,
        }

    return fields


def _label(field: Any) -> str:
    # ``capfirst`` rather than ``str.capitalize``: a verbose name is a translated
    # string, and ``capitalize`` lowercases everything after the first character —
    # which turns "E-Mail Adresse" into "E-mail adresse" and "URL" into "Url".
    verbose = getattr(field, "verbose_name", None) or getattr(field, "name", "")
    return capfirst(str(verbose).replace("_", " "))


# ── schema ──────────────────────────────────────────────────────────────────


def choices_payload(choices: Any) -> list[dict[str, Any]] | None:
    """Declared choices, keeping optgroups as nested groups rather than flattening.

    Flattening would lose the grouping a client needs to draw the same ``<select>`` the
    panel draws, and there is no way to recover it afterwards.
    """
    if not choices:
        return None
    payload: list[dict[str, Any]] = []
    for value, label in choices:
        if isinstance(label, list | tuple):
            payload.append(
                {
                    "group": str(value),
                    "choices": [
                        {"value": json_value(inner), "label": str(text)} for inner, text in label
                    ],
                }
            )
        else:
            payload.append({"value": json_value(value), "label": str(label)})
    return payload


def widget_payload(spec: WidgetSpec, *, options_url: str | None = None) -> dict[str, Any]:
    """A :class:`~vantage.widgets.WidgetSpec` as JSON.

    The spec is already a serialisable description of a control — that is what it was
    built to be — so this is a transcription rather than a translation. The one addition
    is ``options_url``: a relation that ``needs_search`` cannot ship its options inline
    without the outage the threshold exists to prevent, so it ships the endpoint that
    answers them instead.
    """
    related = spec.related_model
    return {
        "kind": str(spec.kind),
        "input_type": spec.input_type,
        "attrs": dict(spec.attrs),
        "multiple": spec.multiple,
        "needs_search": spec.needs_search,
        "nullable": spec.nullable,
        "placeholder": spec.placeholder,
        "choices": choices_payload(spec.choices),
        "related_model": related._meta.label_lower if related is not None else None,
        "options_url": options_url,
    }


def field_schema(
    model: type[models.Model],
    name: str,
    *,
    form_field: Any = None,
    readonly: bool = False,
    options_url: str | None = None,
) -> dict[str, Any]:
    """One field, described well enough for a generic client to render and validate it.

    ``required`` and ``label`` come from the bound form field when there is one, because
    that is what will actually reject the write — ``blank=True`` on the model is only
    the input to that decision, and a panel is free to override it. Everything else
    comes from :func:`~vantage.widgets.infer_widget`, so the control a client builds is
    the control the HTML form builds.
    """
    hops = resolve_path(model, name)
    model_field = hops[-1].field if hops else None
    spec = infer_widget(model_field) if model_field is not None else None

    label = getattr(form_field, "label", None) or (spec.label if spec else name)
    help_text = getattr(form_field, "help_text", "") or (spec.help_text if spec else "")

    payload: dict[str, Any] = {
        "name": name,
        "label": capfirst(str(label)) if label else name,
        "kind": str(classify(model_field)) if model_field is not None else "unknown",
        "readonly": readonly,
        # A readonly field is not in the form at all — that is the security boundary
        # formviews.py describes — so it can never be required of a client.
        "required": bool(getattr(form_field, "required", False)) and not readonly,
        "help_text": str(help_text),
        "widget": widget_payload(spec, options_url=options_url)
        if spec is not None
        else {"kind": "unknown"},
    }
    if form_field is not None and not callable(form_field.initial):
        payload["initial"] = json_value(form_field.initial)
    return payload


# ── list-page furniture ─────────────────────────────────────────────────────


def filter_payload(filt, *, with_choices: bool = False) -> dict[str, Any]:
    """One filter's state, and its options only when they were asked for.

    Options are opt-in because computing them is a query for a relation filter and a
    disjunctive aggregate for a counted one. A client that only wants to know what is
    currently applied — the common case, and what makes a URL transferable — pays
    nothing for the options it is not going to draw.
    """
    payload: dict[str, Any] = {
        "path": filt.path,
        "parameter": filt.parameter_name,
        "parameters": list(filt.expected_parameters()),
        "label": filt.label,
        "type": type(filt).__name__.removesuffix("Filter").lower(),
        "multi": filt.multi,
        "values": list(filt.values),
        "active": filt.is_active,
    }
    if with_choices:
        payload["choices"] = [
            {
                "value": choice.value,
                "label": choice.label,
                "count": choice.count,
                "selected": choice.selected,
            }
            for choice in filt.choices()
        ]
    return payload


def summary_payload(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The footer bar, keyed by column, as JSON."""
    return {
        column: {
            "function": entry.function,
            "value": json_value(entry.value),
            "display": entry.display,
        }
        for column, entry in summary.items()
    }
