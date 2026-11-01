"""Saved views: a named filter, sort and search state for a list page.

A saved view is **a query re-evaluated per viewer**, never a stored result set. Two
people opening the same shared view legitimately see different rows and different
counts, because the rows are filtered by
:meth:`~vantage.permissions.PanelPermissions.filter_queryset` on every request. That is
the whole security model in one sentence, and it is why the stored state is only ever
query parameters: **URL filter state has zero security value.** A view cannot widen
access, because nothing in it is ever trusted to decide which rows exist — it decides
only which of the rows you may already see are worth showing you.

Four decisions carry the rest of the design.

**Never autosave a shared view.** Airtable's entire personal/locked view feature line
exists to patch one failure: collaborative views autosave, so one person narrowing a
filter silently changed what everybody else was looking at. :attr:`SavedView.autosaves
<vantage.models.SavedView.autosaves>` is true only for a private, owner-only, unlocked
view — including for the owner of a shared one, who is precisely the person whose
habitual edits would land on everyone else's screen.

**Save degrades to Save-as when the view is immutable to you.** Shopify Polaris's
control set — Save / Save as / Reset — with one rule standing in for every permission
case: not the owner, not a named editor, view is locked, view is somebody else's. All
of them produce the same affordance, so nobody has to be told which of the four
applies before they can get their filter written down.

**Field references are validated on load, never on save.** Fields get renamed and
``list_filter`` gets edited, and both happen long after the view was written. A stored
predicate whose field has gone is rendered as a broken pill with a banner saying the
results may be broader than intended — never auto-repaired, and above all never
silently dropped, because a filter that quietly stops applying shows people more rows
than they asked to see and tells them nothing.

**The URL is ``?view=42&f=<override>``.** ``f`` carries an override query string and is
consumed by :class:`SavedViewApplyView`, which expands it into ordinary filter
parameters and redirects. Expanding eagerly is what lets the list page, the exporter,
the paginator and every ``querystring_replace`` link keep working without any of them
knowing that saved views exist. ``f`` surviving into the canonical URL is the marker
that the page has diverged from what was saved; its absence means pristine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.apps import apps as django_apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q, QuerySet
from django.db.models.constants import LOOKUP_SEP
from django.http import HttpResponseRedirect, QueryDict
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings
from vantage.models import SavedView, SavedViewGrant, SavedViewUserState
from vantage.permissions import get_permissions
from vantage.views import SiteView

__all__ = [
    "LIST_VIEW_KEY",
    "OVERRIDE_PARAM",
    "STATE_VERSION",
    "VIEW_PARAM",
    "BrokenReference",
    "Hydrated",
    "SavedViewApplyView",
    "SavedViewWriteView",
    "autosave",
    "can_delete",
    "can_edit",
    "can_use",
    "capture",
    "enabled",
    "hydrate",
    "list_context",
    "save_mode",
    "visible_views",
]

#: Which saved view is in effect.
VIEW_PARAM = "view"

#: The override. Present means diverged, absent means pristine.
OVERRIDE_PARAM = "f"

#: Stamped into every stored state. Versioned from day one because the filter grammar
#: will grow a nested predicate tree, and a reader that cannot tell which shape it is
#: holding has to guess — which it will eventually do wrongly, on somebody's data.
STATE_VERSION = 1

#: Parameters that belong to the page rather than to a filter, and are stored anyway
#: because a saved view without its search term or its sort is only half a view.
SEARCH_PARAM = "q"
SORT_PARAM = "sort"

#: The stored state's key for the list view (``"kanban"``…). In the URL it shares
#: ``?view=`` with the saved view's own primary key — digits are a saved view, a name
#: is a list view; see :mod:`vantage.listviews`.
LIST_VIEW_KEY = "list_view"
#: The field that list view is arranged by (``?by=``), kept only beside a list view
#: that offers a choice: "open orders, as a board by priority".
LIST_VIEW_FIELD_KEY = "list_view_field"


def _list_view_param(params) -> str:
    """The list-view half of ``?view=``: the first value that is not all digits."""
    for value in params.getlist(VIEW_PARAM):
        value = (value or "").strip()
        if value and not value.isdigit():
            return value
    return ""


def enabled() -> bool:
    """Whether the feature is on.

    Read through ``getattr`` with a default so this module works whether or not the
    project's settings layer has grown a ``SAVED_VIEWS`` key yet — an unknown Vantage
    setting raises rather than returning ``None``, and a feature that cannot be
    imported before it is configured is a feature nobody can configure.
    """
    return bool(getattr(vantage_settings, "SAVED_VIEWS", True))


# ── the stored state ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BrokenReference:
    """A stored predicate that no longer means anything on this model."""

    parameter: str
    values: list[str]
    reason: str

    #: Whether losing this predicate makes the result set *larger*. A dropped filter
    #: does; a dropped sort does not, and telling a user their results may be too
    #: broad when only the ordering changed trains them to ignore the banner.
    widens: bool = True


@dataclass(frozen=True)
class Hydrated:
    """What a stored state turns into against the model as it exists right now."""

    query: str
    broken: list[BrokenReference] = field(default_factory=list)

    @property
    def widened(self) -> bool:
        return any(item.widens for item in self.broken)


def _owned_parameters(panel) -> list[str]:
    """Every query parameter this panel's filters currently own, in rail order.

    Derived from :func:`vantage.filters.build_filters` rather than from the field
    names, because one filter can own two parameters — a numeric range owns
    ``total__gte`` and ``total__lte`` — and a saved view that stored only half of a
    range would restore as a different query.
    """
    from vantage.filters import build_filters

    names: list[str] = []
    for filt in build_filters(panel):
        for name in filt.expected_parameters():
            if name not in names:
                names.append(name)
    return names


def capture(panel, params) -> dict[str, Any]:
    """The current query string, reduced to what a view is allowed to remember.

    An allowlist, not a copy: only parameters the panel's own filters own, plus the
    search term, the sort and the list view (table, board, …). Everything else in the
    URL — the page number, the export flag, the facet opt-in, the saved view's own pk
    and ``f`` — is about this visit rather
    than about this view, and storing it would make "reload my saved view" reopen page
    seven of a CSV download.
    """
    params = _as_query_dict(params)
    filters = {}
    for name in _owned_parameters(panel):
        values = [value for value in params.getlist(name) if value != ""]
        if values:
            filters[name] = values
    state = {
        "version": STATE_VERSION,
        "filters": filters,
        SEARCH_PARAM: params.get(SEARCH_PARAM, "").strip(),
        SORT_PARAM: params.get(SORT_PARAM, "").strip(),
    }
    # How the rows were being looked at is part of the view: "Open orders, as a
    # board" is a different thing to save than the same filter as a table. Only a
    # mode this panel offers is kept, so a hand-made URL cannot store nonsense.
    list_view = _list_view_param(params)
    if list_view and list_view in panel.get_list_views():
        state[LIST_VIEW_KEY] = list_view
        by = params.get(_field_param(), "").strip()
        if by and by in _field_choices(panel, list_view):
            state[LIST_VIEW_FIELD_KEY] = by
    return state


def _field_param() -> str:
    from vantage.listviews.base import ListMode

    return ListMode.FIELD_PARAM


def _field_choices(panel, list_view: str) -> list[str]:
    from vantage.listviews import MODES

    mode = MODES.get(list_view)
    return mode(panel).field_choices() if mode is not None else []


def hydrate(panel, state: dict | None) -> Hydrated:
    """Turn a stored state into a query string, validating it against the model *now*.

    This is the load-time validation the whole feature turns on. A field that has been
    renamed, or simply taken out of ``list_filter``, leaves a stored predicate with
    nothing to bind to. Dropping it silently would show more rows than the view asked
    for and say nothing; guessing at a replacement would show the wrong rows and say
    nothing. So it is reported, and the caller renders it as a broken pill.
    """
    state = state or {}
    owned = set(_owned_parameters(panel))
    broken: list[BrokenReference] = []
    pairs: list[tuple[str, str]] = []

    for name, values in (state.get("filters") or {}).items():
        values = [str(value) for value in values]
        if name in owned:
            pairs.extend((name, value) for value in values)
            continue
        broken.append(BrokenReference(name, values, _why_broken(panel, name)))

    search = str(state.get(SEARCH_PARAM) or "").strip()
    if search:
        pairs.append((SEARCH_PARAM, search))

    sort = str(state.get(SORT_PARAM) or "").strip()
    if sort:
        if sort.lstrip("-") in panel.get_list_display():
            pairs.append((SORT_PARAM, sort))
        else:
            broken.append(
                BrokenReference(
                    SORT_PARAM,
                    [sort],
                    _(
                        "“%(column)s” is no longer a column on this page, "
                        "so the saved ordering was not applied."
                    )
                    % {"column": sort.lstrip("-")},
                    widens=False,
                )
            )

    list_view = str(state.get(LIST_VIEW_KEY) or "").strip()
    if list_view:
        if list_view in panel.get_list_views() and not list_view.isdigit():
            pairs.append((VIEW_PARAM, list_view))
            by = str(state.get(LIST_VIEW_FIELD_KEY) or "").strip()
            if by and by in _field_choices(panel, list_view):
                pairs.append((_field_param(), by))
            elif by:
                broken.append(
                    BrokenReference(
                        _field_param(),
                        [by],
                        _(
                            "“%(field)s” can no longer arrange this view, "
                            "so its default field is used."
                        )
                        % {"field": by},
                        widens=False,
                    )
                )
        else:
            broken.append(
                BrokenReference(
                    VIEW_PARAM,
                    [list_view],
                    _("The “%(view)s” view is no longer offered here, so the table is shown.")
                    % {"view": list_view},
                    widens=False,
                )
            )

    query = QueryDict(mutable=True)
    for name, value in pairs:
        query.appendlist(name, value)
    return Hydrated(query=query.urlencode(), broken=broken)


def _why_broken(panel, parameter: str) -> str:
    """Distinguish "the field is gone" from "the field is no longer a filter here".

    They read identically to the code and completely differently to the person: one is
    a schema change somebody else made, the other is a panel edit they can undo.
    """
    from vantage.introspection import resolve_path

    base = parameter
    for suffix in (f"{LOOKUP_SEP}gte", f"{LOOKUP_SEP}lte"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if not resolve_path(panel.model, base):
        return _("“%(field)s” no longer exists on %(records)s.") % {
            "field": base,
            "records": panel.get_label_plural().lower(),
        }
    return _("“%(field)s” is no longer one of this page's filters.") % {"field": base}


def _normalise(state: dict | None) -> tuple:
    """A comparable form of a state, for "has this diverged from what was saved".

    Value order inside one filter is not meaning — ``?status=paid&status=draft`` is
    the same query either way round — so it is sorted away before comparing. Without
    this, clicking a checkbox off and back on would report the view as diverged.
    """
    state = state or {}
    filters = tuple(
        (name, tuple(sorted(str(value) for value in values)))
        for name, values in sorted((state.get("filters") or {}).items())
        if values
    )
    return (
        filters,
        str(state.get(SEARCH_PARAM) or "").strip(),
        str(state.get(SORT_PARAM) or "").strip(),
        str(state.get(LIST_VIEW_KEY) or "").strip(),
    )


# ── who may do what ─────────────────────────────────────────────────────────


def visible_views(request, panel) -> QuerySet:
    """The views this user may open on this page.

    Gated on the model permission first, so a saved view can never be a side channel
    that tells you a model exists — or what people filter it by — when the panel would
    not have shown you the model at all.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return SavedView.objects.none()
    if not get_permissions().has_model_perm(request, panel.model, "view"):
        return SavedView.objects.none()

    reachable = Q(visibility=SavedView.Visibility.SHARED) | Q(owner=user)
    groups = getattr(user, "groups", None)
    if groups is not None:
        reachable |= Q(grants__user=user) | Q(grants__group__in=groups.all())
    else:  # pragma: no cover - only for a custom user model without groups
        reachable |= Q(grants__user=user)

    return (
        SavedView.objects.filter(model_label=panel.model._meta.label_lower)
        .filter(reachable)
        .distinct()
    )


def can_use(view: SavedView, request) -> bool:
    """Whether this user may open ``view`` at all."""
    user = getattr(request, "user", None)
    if view is None or user is None or not user.is_authenticated:
        return False
    if view.owner_id == user.pk or view.is_shared:
        return True
    return _grant_for(view, user) is not None


def can_edit(view: SavedView, request) -> bool:
    """Whether Save may overwrite ``view`` in place, rather than degrading to Save-as.

    No superuser bypass, deliberately. Being able to rebrand the site is not a reason
    to be able to rewrite a colleague's private view, and Save-as is one click away —
    so the bypass would buy nothing and cost the one guarantee the feature makes.
    Locking binds the creator for the same reason: a lock the owner can ignore does
    not protect anyone from the person most likely to change the view by habit.
    """
    user = getattr(request, "user", None)
    if view is None or user is None or not user.is_authenticated or view.pk is None:
        return False
    if view.locked:
        return False
    if not can_use(view, request):
        return False

    if view.edit_scope == SavedView.EditScope.EVERYONE:
        return True
    if view.owner_id == user.pk:
        return True
    if view.edit_scope == SavedView.EditScope.EDITORS:
        grant = _grant_for(view, user)
        return grant is not None and grant.role == SavedViewGrant.Role.EDITOR
    return False


def can_delete(view: SavedView, request) -> bool:
    """Whether ``view`` may be deleted: by its owner, and nobody else.

    Not :func:`can_edit`. Editing rewrites a filter that the next Save can put back;
    deleting takes with it every colleague's default, and every scheduled report
    anyone subscribed to it. An edit scope of "everyone" is an invitation to refine a
    shared view, not to remove it from under the people relying on it.
    """
    user = getattr(request, "user", None)
    return bool(
        view is not None
        and user is not None
        and user.is_authenticated
        and view.owner_id is not None
        and view.owner_id == user.pk
    )


def save_mode(view: SavedView | None, request) -> str:
    """``"save"`` or ``"save_as"`` — the one rule that covers every permission case."""
    return "save" if can_edit(view, request) else "save_as"


def _grant_for(view: SavedView, user) -> SavedViewGrant | None:
    groups = getattr(user, "groups", None)
    query = Q(user=user)
    if groups is not None:
        query |= Q(group__in=groups.all())
    # Editor beats viewer when both a direct grant and a group grant apply, which is
    # the only ordering that does not take away access somebody was deliberately given.
    return view.grants.filter(query).order_by("role").first()


# ── writing ─────────────────────────────────────────────────────────────────


class VersionConflict(Exception):
    """Someone else saved this view while this page was open."""


def write_state(view: SavedView, request, state: dict, expected_version: int | None) -> SavedView:
    """Overwrite a view's state under an optimistic lock.

    A single conditional ``UPDATE`` rather than read-modify-write: two people pressing
    Save at the same moment is exactly the case this exists for, and a check performed
    in Python has already lost the race by the time it runs. ``expected_version`` of
    ``None`` skips the check, which is only ever right for a view nobody else can see.
    """
    query = SavedView.objects.filter(pk=view.pk)
    if expected_version is not None:
        query = query.filter(version=expected_version)
    updated = query.update(
        state=state,
        version=F("version") + 1,
        updated_by=request.user,
        updated_at=timezone.now(),
    )
    if not updated:
        raise VersionConflict(
            _(
                "“%(name)s” was changed by someone else while this page was open. "
                "Reload to see their version, or use Save as to keep yours."
            )
            % {"name": view.name}
        )
    view.refresh_from_db()
    return view


def autosave(request, panel, view: SavedView | None, params) -> bool:
    """Quietly fold the current filters into ``view``. Returns whether it wrote.

    Refuses for anything that is not a private, owner-only, unlocked view. This is the
    single most consequential guard in the module: a shared view that autosaves is one
    person's filter rewriting everyone else's screen, and every product that has
    shipped it has had to build a second kind of view to escape it.
    """
    if view is None or not view.autosaves or not can_edit(view, request):
        return False
    state = capture(panel, params)
    if _normalise(state) == _normalise(view.state):
        return False
    # No expected version: an autosaving view is private and owner-only, so there is
    # no second writer to lose a race against.
    write_state(view, request, state, expected_version=None)
    return True


def set_default(request, view: SavedView) -> None:
    """Make ``view`` the one that opens instead of the bare list page, for this user.

    Per user, not per view: making a shared view your default must not make it
    everyone's. Clearing the other defaults for the same model is done here rather
    than with a constraint because the model label lives on the view, and a database
    constraint cannot reach through the join to see it.
    """
    siblings = SavedView.objects.filter(model_label=view.model_label).values_list("pk", flat=True)
    SavedViewUserState.objects.filter(user=request.user, view__in=list(siblings)).update(
        is_default=False
    )
    SavedViewUserState.objects.update_or_create(
        view=view, user=request.user, defaults={"is_default": True}
    )


def touch(request, view: SavedView) -> None:
    """Record that this user just opened this view. Never raises."""
    try:
        SavedViewUserState.objects.update_or_create(
            view=view, user=request.user, defaults={"last_used_at": timezone.now()}
        )
    except Exception:  # a bookkeeping row is not worth a 500 on a redirect
        pass


def default_view_for(request, panel) -> SavedView | None:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return (
        visible_views(request, panel)
        .filter(user_states__user=user, user_states__is_default=True)
        .first()
    )


# ── the list-page strip ─────────────────────────────────────────────────────


def list_context(request, panel) -> dict[str, Any]:
    """Everything ``vantage/partials/saved_views.html`` needs. One line from ListView.

    Returns an empty dict when the feature is off or the routes are not mounted, so
    the include costs a page that does not use saved views precisely nothing.

    This can write, on a GET, through :func:`autosave` — and only ever to the
    requesting user's own private, owner-only, unlocked view, whose whole purpose is
    to behave like a workspace rather than a document. Nothing shared is ever touched
    here, which is the property the rest of the feature depends on.
    """
    from django.urls import NoReverseMatch

    if not enabled():
        return {}
    try:
        write_url = reverse("vantage:saved-views")
    except NoReverseMatch:
        # The routes are not mounted. The include is then a no-op rather than a 500,
        # which is what lets the template ship before the urlconf does.
        return {}

    views = list(visible_views(request, panel).order_by("name", "pk"))
    current = _current_view(request, views)

    hydrated = hydrate(panel, current.state) if current is not None else Hydrated(query="")
    if current is not None:
        autosave(request, panel, current, request.GET)

    diverged = current is not None and (
        OVERRIDE_PARAM in request.GET
        or _normalise(capture(panel, request.GET)) != _normalise(current.state)
    )

    from vantage.reportviews import subscribe_context

    return {
        **subscribe_context(request, panel, current),
        "saved_views": [
            {
                "view": item,
                "url": apply_url(item),
                "current": current is not None and item.pk == current.pk,
            }
            for item in views
        ],
        "saved_view": current,
        "saved_view_mode": save_mode(current, request),
        "saved_view_diverged": diverged,
        "saved_view_broken": hydrated.broken,
        "saved_view_widened": hydrated.widened,
        "saved_view_reset_url": apply_url(current) if current is not None else None,
        "saved_view_write_url": write_url,
        "saved_view_model": panel.model._meta.label_lower,
        "saved_view_can_save": current is not None and can_edit(current, request),
    }


def _current_view(request, views: list[SavedView]) -> SavedView | None:
    """The saved view named by the digit half of ``?view=``, if any.

    Every value is scanned rather than ``.get()``, which returns the *last* one:
    ``?view=42&view=kanban`` is saved view 42 shown as a board, and reading only the
    last value would lose the saved view to the list view sharing its parameter.
    """
    for raw in request.GET.getlist(VIEW_PARAM):
        raw = (raw or "").strip()
        if raw.isdigit():
            return next((item for item in views if item.pk == int(raw)), None)
    return None


def apply_url(view: SavedView) -> str:
    return reverse("vantage:saved-view-apply", args=[view.pk])


# ── views ───────────────────────────────────────────────────────────────────


def _panel_for(site, label: str):
    """The panel for ``"shop.order"``, or ``None``."""
    try:
        model = django_apps.get_model(label)
    except (LookupError, ValueError):
        return None
    return site.get_panel(model) if site.is_registered(model) else None


class SavedViewBase(SiteView):
    """Shared resolution: find the view, find its panel, check both."""

    def resolve(self, view: SavedView):
        panel = _panel_for(self.site, view.model_label)
        if panel is None:
            raise PermissionDenied(_("That saved view points at a model this panel does not show."))
        # The model permission again, on every request. The stored query string is
        # user data and proves nothing; this is the check that actually holds.
        if not self.permissions.has_model_perm(self.request, panel.model, "view"):
            raise PermissionDenied(_("You do not have permission to view those records."))
        if not can_use(view, self.request):
            raise PermissionDenied(_("That saved view is not shared with you."))
        return panel


class SavedViewApplyView(SavedViewBase):
    """``GET /_/views/42/`` — expand a view into a real list URL and redirect there.

    Expanding rather than interpreting is the point. Once the redirect lands, the list
    page is looking at ordinary filter parameters, so pagination, sorting, the CSV
    export and every link built by ``querystring_replace`` behave exactly as they do
    without saved views — and the list view needs to know nothing about this module.

    ``?f=<override>`` is honoured here and nowhere else: it is expanded into those same
    parameters and kept in the redirect target, where it reads as "this page has
    diverged from what was saved".
    """

    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        view = self._get_view()
        panel = self.resolve(view)
        touch(request, view)

        override = request.GET.get(OVERRIDE_PARAM)
        if override is not None:
            # The override goes through ``capture`` too, so a hand-made ``f`` cannot
            # smuggle in a parameter the filter layer does not own.
            query = hydrate(panel, capture(panel, QueryDict(override))).query
            marker = f"&{OVERRIDE_PARAM}={_quote(override)}"
        else:
            query = hydrate(panel, view.state).query
            marker = ""

        base = reverse(panel.list_url_name)
        joined = f"{VIEW_PARAM}={view.pk}" + (f"&{query}" if query else "") + marker
        return HttpResponseRedirect(f"{base}?{joined}")

    def _get_view(self) -> SavedView:
        from django.shortcuts import get_object_or_404

        return get_object_or_404(SavedView, pk=self.kwargs["pk"])


class SavedViewWriteView(SavedViewBase):
    """``POST /_/views/`` — save, save as, delete, or make default.

    One endpoint with an ``intent`` rather than four, because every one of them needs
    the same three things resolved first: which panel, which view, and what the current
    query string means. Splitting them would mean four copies of that resolution and
    four chances for one of them to skip the permission check.

    The state is never taken from the request body. It is re-derived from the query
    string of ``next`` — the URL the user was actually looking at — through
    :func:`capture`, which is an allowlist. A posted state could otherwise store a
    parameter the filter layer does not own, and a saved view is a thing other people
    load.
    """

    http_method_names = ["post"]

    INTENTS = ("save", "save_as", "delete", "default", "clear_default")

    def post(self, request, *args, **kwargs):
        if not enabled():
            raise PermissionDenied(_("Saved views are switched off."))

        intent = request.POST.get("intent", "")
        if intent not in self.INTENTS:
            raise PermissionDenied(_("That is not something a saved view can do."))

        target = _safe_target(request)
        handler = getattr(self, f"_{intent}")
        try:
            return handler(request, target)
        except VersionConflict as exc:
            messages.error(request, str(exc))
            return HttpResponseRedirect(target)

    # ── intents ─────────────────────────────────────────────────────────────

    def _save(self, request, target: str):
        view = self._view(request)
        panel = self.resolve(view)
        if not can_edit(view, request):
            # The Save button should already have read "Save as", so reaching here
            # means a stale page or a hand-made POST. Both get the same answer.
            raise PermissionDenied(_("You cannot change that view. Use Save as instead."))
        state = capture(panel, self._params(target))
        # Shared views carry the version check; a private one has no second writer.
        expected = self._expected_version(request) if view.is_shared else None
        write_state(view, request, state, expected)
        messages.success(request, _("Saved “%(name)s”.") % {"name": view.name})
        return HttpResponseRedirect(_with_view(target, view.pk))

    def _save_as(self, request, target: str):
        panel = self._panel(request)
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, _("A saved view needs a name."))
            return HttpResponseRedirect(target)

        visibility = _choice(
            request.POST.get("visibility"), SavedView.Visibility, SavedView.Visibility.PRIVATE
        )
        edit_scope = _choice(
            request.POST.get("edit_scope"), SavedView.EditScope, SavedView.EditScope.OWNER
        )
        view, created = SavedView.objects.get_or_create(
            owner=request.user,
            model_label=panel.model._meta.label_lower,
            name=name,
            defaults={
                "state": capture(panel, self._params(target)),
                "visibility": visibility,
                "edit_scope": edit_scope,
                "updated_by": request.user,
            },
        )
        if not created:
            # The unique constraint is per owner, so this is always the same person's
            # own view. Overwriting it is what they meant; failing would make them
            # invent a second name for the thing they were trying to replace.
            write_state(view, request, capture(panel, self._params(target)), None)

        messages.success(request, _("Saved “%(name)s”.") % {"name": view.name})
        return HttpResponseRedirect(_with_view(target, view.pk))

    def _delete(self, request, target: str):
        view = self._view(request)
        self.resolve(view)
        if not can_delete(view, request):
            raise PermissionDenied(_("You cannot delete that view."))
        name = view.name
        view.delete()
        messages.success(request, _("Deleted “%(name)s”.") % {"name": name})
        return HttpResponseRedirect(_without_view(target))

    def _default(self, request, target: str):
        view = self._view(request)
        self.resolve(view)
        set_default(request, view)
        messages.success(
            request, _("“%(name)s” now opens by default for you.") % {"name": view.name}
        )
        return HttpResponseRedirect(_with_view(target, view.pk))

    def _clear_default(self, request, target: str):
        view = self._view(request)
        self.resolve(view)
        SavedViewUserState.objects.filter(view=view, user=request.user).update(is_default=False)
        return HttpResponseRedirect(_with_view(target, view.pk))

    # ── resolution ──────────────────────────────────────────────────────────

    def _view(self, request) -> SavedView:
        from django.shortcuts import get_object_or_404

        return get_object_or_404(SavedView, pk=request.POST.get("view") or 0)

    def _panel(self, request):
        panel = _panel_for(self.site, request.POST.get("model", ""))
        if panel is None:
            raise PermissionDenied(_("That is not a model this panel shows."))
        if not self.permissions.has_model_perm(request, panel.model, "view"):
            raise PermissionDenied(_("You do not have permission to view those records."))
        return panel

    @staticmethod
    def _params(target: str) -> QueryDict:
        # Not ``_, _, query``: ``_`` is the translation callable in this module.
        query = target.partition("?")[2]
        return QueryDict(query)

    @staticmethod
    def _expected_version(request) -> int | None:
        raw = (request.POST.get("version") or "").strip()
        return int(raw) if raw.isdigit() else None


# ── url helpers ─────────────────────────────────────────────────────────────


def _safe_target(request) -> str:
    """``next`` from the form, if it stays on this site; the site's home otherwise.

    Every intent redirects to it, so an unchecked value made this endpoint an open
    redirect: a page elsewhere could post here and land the person on any address,
    straight from the panel's own origin.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    match = getattr(request, "resolver_match", None)
    return reverse("vantage:dashboard", current_app=getattr(match, "namespace", None))


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _with_view(target: str, pk: int) -> str:
    """Put ``?view=<pk>`` on a URL and take the divergence marker back off it.

    Saving is what makes a page pristine again: the thing it diverged from is now what
    it shows.
    """
    # Not ``path, _, query``: ``_`` is the translation callable in this module.
    path, _sep, query = target.partition("?")
    params = QueryDict(query, mutable=True)
    params.pop(OVERRIDE_PARAM, None)
    # The saved view's pk replaces any other pk; a list-view name sharing the
    # parameter stays, so saving from the board lands back on the board.
    names = [value for value in params.getlist(VIEW_PARAM) if not value.strip().isdigit()]
    params.setlist(VIEW_PARAM, [str(pk), *names])
    return f"{path}?{params.urlencode()}"


def _without_view(target: str) -> str:
    path, _sep, query = target.partition("?")
    params = QueryDict(query, mutable=True)
    names = [value for value in params.getlist(VIEW_PARAM) if not value.strip().isdigit()]
    params.pop(VIEW_PARAM, None)
    if names:
        params.setlist(VIEW_PARAM, names)
    params.pop(OVERRIDE_PARAM, None)
    encoded = params.urlencode()
    return f"{path}?{encoded}" if encoded else path


def build_urls(site) -> list:
    """The two routes saved views need, under the ``_/`` prefix :mod:`vantage.urls`
    already reserves for panel machinery so they can never collide with an app label."""
    from django.urls import path

    return [
        path("_/views/", SavedViewWriteView.as_view(site=site), name="saved-views"),
        path(
            "_/views/<int:pk>/",
            SavedViewApplyView.as_view(site=site),
            name="saved-view-apply",
        ),
    ]


def _as_query_dict(params) -> QueryDict:
    if isinstance(params, QueryDict):
        return params
    query = QueryDict(mutable=True)
    for key, value in (params or {}).items():
        if isinstance(value, list | tuple):
            for item in value:
                query.appendlist(key, str(item))
        else:
            query[key] = str(value)
    return query


def _choice(raw, choices, fallback):
    values = {value for value, _label in choices.choices}
    return raw if raw in values else fallback
