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

**And the whole of it is arrangeable from the dashboard itself.** Everything above is
the *code* layer; :class:`DashboardLayout` puts a row underneath it, with the
resolution order :mod:`vantage.listprefs` established one page over::

    per-user row  >  site-wide row  >  VANTAGE["DASHBOARD"]  >  DefaultDashboard

A stored arrangement is a list of entries, and an entry is either a pointer at a
widget the code declares — by key, so the declaration stays the source of what that
card *means* while the row only says where it sits, how wide it is and whether it
shows — or a card somebody built in the UI over any registered model they may view.
The second kind stores *ingredients*: a model label, an aggregate name, a column, a
grouping and a filter query string, every one of them re-checked against what the
panel currently exposes on the way in and again on the way out. Nothing stored is ever
an expression, and nothing stored can widen what the panel already offers.

Two properties carry the arrangement layer, and both are the same ones the code layer
has: **the permission check happens before the query**, so a card over a model the
viewer cannot see is neither drawn nor computed and never reaches ``aggregate()``; and
**the cost is one aggregate per card**, because every card still goes through
:func:`vantage.stats.compute_panel` and inherits its join-bucket partitioning rather
than growing a second aggregation engine here.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, replace
from typing import Any

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import models
from django.db.models import Count, QuerySet
from django.db.utils import OperationalError, ProgrammingError
from django.http import HttpResponseRedirect, QueryDict
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings
from vantage.permissions import get_permissions
from vantage.stats import DEFAULT_CURRENCY, Budget, Chart, Stat, compute_panel
from vantage.views import SiteView

__all__ = [
    "ARRANGE_PARAM",
    "CARD_SHAPES",
    "DECLARED",
    "MAX_BUILT_CARDS",
    "MAX_CARDS",
    "ArrangeDashboardView",
    "ArrangedDashboard",
    "Arrangement",
    "ChartCard",
    "Dashboard",
    "DefaultDashboard",
    "ListCard",
    "StatCard",
    "TableCard",
    "TemplateCard",
    "Widget",
    "WidgetResult",
    "arranging",
    "build_urls",
    "clean_cards",
    "dashboard_context",
    "get_dashboard",
    "may_set_site_default",
    "posted_layout",
    "reset_layout",
    "store_layout",
    "stored_layout",
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
    #: Where this card sat in the arrangement it was drawn from, so the editor's
    #: controls can address it. ``-1`` on a dashboard nobody is arranging.
    index: int = -1
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
        key: str | None = None,
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
        self.key = key

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label!r}>"

    def get_key(self) -> str:
        """A stable name a stored arrangement can point at this card by.

        Derived from the label rather than from the position, so inserting a card at
        the top of ``get_widgets`` does not silently re-point every stored entry at its
        neighbour. Declare ``key=`` on a card whose label is going to be reworded, which
        is the one thing that would otherwise break the link.
        """
        return self.key or slugify(self.label) or "card"

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
        from vantage.render import render_cell, table_cell

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
                "obj": obj,
                "pk": obj.pk,
                "url": _detail_url(panel, obj),
                "cells": [
                    table_cell(render_cell(panel, obj, name), name == link) for name in columns
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

    def get_declared_keys(self, request) -> set[str]:
        """Every key this dashboard could ever produce, regardless of what this
        particular viewer may see.

        This is the baseline an arrangement is compared against to tell a declared
        card that already existed and was left out on purpose from one that is new to
        the code and must not be swallowed — see :meth:`ArrangedDashboard.plan`.

        The default assumes ``get_widgets`` does not itself vary with permission, true
        of every hand-written dashboard: per-card visibility there is decided by
        :meth:`Widget.has_permission` once the list comes back, not by filtering the
        list itself. :class:`DefaultDashboard` is the one dashboard where that
        assumption is false, so it overrides this rather than inheriting it.
        """
        return set(keyed_widgets(self.get_widgets(request)))

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
        return self._widgets_for(request, panels)

    def get_declared_keys(self, request) -> set[str]:
        """Every model's card, whether or not this viewer may see the model, and
        whether or not the display cap would have drawn it.

        The base implementation compares against ``get_widgets``, but this dashboard's
        own ``get_widgets`` both filters by permission and caps what it shows — either
        one, left in, would make a card that only a *differently* scoped viewer would
        ever produce look new to the code:

        * A permission filter, because a card that only becomes visible once somebody
          is granted it would otherwise read as new and get appended back into an
          arrangement that never mentioned it on purpose. A model's card exists once
          the model is registered, not once a particular viewer may open it.
        * The cap, because it is computed *after* permission-filtering (a restricted
          viewer's one visible model must never be capped away), so the one model that
          wins the single timeseries chart slot for a narrowly-scoped viewer is not
          necessarily the one that would win it across every registered model. Both
          are legitimate "the code could produce this" answers, so both count as
          known.
        """
        panels = list(self.get_site().panels())
        return set(keyed_widgets(self._widgets_for(request, panels, capped=False)))

    def _widgets_for(self, request, panels: list, *, capped: bool = True) -> list[Widget]:
        """The cards for ``panels``, in the shape :meth:`get_widgets` promises.

        Factored out so :meth:`get_declared_keys` can ask the same question over every
        registered panel instead of only the ones this request may see, and without the
        display cap — ``capped=False`` answers "could the code ever produce this key",
        not "does this request's grid have room for it".
        """
        stat_cap = MAX_DEFAULT_STATS if capped else len(panels)
        chart_cap = 1 if capped else len(panels)
        table_cap = MAX_DEFAULT_TABLES if capped else len(panels)

        widgets: list[Widget] = [
            StatCard(
                panel.get_label_plural(),
                panel.get_queryset(request),
                model=panel.model,
                icon=panel.get_icon(),
                url=_list_url(panel),
            )
            for panel in panels[:stat_cap]
        ]

        # Registry order for the counts, so the cards read in the same order as the
        # sidebar; the project's own models first for the cards that cost real space.
        timestamped = sorted(
            ((p, field) for p in panels if (field := creation_field(p.model))),
            key=lambda pair: pair[0].opts.app_label in FRAMEWORK_APPS,
        )

        for panel, field in timestamped[:chart_cap]:
            widgets.append(
                ChartCard(
                    _("New %(model)s") % {"model": panel.get_label_plural().lower()},
                    panel.get_queryset(request),
                    model=panel.model,
                    chart=Chart.timeseries(
                        panel.get_label_plural(), date=field, value=Count("pk")
                    ),
                )
            )

        for panel, field in timestamped[:table_cap]:
            widgets.append(
                TableCard(
                    _("Recent %(model)s") % {"model": panel.get_label_plural().lower()},
                    panel.get_queryset(request),
                    model=panel.model,
                    order_by=[f"-{field}"],
                )
            )

        return widgets


def code_dashboard(site=None) -> Dashboard:
    """What the *code* says the dashboard is: ``VANTAGE["DASHBOARD"]``, or the default.

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


def get_dashboard(site=None, request=None) -> Dashboard:
    """The dashboard this request should see, resolved through the whole cascade.

    Without a request there is nobody to read a stored arrangement for, so the answer
    is the code's — which is also what ``check``, ``scaffold`` and every other caller
    that asks the question outside a request is actually asking. Same rule, and the
    same reason, as :func:`vantage.listprefs.override`.
    """
    bundle = _stored_bundle(site, request)
    if bundle is None:
        return code_dashboard(site)
    return ArrangedDashboard(
        code_dashboard(site), bundle.cards, site, known_keys=bundle.known_keys
    )


def dashboard_context(site, request, permissions=None) -> dict[str, Any]:
    """Everything ``vantage/dashboard.html`` needs.

    The view stays three lines long because the whole page is assembled here: resolving
    which dashboard is in play, gating its widgets and computing them are one concern,
    and a view is the wrong place to keep it.
    """
    from vantage.sites import site as default_site

    site = site or default_site
    dashboard = get_dashboard(site, request)
    stored = isinstance(dashboard, ArrangedDashboard)
    editing = arranging(request)

    if stored or editing:
        # One plan, indexed, so the editor's controls and the cards they act on can
        # never disagree about which slot is which.
        arranged = dashboard if stored else ArrangedDashboard(dashboard, [], site)
        arrangement = arranged.arrange(request, permissions=permissions)
        widgets = arrangement.cards
    else:
        # Nothing stored and nobody arranging: go straight through the documented
        # extension point, so a project that overrode ``Dashboard.render`` still owns
        # its own page.
        arrangement = None
        widgets = dashboard.render(request, permissions=permissions)

    return {
        "dashboard": dashboard,
        "widgets": widgets,
        # The heading may fall back to the site title; the browser tab must not, or it
        # reads "Acme · Acme".
        "page_title": dashboard.title or _("Dashboard"),
        "page_description": dashboard.description,
        "arranging": editing,
        "arranged": stored,
        "arrange_url": _arrange_url(request, on=not editing),
        "editor": editor_context(site, request, arrangement, stored=stored)
        if editing
        else None,
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


# ════════════════════════════════════════════════════════════════════════════
# Arranging the dashboard from the dashboard
# ════════════════════════════════════════════════════════════════════════════
#
# Everything above is the code layer. Everything below puts a row underneath it,
# following :mod:`vantage.listprefs` line for line: the same two-row cascade, the same
# meaning for a missing row, the same "the panel is the ceiling" rule applied twice —
# once to reject a hostile POST and once to stop an arrangement stored last month from
# resurrecting a column the panel no longer exposes.
#
# It borrows :mod:`vantage.listprefs`'s validators and expression builders outright
# rather than restating them. There is one aggregate vocabulary in this package and one
# place that turns ``{"aggregate": "sum", "field": "total"}`` into a
# :class:`~vantage.stats.Stat`; a dashboard card is that same thing with a model and a
# grid position attached, so it goes through the same door.

#: Query parameter that puts the dashboard into arranging mode. A parameter rather than
#: a stored flag: arranging is something you are doing for the next thirty seconds, not
#: a preference, and a URL you can leave and come back to is the cheapest way to say so.
ARRANGE_PARAM = "arrange"

#: An entry pointing at a card the *code* declares, addressed by
#: :meth:`Widget.get_key`.
DECLARED = "declared"

#: The shapes a UI-built card can take. Deliberately the two the request named — a
#: number or a chart — and not the table and list cards, which need columns and
#: hand-written pairs that no picker can ask for.
CARD_SHAPES: tuple[str, ...] = ("stat", "chart")

#: Ceilings on one stored arrangement. Two of them, because the two kinds of entry cost
#: different things: an entry pointing at a declared card only says where a card the
#: code was going to draw anyway should sit, while a UI-built card is an aggregate the
#: page would not otherwise have run. The second number is therefore the one that
#: bounds the query cost, and it is the smaller of the two.
#:
#: Neither is a security boundary — every card is a validated aggregate over a column
#: the panel exposes — and, just as importantly, neither is applied to a slice of the
#: registry. Capping a registry slice *before* filtering it by permission is how a
#: restricted user once got a blank dashboard here; these cap a list somebody
#: explicitly arranged, and the permission filter runs later, at render, over whatever
#: survives.
MAX_CARDS = 60
MAX_BUILT_CARDS = 24

#: The longest filter query string a card may carry.
MAX_FILTER = 500

#: Cached on the request, because the dashboard asks for the layout to render the grid
#: and again to draw the editor, and both must see the same one.
_LAYOUT_ATTR = "_vantage_dashboard_layout"
_MISSING = object()


# ── the stored entry, validated ─────────────────────────────────────────────


def resource_key(panel_or_model: Any) -> str:
    """``"shop.order"`` — how a card names the model it is about."""
    from vantage import listprefs

    return listprefs.resource_key(panel_or_model)


def _panels_by_label(site) -> dict[str, Any]:
    return {resource_key(panel): panel for panel in site.panels()}


def _clean_width(value: Any) -> int | None:
    """A grid width, or ``None`` for "leave the card at its declared width"."""
    if isinstance(value, bool):
        return None
    try:
        width = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return width if 1 <= width <= GRID_COLUMNS else None


def clean_filter(panel, raw: Any, request=None) -> str:
    """A card's filter, re-expressed as the query string the list page would produce.

    The list page's own machinery does the whole of the work: every parameter is
    matched against a filter the panel *declares*, parsed by that filter, and written
    back out from the values it accepted. A parameter the panel does not offer, or a
    value the filter refuses, is dropped in silence — the same bargain a stale bookmark
    strikes on the list page itself, and for the same reason: a filter that has stopped
    making sense must widen the card, never break it.

    Multi-valued paths are refused outright, and that is not a simplification. A join
    to a to-many relation fans the rows out, and while a list page can hide that with
    ``DISTINCT``, an aggregate cannot: the sum is counted once per joined row and the
    card reports a number nobody can reproduce. It is the same fan-out
    :mod:`vantage.stats` partitions its queries to avoid, one layer up.
    """
    from vantage.filters import build_filter

    if not isinstance(raw, str) or not raw.strip():
        return ""

    params = QueryDict(raw.strip().lstrip("?")[:MAX_FILTER])
    kept = QueryDict(mutable=True)
    for path in panel.get_list_filter(request):
        built = build_filter(panel.model, path, params)
        if built is None or not built.is_active or built.is_multivalued:
            continue
        for value in built.values:
            kept.appendlist(built.parameter_name, value)
    return kept.urlencode()


def card_filters(panel, spec: str | None, request=None) -> list[Any]:
    """The :class:`~vantage.filters.Filter` objects behind a card's stored filter.

    Built, never evaluated: constructing a filter is path resolution and string
    parsing, so a card that is about to be refused on permission has still cost
    nothing.
    """
    from vantage.filters import build_filter

    if not spec:
        return []
    params = QueryDict(spec)
    built = []
    for path in panel.get_list_filter(request):
        filt = build_filter(panel.model, path, params)
        if filt is not None and filt.is_active and not filt.is_multivalued:
            built.append(filt)
    return built


def describe_filter(panel, spec: str | None, request=None) -> list[str]:
    """One human phrase per active filter, for the chip under a card's title.

    Labels come from the field's own ``choices`` and never from the database. A
    relation filter therefore reads as its key rather than as the row's name, which is
    the right trade: a description is decoration, and a dashboard that runs a query per
    card just to decorate it has given back the thing this whole layer promises.
    """
    from vantage.stats import _segment_label

    phrases = []
    for filt in card_filters(panel, spec, request):
        declared = getattr(filt.field, "choices", None) or []
        choices = {value: str(label) for value, label in declared}
        values = ", ".join(
            _segment_label(_coerce_choice(value, choices), choices) for value in filt.values
        )
        phrases.append(f"{filt.label}: {values}")
    return phrases


def _coerce_choice(value: str, choices: dict) -> Any:
    """Match a query-string value against a choices map whose keys may be integers."""
    if value in choices:
        return value
    for candidate in choices:
        if str(candidate) == value:
            return candidate
    return value


def clean_card(raw: Any, site, request=None) -> dict[str, Any] | None:
    """One stored or posted entry, or ``None`` when it is not something we may draw.

    Nothing raises. An entry naming a model that has been unregistered, a column that
    has left ``list_display`` or an aggregate that never existed resolves to ``None``
    and disappears from the arrangement — because the alternative is a 500 on the
    landing page caused by a row nobody remembers writing.
    """
    from vantage import listprefs

    if not isinstance(raw, dict):
        return None

    shape = raw.get("card")
    if shape == DECLARED:
        key = raw.get("key")
        if not isinstance(key, str) or not key or len(key) > 120:
            return None
        entry = {"card": DECLARED, "key": key, "show": raw.get("show") is not False}
        width = _clean_width(raw.get("width"))
        if width:
            entry["width"] = width
        return entry

    if shape not in CARD_SHAPES:
        return None

    label = raw.get("model")
    panel = _panels_by_label(site).get(label) if isinstance(label, str) else None
    if panel is None:
        return None

    # The panel's own answer, with the list-page preference layer switched off — the
    # same allowlist that draws the picker is the one that validates what comes back.
    opts = listprefs.options(panel, request)

    if shape == "stat":
        measure = listprefs._clean_measure(raw, opts)
        if measure is None:
            return None
        entry = {"card": "stat", "model": label, **measure}
        default_width = StatCard.default_width
    else:
        chart = listprefs._clean_chart(raw, opts)
        if chart is None:
            return None
        entry = {"card": "chart", "model": label, **chart}
        default_width = ChartCard.default_width

    entry["show"] = raw.get("show") is not False
    entry["width"] = _clean_width(raw.get("width")) or default_width
    entry["filter"] = clean_filter(panel, raw.get("filter"), request)
    return entry


def clean_cards(value: Any, site, request=None) -> list[dict[str, Any]] | None:
    """A whole arrangement, validated. ``None`` means "there is nothing usable here".

    An arrangement that was stored empty stays empty — somebody removed every card and
    is entitled to see the result. An arrangement that was stored full and has since
    validated down to nothing is a different thing entirely: every card it named has
    gone, so it has stopped being an opinion and the code gets the page back.
    """
    if not isinstance(value, list | tuple):
        return None

    kept: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    built = 0
    for raw in value:
        entry = clean_card(raw, site, request)
        if entry is None:
            continue
        if entry["card"] == DECLARED:
            # One entry per declared card. Two would draw the same widget twice and
            # give the editor two controls that fight over one row.
            if entry["key"] in seen_keys:
                continue
            seen_keys.add(entry["key"])
        else:
            if built >= MAX_BUILT_CARDS:
                continue
            built += 1
        kept.append(entry)
        if len(kept) >= MAX_CARDS:
            break

    if kept:
        return kept
    return [] if not value else None


# ── turning an entry into a widget ──────────────────────────────────────────


def build_card(entry: dict[str, Any], site, request=None) -> Widget | None:
    """The :class:`Widget` one UI-built entry describes, still holding no rows.

    The source is a callable so the queryset is not built until the card is rendered,
    which is after the permission gate — the property the whole module rests on.
    """
    from vantage import listprefs
    from vantage.filters import apply_filters

    panel = _panels_by_label(site).get(entry.get("model", ""))
    if panel is None:
        return None

    spec = entry.get("filter") or ""
    filters = card_filters(panel, spec, request)

    def source(req, panel=panel, filters=filters):
        return apply_filters(panel.get_queryset(req), filters)

    url = _list_url(panel)
    if url and spec:
        url = f"{url}?{spec}"

    shared = {
        "model": panel.model,
        "width": entry.get("width"),
        "url": url,
        "key": _entry_key(entry),
        "description": " · ".join(describe_filter(panel, spec, request)) or None,
    }
    if entry["card"] == "stat":
        stat = listprefs._stat(entry)
        return StatCard(entry["label"], source, aggregate=stat.value, **shared)
    return ChartCard(entry["label"], source, chart=listprefs._chart(entry), **shared)


def _entry_key(entry: dict[str, Any]) -> str:
    """A stable identity for a UI-built card, used only to keep keys from colliding."""
    parts = [
        "ui",
        entry.get("model", ""),
        entry.get("card", ""),
        entry.get("mode", ""),
        entry.get("target", ""),
        entry.get("aggregate", ""),
        entry.get("field", "") or "rows",
    ]
    return slugify("-".join(part for part in parts if part)) or "ui-card"


def keyed_widgets(widgets: list[Widget]) -> dict[str, Widget]:
    """Every declared widget, under a key an arrangement can point at.

    Collisions are broken positionally rather than refused: two cards may legitimately
    be called "Revenue", and a dashboard that raises because of it would be a dashboard
    the arrangement layer broke.
    """
    keyed: dict[str, Widget] = {}
    for widget in widgets:
        base = widget.get_key()
        key, suffix = base, 2
        while key in keyed:
            key = f"{base}-{suffix}"
            suffix += 1
        keyed[key] = widget
    return keyed


# ── the arranged dashboard ──────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanItem:
    """One slot in the arrangement: the entry that put it there and what it draws."""

    entry: dict[str, Any]
    widget: Widget


@dataclass(frozen=True)
class Arrangement:
    """A rendered dashboard, plus everything the editor needs that is not on screen."""

    #: The cards to draw, each carrying the slot index its controls address.
    cards: list[WidgetResult]
    #: Cards that exist in the arrangement but are switched off, so the editor can
    #: offer them back.
    hidden: list[dict[str, Any]]
    #: Slots this viewer may not see. Carried through a save as opaque indexes so that
    #: somebody with narrower access rearranging the page cannot delete a colleague's
    #: cards by not having been shown them.
    unseen: list[dict[str, Any]]


class ArrangedDashboard(Dashboard):
    """A dashboard read through a stored arrangement.

    Composition rather than a subclass of whatever the project declared: the code's
    dashboard keeps deciding what each declared card *is*, and this only decides which
    of them appear, in what order, how wide, and what has been added alongside them.
    """

    def __init__(
        self,
        base: Dashboard,
        layout: list[dict[str, Any]],
        site=None,
        *,
        known_keys: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(site or base.site)
        self.base = base
        self.layout = list(layout)
        # The declared keys that existed the moment this layout was last saved,
        # independent of who saved it. Empty means "no snapshot" — either nothing has
        # ever been saved (the editor's own preview wrapper) or the row predates this
        # field — and both read the same way: nothing is known to have been removed, so
        # every declared card is still a candidate to append. See ``plan``.
        self.known_keys = known_keys
        # Instance attributes shadowing the class ones, so the page heading and the
        # browser tab still read what the project named its dashboard.
        self.title = base.title
        self.description = base.description

    def __repr__(self) -> str:
        return f"<ArrangedDashboard over {self.base!r} ({len(self.layout)} entries)>"

    def get_title(self) -> str:
        return self.base.get_title()

    def plan(self, request) -> list[PlanItem]:
        """Every slot, in order, before anything is gated or computed.

        A declared card the arrangement never mentions is appended at the end rather
        than dropped — the same choice ``NAV_UNLISTED = "append"`` makes for the
        sidebar, and for the same reason: a developer adding a card in Python must not
        have it silently swallowed by an arrangement saved before it existed.

        But "never mentions" is ambiguous on its own: a key absent from the layout is
        either a card that did not exist when the arrangement was last saved, or one
        that did and was left out on purpose — a removal. ``self.known_keys`` is what
        tells the two apart: a key recorded there existed at save time, so its absence
        now is a decision and the card stays gone. A key that is neither mentioned nor
        recorded is new since that save, so it is appended. Hiding a card the
        arrangement *does* mention is a separate, explicit ``show: False`` — see
        :meth:`arrange`.
        """
        declared = keyed_widgets(list(self.base.get_widgets(request)))
        items: list[PlanItem] = []
        used: set[str] = set()

        for entry in self.layout:
            if entry.get("card") == DECLARED:
                widget = declared.get(entry["key"])
                if widget is None:
                    # The code no longer declares it. An inert entry, left to be tidied
                    # away by the next save rather than raising on the landing page.
                    continue
                used.add(entry["key"])
                items.append(PlanItem(entry=entry, widget=widget))
                continue
            widget = build_card(entry, self.get_site(), request)
            if widget is not None:
                items.append(PlanItem(entry=entry, widget=widget))

        for key, widget in declared.items():
            if key in used or key in self.known_keys:
                continue
            items.append(PlanItem(entry={"card": DECLARED, "key": key, "show": True},
                                  widget=widget))
        return items

    def arrange(self, request, permissions=None) -> Arrangement:
        """Render the plan, in the one order that is not negotiable.

        Permission first, then visibility, then the query. A card the viewer may not
        see never reaches ``has_permission``'s far side, so it is never rendered and
        never computed — and it is checked before the "is this switched off" test too,
        so a hidden card cannot become a way of asking whether a model exists.
        """
        permissions = permissions or get_permissions()
        cards: list[WidgetResult] = []
        hidden: list[dict[str, Any]] = []
        unseen: list[dict[str, Any]] = []

        for index, item in enumerate(self.plan(request)):
            if not item.widget.has_permission(request, permissions):
                unseen.append({"index": index, "position": index + 1})
                continue
            entry = item.entry
            if not entry.get("show", True):
                hidden.append(
                    {
                        "index": index,
                        "position": index + 1,
                        "label": item.widget.label,
                        "width": entry.get("width") or item.widget.width,
                    }
                )
                continue
            result = _render_safely(item.widget, request)
            cards.append(
                replace(result, index=index, width=entry.get("width") or result.width)
            )
        return Arrangement(cards=cards, hidden=hidden, unseen=unseen)

    def render(self, request, permissions=None) -> list[WidgetResult]:
        return self.arrange(request, permissions=permissions).cards


def plan_entries(site, request) -> list[dict[str, Any]]:
    """The canonical entry behind each slot, by index — what a POST addresses.

    Rebuilt on the way in rather than transmitted through the form. An index is the
    only thing that has to survive the round trip, so nothing about a card has to come
    back from the browser and be trusted; it is the same reasoning behind
    :func:`vantage.listprefs._kept`.
    """
    dashboard = get_dashboard(site, request)
    arranged = (
        dashboard
        if isinstance(dashboard, ArrangedDashboard)
        else ArrangedDashboard(dashboard, [], site)
    )
    return [{"show": True, **item.entry} for item in arranged.plan(request)]


# ── reading the stored arrangement ──────────────────────────────────────────


def _model():
    """The model class, or ``None`` if asking is not safe yet."""
    from django.apps import apps

    if not apps.ready:
        return None
    try:
        return apps.get_model("vantage", "DashboardLayout")
    except LookupError:  # pragma: no cover - only when the app is not installed
        return None


def _rows(site_name: str, user) -> tuple[Any, Any] | None:
    """Both rows that can apply, in one query, or ``None`` if the table cannot answer.

    A missing table resolves to "the code is the whole truth", silently. A layer that
    raised when its own table was absent would make the migration that creates it
    impossible to run, and would take the landing page down with it.
    """
    from django.db.models import Q

    model = _model()
    if model is None:
        return None

    scoped = getattr(user, "is_authenticated", False) and getattr(user, "pk", None) is not None
    try:
        queryset = model.objects.filter(site_name=site_name)
        queryset = (
            queryset.filter(Q(user__isnull=True) | Q(user=user.pk))
            if scoped
            else queryset.filter(user__isnull=True)
        )
        rows = list(queryset)
    except (OperationalError, ProgrammingError):
        return None
    except RuntimeError as exc:
        # pytest-django blocks the connection for tests that did not ask for a
        # database, and rendering a dashboard is not a reason to fail one. Narrow on
        # the blocker's own message so a real RuntimeError still surfaces.
        if "Database access not allowed" not in str(exc):
            raise
        return None

    return (
        next((row for row in rows if row.user_id is not None), None),
        next((row for row in rows if row.user_id is None), None),
    )


@dataclass(frozen=True)
class _StoredLayout:
    """A row's two halves, read together: what to draw, and what existed when it was
    last written. Bundled so a single cached read serves both."""

    cards: list[dict[str, Any]]
    known_keys: frozenset[str]


def stored_layout(site=None, request=None) -> list[dict[str, Any]] | None:
    """The arrangement in force, or ``None`` to let the code decide.

    The cascade in one function: the person's own row, then the row everybody shares,
    then nothing. Whole rows rather than merged fields — unlike a list page, where the
    columns and the filters are independent settings, a dashboard layout is one
    indivisible thing, and interleaving two people's card orders would produce a page
    neither of them arranged.
    """
    bundle = _stored_bundle(site, request)
    return None if bundle is None else bundle.cards


def _stored_bundle(site, request) -> _StoredLayout | None:
    """:func:`stored_layout`'s cards, plus the ``known_keys`` snapshot beside them.

    Cached on the request under the same attribute :func:`stored_layout` always used,
    so the two never disagree about which row they came from and a request that reads
    both pays for the query once.
    """
    if request is None:
        return None

    cached = getattr(request, _LAYOUT_ATTR, _MISSING)
    if cached is not _MISSING:
        return cached

    bundle = _read_layout(site, request)
    try:
        setattr(request, _LAYOUT_ATTR, bundle)
    except AttributeError:  # pragma: no cover - an exotic request object
        pass
    return bundle


def _read_layout(site, request) -> _StoredLayout | None:
    from vantage.sites import site as default_site

    site = site or default_site
    rows = _rows(site.name, getattr(request, "user", None))
    if rows is None:
        return None
    for row in rows:
        if row is None or row.cards is None:
            continue
        cleaned = clean_cards(row.cards, site, request)
        if cleaned is not None:
            return _StoredLayout(cards=cleaned, known_keys=frozenset(row.known_keys or ()))
    return None


def row_for(site, user) -> Any:
    """The stored row for one scope, or ``None``. ``user=None`` is the site default."""
    model = _model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return None
    if user is None:
        return model.objects.filter(site_name=site.name, user__isnull=True).first()
    return model.objects.filter(site_name=site.name, user=user).first()


# ── writing ─────────────────────────────────────────────────────────────────


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _getlist(data, name: str) -> list:
    getlist = getattr(data, "getlist", None)
    if getlist is not None:
        return list(getlist(name))
    value = data.get(name)
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def posted_layout(site, request, data) -> list[dict[str, Any]]:
    """What the editor's form is allowed to store, as a validated arrangement.

    The POST body is treated as hostile, because it is: a column name from here goes on
    to become part of an aggregate on every later render of the page. Every value is
    re-validated against the same :class:`~vantage.listprefs.Options` that drew the
    control it came from, and anything else is dropped without comment.
    """
    entries = plan_entries(site, request)
    kept = {index for index in (_as_int(raw) for raw in _getlist(data, "keep"))
            if index is not None and 0 <= index < len(entries)}

    rows: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if index in kept:
            row = {**entry, "show": True}
            width = _clean_width(data.get(f"width__{index}"))
            if width:
                row["width"] = width
        elif entry.get("card") == DECLARED:
            # A declared card is never deleted, only switched off: the code still
            # declares it, and an absence would simply be re-appended on the next read.
            row = {**entry, "show": False}
        else:
            # A card somebody built in the UI and then unticked is gone. It is theirs.
            continue
        rows.append((_position(data, index, len(entries)), index, row))

    rows.sort(key=lambda row: (row[0], row[1]))
    layout = [row for _position_, _index_, row in rows]

    added = _added_card(site, request, data)
    if added is not None:
        layout.append(added)

    return clean_cards(layout, site, request) or []


def _position(data, index: int, fallback: int) -> int:
    """The number the order box carries, or "last" when it is missing or unreadable.

    The number boxes are the accessible, scriptless way to reorder and they are what
    the server reads; dragging only rewrites them, so the two can never disagree about
    the order.
    """
    value = _as_int(data.get(f"order__{index}"))
    return fallback + 1 if value is None else value


def _added_card(site, request, data) -> dict[str, Any] | None:
    """The one card the "add" form describes, or ``None`` when it was left alone.

    The column, date and grouping pickers list every model at once, each option
    prefixed with the model it belongs to, so the whole form is one round trip with no
    scripting. The prefix is checked here and the bare name is checked again against
    the chosen panel's own options, so picking Orders and a column belonging to
    Products produces no card rather than a query across two tables.
    """
    label = data.get("add_model")
    if not label:
        return None

    shape = data.get("add_shape")
    mode = data.get("add_mode")
    target = data.get("add_date") if mode == "timeseries" else data.get("add_group")
    return clean_card(
        {
            "card": shape,
            "model": label,
            "aggregate": data.get("add_aggregate"),
            "field": _scoped(data.get("add_field"), label),
            "mode": mode,
            "target": _scoped(target, label),
            "kind": data.get("add_kind"),
            "label": data.get("add_label"),
            "width": data.get("add_width"),
            "filter": data.get("add_filter"),
        },
        site,
        request,
    )


def _scoped(value: Any, label: str) -> str:
    """``"shop.order:total"`` narrowed to ``"total"``, but only for the chosen model."""
    if not isinstance(value, str) or ":" not in value:
        return ""
    prefix, _, name = value.partition(":")
    return name if prefix == label else ""


def store_layout(site, request, cards: list[dict[str, Any]], *, user) -> None:
    """Write an arrangement to one scope. ``user=None`` writes the site-wide default."""
    model = _model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return

    row = row_for(site, user) or model(site_name=site.name, user=user)
    row.cards = cards
    # The code's own declared keys, right now — always the *code's* dashboard, never a
    # previously stored arrangement, because "known" means "existed in Python", and an
    # arrangement's own opinion about which cards to show is exactly what this snapshot
    # exists to be compared against later. See ``ArrangedDashboard.plan``.
    row.known_keys = sorted(code_dashboard(site).get_declared_keys(request))
    _persist(row, request)


def reset_layout(site, request, *, user) -> None:
    """Clear one scope back to what the code declares.

    Reset writes ``NULL``, not today's resolved layout: the point of the button is that
    the project's Python becomes the answer again *and stays* the answer when the
    project's Python changes.
    """
    row = row_for(site, user)
    if row is None:
        return
    row.cards = None
    _persist(row, request)


def _persist(row, request) -> None:
    """Save the row, or delete it once it has stopped saying anything.

    The request's cached layout is dropped either way: it was filled while validating
    this very POST, and a handler that went on to render rather than redirect would
    otherwise draw the arrangement as it was a moment before the save.
    """
    if row.is_empty():
        if row.pk:
            row.delete()
    else:
        user = getattr(request, "user", None)
        row.updated_by = user if getattr(user, "is_authenticated", False) else None
        row.save()

    try:
        delattr(request, _LAYOUT_ATTR)
    except AttributeError:
        pass


def may_set_site_default(request) -> bool:
    """Whether this person may arrange the dashboard everybody else opens on.

    Deliberately the same gate as the list-page rail's, read from the same
    ``VANTAGE["LIST_PREFS_PERMISSION"]``: both answer one question — may this person
    save a UI arrangement *for everybody* — and two knobs for one question is two
    chances to grant the wrong one. Superusers only until it is set.
    """
    from vantage import listprefs

    return listprefs.may_set_site_default(request)


# ── the editor's view model ─────────────────────────────────────────────────


def arranging(request) -> bool:
    """Whether this request asked for the editor."""
    params = getattr(request, "GET", None)
    return bool(params and params.get(ARRANGE_PARAM))


def _arrange_url(request, *, on: bool) -> str:
    """This page with the editor switched on or off, keeping everything else."""
    path = request.path if hasattr(request, "path") else ""
    params = getattr(request, "GET", None)
    query = params.copy() if params is not None else QueryDict(mutable=True)
    if on:
        query[ARRANGE_PARAM] = "1"
    else:
        query.pop(ARRANGE_PARAM, None)
    encoded = query.urlencode()
    return f"{path}?{encoded}" if encoded else path


def editor_context(site, request, arrangement: Arrangement | None, *, stored: bool) -> dict:
    """Everything ``partials/card_editor.html`` renders, resolved in Python.

    Every list on it is an allowlist, and it is the same one that will validate the
    POST — so the editor can never offer a control whose value would then be rejected,
    and the models on offer are the models this person may already open.
    """
    from vantage import listprefs

    permissions = get_permissions()
    # Permission first, and there is no cap after it. The order is the whole lesson of
    # the bug this file's history records: a cap applied to a registry slice before the
    # permission filter turns a display limit into an access rule.
    panels = permissions.visible_panels(request, list(site.panels()))

    models = []
    for panel in panels:
        opts = listprefs.options(panel, request)
        key = resource_key(panel)
        models.append(
            {
                "value": key,
                "label": panel.get_label_plural(),
                "measures": _paths(panel, key, opts.measures),
                "dates": _paths(panel, key, opts.dates),
                "groups": _paths(panel, key, opts.groups),
                "list_url": _list_url(panel),
            }
        )

    try:
        action = reverse("vantage:dashboard-arrange")
    except NoReverseMatch:  # pragma: no cover - only before the route is mounted
        action = None

    return {
        "action": action,
        "next": request.get_full_path() if hasattr(request, "get_full_path") else "",
        "done_url": _arrange_url(request, on=False),
        "models": models,
        "aggregates": listprefs.AGGREGATES,
        "chart_modes": listprefs.CHART_MODES,
        "chart_kinds": sorted({k for kinds in listprefs.CHART_KINDS.values() for k in kinds}),
        "widths": range(1, GRID_COLUMNS + 1),
        "shapes": CARD_SHAPES,
        "hidden": arrangement.hidden if arrangement else [],
        "unseen": arrangement.unseen if arrangement else [],
        "may_set_default": may_set_site_default(request),
        "stored": stored,
    }


def _paths(panel, key: str, names: tuple[str, ...]) -> list[dict[str, str]]:
    """Field options for one model, each carrying the model it belongs to."""
    from vantage import listprefs

    return [
        {"value": f"{key}:{name}", "label": listprefs._label(panel.model, name)}
        for name in names
    ]


# ── the write end ───────────────────────────────────────────────────────────


class ArrangeDashboardView(SiteView):
    """Store or clear the dashboard arrangement. POST only; renders nothing.

    The editor is drawn by the dashboard it belongs to; this exists to change a row and
    send the browser back. A GET is answered with a redirect rather than a 405, because
    a URL that ends up in somebody's history should not greet them with an error page.
    """

    http_method_names = ["get", "post"]

    def get(self, request, *args, **kwargs):
        return self.back(request)

    def post(self, request, *args, **kwargs):
        action, scope = self.intent(request)
        user = self.scope_user(request, scope)

        # One finished sentence per outcome. "for everyone" and "for you" are not
        # interchangeable fragments to slot into a shared sentence: the scope can change
        # the verb, the word order or the case of what comes before it.
        if action == "reset":
            reset_layout(self.site, request, user=user)
            message = (
                _("Dashboard reset to what the code declares, for everyone.")
                if user is None
                else _("Dashboard reset to what the code declares, for you.")
            )
        else:
            cards = posted_layout(self.site, request, request.POST)
            store_layout(self.site, request, cards, user=user)
            message = (
                _("Dashboard saved for everyone.")
                if user is None
                else _("Dashboard saved for you.")
            )

        messages.success(request, message)
        return self.back(request)

    @staticmethod
    def intent(request) -> tuple[str, str]:
        """What was asked for, from the one button that was pressed.

        Four buttons share one name — ``save:user``, ``save:site``, ``reset:user``,
        ``reset:site`` — because only the pressed submit contributes a value, and one
        parameter is one place for the permission check to read. Missing or
        unrecognised falls to the least surprising pair: save, for the person doing it.
        """
        action, _sep, scope = request.POST.get("do", "").partition(":")
        return (
            action if action in ("save", "reset") else "save",
            scope if scope in ("user", "site") else "user",
        )

    @staticmethod
    def scope_user(request, scope: str):
        """The row this POST may write: the person, or ``None`` for everybody.

        A request for the site-wide row without the permission is refused rather than
        quietly downgraded to a personal save — a silent downgrade looks exactly like
        success, and the person would never know their colleagues had not been changed.
        """
        if scope != "site":
            return request.user
        if not may_set_site_default(request):
            raise PermissionDenied(
                _("You do not have permission to change the dashboard for everyone.")
            )
        return None

    def back(self, request):
        """Back to the dashboard, keeping the editor open if it was.

        ``next`` comes from the browser, so it is checked before it is followed: an
        unchecked redirect target on an authenticated page is an open redirect that
        borrows the panel's credibility.
        """
        target = request.POST.get("next") or request.GET.get("next") or ""
        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return HttpResponseRedirect(target)
        return HttpResponseRedirect(reverse("vantage:dashboard"))


def build_urls(site) -> list:
    """The one route the editor needs, under the ``_/`` prefix :mod:`vantage.urls`
    already reserves for panel machinery so it can never collide with an app label."""
    from django.urls import path

    return [
        path(
            "_/dashboard/",
            ArrangeDashboardView.as_view(site=site),
            name="dashboard-arrange",
        )
    ]
