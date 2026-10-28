"""The Kanban board: one column per value of a status-like field.

Two costs are bounded here, and both are easy to get wrong.

**Counts come from one aggregate.** ``SELECT status, COUNT(*) … GROUP BY status`` over
the filtered rows, once. A count per column would be a query per choice, and a count
derived from the cards on screen would be wrong the moment a column is truncated.

**Cards come from one windowed query.** Each column shows at most
:attr:`KanbanMode.column_limit` cards, and "the first twenty of *each* status" is a
per-group limit SQL can only express with a window function: ``ROW_NUMBER() OVER
(PARTITION BY status ORDER BY …)`` filtered to the top N. One query, however many
columns. A backend without window support gets one bounded query per column instead,
which is still bounded by the number of choices rather than by the number of rows.

Moving a card is a write, and it goes through exactly the path an inline cell edit
does — :mod:`vantage.listviews.moves` subclasses the inline write view, so the move is
scoped, permission-checked per object, validated by the same ``ModelForm`` (and so by
``full_clean``), guarded by the optimistic lock and written to the audit log. A board
that wrote through a shortcut would be the one place in the panel where dragging a card
could do what the change form refuses.
"""

from __future__ import annotations

import json
from typing import Any

from django.db import connections, models, router
from django.db.models import Count, F, Window
from django.db.models.functions import RowNumber
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from vantage.listviews.base import ListMode, card_for, field_labels, with_related
from vantage.listviews.inference import kanban_field_ok

__all__ = ["KanbanMode", "build_urls", "column_slug", "encode_value", "local_field"]


def local_field(model, name: str | None) -> models.Field | None:
    """A concrete field on the model itself, or ``None``. Boards never cross a join."""
    if not name:
        return None
    try:
        field = model._meta.get_field(name)
    except Exception:
        return None
    return field if getattr(field, "concrete", False) else None


def encode_value(value: Any) -> str:
    """The column value as JSON, which is what the browser posts back on a move.

    JSON rather than ``str()`` because the type has to survive the round trip: the
    integer choice ``3`` and the string ``"3"`` are different values to a form, and a
    ``None`` column must post ``null`` rather than the string ``"None"``.
    """
    return json.dumps(value, default=str)


def column_slug(value: Any) -> str:
    """A short, URL- and id-safe name for a column, for anchors and ``?more=``."""
    if value is None:
        return "none"
    if value == "":
        return "empty"
    if isinstance(value, bool):
        return "true" if value else "false"
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value).lower())


class KanbanMode(ListMode):
    name = "kanban"
    label = _("Board")
    icon = "kanban"
    template = "vantage/list_views/kanban.html"
    field_label = _("Group by")
    #: "Show more" names columns of the old field, which mean nothing under a new one.
    field_resets = ("page", "more")

    #: Cards per column before "Show more". Each click on it adds this many again.
    column_limit = 20
    #: The most cards one column will ever render, however often "Show more" is used.
    MAX_COLUMN_LIMIT = 500

    @classmethod
    def supports(cls, panel) -> bool:
        field = local_field(panel.model, panel.get_kanban_field())
        return field is not None and kanban_field_ok(field)

    # ── the field and its columns ───────────────────────────────────────────

    def field_choices(self) -> list[str]:
        request = self.request
        return [
            name
            for name in self.panel.get_kanban_fields(request)
            if (field := local_field(self.panel.model, name)) is not None and kanban_field_ok(field)
        ]

    @property
    def field(self) -> models.Field:
        return local_field(self.panel.model, self.field_name)

    def declared_values(self) -> list[tuple[Any, str]]:
        field = self.field
        if isinstance(field, models.BooleanField):
            values = [
                (True, str(_("Yes"))),
                (False, str(_("No"))),
            ]
        else:
            values = [(value, str(label)) for value, label in field.flatchoices]
        if field.null:
            values.append((None, str(_("No value"))))
        return values

    def is_writable(self, request=None) -> bool:
        """Whether the change form would let anybody write this field at all.

        Restated from the inline allowlist's own source of truth — the change form's
        field list, minus ``readonly_fields`` and ``exclude`` — so a field the edit page
        shows read-only can never be moved here either.
        """
        from vantage.inline import _change_form_names

        field = self.field
        if field is None or not getattr(field, "editable", False):
            return False
        if field.name not in _change_form_names(self.panel):
            return False
        # Read-only for this person: the board still groups by it, nobody can drag.
        from vantage.fieldperms import access

        return access(request if request is not None else self.request, self.panel).can_change(
            field.name
        )

    def move_url(self) -> str:
        try:
            return reverse(f"vantage:{self.panel.slug}-kanban-move")
        except NoReverseMatch:
            return ""

    def can_move(self, request, permissions) -> bool:
        """Model-level answer, for drawing the controls. Each move re-checks the object."""
        if not self.move_url() or not self.is_writable(request):
            return False
        return permissions.has_model_perm(request, self.panel.model, "change")

    # ── the page ────────────────────────────────────────────────────────────

    def limits(self, request, values: list[Any]) -> dict[Any, int]:
        wanted = request.GET.getlist("more") if request is not None else []
        limits = {}
        for value in values:
            extra = wanted.count(column_slug(value))
            limits[value] = min(self.column_limit * (1 + extra), self.MAX_COLUMN_LIMIT)
        return limits

    def counts(self, queryset) -> dict[Any, int]:
        """Every column's total, in one ``GROUP BY``."""
        name = self.field.name
        rows = queryset.order_by().values(name).annotate(_v_count=Count("pk"))
        return {row[name]: row["_v_count"] for row in rows}

    def context(self, request, queryset, *, permissions) -> dict[str, Any]:
        field = self.field
        name = field.name
        counts = self.counts(queryset)

        columns_spec = self.declared_values()
        known = {value for value, _label in columns_spec}
        # A value the data holds but the choices no longer list — a retired status, an
        # empty string on a blank CharField — gets a column too. Hiding those rows
        # would make the board's totals disagree with the table's.
        for value in counts:
            if value not in known:
                label = str(_("No value")) if value in (None, "") else str(value)
                columns_spec.append((value, label))
                known.add(value)

        values = [value for value, _label in columns_spec]
        limits = self.limits(request, values)
        card_fields = self.panel.get_card_fields(request, exclude=(name,))
        queryset = with_related(queryset, self.panel.model, card_fields)
        grouped = self.fetch(queryset, values, limits, counts)

        labels = field_labels(self.panel, card_fields)
        from vantage import concurrency
        from vantage.render import _tone_for

        base_url = self.own_url(request)
        columns = []
        for value, label in columns_spec:
            cards = []
            for obj in grouped.get(value, [])[: limits[value]]:
                card = card_for(self.panel, obj, card_fields, labels)
                card["version"] = concurrency.token_for(obj) or ""
                cards.append(card)
            total = counts.get(value, 0)
            slug = column_slug(value)
            more = []
            if total > len(cards) and limits[value] < self.MAX_COLUMN_LIMIT:
                more = [*request.GET.getlist("more"), slug]
            columns.append(
                {
                    "value": value,
                    "key": encode_value(value),
                    "slug": slug,
                    "label": label,
                    "count": total,
                    "cards": cards,
                    "remaining": total - len(cards),
                    "more_url": _with_more(base_url, more) if more else "",
                    "tone": _tone_for(value) if isinstance(value, str) else None,
                }
            )

        can_move = self.can_move(request, permissions)
        return {
            "kanban_columns": columns,
            "kanban_field": name,
            "kanban_field_label": str(field.verbose_name).capitalize(),
            "kanban_can_move": can_move,
            "kanban_move_url": self.move_url() if can_move else "",
            "view_field": self.field_picker(request),
        }

    def fetch(self, queryset, values, limits, counts) -> dict[Any, list]:
        """The first N cards of every column, in as few queries as the backend allows."""
        name = self.field.name
        wanted = [value for value in values if counts.get(value)]
        if not wanted:
            return {}
        top = max(limits[value] for value in wanted)
        ordering = _order_expressions(queryset)

        alias = router.db_for_read(self.panel.model)
        if connections[alias].features.supports_over_clause:
            ranked = queryset.annotate(
                _v_rank=Window(RowNumber(), partition_by=[F(name)], order_by=ordering)
            ).filter(_v_rank__lte=top)
            grouped: dict[Any, list] = {}
            for obj in ranked:
                grouped.setdefault(getattr(obj, name), []).append(obj)
            return grouped

        grouped = {}  # pragma: no cover - every CI backend supports window functions
        for value in wanted:  # pragma: no cover
            lookup = {f"{name}__isnull": True} if value is None else {name: value}
            grouped[value] = list(queryset.filter(**lookup)[: limits[value]])
        return grouped  # pragma: no cover


def _order_expressions(queryset) -> list:
    """The queryset's ordering as expressions a window's ``ORDER BY`` accepts.

    A card column is read top to bottom in the same order as the table, so the window
    ranks by the list's own sort — the clicked column, else the panel's ordering —
    with the primary key as the tiebreaker that keeps "Show more" from repeating a card.
    """
    raw = list(queryset.query.order_by) or list(queryset.model._meta.ordering or [])
    expressions = []
    for entry in raw:
        if isinstance(entry, str):
            if entry == "?":
                continue
            if entry.startswith("-"):
                expressions.append(F(entry[1:]).desc())
            else:
                expressions.append(F(entry.lstrip("+")).asc())
        elif hasattr(entry, "resolve_expression"):
            expressions.append(entry)
    expressions.append(F("pk").desc())
    return expressions


def _with_more(base_url: str, more: list[str]) -> str:
    from django.http import QueryDict

    params = QueryDict(base_url.lstrip("?"), mutable=True)
    params.setlist("more", more)
    return f"?{params.urlencode()}"


def build_urls(site) -> list:
    """``<app>/<model>/_kanban/move/`` for every panel.

    Mounted before the per-panel loop for the reason every ``_``-prefixed record route
    is: the detail route's ``<path:pk>`` would otherwise swallow ``_kanban``.

    Every panel, not only those that can show a board when the URLconf is built:
    whether a board exists is a request-time answer (``get_list_views(request)``, a
    panel reconfigured after import), and the view refuses any field it may not write.
    Filtering here made the route's existence depend on when the URLconf was loaded.
    """
    from django.urls import path

    from vantage.listviews.moves import KanbanMoveView

    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        urlpatterns.append(
            path(
                f"{opts.app_label}/{opts.model_name}/_kanban/move/",
                KanbanMoveView.as_view(site=site, panel=panel),
                name=f"{panel.slug}-kanban-move",
            )
        )
    return urlpatterns
