"""Which rows does a list request mean?

One question, asked by four callers: the HTML list page, the CSV export, a background
job resolving a "select all matching" scope, and the JSON API. The answer involves
permissions, soft-delete, search, filters and sort, in that order, and getting the
order wrong is a security bug rather than a cosmetic one — scoping has to run before
anything a URL parameter can influence.

This module exists so there is exactly one place that composes them. It used to be a
method on :class:`~vantage.views.ListView`, which meant every other caller had to
construct a ``TemplateView`` and assign ``request`` to it in order to ask a question
that has nothing to do with rendering. The view still owns presentation — columns,
headers, tree indentation, the pager's markup — and delegates only this.

Nothing here renders, paginates or touches the response. Pagination is deliberately
left out: the HTML page wants numbered pages and the API wants cursors, and that is a
real difference rather than a duplication.
"""

from __future__ import annotations

from typing import NamedTuple

from django.db.models import Q, QuerySet

from vantage.filters import apply_filters, build_filters

__all__ = [
    "ListQuery",
    "build",
    "resolve_sort",
    "search_q",
    "showing_trash",
    "showing_tree",
]

#: GET parameters that do not count as "the user is looking at a subset". Paging,
#: opening the trash, asking for the flat view and requesting facet counts all leave
#: the row *set* alone, so none of them should switch a hierarchy off.
TREE_NEUTRAL_PARAMS = frozenset({"page", "trash", "flat", "_facets"})


class ListQuery(NamedTuple):
    """The resolved list request.

    A tuple rather than a bare queryset because two callers need the working: the facet
    engine needs ``base`` — the rows *before* filters — to compute a disjunctive count,
    since the count for an unticked option has to ignore its own filter or it always
    reads zero. And every caller that renders controls needs ``filters``, ``query`` and
    ``sort`` to show what is currently applied.
    """

    #: The rows themselves: scoped, searched, filtered and ordered.
    queryset: QuerySet
    #: The same rows with search applied but filters not, for facet counting.
    base: QuerySet
    #: The resolved :class:`~vantage.filters.Filter` objects, holding their own state.
    filters: list
    #: The search term, stripped. Empty string when there was none.
    query: str
    #: ``{"path", "descending", "order_by"}``, or ``None`` when unsorted.
    sort: dict | None


def build(panel, request, permissions, *, allow_tree: bool = True) -> ListQuery:
    """Resolve ``request`` against ``panel`` into the rows it names.

    ``allow_tree`` is how a caller that has no way to *draw* a hierarchy opts out of
    being handed one. The JSON API sets it: parent-before-child ordering is a
    presentation choice that only means something next to indentation, and it costs the
    API cursor pagination, because a tree ordering is not one the keyset window can
    express.
    """
    patterns = panel.get_patterns()

    if showing_trash(panel, request):
        queryset = permissions.filter_queryset(
            request, panel.get_trash_queryset(request), "view"
        )
    else:
        queryset = permissions.filter_queryset(
            request, panel.get_list_queryset(request), "view"
        )
        if patterns.soft_delete and not patterns.soft_delete.manager_hides_trashed:
            # A model whose manager does not already exclude them: the list means
            # live records, and the trash is a place you go to deliberately.
            queryset = queryset.filter(patterns.soft_delete.live)

    query = request.GET.get("q", "").strip()
    if query:
        queryset = queryset.filter(search_q(panel, query, request))

    base = queryset
    filters = build_filters(panel, request)
    queryset = apply_filters(queryset, filters)

    sort = resolve_sort(panel, request)
    if sort:
        queryset = queryset.order_by(*sort["order_by"])
    elif allow_tree and showing_tree(panel, request) and patterns.tree.depth_field:
        # Only the library trees have a column that sorts parent-before-child. A plain
        # self-FK is arranged in Python once the page is fetched.
        queryset = queryset.order_by(*patterns.tree.ordering)

    return ListQuery(queryset, base, filters, query, sort)


# ── the pieces ──────────────────────────────────────────────────────────────


def search_q(panel, query: str, request=None) -> Q:
    """An OR across every searchable path, as a substring match.

    The ``^=@`` prefixes are ``contrib.admin``'s notation for anchoring a search term,
    accepted on ``search_fields`` so an adopted ``ModelAdmin`` keeps working. They are
    stripped rather than honoured here, which is the pre-existing behaviour.
    """
    combined = Q()
    for path in panel.get_list_search(request):
        combined |= Q(**{f"{path.lstrip('^=@')}__icontains": query})
    return combined


def resolve_sort(panel, request, allowed: list[str] | None = None) -> dict | None:
    """Validate ``?sort=`` against real, sortable field paths.

    A sort parameter goes straight into ``order_by``, so it is never trusted: anything
    that does not resolve to a concrete single-valued field is dropped. Sorting on a
    multi-valued path would silently duplicate rows.

    ``allowed`` is the set of paths this caller is willing to sort by, defaulting to the
    columns the list page is showing. The JSON API passes the fields it was asked to
    return, so a client can sort by anything it can read — the rule is the same one
    ("only what is on screen"), applied to a screen the client chose.
    """
    from vantage.introspection import resolve_path

    raw = request.GET.get("sort", "").strip()
    if not raw:
        return None

    descending = raw.startswith("-")
    path = raw.lstrip("-")
    if path not in (panel.get_list_display(request) if allowed is None else allowed):
        return None

    hops = resolve_path(panel.model, path)
    if not hops or any(hop.is_multivalued for hop in hops):
        return None

    prefix = "-" if descending else ""
    # A tiebreaker on the primary key keeps pagination stable when the sort column has
    # duplicate values, which is the usual case for a status column.
    return {
        "path": path,
        "descending": descending,
        "order_by": [f"{prefix}{path}", "-pk"],
    }


def showing_tree(panel, request) -> bool:
    """Whether to render the hierarchy rather than a flat page of rows.

    Off while searching, filtering or sorting: those all produce a subset, and a tree
    drawn from a subset shows children whose parents are missing, which reads as
    corruption rather than as a filtered view.
    """
    patterns = panel.get_patterns()
    if patterns.tree is None or request.GET.get("flat") == "1":
        return False
    if request.GET.get("q") or request.GET.get("sort"):
        return False
    return not any(
        key not in TREE_NEUTRAL_PARAMS and value for key, value in request.GET.items()
    )


def showing_trash(panel, request) -> bool:
    """Whether this request is asking for the trash rather than the live list."""
    return (
        request.GET.get("trash") == "1" and panel.get_patterns().soft_delete is not None
    )
