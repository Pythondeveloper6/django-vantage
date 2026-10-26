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

    #: The query parameter a mode arranged by a field reads its choice from. Shared by
    #: every such mode because only one renders at a time, and dropped on a switch.
    FIELD_PARAM: ClassVar[str] = "by"

    def __init__(self, panel, request=None, *, field: str | None = None) -> None:
        self.panel = panel
        self.request = request
        #: An explicit choice (the Kanban move posts the field it was drawn with).
        #: Still checked against :meth:`field_choices`, never trusted as given.
        self._wanted_field = field
        self._field_name: str | bool | None = False  # False: not resolved yet

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

    # ── the field a mode is arranged by ─────────────────────────────────────

    def field_choices(self) -> list[str]:
        """Fields a person may arrange this mode by. Empty for modes with no choice."""
        return []

    @property
    def field_name(self) -> str | None:
        """The field this render uses: asked for, else remembered, else the default.

        Asked for means ``?by=`` (or the explicit ``field=``); remembered means this
        person's last pick for this model and mode. Anything not in
        :meth:`field_choices` — a stale bookmark, a field since made sensitive, a typo —
        falls through to the next source rather than erroring.
        """
        if self._field_name is not False:
            return self._field_name
        from vantage.listviews import remember_field, stored_field

        choices = self.field_choices()
        default = choices[0] if choices else None
        request = self.request
        if self._wanted_field is not None:
            # Explicit (a move names the column it was drawn in): that or the default,
            # never a stored preference that may have changed since the page loaded.
            name = self._wanted_field if self._wanted_field in choices else default
        else:
            wanted = request.GET.get(self.FIELD_PARAM) if request is not None else None
            if wanted in choices:
                remember_field(self.panel, request, self.name, wanted, default=default)
                name = wanted
            else:
                stored = stored_field(self.panel, request, self.name)
                name = stored if stored in choices else default
        self._field_name = name
        return name

    def own_url(self, request) -> str:
        """This mode's URL with its own navigation cleared but its field kept.

        :func:`~vantage.listviews.mode_url` drops ``?by=`` because a field means
        nothing to another mode; within the same mode — the next month, "Show more" —
        it is exactly what must survive.
        """
        from django.http import QueryDict

        from vantage.listviews import mode_url

        base = mode_url(request, self.name)
        if len(self.field_choices()) < 2 or not self.field_name:
            return base
        params = QueryDict(base.lstrip("?"), mutable=True)
        params[self.FIELD_PARAM] = self.field_name
        return f"?{params.urlencode()}"

    #: The picker's label ("Group by", "Dates from"). Lazily translated.
    field_label: ClassVar[Any] = ""
    #: Parameters the picker drops when the field changes: they belong to the old one.
    field_resets: ClassVar[tuple[str, ...]] = ("page",)

    def field_picker(self, request) -> dict[str, Any]:
        """``view_field`` for ``field_picker.html``. No options when there is no choice."""
        choices = self.field_choices()
        if len(choices) < 2:
            return {"options": []}
        labels = field_labels(self.panel, choices)
        current = self.field_name
        dropped = {self.FIELD_PARAM, *self.field_resets}
        keep = [
            (key, value)
            for key, values in (request.GET.lists() if request is not None else [])
            if key not in dropped
            for value in values
        ]
        return {
            "mode": self.name,
            "label": self.field_label,
            "param": self.FIELD_PARAM,
            "keep": keep,
            "options": [
                {"name": name, "label": labels[name], "current": name == current}
                for name in choices
            ],
        }


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
