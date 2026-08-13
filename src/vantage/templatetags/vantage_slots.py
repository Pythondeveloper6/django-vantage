"""The ``{% slot %}`` tag.

A separate library from ``vantage`` so that a project overriding a shipped template
loads only what it uses, and so the extension surface has its own front door::

    {% load vantage_slots %}
    {% slot "before_list" %}

Anything a slot renders is already escaped or already marked safe by
:mod:`vantage.slots`, which is why the tag returns a ``SafeString`` — the escaping
decision belongs with the contribution, not with the template that hosts it.
"""

from __future__ import annotations

from django import template

from vantage import slots

register = template.Library()


@register.simple_tag(takes_context=True, name="slot")
def slot_tag(context, name: str, **extra):
    """``{% slot "row_actions" object=row.obj %}``.

    Keyword arguments are merged over the page context for this slot only, so a
    per-row fragment can be handed its row without the view knowing slots exist.
    """
    return slots.render_slot(name, context, **extra)


@register.simple_tag(takes_context=True, name="slot_filled")
def slot_filled_tag(context, name: str) -> bool:
    """``{% slot_filled "row_actions" as has_actions %}``.

    For the wrapper markup a slot needs when it has content and must not emit when it
    does not — an empty trailing table column on every row is worse than no column.
    Answered without rendering, so a guarded slot costs one permission check rather
    than a second pass over every contribution.
    """
    return slots.is_filled(name, context)
