"""Different ways to look at one list: table, Kanban, calendar, gallery and map.

Every list page used to be a table. This package adds the other four as *modes* of the
same page rather than as pages of their own, which is the decision everything else
follows from. A mode is chosen by ``?view=<name>`` on the list URL and renders inside
the list template, under the same control band, beside the same filter rail and below
the same stats strip, so search, filters, trash, tenancy and permissions apply to it
for free: they are resolved by :func:`vantage.listquery.build` before any mode runs.
A separate ``/kanban/`` route per model would have had to re-implement every one of
those and would eventually have missed one.

**One parameter, two meanings, disjoint values.** ``?view=`` already carried a saved
view's primary key (:data:`vantage.savedviews.VIEW_PARAM`) before modes existed. Rather
than inventing a second parameter for the same word, a mode name is never all digits
and a primary key always is, so both can ride along — ``?view=42&view=kanban`` is saved
view 42 shown as a board. :func:`requested` and ``savedviews._current_view`` each read
only their own half.

**Remembered per person, per model.** Opening a list with no ``?view=`` shows the mode
that person last picked for that model, stored on their
:class:`~vantage.models.ListPreference` row next to the column choices it already
holds. The write happens only when an explicit ``?view=`` differs from what is stored,
so browsing a list costs no write at all.

**Each mode is one class.** :class:`~vantage.listviews.base.ListMode` subclasses in
sibling modules, registered in :data:`MODES`. Adding a sixth view is one module and one
line here, and nothing in :mod:`vantage.views` has to learn its name.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from django.http import QueryDict

from vantage.listviews.base import ListMode
from vantage.listviews.calendar import CalendarMode
from vantage.listviews.gallery import GalleryMode
from vantage.listviews.kanban import KanbanMode
from vantage.listviews.map import MapMode
from vantage.listviews.table import TableMode

__all__ = [
    "MODES",
    "MODE_PARAMS",
    "VIEW_PARAM",
    "ListMode",
    "available",
    "build_urls",
    "mode_url",
    "remember",
    "requested",
    "resolve",
    "supported",
    "switcher",
]

logger = logging.getLogger("vantage.listviews")

#: Shared with :mod:`vantage.savedviews` on purpose — see the module docstring.
VIEW_PARAM = "view"

#: Every mode, by name, in the order the switcher draws them when a panel does not say.
MODES: dict[str, type[ListMode]] = {
    mode.name: mode for mode in (TableMode, KanbanMode, CalendarMode, GalleryMode, MapMode)
}

#: Query parameters that belong to one mode's own navigation — the month on show, the
#: Kanban column being expanded. Dropped when switching to another mode, because a
#: month means nothing to a board and keeping it would make the URL lie.
MODE_PARAMS = ("page", "month", "more")


def supported(panel, name: str) -> bool:
    """Whether ``panel``'s model can be drawn as ``name``. Unknown names cannot."""
    mode = MODES.get(name)
    return mode is not None and mode.supports(panel)


def available(panel, request=None) -> list[type[ListMode]]:
    return [MODES[name] for name in panel.get_list_views(request) if name in MODES]


def requested(request) -> str | None:
    """The mode named in the URL, or ``None``. Digit values are saved views, skipped."""
    if request is None:
        return None
    for value in request.GET.getlist(VIEW_PARAM):
        value = (value or "").strip()
        if value and not value.isdigit():
            return value
    return None


def resolve(panel, request) -> ListMode:
    """The mode this request renders: the URL, else the remembered one, else the first.

    A name the panel does not offer — a stale bookmark, a hand-typed URL, a mode the
    panel author has since switched off — falls back rather than erroring. Falling back
    is honest here because the switcher shows which mode actually rendered.
    """
    modes = available(panel, request)
    names = [mode.name for mode in modes]
    wanted = requested(request)
    if wanted in names:
        remember(panel, request, wanted, default=names[0])
        return MODES[wanted](panel)
    stored = _stored(panel, request)
    if stored in names:
        return MODES[stored](panel)
    return modes[0](panel) if modes else TableMode(panel)


def mode_url(request, name: str) -> str:
    """The current URL with the mode swapped for ``name``. Keeps the saved view."""
    params = request.GET.copy() if request is not None else QueryDict(mutable=True)
    kept = [value for value in params.getlist(VIEW_PARAM) if value.strip().isdigit()]
    params.setlist(VIEW_PARAM, [*kept, name])
    for key in MODE_PARAMS:
        params.pop(key, None)
    return f"?{params.urlencode()}"


def switcher(panel, request, current: ListMode) -> list[dict[str, Any]]:
    """The segmented control's entries. Empty when there is nothing to switch between."""
    modes = available(panel, request)
    if len(modes) < 2:
        return []
    return [
        {
            "name": mode.name,
            "label": mode.label,
            "icon": mode.icon,
            "url": mode_url(request, mode.name),
            "current": mode.name == current.name,
        }
        for mode in modes
    ]


# ── remembering ─────────────────────────────────────────────────────────────


def _stored(panel, request) -> str | None:
    """The mode this person last chose here, read off the already-resolved spec.

    :func:`vantage.listprefs.resolve` read the row in the one query it makes per list
    page, so asking costs nothing.
    """
    from vantage import listprefs

    if request is None:
        return None
    return getattr(listprefs.resolve(panel, request), "list_view", None)


def remember(panel, request, name: str, *, default: str | None = None) -> None:
    """Store ``name`` as this person's mode for this model. Never raises.

    Skipped when nothing would change, and when a person with no stored row picks the
    default — a row that only says "the table, as usual" is a tombstone.
    """
    from vantage import listprefs

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return
    spec = listprefs.resolve(panel, request)
    current = getattr(spec, "list_view", None)
    if current == name or (current is None and name == default):
        return
    try:
        model = listprefs._model()
        if model is None:  # pragma: no cover - only when the app is not installed
            return
        row = listprefs.row_for(panel, user)
        if row is None:
            row = model(resource_key=listprefs.resource_key(panel), user=user)
        row.list_view = name
        row.save()
    except Exception:  # a preference is not worth a 500 on a page view
        logger.exception("Could not remember the list view for %s.", panel.opts.label)
        return
    cache = getattr(request, listprefs._CACHE_ATTR, None)
    if cache is not None:
        cache[spec.key] = replace(spec, list_view=name)


def build_urls(site) -> list:
    """Per-panel routes the modes need. Only the Kanban move writes anything."""
    from vantage.listviews.kanban import build_urls as kanban_urls

    return kanban_urls(site)
