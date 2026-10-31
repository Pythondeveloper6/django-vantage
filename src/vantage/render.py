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

__all__ = ["is_empty", "render_cell", "table_cell"]

EMPTY = mark_safe('<span class="v-cell-empty">—</span>')


def is_empty(value) -> bool:
    """Whether a rendered cell is the empty placeholder.

    Identity, not equality: a value that merely *contains* a dash is content.
    """
    return value is EMPTY


def table_cell(value, is_link: bool) -> dict:
    """One table cell, as the list, related-block and widget templates read it."""
    return {"value": value, "is_link": is_link, "is_empty": is_empty(value)}


#: Choice values whose name suggests a state, mapped to a badge tone. Purely
#: cosmetic, and deliberately conservative: a wrong colour is worse than no colour.
TONES = {
    "success": ("published", "active", "approved", "paid", "complete", "completed", "done", "sent"),
    "warning": ("pending", "review", "in_review", "draft", "processing", "waiting", "hold"),
    "destructive": ("failed", "error", "cancelled", "canceled", "refunded", "rejected", "banned"),
}


# Schemes a URL column may link to. An allow-list, not a deny-list of ``javascript:``:
# browsers also execute ``vbscript:`` and render ``data:`` documents, and the next
# executable scheme should not need a release to be refused.
LINKABLE_SCHEMES = frozenset({"http", "https", "ftp", "ftps"})


def is_linkable_url(value) -> bool:
    """Whether a stored URL is safe to put in an ``href``.

    ``URLField`` validates its scheme only in a form. A row written by a script, an
    import, a data migration or another service on the same table never passed through
    one, so the stored value is whatever was put there. Browsers strip ASCII whitespace
    and control characters out of a scheme before reading it (``java\tscript:`` runs),
    so the check does the same before comparing.
    """
    cleaned = "".join(ch for ch in str(value) if ord(ch) > 0x20 and ord(ch) != 0x7F)
    scheme, sep, _ = cleaned.partition(":")
    return bool(sep) and scheme.lower() in LINKABLE_SCHEMES


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

    # Before anything formats the value: a masked cell must not depend on a renderer
    # remembering to be careful. See vantage.sensitive.
    from vantage.sensitive import is_sensitive_path, masked_html

    sensitive = is_sensitive_path(panel, name)

    if not hops:
        value = getattr(obj, name, None)
        if sensitive:
            return masked_html(None, obj, name, value)
        return _plain(value)

    value = obj
    for hop in hops:
        if value is None:
            return EMPTY
        value = getattr(value, hop.name, None)

    field = hops[-1].field

    if sensitive:
        # Only a local column gets a reveal control: revealing customer__api_token is a
        # question for the customer's own panel and its own permission answer.
        label = str(getattr(field, "verbose_name", name))
        return masked_html(panel if len(hops) == 1 else None, obj, name, value, label)

    # Currency before the generic numeric path: a bare Decimal reads as data, and a
    # column of unaligned unformatted numbers is the thing people complain about
    # first. The symbol is deliberately not guessed - only the shape is fixed.
    if len(hops) == 1 and name in _money_fields(panel) and value is not None:
        places = _money_fields(panel)[name]
        return format_html('<span class="v-money">{}</span>', f"{value:,.{places}f}")

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
        from vantage.sensitive import safe_str

        return format_html('<span class="v-badge">{}</span>', safe_str(value))

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
        if not is_linkable_url(value):
            # Shown, never linked: see is_linkable_url.
            return format_html('<span class="v-cell-url">{}</span>', value)
        return format_html(
            '<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', value, value
        )

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
