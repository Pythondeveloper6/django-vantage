"""Searching every registered model at once.

The list page's search box asks one model a question. Global search asks all of them,
and the whole difference between the two is restraint.

Two constraints shape everything below, and both are met by refusing to take a
shortcut. The first is access: a result the user cannot open is not a near miss, it is
a leak — the row's ``__str__`` alone can disclose a name, an email address or an order
number. So every model passes :meth:`PanelPermissions.has_model_perm` before it is
searched at all, and every queryset passes through
:meth:`PanelPermissions.filter_queryset` before a single row is read. The result set is
therefore a strict subset of what the user could already reach by hand.

The second is cost: a panel with forty models must not fire forty unbounded queries.
Every model contributes exactly one sliced query, relevance is decided afterwards in
Python over rows the database has already agreed a limit on, and the overall cap is
applied last so a wide search costs the same as a narrow one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from django.db.models import Q
from django.http import JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils.http import urlencode
from django.utils.text import Truncator

from vantage.introspection import resolve_path
from vantage.optimizer import plan_related
from vantage.permissions import get_permissions
from vantage.views import SiteView

__all__ = [
    "GlobalSearch",
    "SearchGroup",
    "SearchHit",
    "SearchJSONView",
    "SearchView",
]

#: Rows offered per model, and across all of them. Small on purpose: the palette is a
#: way to *reach* a record, not a way to browse one model — that is what the list page
#: is for, and every group links to it.
PER_MODEL_LIMIT = 5
TOTAL_LIMIT = 30

#: One character matches most of most databases. Two is where the answer starts
#: meaning something, and it is also what stops a stray keystroke costing N queries.
MIN_LENGTH = 2

#: The prefix characters ``contrib.admin`` allows on a search field. Vantage's list
#: page strips them and matches with ``icontains``; global search agrees with it,
#: because two search boxes that disagree about what a query means is a bug report.
PREFIXES = "^=@"

LABEL_CHARS = 90
SUBLABEL_CHARS = 80

#: Relevance bands, applied to the row's display string. Deliberately coarse: this
#: ranks a handful of already-matching rows, it is not a scoring engine, and anything
#: finer would be a promise the database's ``icontains`` cannot keep.
EXACT = 1.0
PREFIX = 0.8
WORD_START = 0.6
SUBSTRING = 0.4
#: The row matched on some other field — an email, a slug, a related name. Still a
#: real hit, just not one the user can see the reason for at a glance.
ELSEWHERE = 0.2


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One record the user may open."""

    id: str
    label: str
    sublabel: str
    url: str
    score: float

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "sublabel": self.sublabel,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class SearchGroup:
    """The hits from one model, plus the way through to that model's list page."""

    key: str
    label: str
    icon: str
    url: str
    hits: tuple[SearchHit, ...]
    truncated: bool
    score: float

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "icon": self.icon,
            "url": self.url,
            "truncated": self.truncated,
            "results": [hit.as_dict() for hit in self.hits],
        }


class GlobalSearch:
    """One question, asked of every model this request may open.

    Instantiate per request. Subclass and swap the class attributes to retune the
    caps for a particular deployment.
    """

    per_model_limit = PER_MODEL_LIMIT
    total_limit = TOTAL_LIMIT
    min_length = MIN_LENGTH

    #: Rows read per model before scoring, as a multiple of ``per_model_limit``.
    #: Relevance is decided in Python, so the database has to hand over more rows than
    #: will be shown — otherwise ranking only ever reorders an arbitrary slice and an
    #: exact match on row 6 loses to a substring match on row 1.
    scoring_window = 3

    def __init__(
        self,
        site,
        request,
        *,
        per_model_limit: int | None = None,
        total_limit: int | None = None,
    ) -> None:
        self.site = site
        self.request = request
        self.permissions = get_permissions()
        if per_model_limit is not None:
            self.per_model_limit = per_model_limit
        if total_limit is not None:
            self.total_limit = total_limit

    # ── what gets searched ──────────────────────────────────────────────────

    def targets(self) -> list[tuple[Any, list[str], float]]:
        """``(panel, paths, weight)`` for every model worth asking.

        ``visible_panels`` is the gate, and it is deliberately the only way a panel
        enters this list. Nothing downstream re-checks model access, so nothing
        downstream can forget to.
        """
        panels = self.permissions.visible_panels(self.request, list(self.site.panels()))
        found: list[tuple[Any, list[str], float]] = []
        for panel in panels:
            # A panel leaves global search by weighing itself zero, which keeps the
            # opt-out and the ranking dial the same single attribute.
            weight = float(getattr(panel, "search_weight", 1.0))
            if weight <= 0:
                continue
            paths = self.paths(panel)
            if paths:
                found.append((panel, paths, weight))
        return found

    def paths(self, panel) -> list[str]:
        """``panel``'s search fields, minus anything that is not a real field path.

        Dropped rather than raised on: one panel with a typo in ``list_search`` must
        not take global search down for every other model. The system checks are where
        a bad path gets reported.
        """
        resolved = []
        for raw in panel.get_list_search(self.request):
            path = raw.lstrip(PREFIXES)
            if path and resolve_path(panel.model, path):
                resolved.append(path)
        return resolved

    # ── searching ───────────────────────────────────────────────────────────

    def search(self, query: str) -> list[SearchGroup]:
        """Grouped, ranked, capped results for ``query``."""
        query = (query or "").strip()
        if len(query) < self.min_length:
            return []

        groups = [
            group
            for panel, paths, weight in self.targets()
            if (group := self._group(panel, paths, weight, query)) is not None
        ]
        groups.sort(key=lambda group: (-group.score, group.label.lower()))
        return self._cap(groups)

    def as_dict(self, query: str) -> dict:
        """The JSON payload the palette consumes."""
        query = (query or "").strip()
        groups = self.search(query)
        return {
            "query": query,
            "min_length": self.min_length,
            "total": sum(len(group.hits) for group in groups),
            "truncated": any(group.truncated for group in groups),
            "groups": [group.as_dict() for group in groups],
        }

    # ── internals ───────────────────────────────────────────────────────────

    def _group(self, panel, paths: list[str], weight: float, query: str) -> SearchGroup | None:
        queryset = self.permissions.filter_queryset(
            self.request, panel.get_queryset(self.request), "view"
        )
        # Only the joinable half of the plan: a search row shows a name and a subtitle,
        # never a multi-valued column, so a prefetch here would be an extra query per
        # model bought for nothing.
        select, _ = plan_related(panel.model, paths)
        if select:
            queryset = queryset.select_related(*sorted(select))

        queryset = queryset.filter(_search_q(paths, query))
        if _spans_multivalued(panel.model, paths):
            # A reverse or m2m search path multiplies rows by the number of matching
            # children, so the same record would appear three times.
            queryset = queryset.distinct()

        ordering = panel.get_ordering(self.request)
        if ordering:
            queryset = queryset.order_by(*ordering)

        window = self.per_model_limit * self.scoring_window
        rows = list(queryset[: window + 1])
        if not rows:
            return None
        more = len(rows) > window

        hits = []
        for obj in rows[:window]:
            url = _detail_url(panel, obj)
            if url is None:  # pragma: no cover - only when urls are not mounted
                continue
            label = str(obj)
            hits.append(
                SearchHit(
                    id=f"{panel.slug}:{obj.pk}",
                    label=Truncator(label).chars(LABEL_CHARS),
                    sublabel=_sublabel(obj, paths, label),
                    url=url,
                    score=_score(label, query) * weight,
                )
            )
        if not hits:  # pragma: no cover - only when urls are not mounted
            return None

        hits.sort(key=lambda hit: (-hit.score, hit.label.lower()))
        kept = tuple(hits[: self.per_model_limit])
        return SearchGroup(
            key=panel.opts.label_lower,
            label=panel.get_label_plural(),
            icon=panel.get_icon(),
            url=_list_url(panel, query),
            hits=kept,
            truncated=more or len(hits) > len(kept),
            score=kept[0].score,
        )

    def _cap(self, groups: list[SearchGroup]) -> list[SearchGroup]:
        """Spend the overall budget in relevance order, best group first."""
        remaining = self.total_limit
        capped: list[SearchGroup] = []
        for group in groups:
            if remaining <= 0:
                break
            if len(group.hits) > remaining:
                group = replace(group, hits=group.hits[:remaining], truncated=True)
            remaining -= len(group.hits)
            capped.append(group)
        return capped


# ── helpers ─────────────────────────────────────────────────────────────────


def _search_q(paths: list[str], query: str) -> Q:
    combined = Q()
    for path in paths:
        combined |= Q(**{f"{path}__icontains": query})
    return combined


def _spans_multivalued(model, paths: list[str]) -> bool:
    return any(
        hop.is_multivalued for path in paths for hop in resolve_path(model, path)
    )


def _score(label: str, query: str) -> float:
    """How well the row's display string explains why it matched."""
    lowered = label.lower()
    wanted = query.lower()
    if lowered == wanted:
        return EXACT
    index = lowered.find(wanted)
    if index == 0:
        return PREFIX
    if index < 0:
        return ELSEWHERE
    return WORD_START if not lowered[index - 1].isalnum() else SUBSTRING


def _sublabel(obj, paths: list[str], label: str) -> str:
    """A second line, so two records with the same name are still tellable apart.

    The first search field that says something the title does not — usually an email,
    a slug or a reference number. Plain text, never rendered HTML: this string is
    JSON-encoded and written into the DOM by the palette.
    """
    for path in paths:
        value = _traverse(obj, path)
        if value is None or isinstance(value, bool):
            continue
        text = " ".join(str(value).split())
        if text and text != label:
            return Truncator(text).chars(SUBLABEL_CHARS)
    return ""


def _traverse(obj, path: str):
    value = obj
    for part in path.split("__"):
        value = getattr(value, part, None)
        if value is None:
            return None
    return value


def _detail_url(panel, obj) -> str | None:
    try:
        return reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
    except NoReverseMatch:  # pragma: no cover - only when urls are not mounted
        return None


def _list_url(panel, query: str) -> str:
    try:
        base = reverse(panel.list_url_name)
    except NoReverseMatch:  # pragma: no cover - only when urls are not mounted
        return ""
    return f"{base}?{urlencode({'q': query})}"


# ── views ───────────────────────────────────────────────────────────────────


class SearchView(SiteView):
    """The server-rendered results page.

    This is what the header search box submits to, and it is the reason the palette is
    an enhancement rather than a dependency: with JavaScript off, the same query
    against the same permission-filtered querysets renders as a page.
    """

    template_name = "vantage/search.html"
    page_title = "Search"

    def get_breadcrumbs(self):
        return [("Search", None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        search = self.get_search()
        query = self.request.GET.get("q", "").strip()
        groups = search.search(query)
        context.update(
            query=query,
            groups=groups,
            total=sum(len(group.hits) for group in groups),
            min_length=search.min_length,
            page_title=f"Search: {query}" if query else "Search",
        )
        return context

    def get_search(self) -> GlobalSearch:
        return GlobalSearch(self.site, self.request)


class SearchJSONView(SearchView):
    """What the palette calls while the user types.

    Inherits :class:`SearchView` so the two can never drift: the page and the palette
    are the same search, serialised differently.
    """

    def get(self, request, *args, **kwargs):
        payload = self.get_search().as_dict(request.GET.get("q", ""))
        response = JsonResponse(payload)
        # Permission-scoped rows. A shared cache handing one user's results to the
        # next would undo every check above.
        response["Cache-Control"] = "no-store"
        return response
