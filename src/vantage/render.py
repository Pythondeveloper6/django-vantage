"""Turning a field value into the HTML a table cell shows.

Rendering is deliberately separate from inference. Inference decides *which* columns
appear; this decides what each one looks like. Keeping them apart means a project can
change one without disturbing the other.
"""

from __future__ import annotations

from django.db import models
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from django.utils.text import Truncator

from vantage.introspection import resolve_path

__all__ = ["render_cell"]

EMPTY = mark_safe('<span class="v-cell-empty">—</span>')

#: Choice values whose name suggests a state, mapped to a badge tone. Purely
#: cosmetic, and deliberately conservative: a wrong colour is worse than no colour.
TONES = {
    "success": ("published", "active", "approved", "paid", "complete", "completed", "done", "sent"),
    "warning": ("pending", "review", "in_review", "draft", "processing", "waiting", "hold"),
    "destructive": ("failed", "error", "cancelled", "canceled", "refunded", "rejected", "banned"),
}


def _tone_for(value: str) -> str | None:
    lowered = str(value).lower()
    for tone, values in TONES.items():
        if lowered in values:
            return tone
    return None


def render_cell(panel, obj, name: str) -> SafeString | str:
    """Render one cell of ``obj`` for the column ``name``."""
    # A panel method wins, so a project can always override rendering in Python.
    method = getattr(panel, name, None)
    if callable(method):
        return method(obj)

    hops = resolve_path(panel.model, name)
    if not hops:
        return _plain(getattr(obj, name, None))

    value = obj
    for hop in hops:
        if value is None:
            return EMPTY
        value = getattr(value, hop.name, None)

    field = hops[-1].field
    return _render_value(value, field, obj, hops[0])


def _render_value(value, field, obj, first_hop):
    if value is None or value == "":
        return EMPTY

    # A relation rendered as a reference chip.
    if getattr(first_hop, "is_relation", False) and len(str(value)) and field is first_hop.field:
        return format_html('<span class="v-badge">{}</span>', str(value))

    if isinstance(field, models.BooleanField):
        return format_html(
            '<span class="v-cell-bool" data-value="{}">{}</span>',
            "true" if value else "false",
            "Yes" if value else "No",
        )

    if getattr(field, "choices", None):
        label = dict(field.flatchoices).get(value, value)
        tone = _tone_for(value)
        if tone:
            return format_html('<span class="v-badge" data-tone="{}">{}</span>', tone, label)
        return format_html('<span class="v-badge">{}</span>', label)

    if isinstance(field, models.URLField):
        return format_html('<a href="{}" rel="noopener" target="_blank">{}</a>', value, value)

    if isinstance(field, models.EmailField):
        return format_html('<a href="mailto:{}">{}</a>', value, value)

    if isinstance(field, models.DateTimeField):
        return format_html(
            '<span title="{}">{}</span>',
            value.isoformat(),
            value.strftime("%d %b %Y, %H:%M"),
        )

    if isinstance(field, models.DateField):
        return value.strftime("%d %b %Y")

    return _plain(value)


def _plain(value):
    if value is None or value == "":
        return EMPTY
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return Truncator(str(value)).chars(80)
