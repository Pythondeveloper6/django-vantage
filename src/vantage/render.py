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
from django.utils.translation import gettext_lazy as _

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


def _money_fields(panel) -> dict[str, int]:
    """Column name -> decimal places, for the columns detected as currency."""
    cached = getattr(panel, "_money", None)
    if cached is None:
        cached = {m.name: m.decimal_places for m in panel.get_patterns().money}
        panel._money = cached
    return cached


def render_cell(panel, obj, name: str, *, size: str = "sm") -> SafeString | str:
    """Render one cell of ``obj`` for the column ``name``.

    ``size`` is the thumbnail box a file column uses, and is the one thing about a cell
    that depends on where the cell is: a 40-pixel thumbnail is right in a table row and
    is not a detail page's idea of showing you the picture. It is keyword-only and
    defaults to the table size, so every existing caller keeps its behaviour.
    """
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

    # Currency before the generic numeric path: a bare Decimal reads as data, and a
    # column of unaligned unformatted numbers is the thing people complain about
    # first. The symbol is deliberately not guessed - only the shape is fixed.
    if len(hops) == 1 and name in _money_fields(panel) and value is not None:
        places = _money_fields(panel)[name]
        return format_html(
            '<span class="v-money">{}</span>', f"{value:,.{places}f}"
        )

    # Before the generic path, because the generic path would print the storage key.
    # An empty file is handled here too: a FieldFile with no name is falsy, and letting
    # it reach the renderer would draw a chip with no name on it.
    if isinstance(field, models.FileField):
        if not value:
            return EMPTY
        from vantage.media import render_file

        return render_file(value, obj=obj, size=size)

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
            _("Yes") if value else _("No"),
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
        return _("Yes") if value else _("No")
    return Truncator(str(value)).chars(80)
