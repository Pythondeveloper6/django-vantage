"""The contract every alternative list view implements, and the card they share.

A view mode is a *presentation* of the rows a list request means, never a second
answer to which rows those are. :func:`vantage.listquery.build` has already applied
permissions, tenancy, soft-delete, search and filters by the time a mode sees the
queryset, and a mode may only narrow it further — to a month, to the first twenty cards
of a column, to the rows that have coordinates. That is the property that makes every
view honour the filter rail, the search box and a saved view without knowing any of
them exist, and it is why :meth:`ListMode.context` takes a queryset rather than a
request it could rebuild one from.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import QuerySet
from django.urls import NoReverseMatch, reverse

from vantage.optimizer import plan_related

__all__ = ["Card", "ListMode", "card_for", "detail_url", "with_related"]


class ListMode:
    """One way of looking at a list. Subclass, set the class attributes, register.

    Instances are cheap and per request: they hold the panel and nothing else, so a
    long-lived panel never carries one request's state into the next.
    """

    #: The value of ``?view=``. Lower-case, never all digits — a digit string is a
    #: saved view's primary key on the same parameter (see :mod:`vantage.listviews`).
    name: ClassVar[str] = ""
    #: The switcher's label. Lazily translated.
    label: ClassVar[Any] = ""
    #: An icon name from the package's sprite.
    icon: ClassVar[str] = "box"
    template: ClassVar[str] = ""

    def __init__(self, panel) -> None:
        self.panel = panel

    @property
    def is_table(self) -> bool:
        return False

    @classmethod
    def supports(cls, panel) -> bool:
        """Whether this model has what the view needs. Pure: no request, no queries."""
        raise NotImplementedError

    def context(self, request, queryset: QuerySet, *, permissions) -> dict[str, Any]:
        """Template context for the rows ``queryset`` names. Must stay bounded."""
        raise NotImplementedError


class Card(dict):
    """One record as a card: ``obj``, ``pk``, ``url``, ``title`` and ``fields``.

    A dict subclass so templates read it with plain attribute lookups, and so a mode
    can add its own keys (a version token, a column) without a class per mode.
    """


def detail_url(panel, obj) -> str | None:
    try:
        return reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
    except NoReverseMatch:  # pragma: no cover - a panel always has a detail route
        return None


def card_for(panel, obj, fields: list[str], labels: dict[str, str]) -> Card:
    """Build one card. ``labels`` is resolved once per page, not once per card."""
    from vantage.render import is_empty, render_cell

    rendered = []
    for name in fields:
        value = render_cell(panel, obj, name)
        if is_empty(value):
            continue
        rendered.append({"name": name, "label": labels.get(name, name), "value": value})
    return Card(
        obj=obj,
        pk=obj.pk,
        url=detail_url(panel, obj),
        title=str(obj),
        fields=rendered,
    )


def field_labels(panel, names: list[str]) -> dict[str, str]:
    """Column labels, exactly as the table header spells them."""
    from vantage.introspection import resolve_path

    labels = {}
    for name in names:
        hops = resolve_path(panel.model, name)
        label = str(getattr(hops[-1].field, "verbose_name", name)) if hops else name
        labels[name] = label.replace("_", " ").capitalize()
    return labels


def with_related(queryset: QuerySet, model, paths: list[str]) -> QuerySet:
    """Join what the cards render, derived rather than declared.

    The list queryset already joins the table's columns; a card shows a different
    subset, and a relation a card reads that the table did not would otherwise be a
    query per card — the exact regression ``CONTRIBUTING`` rule four forbids.
    """
    select, prefetch = plan_related(model, paths)
    if select:
        queryset = queryset.select_related(*sorted(select))
    if prefetch:
        queryset = queryset.prefetch_related(*sorted(prefetch))
    return queryset
