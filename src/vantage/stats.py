"""List-page statistics and charts, declared in Python and computed in SQL.

A panel author writes ``list_stats`` and ``list_charts`` as data; this module turns
them into the smallest number of queries that can answer them correctly, against the
queryset the user is actually looking at.

Three properties are load-bearing, and each corrects a mistake a shipping product makes:

* **Stats come from the filter predicate, never from the rendered page.** Retool's
  summary row aggregates the rows it happens to have loaded, which stops being true the
  moment the result exceeds one page. A sliced queryset is rejected outright here.
* **One round trip for the whole stat row.** Filament's canonical example runs one
  ``COUNT(*)`` per card, each re-running the full filtered query. Every stat that can
  share an ``aggregate()`` call does.
* **Sharing an ``aggregate()`` call is not always safe.** Two aggregates over different
  multi-valued relations produce a cartesian product and *both* numbers come out wrong
  (Django #10060). ``distinct=True`` rescues ``Count`` and nothing else, so stats are
  partitioned by the join they force and only joins that agree share a query. This is
  the one place where correctness costs a query, and it takes it.

Nothing here decides how anything looks. Results are plain, JSON-serialisable values
plus a preformatted display string; tone names are passed through untouched. Colour,
icons and layout belong to the template layer.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.core.exceptions import FieldError
from django.db.models import Count, F, Max, Min, Q, QuerySet
from django.db.models.constants import LOOKUP_SEP
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek
from django.utils.translation import gettext_lazy as _

from vantage.introspection import resolve_path

__all__ = [
    "Budget",
    "Chart",
    "ChartResult",
    "ChartSet",
    "Stat",
    "StatResult",
    "StatSet",
    "Trend",
    "compute_charts",
    "compute_panel",
    "compute_stats",
    "format_value",
    "plan_queries",
]

#: Supported display formats. ``percent`` reads its input as a fraction, because the
#: aggregates that produce percentages — ``Avg`` over a boolean, a ratio of two counts —
#: naturally yield 0..1, and guessing from magnitude would make 0.5 ambiguous.
FORMATS = ("number", "currency", "percent", "duration")

DEFAULT_CURRENCY = "$"

#: Bucket thresholds for a timeseries, in days of span. Chosen so a chart never renders
#: more than roughly 100 points from its default bucket.
DAY_SPAN_LIMIT = 62
WEEK_SPAN_LIMIT = 730

#: Hard ceiling on points returned by one chart, whatever the bucket.
MAX_POINTS = 500

#: A breakdown beyond this many distinct groups is a cardinality mistake, not a chart.
#: The query is sliced one past the limit so the refusal costs nothing extra.
MAX_GROUPS_SCANNED = 200

#: Segments rendered before the remainder is folded into "Other".
DEFAULT_SEGMENTS = 20

OTHER_LABEL = _("Other")
NULL_LABEL = "—"

#: Aggregates whose partial results can be added together to form the "Other" segment.
#: An average cannot, so a truncated breakdown of averages reports no remainder rather
#: than a wrong one.
ADDITIVE_AGGREGATES = ("Count", "Sum")


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "stat"


# ── declarations ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Stat:
    """One number pinned above the list.

    ``value`` is either an aggregate expression, which is folded into a shared
    ``aggregate()`` call, or a callable taking the filtered queryset — the escape hatch
    for anything the ORM cannot say in one expression. A callable costs whatever it
    costs and is never combined with anything.

    ``trend`` names a date field. The same aggregate is then computed over the period
    immediately preceding the data's own span, and the pair becomes a delta chip.
    """

    label: str
    value: Any
    format: str = "number"
    tone: str | None = None
    trend: str | None = None
    description: str | None = None
    currency: str = DEFAULT_CURRENCY
    key: str | None = None

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ValueError(f"Stat format must be one of {FORMATS}, got {self.format!r}.")
        if self.key is None:
            object.__setattr__(self, "key", _slug(self.label))

    @property
    def is_callable(self) -> bool:
        """A plain callable, as opposed to an ORM expression (which is also callable-ish)."""
        return callable(self.value) and not hasattr(self.value, "resolve_expression")


@dataclass(frozen=True)
class Chart:
    """One grouped query rendered as a chart.

    Constructed through :meth:`timeseries` or :meth:`breakdown` rather than directly,
    because the two shapes take different arguments and share only their output.
    """

    label: str
    mode: str
    value: Any
    kind: str = "line"
    date: str | None = None
    bucket: str | None = None
    group_by: str | None = None
    segments: int = DEFAULT_SEGMENTS
    allow_high_cardinality: bool = False
    key: str | None = None

    def __post_init__(self) -> None:
        if self.key is None:
            object.__setattr__(self, "key", _slug(self.label))
        if self.bucket is not None and self.bucket not in ("day", "week", "month"):
            raise ValueError(f"Chart bucket must be day, week or month, got {self.bucket!r}.")

    @classmethod
    def timeseries(
        cls,
        label: str,
        *,
        date: str,
        value: Any,
        kind: str = "line",
        bucket: str | None = None,
        key: str | None = None,
    ) -> Chart:
        """Aggregate over time, bucketed by day, week or month.

        The bucket is chosen from the span of the data unless pinned, so a chart of one
        week and a chart of five years are both readable without configuration.
        """
        return cls(label=label, mode="timeseries", value=value, kind=kind, date=date,
                   bucket=bucket, key=key)

    @classmethod
    def breakdown(
        cls,
        label: str,
        *,
        group_by: str,
        value: Any,
        kind: str = "bar",
        segments: int = DEFAULT_SEGMENTS,
        allow_high_cardinality: bool = False,
        key: str | None = None,
    ) -> Chart:
        """Aggregate grouped by a field, capped at ``segments`` plus an "Other" bucket."""
        return cls(label=label, mode="breakdown", value=value, kind=kind, group_by=group_by,
                   segments=segments, allow_high_cardinality=allow_high_cardinality, key=key)


@dataclass(frozen=True)
class Budget:
    """How much a list page is willing to spend on analytics.

    The default list page must never be slower because stats exist. When the plan
    exceeds the budget the panel is returned *deferred* — zero queries, a reason, and a
    flag the template renders as a "compute" button — rather than being made cheaper by
    silently computing something less correct.

    ``max_rows`` is compared against an estimate the caller supplies. This module never
    counts rows itself: ``COUNT(*)`` under MVCC is exactly the cost being avoided.
    """

    enabled: bool = True
    max_queries: int = 8
    max_rows: int | None = None

    def deferral(self, *, queries: int, estimated_rows: int | None = None) -> str | None:
        if not self.enabled:
            return _("disabled")
        if queries > self.max_queries:
            return _("%(queries)s queries exceeds the budget of %(budget)s") % {
                "queries": queries,
                "budget": self.max_queries,
            }
        if self.max_rows is not None and estimated_rows is not None:
            if estimated_rows > self.max_rows:
                return _("~%(rows)s rows exceeds the budget of %(budget)s") % {
                    "rows": estimated_rows,
                    "budget": self.max_rows,
                }
        return None


# ── results ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Trend:
    """A stat's movement against the preceding equivalent period."""

    previous: Any
    delta: Any
    percent: float | None
    direction: str
    delta_display: str
    percent_display: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous": self.previous,
            "delta": self.delta,
            "percent": self.percent,
            "direction": self.direction,
            "delta_display": self.delta_display,
            "percent_display": self.percent_display,
        }


@dataclass(frozen=True)
class StatResult:
    key: str
    label: str
    value: Any
    display: str
    format: str
    tone: str | None = None
    description: str | None = None
    trend: Trend | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "display": self.display,
            "format": self.format,
            "tone": self.tone,
            "description": self.description,
            "trend": self.trend.as_dict() if self.trend else None,
            "error": self.error,
        }


@dataclass(frozen=True)
class ChartResult:
    key: str
    label: str
    kind: str
    labels: list[str] = field(default_factory=list)
    series: list[dict[str, Any]] = field(default_factory=list)
    bucket: str | None = None
    truncated: bool = False
    empty: bool = True
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "labels": self.labels,
            "series": self.series,
            "bucket": self.bucket,
            "truncated": self.truncated,
            "empty": self.empty,
            "error": self.error,
        }


@dataclass(frozen=True)
class StatSet(Sequence):
    """The computed stat row, or the reason it was not computed.

    Sequence-shaped so a template can iterate it directly while still being able to ask
    ``deferred``.
    """

    stats: tuple[StatResult, ...] = ()
    deferred: bool = False
    reason: str | None = None

    def __len__(self) -> int:
        return len(self.stats)

    def __getitem__(self, index: int) -> StatResult:  # type: ignore[override]
        return self.stats[index]

    def __iter__(self) -> Iterator[StatResult]:
        return iter(self.stats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stats": [s.as_dict() for s in self.stats],
            "deferred": self.deferred,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ChartSet(Sequence):
    charts: tuple[ChartResult, ...] = ()
    deferred: bool = False
    reason: str | None = None

    def __len__(self) -> int:
        return len(self.charts)

    def __getitem__(self, index: int) -> ChartResult:  # type: ignore[override]
        return self.charts[index]

    def __iter__(self) -> Iterator[ChartResult]:
        return iter(self.charts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "charts": [c.as_dict() for c in self.charts],
            "deferred": self.deferred,
            "reason": self.reason,
        }


# ── formatting ──────────────────────────────────────────────────────────────


def _number(value: Any) -> Any:
    """Coerce a database value to something JSON can carry, and NULL to zero.

    An empty filtered set is a normal state, not an error: ``Sum`` returns NULL and the
    card must read ``0``, not ``None`` and not a traceback.
    """
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return value


def _duration_display(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    total = int(abs(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{sign}{days}d {hours}h"
    if hours:
        return f"{sign}{hours}h {minutes}m"
    if minutes:
        return f"{sign}{minutes}m {secs}s"
    return f"{sign}{secs}s"


def format_value(value: Any, fmt: str = "number", *, currency: str = DEFAULT_CURRENCY,
                 signed: bool = False) -> str:
    """Render a raw aggregate as the string a card shows.

    ``signed`` is for deltas, where a leading ``+`` carries meaning.
    """
    if value is None:
        value = 0
    if not isinstance(value, int | float | Decimal):
        return str(value)

    number = float(value)
    prefix = "+" if signed and number > 0 else ""

    if fmt == "currency":
        body = f"{abs(number):,.2f}"
        sign = "-" if number < 0 else prefix
        return f"{sign}{currency}{body}"
    if fmt == "percent":
        return f"{prefix}{number * 100:,.1f}%"
    if fmt == "duration":
        return (prefix if number > 0 else "") + _duration_display(number)
    if float(number).is_integer():
        return f"{prefix}{int(number):,}"
    return f"{prefix}{number:,.2f}"


# ── join analysis (Django #10060) ───────────────────────────────────────────


def _expression_paths(node: Any) -> set[str]:
    """Every field path an expression or ``Q`` reaches, at any depth."""
    paths: set[str] = set()
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, F):
            paths.add(current.name)
            continue
        if isinstance(current, Q):
            for child in current.children:
                if isinstance(child, Q):
                    stack.append(child)
                elif isinstance(child, tuple | list) and child:
                    if isinstance(child[0], str):
                        paths.add(child[0])
                    if len(child) > 1 and hasattr(child[1], "resolve_expression"):
                        stack.append(child[1])
            continue
        stack.append(getattr(current, "filter", None))
        sources = getattr(current, "get_source_expressions", None)
        if callable(sources):
            stack.extend(sources())
    return paths


def _fanout_prefix(model: type, path: str) -> str | None:
    """The prefix of ``path`` up to and including its first multi-valued hop."""
    prefix: list[str] = []
    for hop in resolve_path(model, path):
        prefix.append(hop.name)
        if hop.is_multivalued:
            return LOOKUP_SEP.join(prefix)
    return None


def _join_key(model: type, expression: Any) -> frozenset[str]:
    """Which multi-valued relations an expression joins to.

    Expressions sharing a key can share an ``aggregate()`` call. Expressions with
    different keys cannot: the joins multiply and every aggregate in the call is
    inflated by the other relation's row count — including aggregates over local
    columns, which is the part that surprises people.
    """
    paths = _expression_paths(expression)
    return frozenset(p for p in (_fanout_prefix(model, path) for path in paths) if p)


def _check_single_relation(stat: Stat, key: frozenset[str]) -> None:
    if len(key) > 1:
        raise ValueError(
            f"Stat {stat.label!r} aggregates across the multi-valued relations "
            f"{sorted(key)} in one expression. Two joins in one aggregate produce a "
            f"cartesian product and both numbers come out wrong; distinct=True fixes "
            f"Count only. Express one of them as a correlated Subquery instead."
        )


# ── stats ───────────────────────────────────────────────────────────────────


def _reject_sliced(queryset: QuerySet) -> None:
    if getattr(queryset.query, "is_sliced", False):
        raise ValueError(
            "Stats must be computed from the filtered queryset before pagination. "
            "A sliced queryset would aggregate the current page only."
        )


def _filter_condition(expression: Any) -> Q | None:
    """The ``Q`` behind an aggregate's ``filter=``, whatever Django stores it as.

    Django 6.1 wraps it in an internal ``AggregateFilter`` expression; earlier versions
    keep the bare ``Q``. Reading through ``.condition`` covers both.
    """
    existing = getattr(expression, "filter", None)
    if existing is None:
        return None
    return getattr(existing, "condition", existing)


def _as_aggregate_filter(condition: Q) -> Any:
    """Wrap a ``Q`` the way the installed Django wraps one.

    Asking a throwaway aggregate to do the wrapping keeps this on the public
    constructor and correct across the versions the package supports, rather than
    hard-coding an internal class that exists in some of them.
    """
    return Count("pk", filter=condition).filter


def _with_window(expression: Any, window: Q) -> Any:
    """The same aggregate, restricted to a period, without rebuilding it.

    Folding the period into each aggregate's ``FILTER (WHERE ...)`` clause is what lets
    every trending stat share one query for the previous period, instead of one query
    per stat — and it composes with a ``filter=`` the author already wrote instead of
    replacing it.
    """
    clone = expression.copy()
    existing = _filter_condition(expression)
    condition = (existing & window) if existing is not None else window
    clone.filter = _as_aggregate_filter(condition)
    return clone


def _previous_window(low: Any, high: Any) -> tuple[Any, Any] | None:
    """The period immediately before the data's own span, of equal length."""
    if low is None or high is None:
        return None
    span = high - low
    if not span:
        span = dt.timedelta(days=1)
    return (low - span, low)


def _trend(stat: Stat, current: Any, previous: Any) -> Trend:
    current_number = _number(current)
    previous_number = _number(previous)
    delta = current_number - previous_number
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    else:
        direction = "flat"
    percent = (delta / abs(previous_number)) if previous_number else None
    return Trend(
        previous=previous_number,
        delta=delta,
        percent=percent,
        direction=direction,
        delta_display=format_value(delta, stat.format, currency=stat.currency, signed=True),
        percent_display=None if percent is None else format_value(percent, "percent", signed=True),
    )


def plan_queries(model: type, stats: Iterable[Stat] = (), charts: Iterable[Chart] = ()) -> int:
    """How many queries computing these would cost. The budget gate reads this."""
    stats = list(stats)
    charts = list(charts)

    expression_stats = [s for s in stats if not s.is_callable]
    buckets: dict[frozenset[str], list[Stat]] = {}
    for stat in expression_stats:
        buckets.setdefault(_join_key(model, stat.value), []).append(stat)

    total = len(buckets)
    total += sum(1 for group in buckets.values() if any(s.trend for s in group))
    for stat in stats:
        if stat.is_callable:
            total += 2 if stat.trend else 1

    total += len(charts)
    if any(c.mode == "timeseries" and c.bucket is None for c in charts):
        total += 1
    return total


def compute_stats(
    queryset: QuerySet,
    stats: Iterable[Stat],
    *,
    baseline: QuerySet | None = None,
    budget: Budget | None = None,
    estimated_rows: int | None = None,
) -> StatSet:
    """Compute every stat against the filtered queryset.

    ``baseline`` is the queryset used for the *previous* period of a trending stat. It
    defaults to ``queryset``, which is right until the user filters on the trend field
    itself — at that point the preceding period has been filtered away and the trend
    reads flat. The caller that owns the filters can pass the same queryset with that
    one filter removed, exactly as faceted counts do, and get a real comparison back.
    """
    stats = list(stats)
    budget = budget or Budget()
    if not stats:
        return StatSet()

    _reject_sliced(queryset)
    reason = budget.deferral(
        queries=plan_queries(queryset.model, stats), estimated_rows=estimated_rows
    )
    if reason:
        return StatSet(deferred=True, reason=reason)

    results: dict[str, StatResult] = {}
    expression_stats = [s for s in stats if not s.is_callable]

    buckets: dict[frozenset[str], list[Stat]] = {}
    for stat in expression_stats:
        key = _join_key(queryset.model, stat.value)
        _check_single_relation(stat, key)
        buckets.setdefault(key, []).append(stat)

    for group in buckets.values():
        results.update(_compute_group(queryset, baseline, group))

    for stat in stats:
        if stat.is_callable:
            results[stat.key] = _compute_callable(queryset, baseline, stat)

    return StatSet(stats=tuple(results[s.key] for s in stats))


def _compute_group(
    queryset: QuerySet, baseline: QuerySet | None, group: list[Stat]
) -> dict[str, StatResult]:
    """One ``aggregate()`` for the group, plus at most one more for all its trends."""
    aggregates: dict[str, Any] = {}
    for index, stat in enumerate(group):
        aggregates[f"vantage_stat_{index}"] = stat.value

    trend_fields = sorted({s.trend for s in group if s.trend})
    for index, name in enumerate(trend_fields):
        aggregates[f"vantage_low_{index}"] = Min(name)
        aggregates[f"vantage_high_{index}"] = Max(name)

    row = queryset.order_by().aggregate(**aggregates)

    windows: dict[str, tuple[Any, Any]] = {}
    for index, name in enumerate(trend_fields):
        window = _previous_window(row[f"vantage_low_{index}"], row[f"vantage_high_{index}"])
        if window:
            windows[name] = window

    previous: dict[int, Any] = {}
    if windows:
        source = baseline if baseline is not None else queryset
        _reject_sliced(source)
        previous_aggregates: dict[str, Any] = {}
        for index, stat in enumerate(group):
            if not stat.trend or stat.trend not in windows:
                continue
            start, end = windows[stat.trend]
            window_q = Q(**{f"{stat.trend}__gte": start, f"{stat.trend}__lt": end})
            previous_aggregates[f"vantage_prev_{index}"] = _with_window(stat.value, window_q)
        if previous_aggregates:
            previous_row = source.order_by().aggregate(**previous_aggregates)
            previous = {
                int(key.rsplit("_", 1)[1]): value for key, value in previous_row.items()
            }

    results: dict[str, StatResult] = {}
    for index, stat in enumerate(group):
        value = _number(row[f"vantage_stat_{index}"])
        trend = None
        if index in previous:
            trend = _trend(stat, value, previous[index])
        results[stat.key] = StatResult(
            key=stat.key,
            label=stat.label,
            value=value,
            display=format_value(value, stat.format, currency=stat.currency),
            format=stat.format,
            tone=stat.tone,
            description=stat.description,
            trend=trend,
        )
    return results


def _compute_callable(queryset: QuerySet, baseline: QuerySet | None, stat: Stat) -> StatResult:
    """The escape hatch. Its cost is the author's problem; its failure is not the page's."""
    try:
        value = _number(stat.value(queryset))
    except Exception as exc:  # a custom manager method is arbitrary user code
        return StatResult(
            key=stat.key, label=stat.label, value=0, display="—", format=stat.format,
            tone=stat.tone, description=stat.description, error=str(exc),
        )

    trend = None
    if stat.trend:
        source = baseline if baseline is not None else queryset
        # The span comes from the queryset on screen; the comparison period is drawn
        # from the baseline, which is the only queryset that still contains it.
        span = queryset.order_by().aggregate(low=Min(stat.trend), high=Max(stat.trend))
        window = _previous_window(span["low"], span["high"])
        if window:
            start, end = window
            scoped = source.filter(**{f"{stat.trend}__gte": start, f"{stat.trend}__lt": end})
            trend = _trend(stat, value, _number(stat.value(scoped)))

    return StatResult(
        key=stat.key,
        label=stat.label,
        value=value,
        display=format_value(value, stat.format, currency=stat.currency),
        format=stat.format,
        tone=stat.tone,
        description=stat.description,
        trend=trend,
    )


# ── charts ──────────────────────────────────────────────────────────────────

TRUNCATORS = {"day": TruncDay, "week": TruncWeek, "month": TruncMonth}


def _choose_bucket(low: Any, high: Any) -> str:
    if low is None or high is None:
        return "day"
    span = (high - low).days
    if span <= DAY_SPAN_LIMIT:
        return "day"
    if span <= WEEK_SPAN_LIMIT:
        return "week"
    return "month"


def _bucket_start(moment: dt.date, bucket: str) -> dt.date:
    day = moment.date() if isinstance(moment, dt.datetime) else moment
    if bucket == "week":
        return day - dt.timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def _next_bucket(day: dt.date, bucket: str) -> dt.date:
    if bucket == "week":
        return day + dt.timedelta(days=7)
    if bucket == "month":
        return (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return day + dt.timedelta(days=1)


def _is_additive(expression: Any) -> bool:
    return type(expression).__name__ in ADDITIVE_AGGREGATES


def _group_labels(model: type, path: str) -> tuple[Any, dict[Any, str]]:
    """The terminal field of a group-by path and its value→label map, if it has one."""
    hops = resolve_path(model, path)
    if not hops:
        return None, {}
    terminal = hops[-1].field
    choices = getattr(terminal, "choices", None) or []
    return terminal, {value: str(label) for value, label in choices}


def compute_charts(
    queryset: QuerySet,
    charts: Iterable[Chart],
    *,
    budget: Budget | None = None,
    estimated_rows: int | None = None,
) -> ChartSet:
    """Compute every chart: one grouped query each, plus one shared span probe.

    The probe is a single ``Min``/``Max`` aggregate covering every timeseries whose
    bucket is not pinned. It is what lets the same declaration render a week of data by
    day and five years by month without the author choosing.
    """
    charts = list(charts)
    budget = budget or Budget()
    if not charts:
        return ChartSet()

    _reject_sliced(queryset)
    reason = budget.deferral(
        queries=plan_queries(queryset.model, charts=charts), estimated_rows=estimated_rows
    )
    if reason:
        return ChartSet(deferred=True, reason=reason)

    spans: dict[str, tuple[Any, Any]] = {}
    probe_fields = sorted({c.date for c in charts if c.mode == "timeseries" and not c.bucket})
    if probe_fields:
        probe: dict[str, Any] = {}
        for index, name in enumerate(probe_fields):
            probe[f"vantage_low_{index}"] = Min(name)
            probe[f"vantage_high_{index}"] = Max(name)
        row = queryset.order_by().aggregate(**probe)
        for index, name in enumerate(probe_fields):
            spans[name] = (row[f"vantage_low_{index}"], row[f"vantage_high_{index}"])

    results = []
    for chart in charts:
        try:
            if chart.mode == "timeseries":
                results.append(_compute_timeseries(queryset, chart, spans.get(chart.date)))
            else:
                results.append(_compute_breakdown(queryset, chart))
        except FieldError as exc:
            # A widget failing must not take the table down with it: render the error in
            # this region only.
            results.append(
                ChartResult(key=chart.key, label=chart.label, kind=chart.kind, error=str(exc))
            )
    return ChartSet(charts=tuple(results))


def _compute_timeseries(
    queryset: QuerySet, chart: Chart, span: tuple[Any, Any] | None
) -> ChartResult:
    bucket = chart.bucket or _choose_bucket(*(span or (None, None)))
    truncate = TRUNCATORS[bucket]

    rows = list(
        queryset.order_by()
        .annotate(vantage_bucket=truncate(chart.date))
        .values("vantage_bucket")
        .annotate(vantage_value=chart.value)
        .order_by("vantage_bucket")[: MAX_POINTS + 1]
    )
    truncated = len(rows) > MAX_POINTS
    rows = rows[:MAX_POINTS]

    points: dict[dt.date, Any] = {}
    for row in rows:
        moment = row["vantage_bucket"]
        if moment is None:
            continue
        points[_bucket_start(moment, bucket)] = _number(row["vantage_value"])

    if not points:
        return ChartResult(
            key=chart.key, label=chart.label, kind=chart.kind, bucket=bucket,
            series=[{"name": chart.label, "data": []}],
        )

    # Gaps are filled with zeroes: a line chart that skips an empty week draws a slope
    # where the truth is a floor, and the fill is free.
    labels: list[str] = []
    data: list[Any] = []
    cursor, last = min(points), max(points)
    while cursor <= last and len(labels) < MAX_POINTS:
        labels.append(cursor.isoformat())
        data.append(points.get(cursor, 0))
        cursor = _next_bucket(cursor, bucket)

    return ChartResult(
        key=chart.key,
        label=chart.label,
        kind=chart.kind,
        labels=labels,
        series=[{"name": chart.label, "data": data}],
        bucket=bucket,
        truncated=truncated,
        empty=False,
    )


def _compute_breakdown(queryset: QuerySet, chart: Chart) -> ChartResult:
    terminal, choices = _group_labels(queryset.model, chart.group_by)
    if terminal is not None and not chart.allow_high_cardinality:
        if getattr(terminal, "primary_key", False) or getattr(terminal, "unique", False):
            return ChartResult(
                key=chart.key, label=chart.label, kind=chart.kind,
                error=f"{chart.group_by!r} is unique: one segment per row is not a chart.",
            )

    rows = list(
        queryset.order_by()
        .values(chart.group_by)
        .annotate(vantage_value=chart.value)
        # The group value breaks ties, so a redraw of the same data never reshuffles
        # the segments under the reader.
        .order_by("-vantage_value", chart.group_by)[: MAX_GROUPS_SCANNED + 1]
    )
    if len(rows) > MAX_GROUPS_SCANNED and not chart.allow_high_cardinality:
        return ChartResult(
            key=chart.key, label=chart.label, kind=chart.kind,
            error=(
                f"{chart.group_by!r} has more than {MAX_GROUPS_SCANNED} distinct values; "
                f"a breakdown of it would be unreadable."
            ),
        )
    rows = rows[:MAX_GROUPS_SCANNED]

    head = rows[: chart.segments]
    tail = rows[chart.segments :]

    labels = []
    data = []
    for row in head:
        raw = row[chart.group_by]
        labels.append(_segment_label(raw, choices))
        data.append(_number(row["vantage_value"]))

    if tail and _is_additive(chart.value):
        labels.append(OTHER_LABEL)
        data.append(sum(_number(row["vantage_value"]) for row in tail))

    return ChartResult(
        key=chart.key,
        label=chart.label,
        kind=chart.kind,
        labels=labels,
        series=[{"name": chart.label, "data": data}],
        truncated=bool(tail),
        empty=not labels,
    )


def _segment_label(raw: Any, choices: dict[Any, str]) -> str:
    if raw is None:
        return NULL_LABEL
    if raw in choices:
        return choices[raw]
    if isinstance(raw, bool):
        return _("Yes") if raw else _("No")
    return str(raw)


# ── the whole panel ─────────────────────────────────────────────────────────


def compute_panel(
    queryset: QuerySet,
    *,
    stats: Iterable[Stat] = (),
    charts: Iterable[Chart] = (),
    baseline: QuerySet | None = None,
    budget: Budget | None = None,
    estimated_rows: int | None = None,
) -> dict[str, Any]:
    """Everything a list page's analytics strip needs, as plain JSON-safe data.

    The budget covers stats and charts *together*, so a page cannot slip past it by
    splitting the cost across the two declarations.
    """
    stats = list(stats)
    charts = list(charts)
    budget = budget or Budget()

    if not stats and not charts:
        return {"stats": [], "charts": [], "deferred": False, "reason": None}

    _reject_sliced(queryset)
    reason = budget.deferral(
        queries=plan_queries(queryset.model, stats, charts), estimated_rows=estimated_rows
    )
    if reason:
        return {"stats": [], "charts": [], "deferred": True, "reason": reason}

    unlimited = Budget(max_queries=10**6)
    computed_stats = compute_stats(queryset, stats, baseline=baseline, budget=unlimited)
    computed_charts = compute_charts(queryset, charts, budget=unlimited)
    return {
        "stats": [s.as_dict() for s in computed_stats],
        "charts": [c.as_dict() for c in computed_charts],
        "deferred": False,
        "reason": None,
    }
