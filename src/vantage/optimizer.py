"""Deriving query optimisation from what a page actually renders.

The N+1 query is the single worst thing about a naive admin, and it is entirely
mechanical to avoid: every column, every filter and every search field names a path,
and each path is either joinable or it is not. This module turns a set of paths into
the ``select_related`` / ``prefetch_related`` pair that covers them.

The rule: walk the path staying in ``select_related`` while every hop is joinable;
the moment a hop is multi-valued — a reverse FK, an M2M, a generic relation — the whole
path becomes one ``prefetch_related`` lookup.
"""

from __future__ import annotations

from django.db.models import Model
from django.db.models.constants import LOOKUP_SEP

from vantage.introspection import resolve_path

__all__ = ["plan_related"]


def plan_related(model: type[Model], paths: list[str] | set[str]) -> tuple[set[str], set[str]]:
    """Return ``(select_related, prefetch_related)`` covering ``paths``.

    Paths that name a plain local field contribute nothing — there is no relation to
    follow. Paths that do not resolve at all are ignored rather than raising, because
    ``list_display`` legitimately contains panel methods and model properties; the
    system checks are what report a genuinely bad path.
    """
    select: set[str] = set()
    prefetch: set[str] = set()

    for path in paths:
        hops = resolve_path(model, path)
        prefix: list[str] = []

        for hop in hops:
            if not hop.is_relation:
                break

            if hop.is_multivalued:
                # Everything from here on has to be prefetched, as one lookup.
                prefetch.add(LOOKUP_SEP.join([*prefix, hop.name]))
                break

            if hop.is_joinable:
                prefix.append(hop.name)
                select.add(LOOKUP_SEP.join(prefix))
                continue

            # A GenericForeignKey: no single related model, so it can never be joined.
            prefetch.add(LOOKUP_SEP.join([*prefix, hop.name]))
            break

    # A shorter select_related path is implied by any longer one that extends it.
    select = {
        s
        for s in select
        if not any(other != s and other.startswith(s + LOOKUP_SEP) for other in select)
    }
    return select, prefetch
