"""Template access to the inline-editing engine.

A separate tag library rather than extra keys in the list view's context, for one
reason: :class:`~vantage.views.ListView` builds a row as ``{"value": …, "is_link": …}``
and knows nothing about editing. Teaching it would put the allowlist — the security
boundary — into a view that is also responsible for pagination, sorting, exports and
tree arrangement. Here it stays next to :mod:`vantage.inline`, which is the only place
that decides what may be written.

Nothing in this module makes a decision. Every one of them is
:func:`vantage.inline.inline_fields`; these three tags only ask.
"""

from __future__ import annotations

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from vantage import concurrency, inline

register = template.Library()


@register.simple_tag(takes_context=True, name="inline_config")
def inline_config(context, panel):
    """``{% inline_config panel as inline %}`` — resolved once, then asked repeatedly."""
    return inline.get_config(panel, context.get("request"))


@register.simple_tag(name="inline_row_attrs")
def inline_row_attrs(config, obj) -> str:
    """The row's identity and its version token, for the ``<tr>``.

    The token is rendered into the page rather than fetched later because it has to be
    the version the user was *looking at* when they decided to change something. A
    token fetched at save time would agree with the database by construction and the
    lock would silently never fire.
    """
    if not config.enabled:
        return ""
    token = concurrency.token_for(obj)
    if token is None:
        # No version column on this model. The attribute is omitted rather than
        # emitted empty, so "unlockable" and "not yet loaded" cannot be confused.
        return mark_safe(" data-inline-row")
    return format_html(' data-inline-row data-version="{}"', token)


@register.simple_tag(takes_context=True, name="inline_cell")
def inline_cell(context, config, obj, columns, index):
    """One editable cell, or ``""`` when this column is not one.

    Takes the column list and an index rather than a name because the list template
    walks ``row.cells`` and Django templates cannot subscript a list with a variable.
    Returning empty for a non-editable column lets the caller fall through to the
    ordinary rendered value with a plain ``{% if %}``.
    """
    if not config.enabled:
        return ""
    try:
        name = columns[index]["name"]
    except (IndexError, KeyError, TypeError):  # pragma: no cover - defensive
        return ""

    field = config.field(name)
    if field is None:
        return ""

    form = inline.bound_form(config, obj)
    if name not in form.fields:  # pragma: no cover - the allowlist already ensures this
        return ""

    control = form[name].as_widget(attrs=inline.control_attrs(field, obj))
    return template.loader.render_to_string(
        "vantage/partials/inline_cell.html",
        {
            "field": field,
            "control": control,
            "pk": obj.pk,
            "error_id": inline.error_id(obj, field),
        },
        request=context.get("request"),
    )
