"""Per-list customisation: the third layer under one model's list page.

:mod:`vantage.panels` resolves every attribute independently — setting ``list_display``
overrides the columns and nothing else — and falls back to inference for the rest. This
module adds a database layer *above* that, with the same rule::

    per-user row  >  site-wide row  >  panels.py  >  inference

It is deliberately the same shape as :mod:`vantage.siteconfig`, one level down. That
module puts a row underneath ``vantage_settings.X``; this one puts a row underneath
``panel.get_list_display(request)``, so a call site that already asks the panel picks up
a UI override without knowing a database exists. Four properties are load-bearing, and
three of them are copied from ``siteconfig`` on purpose:

**Lazy.** Nothing reads the database until a list page is actually rendered, and never
during app loading, ``check``, ``makemigrations`` or ``scaffold``.

**Failure-tolerant.** A missing table resolves to the code declaration, silently. A
preference layer that raises when its own table is absent makes the migration that
would create it impossible to run, and takes every list page down with it.

**Cached per request.** One query answers both rows for one list, once, however many
getters ask. Without that, a page with six calls to ``get_list_display`` would pay six
queries and query-count assertions would depend on call order.

**The panel is the ceiling.** Everything arriving from the form — every field name,
every aggregate, every chart target — is checked against what the panel *currently*
exposes, both when it is stored and again when it is read. Twice, because the two
checks answer different questions: the first rejects a hostile POST, the second stops a
preference stored last month from resurrecting a column that has since been removed
from ``list_display``. A stored preference can hide, reorder and re-aggregate what the
panel already offers. It can never widen it.

The candidate lists come from the panel with this layer switched off (see
:func:`options`), which is what makes hiding a column reversible: if the choices were
drawn from the *effective* configuration, hiding a column would remove it from the menu
that could bring it back.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings as django_settings
from django.db.models import BooleanField, Count, DateField, Q
from django.db.utils import OperationalError, ProgrammingError
from django.utils.translation import gettext_lazy as _

from vantage.introspection import resolve_path
from vantage.stats import Chart, Stat, compute_panel
from vantage.summary import NUMERIC, SUMMARY_FUNCTIONS, compute_summary, infer_summaries

__all__ = [
    "AGGREGATES",
    "CHART_MODES",
    "MAX_COLUMN_WIDTH",
    "MIN_COLUMN_WIDTH",
    "NO_AGGREGATE",
    "SECTIONS",
    "SECTION_COLUMNS",
    "ListSpec",
    "Options",
    "analytics",
    "clean_section",
    "may_set_site_default",
    "options",
    "override",
    "panel_context",
    "reset_section",
    "resolve",
    "resource_key",
    "store_section",
    "summary",
]

#: The settings key that widens "save as the default for everyone" beyond superusers.
#: Read defensively so the feature works whether or not ``conf.DEFAULTS`` knows it yet.
PERMISSION_SETTING = "LIST_PREFS_PERMISSION"

#: The rail's sections, and the row columns each one owns. Save and reset both act on
#: exactly this, so a "Reset filters" button can never clear something the filters card
#: does not show — the mistake :mod:`vantage.settingsview` avoids the same way.
SECTION_COLUMNS: dict[str, tuple[str, ...]] = {
    "columns": ("columns", "per_page", "column_widths", "pinned_columns"),
    "search": ("search_enabled", "search_fields"),
    "filters": ("filters_enabled", "filter_fields", "facet_counts"),
    "stats": ("stats_enabled", "stat_keys", "extra_stats"),
    "charts": ("charts_enabled", "chart_keys", "extra_charts"),
    "summary": ("summary_enabled", "summary_aggregates"),
}

SECTIONS: tuple[str, ...] = tuple(SECTION_COLUMNS)

#: The aggregate vocabulary, borrowed wholesale from the summary bar rather than
#: invented again. One list of aggregate names for the footer, the stat strip and the
#: charts means a user who has learned one has learned all three.
AGGREGATES: tuple[str, ...] = tuple(SUMMARY_FUNCTIONS)

#: The word a UI-built card is labelled with when nobody typed a label of their own.
#: Spelled out here rather than capitalising the aggregate's own name: that name is an
#: identifier, and capitalising an identifier produces an English word wearing English
#: capitalisation in every language the panel is read in.
AGGREGATE_LABELS: dict[str, str] = {
    "sum": _("Sum"),
    "avg": _("Avg"),
    "min": _("Min"),
    "max": _("Max"),
    "count": _("Count"),
}

#: A real choice in the summary bar: "do not aggregate this column", as distinct from
#: "no opinion", which is what an absent key means.
NO_AGGREGATE = "none"

CHART_MODES: tuple[str, ...] = ("timeseries", "breakdown")

#: What the chart renderer can actually draw, per mode.
CHART_KINDS: dict[str, tuple[str, ...]] = {
    "timeseries": ("line", "bar"),
    "breakdown": ("bar", "donut"),
}

#: Ceilings on what one person can add from the UI. Not a security boundary — every
#: added stat is a validated aggregate over an allowed column — but a page is a page,
#: and forty stat cards is a denial of service against the reader.
MAX_EXTRA_STATS = 6
MAX_EXTRA_CHARTS = 4
MAX_LABEL = 60
MAX_PER_PAGE = 500

#: Pixel bounds on a dragged column. Below the minimum a column is not a column any
#: more, it is a sliver with a sort arrow in it; above the maximum one wide column can
#: push every other one off the visible table, which is the opposite of what resizing
#: is for. Both ends are enforced again in :func:`_clean_widths` on every read, not
#: only when a value is written — a bound moved in a later release must still apply to
#: a width somebody stored last year.
MIN_COLUMN_WIDTH = 60
MAX_COLUMN_WIDTH = 640


def resource_key(panel_or_model: Any) -> str:
    """``"shop.order"`` — the text key a stored row is found by.

    Deliberately ``Options.label_lower`` and not a ``ContentType`` id: an unregistered
    or renamed model must leave an inert row behind, not a cascade.
    """
    model = getattr(panel_or_model, "model", panel_or_model)
    return model._meta.label_lower


# ── asking the panel what the code declares ─────────────────────────────────

_local = threading.local()


@contextmanager
def _code_only():
    """Ask the panel what *it* declares, with this layer switched off.

    Both the baseline of the cascade and the menu of choices are the panel's own
    answer, and both are obtained by calling the very getters this module overrides.
    Without a re-entrancy switch that call would resolve preferences again, forever.
    Thread-local because two requests resolve concurrently under any threaded server.
    """
    depth = getattr(_local, "depth", 0)
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth = depth


def _suspended() -> bool:
    return bool(getattr(_local, "depth", 0))


# ── what the panel exposes ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Options:
    """Everything the rail may offer, and therefore everything a POST may name.

    One object, built once per render, used both to draw the form and to validate what
    comes back from it. Two lists would eventually disagree, and the day they did the
    disagreement would be a hole rather than a bug.
    """

    columns: tuple[str, ...] = ()
    filter_fields: tuple[str, ...] = ()
    search_fields: tuple[str, ...] = ()
    per_page: int = 50
    #: Local, concrete, numeric columns — the only things a UI-built stat may aggregate.
    measures: tuple[str, ...] = ()
    #: Local date/datetime columns, for a timeseries.
    dates: tuple[str, ...] = ()
    #: Fields with a bounded, labelled set of values, for a breakdown.
    groups: tuple[str, ...] = ()
    stat_keys: tuple[str, ...] = ()
    chart_keys: tuple[str, ...] = ()

    @property
    def summary_columns(self) -> tuple[str, ...]:
        """Columns the footer can aggregate: the numeric ones, and only those.

        Identical to :attr:`measures` by construction rather than by coincidence —
        :func:`vantage.summary.infer_summaries` will not aggregate anything else, so
        offering a control for a text column would be a control with no effect.
        """
        return self.measures


def _local_field(model: type, path: str):
    """The field at ``path`` when it is one plain local column, else ``None``.

    One hop and no relation is the whole test, and it is the same one
    :func:`vantage.summary.infer_summaries` applies. Aggregating across a join
    multiplies values through the fan-out, and a stat card is the last place anyone
    would look for the cause.
    """
    hops = resolve_path(model, path)
    if len(hops) != 1 or hops[0].is_relation:
        return None
    return hops[0].field


def _has_labels(model: type, path: str) -> bool:
    """Whether a breakdown of ``path`` would come back with readable segment labels.

    Restricted to choices and booleans on purpose. A breakdown grouped by a foreign key
    renders one segment per primary key with a number for a label, which is not a chart
    anybody can read — and a free-text or continuous column is not low-cardinality at
    all, which is the other half of the same refusal.
    """
    field_obj = _local_field(model, path)
    if field_obj is None:
        return False
    return bool(getattr(field_obj, "choices", None)) or isinstance(field_obj, BooleanField)


def options(panel, request=None) -> Options:
    """The candidate lists, drawn from the panel with preferences switched off.

    Switched off is the point. Drawn from the *effective* configuration, hiding a
    column would also remove it from the menu, and the only way back would be a shell.
    """
    model = panel.model
    with _code_only():
        columns = tuple(dict.fromkeys(panel.get_list_display(request)))
        filter_fields = tuple(dict.fromkeys(panel.get_list_filter(request)))
        search_fields = tuple(dict.fromkeys(panel.get_list_search(request)))
        per_page = panel.get_per_page(request)

    measures = tuple(name for name in columns if isinstance(_local_field(model, name), NUMERIC))
    # Dates and groups are drawn from the filters as well as the columns: a panel that
    # offers a date filter has declared that date as something worth slicing by, and a
    # chart of it is the same declaration read the other way round.
    dates = tuple(
        name
        for name in dict.fromkeys(columns + filter_fields)
        if isinstance(_local_field(model, name), DateField)
    )
    groups = tuple(
        name for name in dict.fromkeys(filter_fields + columns) if _has_labels(model, name)
    )
    return Options(
        columns=columns,
        filter_fields=filter_fields,
        search_fields=search_fields,
        per_page=per_page,
        measures=measures,
        dates=dates,
        groups=groups,
        stat_keys=tuple(stat.key for stat in (panel.list_stats or ())),
        chart_keys=tuple(chart.key for chart in (panel.list_charts or ())),
    )


# ── validation ──────────────────────────────────────────────────────────────
#
# Every function below takes a value of unknown provenance — a form field, or a JSON
# column written months ago against a panel that has since changed — and returns either
# something the panel demonstrably exposes or ``None``. Nothing raises: a preference
# that has stopped making sense must degrade into the code default, because the
# alternative is a 500 on a list page caused by a row nobody remembers writing.


def _clean_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _clean_per_page(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_PER_PAGE else None


def _clean_paths(value: Any, allowed: tuple[str, ...]) -> list[str] | None:
    """The names the panel exposes, in the order asked for, without repeats.

    An empty result is returned as ``None`` rather than as ``[]``. A stored list that
    validates down to nothing is indistinguishable from a corrupt row, and "show no
    columns at all" is not a state worth being able to reach by accident — switching a
    whole section off has its own control.
    """
    if not isinstance(value, list | tuple):
        return None
    kept: list[str] = []
    for item in value:
        if isinstance(item, str) and item in allowed and item not in kept:
            kept.append(item)
    return kept or None


def _clean_widths(value: Any, opts: Options) -> dict[str, int] | None:
    """Column name -> pixel width, every one clamped into range.

    Clamped rather than dropped: a drag that overshoots the edge of the window, or a
    bound tightened in a later release, is a harmless accident. Silently discarding the
    whole entry would make the column jump back to its inferred width with no
    explanation, which reads as a worse bug than an oversized value would have been.
    """
    if not isinstance(value, dict):
        return None
    kept: dict[str, int] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or name not in opts.columns:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        kept[name] = min(MAX_COLUMN_WIDTH, max(MIN_COLUMN_WIDTH, raw))
    return kept or None


def _clean_label(value: Any, fallback: str) -> str:
    """The label to store: what was typed, else the generated one, always plain text.

    The slice is what resolves a lazy fallback into a real ``str``. That matters: this
    value goes into a JSON column, and a translation promise is not something a row can
    hold. It also means a generated label is frozen in the language of whoever created
    the card, which is the same bargain any user-entered label makes.
    """
    text = value.strip() if isinstance(value, str) else ""
    return (text or fallback)[:MAX_LABEL]


def _clean_measure(raw: Any, opts: Options) -> dict[str, str] | None:
    """One ``{aggregate, field, label}`` triple, or ``None``.

    ``count`` is the one aggregate with no field: it counts rows, so it is expressed as
    ``Count("pk")`` and never as a count of whatever column happened to be selected.
    Every other aggregate must name a numeric column the panel displays.
    """
    if not isinstance(raw, dict):
        return None
    aggregate = raw.get("aggregate")
    if aggregate not in AGGREGATES:
        return None

    if aggregate == "count":
        field_name = ""
        fallback = AGGREGATE_LABELS["count"]
    else:
        field_name = raw.get("field")
        if not isinstance(field_name, str) or field_name not in opts.measures:
            return None
        fallback = _("%(aggregate)s of %(field)s") % {
            "aggregate": AGGREGATE_LABELS.get(aggregate, aggregate),
            "field": field_name.replace("_", " "),
        }

    return {
        "aggregate": aggregate,
        "field": field_name,
        "label": _clean_label(raw.get("label"), fallback),
    }


def _clean_extra_stats(value: Any, opts: Options) -> list[dict[str, str]] | None:
    if not isinstance(value, list | tuple):
        return None
    kept: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        entry = _clean_measure(raw, opts)
        if entry is None:
            continue
        identity = (entry["aggregate"], entry["field"])
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(entry)
        if len(kept) >= MAX_EXTRA_STATS:
            break
    return kept or None


def _clean_extra_charts(value: Any, opts: Options) -> list[dict[str, str]] | None:
    if not isinstance(value, list | tuple):
        return None
    kept: list[dict[str, str]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in value:
        entry = _clean_chart(raw, opts)
        if entry is None:
            continue
        identity = (entry["mode"], entry["target"], entry["aggregate"], entry["field"])
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(entry)
        if len(kept) >= MAX_EXTRA_CHARTS:
            break
    return kept or None


def _clean_chart(raw: Any, opts: Options) -> dict[str, str] | None:
    """One UI-built chart: a mode, a target field, and a measure."""
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    if mode not in CHART_MODES:
        return None

    target = raw.get("target")
    allowed = opts.dates if mode == "timeseries" else opts.groups
    if not isinstance(target, str) or target not in allowed:
        return None

    measure = _clean_measure(raw, opts)
    if measure is None:
        return None

    kind = raw.get("kind")
    kinds = CHART_KINDS[mode]
    label = _clean_label(
        raw.get("label"),
        _("%(measure)s by %(target)s")
        % {"measure": measure["label"], "target": target.replace("_", " ")},
    )
    return {
        "mode": mode,
        "target": target,
        "kind": kind if kind in kinds else kinds[0],
        "aggregate": measure["aggregate"],
        "field": measure["field"],
        "label": label,
    }


def _clean_summary(value: Any, opts: Options) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    kept: dict[str, str] = {}
    for column, aggregate in value.items():
        if not isinstance(column, str) or column not in opts.summary_columns:
            continue
        if aggregate == NO_AGGREGATE or aggregate in AGGREGATES:
            kept[column] = aggregate
    return kept or None


#: Column name -> the function that decides whether a stored value may be used.
_CLEANERS = {
    "columns": lambda value, opts: _clean_paths(value, opts.columns),
    "per_page": lambda value, opts: _clean_per_page(value),
    "column_widths": _clean_widths,
    # A pinned column is still a column: the same allowlist that validates "columns"
    # validates which of them may be frozen, and a name the panel has stopped
    # exposing drops out here exactly as it would from the columns list itself.
    "pinned_columns": lambda value, opts: _clean_paths(value, opts.columns),
    "search_enabled": lambda value, opts: _clean_bool(value),
    "search_fields": lambda value, opts: _clean_paths(value, opts.search_fields),
    "filters_enabled": lambda value, opts: _clean_bool(value),
    "filter_fields": lambda value, opts: _clean_paths(value, opts.filter_fields),
    "facet_counts": lambda value, opts: _clean_bool(value),
    "stats_enabled": lambda value, opts: _clean_bool(value),
    "stat_keys": lambda value, opts: _clean_paths(value, opts.stat_keys),
    "extra_stats": _clean_extra_stats,
    "charts_enabled": lambda value, opts: _clean_bool(value),
    "chart_keys": lambda value, opts: _clean_paths(value, opts.chart_keys),
    "extra_charts": _clean_extra_charts,
    "summary_enabled": lambda value, opts: _clean_bool(value),
    "summary_aggregates": _clean_summary,
}


def clean_value(name: str, value: Any, opts: Options) -> Any:
    """One stored or posted value, validated against what the panel exposes."""
    cleaner = _CLEANERS.get(name)
    if cleaner is None or value is None:
        return None
    return cleaner(value, opts)


# ── building the aggregates ─────────────────────────────────────────────────


def _expression(entry: dict[str, str]):
    """The ORM aggregate behind one UI-built measure.

    ``SUMMARY_FUNCTIONS`` again: there is one aggregation vocabulary in the package and
    this is not the place to start a second one.
    """
    if entry["aggregate"] == "count":
        return Count("pk")
    return SUMMARY_FUNCTIONS[entry["aggregate"]](entry["field"])


def _stat(entry: dict[str, str]) -> Stat:
    """A UI-built stat, as an ordinary :class:`~vantage.stats.Stat`.

    Given an explicit key rather than letting the label slug into one, so renaming a
    card cannot collide with a declared stat's key and quietly replace it.
    """
    return Stat(
        entry["label"],
        _expression(entry),
        key=f"ui-{entry['aggregate']}-{entry['field'] or 'rows'}",
    )


def _chart(entry: dict[str, str]) -> Chart:
    key = f"ui-{entry['mode']}-{entry['target']}-{entry['aggregate']}-{entry['field'] or 'rows'}"
    if entry["mode"] == "timeseries":
        return Chart.timeseries(
            entry["label"],
            date=entry["target"],
            value=_expression(entry),
            kind=entry["kind"],
            key=key,
        )
    return Chart.breakdown(
        entry["label"],
        group_by=entry["target"],
        value=_expression(entry),
        kind=entry["kind"],
        key=key,
    )


# ── the resolved configuration ──────────────────────────────────────────────


@dataclass(frozen=True)
class ListSpec:
    """One list page's effective configuration, and where each part of it came from."""

    key: str
    options: Options

    columns: list[str] = field(default_factory=list)
    per_page: int = 50
    #: Column name -> pixel width, already bounds-checked and already filtered down to
    #: columns that are actually in :attr:`columns` — a width stored against a column
    #: that has since been hidden or removed would otherwise linger, unused but
    #: unnoticed, until the day the column comes back with a stale size.
    column_widths: dict[str, int] = field(default_factory=dict)
    #: Frozen columns, in :attr:`columns` order rather than the order they were pinned
    #: in — that is the order the sticky offsets in the template accumulate over, and
    #: it is also what keeps a hidden-then-reshown pinned column from jumping to the
    #: front of the frozen group.
    pinned_columns: list[str] = field(default_factory=list)

    search_enabled: bool = True
    search_fields: list[str] = field(default_factory=list)

    filters_enabled: bool = True
    filter_fields: list[str] = field(default_factory=list)
    #: ``None`` leaves counts to ``?_facets``, which is where the code left them.
    facet_counts: bool | None = None

    stats_enabled: bool = True
    stats: tuple[Stat, ...] = ()
    charts_enabled: bool = True
    charts: tuple[Chart, ...] = ()

    summary_enabled: bool = True
    #: Ready for :func:`vantage.summary.compute_summary`: column -> aggregate name.
    summary_aggregates: dict[str, str] = field(default_factory=dict)

    #: The stored, validated values, keyed by row column. Only the ones that survived
    #: validation appear, which is exactly what :func:`override` is allowed to apply.
    stored: dict[str, Any] = field(default_factory=dict)
    #: Row column -> ``"user"`` or ``"site"``. Absent means the code decided.
    sources: dict[str, str] = field(default_factory=dict)

    def source(self, name: str) -> str:
        return self.sources.get(name, "code")

    def is_overridden(self, section: str) -> bool:
        """Whether this section has anything stored at all — what Reset acts on."""
        return any(name in self.stored for name in SECTION_COLUMNS[section])

    def facets_on(self, request) -> bool:
        """Whether to compute facet counts for this render.

        Tri-state: a stored ``True``/``False`` decides, and ``None`` hands the question
        back to ``?_facets`` so a list nobody has tuned behaves exactly as before.
        """
        if self.facet_counts is not None:
            return self.facet_counts

        from vantage.filters import facets_requested

        return facets_requested(request)

    def override_for(self, getter: str) -> Any:
        """What :class:`~vantage.panels.ModelPanel` should return instead, or ``None``.

        ``None`` means "nothing is stored", which is what makes the getter fall through
        to its own declaration and then to inference — per attribute, so overriding the
        columns still leaves the filters inferred.
        """
        if getter == "list_display":
            return self.stored.get("columns")
        if getter == "per_page":
            return self.stored.get("per_page")
        if getter == "list_filter":
            if self.stored.get("filters_enabled") is False:
                return []
            return self.stored.get("filter_fields")
        if getter == "list_search":
            if self.stored.get("search_enabled") is False:
                return []
            return self.stored.get("search_fields")
        return None


# ── resolution ──────────────────────────────────────────────────────────────

_CACHE_ATTR = "_vantage_list_prefs"


def override(panel, request, getter: str) -> Any:
    """The stored override for one panel getter, or ``None`` to let the code decide.

    This is the whole of the package's contact surface with ``panels.py``. Returning
    ``None`` — not an empty list, not a default — is what preserves the per-attribute
    fallthrough the panel class promises.

    **It reads an already-resolved spec and never goes to the database itself.** The
    page that owns the rail is the page that turns it on, by calling :func:`resolve`
    once before it asks the panel anything. Two consequences, both wanted:

    * A panel getter is free again everywhere else. ``check``, ``scaffold``, the
      registry and a dashboard's table card all ask a panel questions, and none of them
      should pay a query — or silently answer differently — because somebody hid a
      column on a list page yesterday.
    * A dashboard card over Orders keeps showing what the dashboard declares. Preferences
      are scoped to the list page they are edited on, which is the only place they can
      be seen and undone.
    """
    if request is None or _suspended():
        return None
    cache = getattr(request, _CACHE_ATTR, None)
    spec = cache.get(resource_key(panel)) if cache else None
    return spec.override_for(getter) if spec is not None else None


def resolve(panel, request=None) -> ListSpec:
    """The effective configuration for this list, cached for the request.

    The cascade in one function: read both rows that can apply, take the user's value
    where there is one, the site-wide value where there is not, and the panel's own
    answer where there is neither.

    Calling this is also what arms :func:`override` for the rest of the request, so a
    view calls it once, early, before it asks the panel anything — see
    ``ListView.get``. One query, whatever else the page goes on to ask.
    """
    key = resource_key(panel)
    cache = _cache(request)
    if cache is not None and key in cache:
        return cache[key]

    spec = _build(panel, request, key)
    if cache is not None:
        cache[key] = spec
    return spec


def _cache(request) -> dict[str, ListSpec] | None:
    if request is None:
        return None
    cache = getattr(request, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, _CACHE_ATTR, cache)
        except AttributeError:  # pragma: no cover - an exotic request object
            return None
    return cache


def _build(panel, request, key: str) -> ListSpec:
    opts = options(panel, request)
    # No request, no database: there is no user to read a row for, nowhere to cache the
    # answer, and the callers that pass nothing — ``check``, ``scaffold``, the registry
    # — are all asking what the code says. Same rule as :func:`override`.
    rows = _rows(key, getattr(request, "user", None)) if request is not None else None
    user_row, site_row = rows if rows is not None else (None, None)

    stored: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in SETTING_NAMES:
        for row, origin in ((user_row, "user"), (site_row, "site")):
            if row is None:
                continue
            raw = getattr(row, name, None)
            if raw is None:
                continue
            # The user's value wins *for this attribute only*: a personal choice about
            # columns must not drag the site's filters along with it, and must not be
            # displaced by a site default somebody else saved afterwards.
            value = clean_value(name, raw, opts)
            if value is not None:
                stored[name] = value
                sources[name] = origin
            break

    return _assemble(panel, opts, key, stored, sources)


#: Every column this layer reads, in the order the sections declare them.
SETTING_NAMES: tuple[str, ...] = tuple(name for names in SECTION_COLUMNS.values() for name in names)


def _assemble(panel, opts: Options, key: str, stored: dict, sources: dict) -> ListSpec:
    columns = stored.get("columns") or list(opts.columns)

    # Re-filtered against the *effective* columns, one step tighter than the
    # panel-wide allowlist ``clean_value`` already applied: a width or a pin stored
    # against a column this same row has since hidden must not linger unused, and a
    # pinned name that no longer appears in ``columns`` at all cannot render as a
    # frozen ``<th>`` that was never drawn. ``columns`` order, not the stored pin
    # order, so the template's accumulated sticky offset is deterministic.
    column_widths = {
        name: width
        for name, width in (stored.get("column_widths") or {}).items()
        if name in columns
    }
    pinned_columns = [name for name in columns if name in (stored.get("pinned_columns") or ())]

    search_enabled = stored.get("search_enabled", True)
    search_fields = (
        list(stored.get("search_fields") or opts.search_fields) if search_enabled else []
    )

    filters_enabled = stored.get("filters_enabled", True)
    filter_fields = (
        list(stored.get("filter_fields") or opts.filter_fields) if filters_enabled else []
    )

    stats_enabled = stored.get("stats_enabled", True)
    charts_enabled = stored.get("charts_enabled", True)
    summary_enabled = stored.get("summary_enabled", True)

    stats: tuple[Stat, ...] = ()
    if stats_enabled:
        declared = {stat.key: stat for stat in (panel.list_stats or ())}
        chosen = stored.get("stat_keys")
        kept = [declared[k] for k in chosen if k in declared] if chosen else list(declared.values())
        stats = tuple(kept + [_stat(entry) for entry in stored.get("extra_stats") or []])

    charts: tuple[Chart, ...] = ()
    if charts_enabled:
        declared_charts = {chart.key: chart for chart in (panel.list_charts or ())}
        chosen = stored.get("chart_keys")
        kept_charts = (
            [declared_charts[k] for k in chosen if k in declared_charts]
            if chosen
            else list(declared_charts.values())
        )
        charts = tuple(kept_charts + [_chart(entry) for entry in stored.get("extra_charts") or []])

    summary_aggregates: dict[str, str] = {}
    if summary_enabled:
        # Start from inference, then apply the per-column choices on top. Reusing
        # ``infer_summaries`` keeps ``list_aggregates`` in the cascade: a column the
        # panel author pinned to ``avg`` stays on ``avg`` until somebody says otherwise
        # in the UI, and "none" is how they say to stop aggregating it at all.
        summary_aggregates = infer_summaries(panel, columns)
        for column, aggregate in (stored.get("summary_aggregates") or {}).items():
            if column not in columns:
                continue
            if aggregate == NO_AGGREGATE:
                summary_aggregates.pop(column, None)
            else:
                summary_aggregates[column] = aggregate

    return ListSpec(
        key=key,
        options=opts,
        columns=columns,
        per_page=stored.get("per_page") or opts.per_page,
        column_widths=column_widths,
        pinned_columns=pinned_columns,
        search_enabled=bool(search_enabled),
        search_fields=search_fields,
        filters_enabled=bool(filters_enabled),
        filter_fields=filter_fields,
        facet_counts=stored.get("facet_counts"),
        stats_enabled=bool(stats_enabled),
        stats=stats,
        charts_enabled=bool(charts_enabled),
        charts=charts,
        summary_enabled=bool(summary_enabled),
        summary_aggregates=summary_aggregates,
        stored=stored,
        sources=sources,
    )


# ── reading the rows ────────────────────────────────────────────────────────


def _model():
    """The model class, or ``None`` if asking is not safe yet."""
    from django.apps import apps

    if not apps.ready:
        return None
    try:
        return apps.get_model("vantage", "ListPreference")
    except LookupError:  # pragma: no cover - only when the app is not installed
        return None


def _rows(key: str, user) -> tuple[Any, Any] | None:
    """Both rows that can apply to this list, in one query.

    ``None`` means the database could not answer — no table yet, no connection, or a
    test that never asked for one. Every caller reads that as "the code is the whole
    truth for now", which is the only answer that lets ``migrate`` run at all.
    """
    model = _model()
    if model is None:
        return None

    scoped = getattr(user, "is_authenticated", False) and getattr(user, "pk", None) is not None
    try:
        queryset = model.objects.filter(resource_key=key)
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
        # database, and rendering a list page's preferences is not a reason to fail
        # one. Narrow on the blocker's own message so a real RuntimeError still
        # surfaces — the same bargain :mod:`vantage.siteconfig` strikes.
        if "Database access not allowed" not in str(exc):
            raise
        return None

    return (
        next((row for row in rows if row.user_id is not None), None),
        next((row for row in rows if row.user_id is None), None),
    )


def row_for(panel, user) -> Any:
    """The stored row for one scope, or ``None``. ``user=None`` is the site default."""
    model = _model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return None
    key = resource_key(panel)
    if user is None:
        return model.objects.filter(resource_key=key, user__isnull=True).first()
    return model.objects.filter(resource_key=key, user=user).first()


# ── writing ─────────────────────────────────────────────────────────────────


def clean_section(panel, request, section: str, data) -> dict[str, Any]:
    """What a posted section is allowed to store, as row columns.

    The POST body is treated as hostile, because it is: a field name from here goes on
    to become part of a query. Nothing is trusted for being well-formed, well-typed or
    previously valid — every value goes through the same validators a stored row does,
    against the same :class:`Options`.
    """
    if section not in SECTION_COLUMNS:
        raise ValueError(f"{section!r} is not a section of the list preferences rail.")

    opts = options(panel, request)
    spec = resolve(panel, request)
    raw = _posted(section, data, opts, spec)
    return {name: clean_value(name, raw.get(name), opts) for name in SECTION_COLUMNS[section]}


def _posted(section: str, data, opts: Options, spec: ListSpec) -> dict[str, Any]:
    """Turn one section's form fields into row-column values, still unvalidated."""
    getlist = getattr(data, "getlist", None) or (lambda name: _as_list(data.get(name)))

    if section == "columns":
        return {
            "columns": _ordered(getlist("columns"), data),
            "per_page": _as_int(data.get("per_page")),
            "column_widths": _widths(opts.columns, data),
            "pinned_columns": getlist("pinned_columns") or None,
        }

    if section == "search":
        enabled = _checkbox(data, "search_enabled")
        return {
            "search_enabled": enabled,
            "search_fields": _ordered(getlist("search_fields"), data) if enabled else None,
        }

    if section == "filters":
        enabled = _checkbox(data, "filters_enabled")
        return {
            "filters_enabled": enabled,
            "filter_fields": _ordered(getlist("filter_fields"), data) if enabled else None,
            "facet_counts": _tristate(data.get("facet_counts")),
        }

    if section == "stats":
        return {
            "stats_enabled": _checkbox(data, "stats_enabled"),
            "stat_keys": getlist("stat_keys") or None,
            "extra_stats": _kept(spec.stored.get("extra_stats"), getlist("extra_stats"))
            + _added_stat(data),
        }

    if section == "charts":
        return {
            "charts_enabled": _checkbox(data, "charts_enabled"),
            "chart_keys": getlist("chart_keys") or None,
            "extra_charts": _kept(spec.stored.get("extra_charts"), getlist("extra_charts"))
            + _added_chart(data),
        }

    return {
        "summary_enabled": _checkbox(data, "summary_enabled"),
        "summary_aggregates": {
            column: data.get(f"summary__{column}")
            for column in opts.summary_columns
            if data.get(f"summary__{column}")
        }
        or None,
    }


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def _checkbox(data, name: str) -> bool:
    """A checkbox is only ever ``True`` or ``False`` here, never ``None``.

    An unticked box sends nothing, so "off" and "the field was not in the form" are the
    same POST. The section form always contains the box, so absence means off — and
    storing ``False`` rather than ``None`` is what makes switching a section off an
    override rather than a no-op.
    """
    return name in data


def _tristate(value) -> bool | None:
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _widths(names, data) -> dict[str, int] | None:
    """``width__<name>`` inputs, gathered by every column the panel could show.

    Scanning every option rather than only the ones currently checked: a width is a
    preference about a column, not about whether it happens to be visible right now,
    and a column hidden and later re-shown should not have forgotten how wide it was.
    """
    out: dict[str, int] = {}
    for name in names:
        value = _as_int(data.get(f"width__{name}"))
        if value is not None:
            out[name] = value
    return out or None


def _ordered(names: list, data) -> list | None:
    """Apply the position inputs, which is how the rail reorders without JavaScript.

    A missing or unparseable position sorts last in the order it arrived, so a form
    submitted with scripting off — or with the number boxes left alone — still reads as
    the order on screen rather than collapsing to alphabetical.
    """
    if not names:
        return None
    fallback = len(names) + 1

    def position(pair):
        index, name = pair
        value = _as_int(data.get(f"order__{name}"))
        return (fallback if value is None else value, index)

    return [name for _, name in sorted(enumerate(names), key=position)]


def _kept(stored: list | None, indexes: list) -> list:
    """The UI-built entries whose checkbox survived, addressed by position.

    By index rather than by an encoded identity, so nothing about an entry has to
    survive a round trip through a form value and come back trustworthy.
    """
    entries = stored or []
    kept = []
    for raw in indexes:
        index = _as_int(raw)
        if index is not None and 0 <= index < len(entries):
            kept.append(entries[index])
    return kept


def _added_stat(data) -> list[dict]:
    aggregate = data.get("add_stat_aggregate")
    if not aggregate:
        return []
    return [
        {
            "aggregate": aggregate,
            "field": data.get("add_stat_field") or "",
            "label": data.get("add_stat_label") or "",
        }
    ]


def _added_chart(data) -> list[dict]:
    mode = data.get("add_chart_mode")
    if not mode:
        return []
    target = data.get("add_chart_date") if mode == "timeseries" else data.get("add_chart_group")
    return [
        {
            "mode": mode,
            "target": target or "",
            "kind": data.get("add_chart_kind") or "",
            "aggregate": data.get("add_chart_aggregate") or "",
            "field": data.get("add_chart_field") or "",
            "label": data.get("add_chart_label") or "",
        }
    ]


def store_section(panel, request, section: str, data, *, user) -> None:
    """Write one section to one scope. ``user=None`` writes the site-wide default.

    Only this section's columns are touched. Everything else on the row keeps whatever
    it had, including ``NULL``, so saving the columns card cannot silently freeze the
    filters at today's inferred value.
    """
    model = _model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return

    values = clean_section(panel, request, section, data)
    row = row_for(panel, user)
    if row is None:
        row = model(resource_key=resource_key(panel), user=user)

    for name, value in values.items():
        setattr(row, name, value)
    _persist(row, request)


def reset_section(panel, request, section: str, *, user) -> None:
    """Clear one section back to what the code declares.

    Reset writes ``NULL``, it does not write today's resolved value: the point of the
    button is that the panel's Python becomes the answer again *and stays* the answer
    when the panel changes.
    """
    if section not in SECTION_COLUMNS:
        raise ValueError(f"{section!r} is not a section of the list preferences rail.")

    row = row_for(panel, user)
    if row is None:
        return
    for name in SECTION_COLUMNS[section]:
        setattr(row, name, None)
    _persist(row, request)


def _persist(row, request) -> None:
    """Save the row, or delete it once nothing is overridden any more.

    The request's cache is dropped either way: it was filled while validating this very
    POST, and a handler that went on to render rather than redirect would otherwise
    show the configuration as it was a moment before the save.
    """
    if row.is_empty():
        if row.pk:
            row.delete()
    else:
        user = getattr(request, "user", None)
        row.updated_by = user if getattr(user, "is_authenticated", False) else None
        row.save()

    cache = getattr(request, _CACHE_ATTR, None)
    if cache is not None:
        cache.clear()


# ── who may change the default for everyone ─────────────────────────────────


def may_set_site_default(request) -> bool:
    """Superusers, unless ``VANTAGE["LIST_PREFS_PERMISSION"]`` says otherwise.

    Superuser-only is the honest default, exactly as it is for the settings screen: the
    site-wide row is what every colleague sees on that list page tomorrow morning. A
    permission string or a callable taking the request widens it.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False

    rule = _permission_rule()
    if rule is None:
        return bool(user.is_superuser)
    if callable(rule):
        return bool(rule(request))
    return user.has_perm(str(rule))


def _permission_rule():
    """The configured rule, read through ``conf`` once it knows the key and directly
    until then.

    ``conf.DEFAULTS`` rejects an unrecognised ``VANTAGE`` key outright, so this module
    cannot introduce one on its own — see the note in the package's settings docs.
    Reading the raw dict as a fallback keeps the feature working in both worlds without
    a version check.
    """
    from vantage.conf import DEFAULTS, vantage_settings

    if PERMISSION_SETTING in DEFAULTS:
        return getattr(vantage_settings, PERMISSION_SETTING)
    configured = getattr(django_settings, "VANTAGE", None)
    return configured.get(PERMISSION_SETTING) if isinstance(configured, dict) else None


# ── what the list page computes with the resolved spec ──────────────────────


def analytics(spec: ListSpec, queryset, *, base=None, filters=()) -> dict:
    """Stat cards and charts for this list, or nothing at all — and nothing costs zero.

    Switching the stat strip off has to remove its *queries*, not merely its markup. A
    version that computed the panel and then hid it would leave the page exactly as
    slow as the thing the user turned off to make it faster.

    ``base`` and ``filters`` are the rows before filtering and the filters applied to
    them. With both, a trending stat compares against the same rows with its own date
    filter lifted, so filtering to "last 30 days" compares with the 30 days before.
    """
    if not spec.stats and not spec.charts:
        return {"stats": [], "charts": [], "deferred": False, "reason": None}
    return compute_panel(
        queryset,
        stats=spec.stats,
        charts=spec.charts,
        baseline=_trend_baseline(spec, base, filters),
    )


def _trend_baseline(spec: ListSpec, base, filters):
    """The filtered rows minus any filter on a trending stat's date field, or None."""
    from vantage.filters import apply_filters

    trend_fields = {stat.trend for stat in spec.stats if stat.trend}
    if base is None or not trend_fields:
        return None
    return apply_filters(base, [f for f in filters if f.path not in trend_fields])


def summary(spec: ListSpec, queryset) -> dict:
    """The footer bar, keyed by column so the template can align it. One query, or none."""
    if not spec.summary_aggregates:
        return {}
    return {cell.column: cell for cell in compute_summary(queryset, spec.summary_aggregates)}


# ── the rail's view model ───────────────────────────────────────────────────


def _label(model: type, path: str) -> str:
    hops = resolve_path(model, path)
    if hops:
        return str(getattr(hops[-1].field, "verbose_name", path)).capitalize()
    return path.replace("_", " ").capitalize()


def panel_context(panel, request, spec: ListSpec | None = None) -> dict:
    """Everything ``partials/list_prefs.html`` renders, resolved in Python.

    Built here rather than in the template because every list on it is an allowlist:
    the same :class:`Options` that will validate the POST is what draws the form, so
    the rail can never offer a control whose value would then be rejected.
    """
    from django.urls import NoReverseMatch, reverse

    spec = spec or resolve(panel, request)
    opts = spec.options
    model = panel.model

    try:
        action = reverse(f"vantage:{panel.slug}-prefs")
    except NoReverseMatch:  # pragma: no cover - only before the route is mounted
        action = None

    declared_stats = {stat.key: stat for stat in (panel.list_stats or ())}
    declared_charts = {chart.key: chart for chart in (panel.list_charts or ())}
    chosen_stats = spec.stored.get("stat_keys")
    chosen_charts = spec.stored.get("chart_keys")

    return {
        "action": action,
        "panel": panel,
        "spec": spec,
        "next": request.get_full_path() if hasattr(request, "get_full_path") else "",
        "may_set_default": may_set_site_default(request),
        "columns": [
            {
                "name": name,
                "label": _label(model, name),
                "selected": name in spec.columns,
                "position": _position(spec.columns, name, index),
                "width": spec.column_widths.get(name),
                "pinned": name in spec.pinned_columns,
            }
            for index, name in enumerate(opts.columns)
        ],
        "per_page": spec.per_page,
        "column_width_min": MIN_COLUMN_WIDTH,
        "column_width_max": MAX_COLUMN_WIDTH,
        "search_options": [
            {
                "name": name,
                "label": _label(model, name.lstrip("^=@")),
                "selected": name in spec.search_fields,
            }
            for name in opts.search_fields
        ],
        "filter_options": [
            {
                "name": name,
                "label": _label(model, name),
                "selected": name in spec.filter_fields,
                "position": _position(spec.filter_fields, name, index),
            }
            for index, name in enumerate(opts.filter_fields)
        ],
        "stat_options": [
            {
                "key": key,
                "label": stat.label,
                "selected": key in chosen_stats if chosen_stats else True,
            }
            for key, stat in declared_stats.items()
        ],
        "chart_options": [
            {
                "key": key,
                "label": chart.label,
                "selected": key in chosen_charts if chosen_charts else True,
            }
            for key, chart in declared_charts.items()
        ],
        "extra_stats": [
            {"index": index, "label": entry["label"]}
            for index, entry in enumerate(spec.stored.get("extra_stats") or [])
        ],
        "extra_charts": [
            {"index": index, "label": entry["label"]}
            for index, entry in enumerate(spec.stored.get("extra_charts") or [])
        ],
        "summary_options": [
            {
                "name": name,
                "label": _label(model, name),
                "current": spec.summary_aggregates.get(name) or NO_AGGREGATE,
                "inherited": infer_summaries(panel, list(opts.columns)).get(name),
            }
            for name in opts.summary_columns
        ],
        "measures": [{"name": n, "label": _label(model, n)} for n in opts.measures],
        "dates": [{"name": n, "label": _label(model, n)} for n in opts.dates],
        "groups": [{"name": n, "label": _label(model, n)} for n in opts.groups],
        "aggregates": AGGREGATES,
        "chart_modes": CHART_MODES,
        "overridden": {section: spec.is_overridden(section) for section in SECTIONS},
    }


def _position(chosen: list[str], name: str, fallback: int) -> int:
    """The number the order box shows: where it is now, or where it would land."""
    return chosen.index(name) + 1 if name in chosen else fallback + 1
