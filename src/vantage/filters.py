"""Faceted filtering for the list page.

Three decisions shape this module, and all three are about query cost.

*One aggregate per filter, never one per choice.* Django's own
``SimpleListFilter.get_facet_counts`` builds a ``Q(pk__in=<subquery>)`` for every
choice, which the planner sees as N correlated subqueries and which is pathological
on a large table. Every count here is a ``Count("pk", filter=Q(...))`` folded into a
single ``.aggregate()``, so the database walks the filtered set once and returns all
of the buckets. That form is portable: Postgres and SQLite get a native
``FILTER (WHERE ...)`` clause and Django rewrites it to ``CASE WHEN`` elsewhere.

*Single-select filters share one query; multi-select filters pay for their own.*
Counts for a single-select filter are computed inside the current selection, so every
such filter folds into the same aggregate. A multi-select filter that has a value
ticked must have its own predicate removed and everyone else's applied — otherwise
every option the user did not tick reads ``(0)`` and the filter becomes a dead end —
so it needs a different base queryset, and therefore a query of its own. An untouched
multi-select filter imposes no predicate to exclude, so it rejoins the shared
aggregate: with nothing selected the whole page costs one facet query.

*Counts are off unless asked for, and refused when they would be expensive.* Each
filter is classified CHEAP, COSTLY or NEVER when it is built, and only CHEAP filters
are counted, under a hard budget on the number of facet queries per render.

Filter state is read from the query string as repeated parameters —
``?status=draft&status=review`` — giving OR within one filter and AND across filters.
The query string is hostile input: a value that is not recognised is dropped, never
raised on and never reaches a lookup uncoerced. Relative date windows are stored as
ISO-8601 durations rather than resolved dates, so a saved view still means "the last
seven days" a month later.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count, Q
from django.db.models.constants import LOOKUP_SEP
from django.http import QueryDict
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.text import capfirst
from django.utils.translation import gettext_lazy as _

from vantage import inference
from vantage.introspection import PathHop, resolve_path

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from vantage.panels import ModelPanel

__all__ = [
    "DATE_PRESETS",
    "EMPTY",
    "FACETS_PARAM",
    "MAX_FACET_CHOICES",
    "MAX_FACET_QUERIES",
    "BooleanFilter",
    "Choice",
    "ChoiceFilter",
    "DateFilter",
    "FacetCost",
    "Filter",
    "NumericRangeFilter",
    "RelatedFilter",
    "apply_facet_counts",
    "apply_filters",
    "build_filter",
    "build_filters",
    "facets_requested",
]

logger = logging.getLogger("vantage.filters")

#: Ceiling on facet queries for one render. Beyond it counts are dropped rather than
#: trimmed silently, because a page that is slow is worse than a page without counts.
MAX_FACET_QUERIES = 8

#: A filter with more options than this renders as a typeahead and is never counted:
#: the aggregate would carry one ``CASE WHEN`` per option.
MAX_FACET_CHOICES = 50

#: The value meaning "this field is null". Chosen because it cannot collide with a
#: primary key, and skipped on choice fields that already declare it.
EMPTY = "null"

#: Separator for a custom date window, ``2026-01-01..2026-03-31``. Either side may be
#: omitted for an open-ended window.
RANGE_SEP = ".."

#: Opt-in query parameter for counts, mirroring Django's ``?_facets``.
FACETS_PARAM = "_facets"

#: Relative date windows, keyed by ISO-8601 duration. The window is anchored on whole
#: local days and *includes today*, so "last 7 days" covers seven calendar days rather
#: than the eight that a naive ``today - 7 days`` produces.
DATE_PRESETS: dict[str, tuple[str | Promise, int]] = {
    "P1D": (_("Today"), 1),
    "P7D": (_("Last 7 days"), 7),
    "P30D": (_("Last 30 days"), 30),
    "P90D": (_("Last 90 days"), 90),
}

#: The calendar year to date. ISO-8601 has no notation for it, so it gets a token of
#: its own rather than being mislabelled as a duration.
CURRENT_YEAR = "current-year"

#: Field classes that must never become a filter. Free text and JSON have no bounded
#: set of values to offer, so they belong in search, not in the filter rail.
UNFILTERABLE = (
    models.TextField,
    models.JSONField,
    models.BinaryField,
    models.FileField,
)


class FacetCost(Enum):
    """How expensive it is to count this filter's options.

    ``NEVER`` is not "expensive" but "wrong or meaningless": a count over a
    multi-valued join double-counts rows, and a continuous range has no options.
    """

    CHEAP = "cheap"
    COSTLY = "costly"
    NEVER = "never"


@dataclass(frozen=True)
class Choice:
    """One option in a filter, ready to render."""

    value: str
    label: str
    count: int | None
    selected: bool


# ── base ────────────────────────────────────────────────────────────────────


class Filter:
    """One filter on the list page.

    Subclasses supply the options and the predicate for a single value; everything
    else — parsing, OR-ing the selected values together, counting — is shared.
    """

    #: Whether more than one value may be selected at once. This is what decides
    #: between AND-facets and disjunctive counts, so it is a property of the widget
    #: rather than of the field.
    multi = False

    def __init__(self, path: str, hops: list[PathHop], params: Any = None) -> None:
        self.path = path
        self.hops = hops
        self.field = hops[-1].field
        self.is_multivalued = any(hop.is_multivalued for hop in hops)
        self.values = self.parse(params if params is not None else QueryDict())
        self.cost = self.classify()
        self._counts: dict[str, int] | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.path}={self.values!r}>"

    # ── identity ────────────────────────────────────────────────────────────

    @property
    def parameter_name(self) -> str:
        return self.path

    @property
    def label(self) -> str:
        """A human label, prefixed with the relation for a traversed path."""
        parts = [str(getattr(hop.field, "verbose_name", hop.name)) for hop in self.hops]
        return capfirst(" ".join(parts))

    @property
    def is_active(self) -> bool:
        return bool(self.values)

    def expected_parameters(self) -> list[str]:
        """Query parameters this filter owns, so the view can reject the rest."""
        return [self.parameter_name]

    # ── request state ───────────────────────────────────────────────────────

    def parse(self, params: Any) -> list[str]:
        """Accepted values from the query string, in order and without duplicates.

        A value that does not clean is dropped rather than reported: a filter rail is
        not a form, and a stale bookmark must degrade to a broader result set, not to
        a 500.
        """
        accepted: list[str] = []
        for raw in _getlist(params, self.parameter_name):
            value = self.clean(raw)
            if value is not None and value not in accepted:
                accepted.append(value)
        return accepted if self.multi else accepted[:1]

    def clean(self, value: str) -> str | None:
        """Validate one raw value, returning ``None`` to reject it."""
        return value or None

    # ── predicate ───────────────────────────────────────────────────────────

    def value_q(self, value: str) -> Q | None:
        raise NotImplementedError

    def q(self) -> Q | None:
        """The selected values OR-ed together, or ``None`` when nothing is selected."""
        parts = [q for q in (self.value_q(v) for v in self.values) if q is not None]
        if not parts:
            return None
        combined = parts[0]
        for part in parts[1:]:
            combined |= part
        return combined

    def queryset(self, queryset: QuerySet) -> QuerySet:
        """Apply this filter, de-duplicating rows when the path is multi-valued."""
        predicate = self.q()
        if predicate is None:
            return queryset
        queryset = queryset.filter(predicate)
        if self.is_multivalued:
            queryset = queryset.distinct()
        return queryset

    # ── options and counts ──────────────────────────────────────────────────

    def options(self) -> list[tuple[str, str]]:
        """``(value, label)`` pairs, before counts and selection are applied."""
        return []

    def choices(self) -> list[Choice]:
        counts = self._counts
        return [
            Choice(
                value=value,
                label=label,
                count=None if counts is None else counts.get(value, 0),
                selected=value in self.values,
            )
            for value, label in self.options()
        ]

    def classify(self) -> FacetCost:
        if self.is_multivalued:
            return FacetCost.NEVER
        return FacetCost.CHEAP

    def facet_pairs(self) -> list[tuple[str, Q]] | None:
        """``(value, predicate)`` pairs to count, or ``None`` to skip this filter."""
        if self.cost is not FacetCost.CHEAP:
            return None
        options = self.options()
        if not options or len(options) > MAX_FACET_CHOICES:
            return None
        pairs = [(value, self.value_q(value)) for value, _label in options]
        return [(value, q) for value, q in pairs if q is not None]

    def set_counts(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def _empty_option(self) -> list[tuple[str, str]]:
        """The "no value" option, offered only where null is actually possible."""
        if getattr(self.field, "null", False):
            return [(EMPTY, _("Empty"))]
        return []

    def _isnull_q(self) -> Q:
        return Q(**{f"{self.path}{LOOKUP_SEP}isnull": True})


# ── concrete filters ────────────────────────────────────────────────────────


class ChoiceFilter(Filter):
    """A field with declared ``choices``.

    Multi-select, because a status rail whose options blank out the moment one is
    ticked is the failure mode the disjunctive count rule exists to prevent.
    """

    multi = True

    def __init__(self, path: str, hops: list[PathHop], params: Any = None) -> None:
        # Keys are matched as strings because that is what arrives in the query
        # string, but the query is issued with the declared Python value, which may be
        # an int or a bool.
        self._by_key = {
            str(value): value for value, _label in _flat_choices(hops[-1].field.choices)
        }
        super().__init__(path, hops, params)

    def clean(self, value: str) -> str | None:
        if value in self._by_key:
            return value
        if value == EMPTY and getattr(self.field, "null", False):
            return value
        return None

    def options(self) -> list[tuple[str, str]]:
        options = [(str(value), str(label)) for value, label in _flat_choices(self.field.choices)]
        if EMPTY not in self._by_key:
            options += self._empty_option()
        return options

    def value_q(self, value: str) -> Q | None:
        if value == EMPTY and EMPTY not in self._by_key:
            return self._isnull_q()
        if value not in self._by_key:
            return None
        return Q(**{self.path: self._by_key[value]})


class BooleanFilter(Filter):
    """A boolean field, rendered as a radio group.

    Single-select, so its counts come from the shared AND-facet aggregate. The
    consequence is deliberate: with "Yes" selected, "No" reads zero. For two mutually
    exclusive options that is honest — clearing the filter is one click and needs no
    count to explain itself — and it keeps a boolean off the per-filter query budget.
    """

    def clean(self, value: str) -> str | None:
        if value in ("1", "0"):
            return value
        if value == EMPTY and getattr(self.field, "null", False):
            return value
        return None

    def options(self) -> list[tuple[str, str]]:
        return [("1", _("Yes")), ("0", _("No")), *self._empty_option()]

    def value_q(self, value: str) -> Q | None:
        if value == EMPTY:
            return self._isnull_q()
        return Q(**{self.path: value == "1"})


class DateFilter(Filter):
    """A date or datetime field, filtered by a relative window or a custom range.

    The stored value is the ISO-8601 duration, resolved against today only at query
    time, so a saved view keeps meaning what it said. Windows are whole local days:
    for a ``DateTimeField`` that becomes a half-open ``[start of day, start of the day
    after)`` range, which an index on the column can serve, rather than a ``__date``
    lookup, which one cannot.
    """

    def clean(self, value: str) -> str | None:
        if value in DATE_PRESETS or value == CURRENT_YEAR:
            return value
        if value == EMPTY and getattr(self.field, "null", False):
            return value
        return value if _parse_range(value) else None

    def classify(self) -> FacetCost:
        """Counted only when the column is indexed.

        The buckets are few and fold into one aggregate, but on an unindexed date
        column each of them still costs a scan of the filtered set, which is exactly
        the "costly, render it without counts" case.
        """
        if self.is_multivalued:
            return FacetCost.NEVER
        indexed = self.field.db_index or self.field.unique or self.field.primary_key
        return FacetCost.CHEAP if indexed else FacetCost.COSTLY

    def options(self) -> list[tuple[str, str]]:
        options = [(key, label) for key, (label, _days) in DATE_PRESETS.items()]
        options.append((CURRENT_YEAR, _("This year")))
        return options + self._empty_option()

    def choices(self) -> list[Choice]:
        """The presets, plus a pill for a custom window when one is in effect."""
        choices = super().choices()
        for value in self.values:
            if value in DATE_PRESETS or value in (CURRENT_YEAR, EMPTY):
                continue
            window = _parse_range(value)
            if window:
                choices.append(Choice(value, _range_label(*window), None, True))
        return choices

    def value_q(self, value: str) -> Q | None:
        if value == EMPTY:
            return self._isnull_q()
        today = timezone.localdate()
        if value in DATE_PRESETS:
            days = DATE_PRESETS[value][1]
            return self._window_q(today - dt.timedelta(days=days - 1), today)
        if value == CURRENT_YEAR:
            return self._window_q(dt.date(today.year, 1, 1), today)
        window = _parse_range(value)
        if not window:
            return None
        return self._window_q(*window)

    def _window_q(self, start: dt.date | None, end: dt.date | None) -> Q:
        lookups: dict[str, Any] = {}
        is_datetime = isinstance(self.field, models.DateTimeField)
        if start is not None:
            lookups[f"{self.path}{LOOKUP_SEP}gte"] = _day_start(start) if is_datetime else start
        if end is not None:
            if is_datetime:
                lookups[f"{self.path}{LOOKUP_SEP}lt"] = _day_start(end + dt.timedelta(days=1))
            else:
                lookups[f"{self.path}{LOOKUP_SEP}lte"] = end
        return Q(**lookups)


class RelatedFilter(Filter):
    """A foreign key, one-to-one or many-to-many, filtered by related primary key.

    Options are loaded lazily and capped: past :data:`MAX_FACET_CHOICES` the filter
    becomes COSTLY, offers only the options already selected — so the pills still
    render — and leaves the rest to a typeahead.
    """

    multi = True

    def __init__(self, path: str, hops: list[PathHop], params: Any = None) -> None:
        self._option_cache: list[tuple[str, str]] | None = None
        super().__init__(path, hops, params)

    @property
    def related_model(self) -> type[models.Model]:
        return self.field.related_model

    def clean(self, value: str) -> str | None:
        if value == EMPTY and getattr(self.field, "null", False):
            return value
        try:
            self.field.target_field.to_python(value)
        except (ValidationError, ValueError, TypeError):
            return None
        return value

    def options(self) -> list[tuple[str, str]]:
        if self._option_cache is None:
            self._option_cache = self._load_options()
        return self._option_cache

    def _load_options(self) -> list[tuple[str, str]]:
        # Scoped like every other list of related records: a filter rail offering
        # another tenant's customers leaks their names whether or not you tick one.
        # There is no request here, so this reads the ambient tenant the middleware
        # published. A no-op unless tenancy is on.
        from vantage.tenancy import scope_queryset

        ordering = _option_ordering(self.related_model)
        queryset = scope_queryset(self.related_model._default_manager.all()).order_by(*ordering)
        objects = list(queryset[: MAX_FACET_CHOICES + 1])
        if len(objects) > MAX_FACET_CHOICES:
            # Too many to render as checkboxes, and too many to count: the aggregate
            # would carry one CASE WHEN per row of the related table.
            self.cost = FacetCost.COSTLY
            objects = list(queryset.filter(pk__in=self.values)) if self.values else []
        from vantage.sensitive import safe_str

        return [(str(obj.pk), safe_str(obj)) for obj in objects] + self._empty_option()

    def facet_pairs(self) -> list[tuple[str, Q]] | None:
        if self.cost is not FacetCost.CHEAP:
            return None  # checked before loading, so a skipped filter costs no query
        self.options()  # cardinality is only known once loaded, and may downgrade cost
        return super().facet_pairs()

    def value_q(self, value: str) -> Q | None:
        if value == EMPTY:
            return self._isnull_q()
        try:
            return Q(**{self.path: self.field.target_field.to_python(value)})
        except (ValidationError, ValueError, TypeError):
            return None


class NumericRangeFilter(Filter):
    """A numeric field, filtered by an inclusive ``__gte`` / ``__lte`` pair.

    It owns two parameters rather than one, has no discrete options, and is therefore
    never counted: there is nothing to put a number next to.
    """

    def __init__(self, path: str, hops: list[PathHop], params: Any = None) -> None:
        self.low: Any = None
        self.high: Any = None
        super().__init__(path, hops, params)

    @property
    def low_parameter(self) -> str:
        return f"{self.path}{LOOKUP_SEP}gte"

    @property
    def high_parameter(self) -> str:
        return f"{self.path}{LOOKUP_SEP}lte"

    def expected_parameters(self) -> list[str]:
        return [self.low_parameter, self.high_parameter]

    def parse(self, params: Any) -> list[str]:
        self.low = self._number(_getlist(params, self.low_parameter))
        self.high = self._number(_getlist(params, self.high_parameter))
        return [str(bound) for bound in (self.low, self.high) if bound is not None]

    def _number(self, raw: list[str]) -> Any:
        if not raw:
            return None
        try:
            return self.field.to_python(raw[0])
        except (ValidationError, ValueError, TypeError):
            return None

    def classify(self) -> FacetCost:
        return FacetCost.NEVER

    def q(self) -> Q | None:
        lookups: dict[str, Any] = {}
        if self.low is not None:
            lookups[self.low_parameter] = self.low
        if self.high is not None:
            lookups[self.high_parameter] = self.high
        return Q(**lookups) if lookups else None


# ── construction ────────────────────────────────────────────────────────────


def _filter_class_for(field: Any) -> type[Filter] | None:
    """The filter class for a field, or ``None`` when the field cannot be filtered.

    Relations are checked before choices so that a foreign key never degrades into a
    plain choice list, and choices before the concrete type so that a boolean or an
    integer with ``choices`` keeps its labels.
    """
    if isinstance(field, UNFILTERABLE):
        return None
    if getattr(field, "is_relation", False):
        if field.many_to_one or field.one_to_one or field.many_to_many:
            return RelatedFilter if field.related_model is not None else None
        return None
    if getattr(field, "choices", None):
        return ChoiceFilter
    if isinstance(field, models.BooleanField):
        return BooleanFilter
    if isinstance(field, models.DateField):  # DateTimeField subclasses this
        return DateFilter
    if isinstance(field, models.IntegerField | models.DecimalField | models.FloatField):
        return NumericRangeFilter
    return None


def build_filter(model: type[models.Model], path: str, params: Any = None) -> Filter | None:
    """Build one filter, or return ``None`` when the path cannot carry one.

    A path that does not resolve, or that resolves only partially because it ends in a
    lookup such as ``created_at__year``, is rejected rather than guessed at: half an
    understood path is how a filter silently widens a result set.
    """
    hops = resolve_path(model, path)
    if not hops or LOOKUP_SEP.join(hop.name for hop in hops) != path:
        return None
    filter_class = _filter_class_for(hops[-1].field)
    if filter_class is None:
        return None
    return filter_class(path, hops, params)


def build_filters(
    panel: ModelPanel,
    request: Any = None,
    *,
    facets: bool = False,
    queryset: QuerySet | None = None,
) -> list[Filter]:
    """The filters for a panel, bound to the request's query string.

    ``facets`` is opt-in and off by default. When it is on, ``queryset`` should be the
    page's base queryset with search applied but *without* any of these filters —
    applying them is the facet engine's job, and it needs the unfiltered base to
    compute a disjunctive count.
    """
    params = _query_params(request)
    filters = []
    for path in panel.get_list_filter(request):
        built = build_filter(panel.model, path, params)
        if built is not None:
            filters.append(built)

    if facets and filters:
        base = queryset if queryset is not None else panel.get_queryset(request)
        apply_facet_counts(filters, base)
    return filters


def facets_requested(request: Any) -> bool:
    """Whether the request asked for counts, mirroring Django's ``?_facets``."""
    return FACETS_PARAM in _query_params(request)


def apply_filters(queryset: QuerySet, filters: list[Filter]) -> QuerySet:
    """Apply every filter — AND across filters, OR within each."""
    for filt in filters:
        queryset = filt.queryset(queryset)
    return queryset


# ── faceting ────────────────────────────────────────────────────────────────


def apply_facet_counts(
    filters: list[Filter], queryset: QuerySet, *, budget: int = MAX_FACET_QUERIES
) -> None:
    """Compute and attach counts, in one query per group rather than per choice.

    Filters split into two groups. Everything that is not an *active* multi-select
    filter shares a single aggregate over the fully filtered queryset. Each active
    multi-select filter gets its own aggregate over a queryset with its own predicate
    removed, which is what keeps its unticked options navigable.
    """
    countable = []
    for filt in filters:
        pairs = filt.facet_pairs()
        if pairs:
            countable.append((filt, pairs))

    shared = [entry for entry in countable if not (entry[0].multi and entry[0].is_active)]
    disjunctive = [entry for entry in countable if (entry[0].multi and entry[0].is_active)]

    planned = (1 if shared else 0) + len(disjunctive)
    if planned > budget:
        # Trimming to fit would give some filters counts and others none, which reads
        # as a bug to the user. Dropping the lot is at least legible.
        logger.warning("Skipping facet counts: %d queries needed, budget is %d.", planned, budget)
        return

    # Counting distinct primary keys is only necessary — and only correct — when an
    # applied filter joins a multi-valued relation and fans rows out.
    distinct = any(filt.is_multivalued and filt.is_active for filt in filters)
    base = queryset.order_by()

    if shared:
        _count_group(_narrow(base, filters), shared, distinct=distinct)
    for filt, pairs in disjunctive:
        _count_group(_narrow(base, filters, exclude=filt), [(filt, pairs)], distinct=distinct)


def _narrow(queryset: QuerySet, filters: list[Filter], *, exclude: Filter | None = None):
    """Apply predicates without ``.distinct()``, which aggregates handle themselves."""
    for filt in filters:
        if filt is exclude:
            continue
        predicate = filt.q()
        if predicate is not None:
            queryset = queryset.filter(predicate)
    return queryset


def _count_group(
    queryset: QuerySet, group: list[tuple[Filter, list[tuple[str, Q]]]], *, distinct: bool
) -> None:
    aggregates: dict[str, Count] = {}
    aliases: dict[str, tuple[int, str]] = {}
    for index, (_filter, pairs) in enumerate(group):
        for position, (value, predicate) in enumerate(pairs):
            # Positional aliases, never the value itself: a choice key is user data and
            # has no obligation to be a valid Python identifier.
            alias = f"f{index}_{position}"
            aggregates[alias] = Count("pk", filter=predicate, distinct=distinct)
            aliases[alias] = (index, value)

    if not aggregates:
        return

    result = queryset.aggregate(**aggregates)
    counts: dict[int, dict[str, int]] = {index: {} for index in range(len(group))}
    for alias, (index, value) in aliases.items():
        counts[index][value] = result[alias] or 0
    for index, (filt, _pairs) in enumerate(group):
        filt.set_counts(counts[index])


# ── helpers ─────────────────────────────────────────────────────────────────


def _query_params(request: Any) -> Any:
    """The query string of a request, or the mapping itself if one was passed."""
    if request is None:
        return QueryDict()
    params = getattr(request, "GET", None)
    if params is not None:
        return params
    return request if hasattr(request, "get") else QueryDict()


def _getlist(params: Any, name: str) -> list[str]:
    """Read repeated values from a ``QueryDict`` or a plain dict alike."""
    if params is None:
        return []
    getlist = getattr(params, "getlist", None)
    if getlist is not None:
        return [str(value) for value in getlist(name)]
    value = params.get(name)
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return [str(value)]


def _flat_choices(choices: Any) -> list[tuple[Any, Any]]:
    """Flatten optgroups, which nest a list of pairs under a group label."""
    flat: list[tuple[Any, Any]] = []
    for value, label in choices or ():
        if isinstance(label, list | tuple):
            flat.extend(label)
        else:
            flat.append((value, label))
    return flat


def _option_ordering(model: type[models.Model]) -> list[str]:
    """Order related options by their label, falling back to the model's own order."""
    link = inference.infer_list_link(model)
    hops = resolve_path(model, link)
    if hops and not getattr(hops[-1].field, "primary_key", False):
        return [link]
    return inference.infer_ordering(model)


def _day_start(value: dt.date) -> dt.datetime:
    naive = dt.datetime.combine(value, dt.time.min)
    if django_settings.USE_TZ:
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return naive


def _parse_range(value: str) -> tuple[dt.date | None, dt.date | None] | None:
    if RANGE_SEP not in value:
        return None
    low, _sep, high = value.partition(RANGE_SEP)
    try:
        start = dt.date.fromisoformat(low) if low else None
        end = dt.date.fromisoformat(high) if high else None
    except ValueError:
        return None
    if start is None and end is None:
        return None
    if start and end and start > end:
        return None
    return start, end


def _range_label(start: dt.date | None, end: dt.date | None) -> str:
    if start and end:
        return _("%(start)s to %(end)s") % {
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    if start:
        return _("From %(start)s") % {"start": start.isoformat()}
    return _("Until %(end)s") % {"end": end.isoformat()}
