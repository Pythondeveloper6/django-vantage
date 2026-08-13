"""The dashboard: cards declared in Python, computed during the request.

A dashboard is a class with a method, exactly like :class:`~vantage.pages.Page` — the
ergonomics bar this API has to clear is that the first widget someone adds costs one
line inside one method::

    class OpsDashboard(Dashboard):
        title = "Operations"

        def get_widgets(self, request):
            return [
                StatCard("Revenue", Order.objects.all(), aggregate=Sum("total"),
                         format="currency", trend="created_at"),
                ChartCard("Orders over time", Order.objects.all(),
                          chart=Chart.timeseries("Orders", date="created_at",
                                                 value=Count("pk"))),
                TableCard("Latest orders", Order.objects.all(), limit=5, width=2),
            ]

Four properties are load-bearing, and each is a failure mode a shipping admin panel
has:

* **Nothing is computed at import time.** Widgets are declarations; the queryset a
  widget holds is never evaluated until :meth:`Dashboard.render` runs inside a request,
  and a module-level queryset is re-cloned per request so it cannot serve one user's
  rows to the next.
* **A widget the user may not see never runs its query.** The permission gate is
  checked *before* the card is rendered, not after — a card that computes a number and
  then hides it has already leaked it into the query log and paid for it.
* **Widgets fail independently.** An exception inside one card renders that card as an
  error and leaves its neighbours intact. A dashboard that 500s because one team's
  custom aggregate broke is a dashboard nobody trusts.
* **Aggregation is not reimplemented here.** Every number and every chart goes through
  :func:`vantage.stats.compute_panel`, so the dashboard inherits its formatting, its
  trend windows and its join-fanout correctness for free.

The zero-config default matters as much as the API: :class:`DefaultDashboard` builds a
count card per registered model, a timeseries of new records, and a recent-records
table for every model with a creation timestamp. A project that declares nothing still
gets a dashboard worth opening.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Count, QuerySet
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.module_loading import import_string

from vantage.conf import vantage_settings
from vantage.permissions import get_permissions
from vantage.stats import DEFAULT_CURRENCY, Budget, Chart, Stat, compute_panel

__all__ = [
    "ChartCard",
    "Dashboard",
    "DefaultDashboard",
    "ListCard",
    "StatCard",
    "TableCard",
    "TemplateCard",
    "Widget",
    "WidgetResult",
    "dashboard_context",
    "get_dashboard",
]

logger = logging.getLogger("vantage.dashboards")

#: Columns in the grid. Four is the widest that still lets a stat card be one unit
#: without becoming a stripe, and it divides cleanly into halves and quarters.
GRID_COLUMNS = 4

#: The budget gate belongs to the list page, where analytics are chrome above the thing
#: the user came for. A dashboard *is* the thing the user came for, so its cards are
#: never silently deferred.
UNBUDGETED = Budget(max_queries=10**6)

DEFAULT_TABLE_LIMIT = 5

#: Columns a zero-config recent-records table shows before it stops being a summary.
MAX_TABLE_COLUMNS = 4

#: Field names that conventionally mean "when this row was created". ``auto_now_add``
#: is detected separately and beats every name here.
CREATED_NAMES = (
    "created_at",
    "created",
    "created_on",
    "date_created",
    "added_at",
    "date_joined",
    "joined_on",
    "timestamp",
)

#: Ceilings on what the default dashboard generates. A project with sixty models must
#: still get a page rather than sixty queries and a wall of cards; the sidebar is the
#: complete list, this is the summary.
MAX_DEFAULT_STATS = 12
MAX_DEFAULT_TABLES = 3

#: Apps whose models are plumbing. They still get a count card, but they are the last
#: candidates for the space-hungry cards: a chart of user signups on a two-account
#: project is a flat line where the project's own orders would have been a shape.
FRAMEWORK_APPS = frozenset({"admin", "auth", "contenttypes", "sessions", "vantage"})


# ── declarations ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WidgetResult:
    """One rendered card, as plain data.

    A single shape covers every widget type rather than one class each, for the same
    reason :mod:`vantage.stats` returns dicts: the template layer stays a dumb switch on
    ``kind``, and a widget result can be serialised for an async or partial refresh
    later without redesigning anything.
    """

    kind: str
    label: str
    width: int = 1
    description: str | None = None
    url: str | None = None
    icon: str | None = None
    stat: dict[str, Any] | None = None
    chart: dict[str, Any] | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    items: tuple[dict[str, Any], ...] = ()
    html: str = ""
    empty: bool = False
    error: str | None = None


class Widget:
    """Base class for a dashboard card.

    A widget is inert until rendered: ``__init__`` stores declarations and touches
    neither the database nor the request.
    """

    #: Which branch of ``partials/widget.html`` draws this card.
    kind = "widget"

    #: Columns of the grid this card occupies, 1 to 4.
    default_width = 1

    def __init__(
        self,
        label: str,
        *,
        width: int | None = None,
        permission: str | Any | None = None,
        description: str | None = None,
        url: str | None = None,
        icon: str | None = None,
    ) -> None:
        width = self.default_width if width is None else int(width)
        if not 1 <= width <= GRID_COLUMNS:
            raise ValueError(
                f"Widget {label!r} declares width {width}; the dashboard grid is "
                f"{GRID_COLUMNS} columns wide, so width must be 1 to {GRID_COLUMNS}."
            )
        self.label = label
        self.width = width
        self.permission = permission
        self.description = description
        self.url = url
        self.icon = icon

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label!r}>"

    # ── access ──────────────────────────────────────────────────────────────

    def get_model(self) -> type[models.Model] | None:
        """The model this card is about, when that is knowable without running code.

        Returning a model is what lets the dashboard refuse the card on the same
        permission the model's own pages are gated on, so a widget can never become a
        side channel around the sidebar.
        """
        return None

    def has_permission(self, request, permissions=None) -> bool:
        """Whether this user may see the card — decided before anything is computed."""
        if self.permission is not None:
            allowed = (
                bool(self.permission(request))
                if callable(self.permission)
                else request.user.has_perm(str(self.permission))
            )
            if not allowed:
                return False

        model = self.get_model()
        if model is None:
            return True
        return (permissions or get_permissions()).has_model_perm(request, model, "view")

    # ── rendering ───────────────────────────────────────────────────────────

    def render(self, request) -> WidgetResult:
        """Compute the card. Override this; it is the whole point."""
        raise NotImplementedError

    def card(self, **payload) -> WidgetResult:
        """A :class:`WidgetResult` carrying this widget's identity, plus ``payload``.

        Identity fields are defaults rather than fixed, so a widget that only learns
        its own link while rendering can supply one.
        """
        identity = {
            "kind": self.kind,
            "label": self.label,
            "width": self.width,
            "description": self.description,
            "url": self.url,
            "icon": self.icon,
        }
        return WidgetResult(**{**identity, **payload})


class QuerysetWidget(Widget):
    """A widget whose data comes from a queryset it is careful not to evaluate early.

    The source is a queryset, a manager, or a callable returning one. A callable is the
    escape hatch for anything that depends on the request; a plain queryset is enough
    for everything else and is the reason a widget can be declared at module scope.
    """

    def __init__(
        self,
        label: str,
        source: QuerySet | models.Manager | Any,
        *,
        model: type[models.Model] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(label, **kwargs)
        self.source = source
        self.model = model

    def get_model(self) -> type[models.Model] | None:
        if self.model is not None:
            return self.model
        # A callable source cannot be inspected without running it, and running user
        # code to decide whether the user may run it is backwards. Those widgets
        # declare ``model=`` or ``permission=`` instead.
        return getattr(self.source, "model", None)

    def get_queryset(self, request) -> QuerySet:
        """This request's rows: resolved, cloned, and narrowed to what the user may see."""
        source = self.source
        if callable(source) and not isinstance(source, QuerySet):
            source = _call(source, request)
        if isinstance(source, models.Manager):
            source = source.all()
        if not isinstance(source, QuerySet):
            raise TypeError(
                f"Widget {self.label!r} needs a queryset, a manager, or a callable "
                f"returning one; got {type(source).__name__}."
            )
        # ``.all()`` clones. A queryset declared at module scope caches its rows the
        # first time it is iterated and would serve that snapshot to every later
        # request, which is the classic module-level-queryset bug.
        queryset = source.all()
        # A number computed over rows the user cannot list is a leak, so the same
        # row-scoping the list page applies is applied here. The base implementation is
        # a no-op, so this costs nothing until a project overrides it.
        return get_permissions().filter_queryset(request, queryset, "view")


class StatCard(QuerysetWidget):
    """One number over a queryset, optionally against the preceding period.

    The aggregate defaults to a row count, because that is what most of the first
    cards on any dashboard are and it should not have to be spelled out.
    """

    kind = "stat"

    def __init__(
        self,
        label: str,
        source,
        *,
        aggregate: Any = None,
        format: str = "number",
        tone: str | None = None,
        trend: str | None = None,
        currency: str = DEFAULT_CURRENCY,
        **kwargs,
    ) -> None:
        super().__init__(label, source, **kwargs)
        self.stat = Stat(
            label=label,
            value=Count("pk") if aggregate is None else aggregate,
            format=format,
            tone=tone,
            trend=trend,
            currency=currency,
            description=self.description,
        )

    def render(self, request) -> WidgetResult:
        panel = compute_panel(
            self.get_queryset(request), stats=[self.stat], budget=UNBUDGETED
        )
        return self.card(stat=panel["stats"][0])


class ChartCard(QuerysetWidget):
    """One :class:`~vantage.stats.Chart` over a queryset, drawn as server-side SVG."""

    kind = "chart"
    default_width = 2

    def __init__(self, label: str, source, *, chart: Chart, **kwargs) -> None:
        super().__init__(label, source, **kwargs)
        self.chart = chart

    def render(self, request) -> WidgetResult:
        panel = compute_panel(
            self.get_queryset(request), charts=[self.chart], budget=UNBUDGETED
        )
        return self.card(chart=panel["charts"][0])


class TableCard(QuerysetWidget):
    """The most recent handful of records, as a small linked table.

    Deliberately short and deliberately unpaginated: this is a glance at what just
    happened with a way through to the list page, not a second list page.
    """

    kind = "table"
    default_width = 2

    def __init__(
        self,
        label: str,
        source,
        *,
        columns: list[str] | None = None,
        limit: int = DEFAULT_TABLE_LIMIT,
        order_by: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(label, source, **kwargs)
        self.columns = columns
        self.limit = limit
        self.order_by = order_by

    def render(self, request) -> WidgetResult:
        from vantage.inference import infer_list_display, infer_ordering
        from vantage.optimizer import plan_related
        from vantage.render import render_cell

        queryset = self.get_queryset(request)
        model = queryset.model
        panel = _panel_for(model)

        if self.order_by:
            queryset = queryset.order_by(*self.order_by)
        elif not queryset.ordered:
            # "Recent" is the whole premise of the card, so an unordered source gets
            # the model's inferred order — newest first wherever that is knowable.
            queryset = queryset.order_by(*infer_ordering(model))

        columns = self.columns or infer_list_display(model)[:MAX_TABLE_COLUMNS]
        link = panel.get_list_link(request)

        # Five rows with a relation column is still five extra queries, and a dashboard
        # is the page most likely to hold several of these tables at once. The plan is
        # derived from the columns actually rendered, exactly as the list page does it.
        select, prefetch = plan_related(model, columns)
        if select:
            queryset = queryset.select_related(*sorted(select))
        if prefetch:
            queryset = queryset.prefetch_related(*sorted(prefetch))

        rows = tuple(
            {
                "pk": obj.pk,
                "url": _detail_url(panel, obj),
                "cells": [
                    {"value": render_cell(panel, obj, name), "is_link": name == link}
                    for name in columns
                ],
            }
            for obj in queryset[: self.limit]
        )

        return self.card(
            columns=tuple(_column_label(model, name) for name in columns),
            rows=rows,
            empty=not rows,
            url=self.url or _list_url(panel),
        )


class ListCard(Widget):
    """Arbitrary label/value pairs — the card for numbers the ORM cannot say.

    Items are ``(label, value)`` pairs or dicts with ``label``/``value`` and optional
    ``url`` and ``tone``. A callable is resolved per request, which is what keeps a
    module-level declaration from freezing yesterday's figures into the page.
    """

    kind = "list"

    def __init__(self, label: str, items, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self.items = items

    def render(self, request) -> WidgetResult:
        items = _call(self.items, request) if callable(self.items) else self.items
        normalised = tuple(_list_item(item) for item in items)
        return self.card(items=normalised, empty=not normalised)


class TemplateCard(Widget):
    """The escape hatch: render a template of your own inside a dashboard card.

    Rendered here rather than included by the dashboard template so that a broken
    template fails like any other widget — one error card, not a 500.
    """

    kind = "template"
    default_width = 2

    def __init__(self, label: str, template_name: str, *, context=None, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self.template_name = template_name
        self.context = context

    def get_context(self, request) -> dict[str, Any]:
        context = self.context
        if callable(context):
            context = _call(context, request)
        return dict(context or {})

    def render(self, request) -> WidgetResult:
        context = {"widget": self, **self.get_context(request)}
        return self.card(html=render_to_string(self.template_name, context, request=request))


# ── the dashboard ───────────────────────────────────────────────────────────


class Dashboard:
    """The panel's landing page. Subclass, list widgets, point ``VANTAGE["DASHBOARD"]``
    at it."""

    #: Shown as the page heading. Defaults to the site title, because the dashboard is
    #: the panel's front door and naming it twice is noise.
    title: str = ""
    description: str = ""

    def __init__(self, site=None) -> None:
        self.site = site

    def get_title(self) -> str:
        return self.title or vantage_settings.SITE_TITLE

    def get_widgets(self, request) -> list[Widget]:
        """The cards, in order. Override this; it is the whole point."""
        return []

    def get_site(self):
        from vantage.sites import site as default_site

        return self.site or default_site

    def render(self, request, permissions=None) -> list[WidgetResult]:
        """Every card this user may see, each computed in isolation from the others."""
        permissions = permissions or get_permissions()
        cards = []
        for widget in self.get_widgets(request):
            if not widget.has_permission(request, permissions):
                continue
            cards.append(_render_safely(widget, request))
        return cards


def _render_safely(widget: Widget, request) -> WidgetResult:
    """One widget's failure is one card's failure.

    Every widget runs user-supplied expressions over user-supplied querysets, so
    ``Exception`` is the correct breadth here: the alternative is a dashboard that any
    one team can take down for everyone else.
    """
    try:
        return widget.render(request)
    except Exception as exc:
        logger.exception("Vantage dashboard widget %r failed.", widget.label)
        return widget.card(error=str(exc), empty=True)


class DefaultDashboard(Dashboard):
    """What a project that has configured nothing gets.

    Zero configuration is the package's thesis, so this cannot be a placeholder: it is
    a count for every model the user may see, a timeseries of new records, and a recent
    table for each model that records when its rows were created. The caps exist
    because a sixty-model project needs a summary, not a transcript — the sidebar is
    already the complete list.
    """

    def get_widgets(self, request) -> list[Widget]:
        # Filter by permission BEFORE capping. Capping first means a user who can see
        # only one model, and whose model sorts outside the first N, gets an entirely
        # empty dashboard - the cap silently becomes an access rule.
        from vantage.permissions import get_permissions

        panels = get_permissions().visible_panels(request, list(self.get_site().panels()))

        widgets: list[Widget] = [
            StatCard(
                panel.get_label_plural(),
                panel.get_queryset(request),
                model=panel.model,
                icon=panel.get_icon(),
                url=_list_url(panel),
            )
            for panel in panels[:MAX_DEFAULT_STATS]
        ]

        # Registry order for the counts, so the cards read in the same order as the
        # sidebar; the project's own models first for the cards that cost real space.
        timestamped = sorted(
            ((p, field) for p in panels if (field := creation_field(p.model))),
            key=lambda pair: pair[0].opts.app_label in FRAMEWORK_APPS,
        )

        for panel, field in timestamped[:1]:
            widgets.append(
                ChartCard(
                    f"New {panel.get_label_plural().lower()}",
                    panel.get_queryset(request),
                    model=panel.model,
                    chart=Chart.timeseries(
                        panel.get_label_plural(), date=field, value=Count("pk")
                    ),
                )
            )

        for panel, field in timestamped[:MAX_DEFAULT_TABLES]:
            widgets.append(
                TableCard(
                    f"Recent {panel.get_label_plural().lower()}",
                    panel.get_queryset(request),
                    model=panel.model,
                    order_by=[f"-{field}"],
                )
            )

        return widgets


def get_dashboard(site=None) -> Dashboard:
    """The configured dashboard, or the default one.

    Resolved per request rather than cached: ``import_string`` is a ``sys.modules``
    lookup after the first call, and caching here would need an invalidation hook in
    the settings layer for a saving nobody can measure.
    """
    path = vantage_settings.DASHBOARD
    if not path:
        return DefaultDashboard(site)

    resolved = import_string(path) if isinstance(path, str) else path
    if isinstance(resolved, Dashboard):
        return resolved
    if not (isinstance(resolved, type) and issubclass(resolved, Dashboard)):
        raise ImproperlyConfigured(
            f"VANTAGE['DASHBOARD'] = {path!r} is not a Dashboard subclass. It must be a "
            f"dotted path to a class inheriting vantage.dashboards.Dashboard."
        )
    return resolved(site)


def dashboard_context(site, request, permissions=None) -> dict[str, Any]:
    """Everything ``vantage/dashboard.html`` needs.

    The view stays three lines long because the whole page is assembled here: resolving
    which dashboard is in play, gating its widgets and computing them are one concern,
    and a view is the wrong place to keep it.
    """
    dashboard = get_dashboard(site)
    return {
        "dashboard": dashboard,
        "widgets": dashboard.render(request, permissions=permissions),
        # The heading may fall back to the site title; the browser tab must not, or it
        # reads "Acme · Acme".
        "page_title": dashboard.title or "Dashboard",
        "page_description": dashboard.description,
    }


# ── helpers ─────────────────────────────────────────────────────────────────


def creation_field(model) -> str | None:
    """The field recording when a row was created, or None.

    Deliberately NOT delegated to ``patterns.detect_timeline``. They answer different
    questions: the detector picks the best field to *group by* and therefore prefers a
    domain date such as ``published_at`` over an audit column, while "recent records"
    and "new per week" need the date the row came into existence. ``auto_now_add`` is
    proof of that and wins outright; the detector is only a fallback for models that
    record creation without it.
    """
    from vantage.introspection import concrete_fields

    for field in concrete_fields(model):
        if getattr(field, "auto_now_add", False):
            return field.name

    from vantage.patterns import detect_timeline

    timeline = detect_timeline(model)
    # A nullable date is no good here: half the rows would fall outside every bucket.
    if timeline and not timeline.nullable:
        return timeline.field
    return None



def _call(func, request):
    """Call a source with the request, or without it if it takes no argument.

    Both arities are supported because ``Order.objects.all`` and
    ``lambda request: ...`` are equally natural things to write in a declaration.
    """
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # builtins and C callables have no signature
        return func(request)
    return func(request) if parameters else func()


def _panel_for(model: type[models.Model]):
    """The registered panel for ``model``, so the project's own cell rendering applies."""
    from vantage.panels import ModelPanel
    from vantage.sites import site

    if site.is_registered(model):
        return site.get_panel(model)
    return ModelPanel(model)


def _detail_url(panel, obj) -> str | None:
    try:
        return reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
    except NoReverseMatch:  # pragma: no cover - an unmounted site
        return None


def _list_url(panel) -> str | None:
    try:
        return reverse(panel.list_url_name)
    except NoReverseMatch:  # pragma: no cover - an unmounted site
        return None


def _column_label(model: type[models.Model], name: str) -> str:
    from vantage.introspection import resolve_path

    hops = resolve_path(model, name)
    label = str(getattr(hops[-1].field, "verbose_name", name)) if hops else name
    return label.replace("_", " ").capitalize()


def _list_item(item) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "label": item.get("label", ""),
            "value": item.get("value"),
            "url": item.get("url"),
            "tone": item.get("tone"),
        }
    label, value = item
    return {"label": label, "value": value, "url": None, "tone": None}
