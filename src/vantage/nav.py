"""Building the sidebar.

Three layers, resolved narrowest first — the order this package uses everywhere::

    NavConfiguration row  >  VANTAGE["NAV"]  >  per-app inference

**Inference** is the floor: registering a model is enough to make it reachable, grouped
by app, sorted alphabetically. **Declaration** is ``VANTAGE["NAV"]``: the site owner
states the groups, their order, which items go in them, what those items are called,
and any extra links they want alongside. **The stored arrangement** is the same thing
built in the settings screen instead of in Python, and it wins — until somebody presses
Reset, which deletes the row rather than freezing today's answer, so the code becomes
the sidebar again *and stays* the sidebar when the code changes.

None of the upper two layers can grant access. A stored or declared item naming a model
is resolved against the panels this request may actually see, so an arrangement written
by a superuser never reveals to a colleague that a model exists, let alone links to it.
Reordering, renaming, hiding and extra links are presentation; who may see what is
decided one layer down and asked again on every request.

The other property worth stating once: a model registered *after* an arrangement was
saved still appears. Unmentioned panels are appended exactly as they are under a Python
declaration, honouring ``VANTAGE["NAV_UNLISTED"]``, because a sidebar that silently
omits new models is a worse failure than an untidy one.

That leaves one more question the same paragraph raises: a panel absent from the
arrangement is *either* new since the save *or* placed once and then taken out through
the editor's own "remove" control, and those are opposite decisions. ``known_keys`` on
:class:`~vantage.models.NavConfiguration` is what tells them apart — the same snapshot
:class:`~vantage.models.DashboardLayout` keeps for the dashboard, for the identical
reason. See :func:`save_declaration` and :func:`_stored_nav`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from django.apps import apps as django_apps
from django.core.signals import request_started
from django.db.utils import OperationalError, ProgrammingError
from django.dispatch import receiver
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings

__all__ = [
    "ITEM_KINDS",
    "SAFE_SCHEMES",
    "EditResult",
    "EditorGroup",
    "EditorItem",
    "NavGroup",
    "NavItem",
    "apply_edits",
    "build_nav",
    "check_nav",
    "clean_declaration",
    "clean_item",
    "clean_url",
    "code_declaration",
    "editor_context",
    "icon_names",
    "invalidate_nav",
    "may_see",
    "reset_declaration",
    "resolve_theme",
    "save_declaration",
    "stored_declaration",
    "stored_known_keys",
]


@dataclass
class NavItem:
    label: str
    url: str
    icon: str | None = None
    key: str = ""
    external: bool = False
    badge: str | None = None

    def is_current(self, path: str) -> bool:
        """Whether this item covers the given path.

        Prefix matching so a detail page keeps its list highlighted, but the panel
        root is compared exactly or it would match everything. External links never
        match — they are not part of this site.
        """
        if not self.url or self.external:
            return False
        if self.url.rstrip("/") == "":
            return path.rstrip("/") == ""
        return path == self.url or path.startswith(self.url)


@dataclass
class NavGroup:
    label: str
    items: list[NavItem] = field(default_factory=list)
    icon: str | None = None
    collapsed: bool = False


# ── shared pieces ───────────────────────────────────────────────────────────


def _panel_item(panel) -> NavItem | None:
    try:
        url = reverse(panel.list_url_name)
    except NoReverseMatch:  # pragma: no cover - only when urls are not mounted
        return None
    return NavItem(
        label=panel.get_label_plural(),
        url=url,
        icon=panel.get_icon(),
        key=panel.opts.label_lower,
    )


def _page_item(page) -> NavItem | None:
    """``page`` is the registered class, not an instance."""
    slug = page.get_slug()
    try:
        url = reverse(f"vantage:page-{slug}")
    except NoReverseMatch:  # pragma: no cover
        return None
    return NavItem(
        label=page.get_title(),
        url=url,
        icon=getattr(page, "icon", None),
        key=f"page:{slug}",
    )


def _visible_pages(site, request) -> list:
    """Pages the user may actually open.

    ``Page.has_permission`` is the page's own gate, and it has to be consulted here
    too: listing a page that 403s on click is a worse experience than omitting it,
    and for a page gated on something sensitive the label itself can be a disclosure.
    """
    pages = list(site.pages())
    if request is None:
        return pages
    return [page for page in pages if page(site).has_permission(request)]


def _visible_panels(site, request) -> list:
    from vantage.permissions import get_permissions

    panels = list(site.panels())
    if request is None:
        return panels
    return get_permissions().visible_panels(request, panels)


# ── derived navigation ──────────────────────────────────────────────────────


def _derived_nav(site, request) -> list[NavGroup]:
    groups: dict[str, NavGroup] = {}
    weight: dict[str, int] = {}

    def bucket(label: str, order: int) -> NavGroup:
        if label not in groups:
            groups[label] = NavGroup(label=label)
            weight[label] = order
        weight[label] = min(weight[label], order)
        return groups[label]

    for panel in _visible_panels(site, request):
        item = _panel_item(panel)
        if item is not None:
            bucket(panel.get_group(), panel.order).items.append(item)

    for page in _visible_pages(site, request):
        item = _page_item(page)
        if item is not None:
            group = getattr(page, "group", None) or _("Pages")
            bucket(group, getattr(page, "order", 0)).items.append(item)

    for group in groups.values():
        group.items.sort(key=lambda item: item.label.lower())

    return [groups[label] for label in sorted(groups, key=lambda label: (weight[label], label))]


def _grouped(panels) -> list[NavGroup]:
    """Derived grouping for a subset of panels, used for NAV leftovers."""
    groups: dict[str, NavGroup] = {}
    for panel in panels:
        item = _panel_item(panel)
        if item is None:
            continue
        groups.setdefault(panel.get_group(), NavGroup(label=panel.get_group())).items.append(item)
    for group in groups.values():
        group.items.sort(key=lambda item: item.label.lower())
    return [groups[label] for label in sorted(groups)]


# ── declared navigation ─────────────────────────────────────────────────────


def _resolve_item(spec: Any, panels_by_label: dict, pages_by_slug: dict) -> NavItem | None:
    """Turn one ``NAV`` entry into a link, or ``None`` if it cannot be shown.

    Returning ``None`` rather than raising is deliberate for permissions and for
    models that are not registered: a sidebar that 500s because someone lost a
    permission is a poor trade for strictness. A *malformed* entry does raise, because
    that is a typo the author wants to hear about — and the system check reports it at
    startup rather than leaving it to a request.
    """
    if isinstance(spec, str):
        spec = {"model": spec}
    if not isinstance(spec, dict):
        raise TypeError(
            f"Each VANTAGE['NAV'] item must be a dict or a model label, got {spec!r}."
        )

    label = spec.get("label")
    icon = spec.get("icon")

    if "model" in spec:
        panel = panels_by_label.get(str(spec["model"]).lower())
        item = _panel_item(panel) if panel is not None else None
        if item is None:
            return None
        return NavItem(
            label=label or item.label,
            url=item.url,
            icon=icon or item.icon,
            key=item.key,
            badge=spec.get("badge"),
        )

    if "page" in spec:
        page = pages_by_slug.get(str(spec["page"]))
        item = _page_item(page) if page is not None else None
        if item is None:
            return None
        return NavItem(
            label=label or item.label, url=item.url, icon=icon or item.icon, key=item.key
        )

    if "route" in spec:
        try:
            url = reverse(spec["route"], args=tuple(spec.get("args") or ()))
        except NoReverseMatch:
            return None
        return NavItem(
            label=label or str(spec["route"]), url=url, icon=icon, key=str(spec["route"])
        )

    if "url" in spec:
        url = str(spec["url"])
        external = spec.get("external")
        if external is None:
            external = url.startswith(("http://", "https://", "//"))
        return NavItem(
            label=label or url,
            url=url,
            icon=icon,
            key=url,
            external=bool(external),
            badge=spec.get("badge"),
        )

    raise KeyError(
        f"A VANTAGE['NAV'] item needs one of 'model', 'page', 'route' or 'url'; got {spec!r}."
    )


def _declared_nav(site, request, declaration) -> list[NavGroup]:
    panels = _visible_panels(site, request)
    panels_by_label = {p.opts.label_lower: p for p in panels}
    pages_by_slug = {p.get_slug(): p for p in _visible_pages(site, request)}

    groups: list[NavGroup] = []
    claimed: set[str] = set()

    for entry in declaration:
        if isinstance(entry, dict) and "items" in entry:
            specs = entry["items"]
            group = NavGroup(
                label=entry.get("group") or entry.get("label") or "",
                icon=entry.get("icon"),
                collapsed=bool(entry.get("collapsed")),
            )
        else:
            # A bare item at the top level lives in an unlabelled group, which renders
            # without a heading — the usual shape for a Dashboard or Home link.
            specs = [entry]
            group = NavGroup(label="")

        for spec in specs:
            item = _resolve_item(spec, panels_by_label, pages_by_slug)
            if item is None:
                continue
            group.items.append(item)
            claimed.add(item.key)

        if group.items:
            groups.append(group)

    if vantage_settings.NAV_UNLISTED == "append":
        # Anything unmentioned still appears, so registering a new model never makes it
        # silently unreachable. Set NAV_UNLISTED = "hide" for a strict sidebar.
        leftovers = [p for p in panels if p.opts.label_lower not in claimed]
        groups.extend(_grouped(leftovers))

    return groups


# ── the stored arrangement: what one entry is allowed to be ─────────────────
#
# Everything below treats its input as hostile, because two different kinds of hostile
# arrive here. One is a POST from the settings screen, which is a form and therefore a
# thing anybody with the permission can hand-craft. The other is a *row written months
# ago*, against a registry that has since changed — the same problem `vantage.listprefs`
# solves the same way, and for the same reason: a stored value that has stopped making
# sense must degrade into the code's answer, never into a 500 on every page of the
# panel, because the sidebar is on every page of the panel.
#
# Nothing here raises. An entry that cannot be validated is dropped, and a declaration
# that validates down to nothing is `None`, which is indistinguishable from never having
# been saved and resolves to whatever the code declares.

#: The four things a sidebar entry can point at. These are also exactly the keys
#: :func:`_resolve_item` reads, which is what lets a stored item become a declared spec
#: without a translation table in between.
ITEM_KINDS: tuple[str, ...] = ("model", "page", "route", "url")

#: What an external link may start with. An allowlist rather than a ``javascript:``
#: denylist: the denylist has to be right about every scheme a browser will ever add,
#: including ``data:``, ``vbscript:`` and whatever comes next, and the allowlist only
#: has to be right about the three anybody actually wants in a sidebar.
SAFE_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

MAX_LABEL = 60
MAX_BADGE = 20
MAX_URL = 500
MAX_ARGS = 4
#: Bounds on the whole tree. Not a security boundary — every item is validated
#: individually — but one row is read on every page render, and a sidebar with four
#: hundred groups in it is a denial of service against the person looking at it.
MAX_GROUPS = 40
MAX_ITEMS = 400
MAX_AUDIENCES = 20

_MODEL_LABEL = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_PAGE_SLUG = re.compile(r"^[-\w]+$")
_ROUTE_NAME = re.compile(r"^[a-zA-Z_][\w.:-]*$")
_PERMISSION = re.compile(r"^[a-zA-Z_][\w]*\.[\w]+$")


def icon_names() -> tuple[str, ...]:
    """The bundled icon set, as the editor's menu and as the validator's allowlist.

    One source for both, so the picker can never offer an icon that would then be
    silently dropped on save. Imported here rather than at module scope because the
    template tag library imports Django's template machinery, and this module is
    imported while the app registry is still loading.
    """
    from vantage.templatetags.vantage import ICONS

    return tuple(sorted(ICONS))


def clean_icon(value: Any) -> str:
    """An icon from the bundled set, or ``""`` meaning "whatever the item would use"."""
    return value if isinstance(value, str) and value in icon_names() else ""


def clean_url(value: Any) -> str | None:
    """A URL a sidebar link may point at, or ``None`` if it may not point there.

    Three rules, in the order they matter:

    **A URL with a control character or an interior space in it is refused, not
    repaired.** A browser ignores tabs and newlines inside a scheme, so
    ``java\\tscript:alert(1)`` navigates while a check reading the raw string sees a
    scheme that does not exist — this is the bug the function exists to not have.
    Stripping the characters and carrying on would fix that one and introduce another:
    a stored URL nobody typed. Refusing says so instead.

    **The scheme is allowlisted.** ``http``, ``https`` and ``mailto``; everything else,
    named or unnamed, is refused.

    **A site-relative link starts with a slash.** Anything else without a scheme is
    read as a hostname somebody typed without one and becomes ``https://`` — because
    "example.com" in a link box means the website, and guessing the other way would
    turn a typo into a link to a page of this panel that does not exist.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_URL:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None

    if text.startswith("//"):
        # Protocol-relative. Pinned to https rather than left to inherit the panel's
        # scheme, so a link cannot silently downgrade on an http deployment.
        text = "https:" + text
    elif text.startswith("/"):
        return text

    parts = urlsplit(text)
    if not parts.scheme:
        text = "https://" + text
        parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in SAFE_SCHEMES:
        return None
    if scheme != "mailto" and not parts.netloc:
        return None
    return text


def is_external(url: str) -> bool:
    """Whether a cleaned URL leaves this site. Site-relative links start with a slash."""
    return bool(url) and not url.startswith("/")


def _text(value: Any, limit: int) -> str:
    """Plain, trimmed, bounded text. A lazy translation is resolved by the caller.

    ``str`` rather than ``str | None``: this goes into a JSON column, and having one
    spelling of "nothing was typed" — the empty string — is what stops the resolution
    code below needing to ask twice.
    """
    return value.strip()[:limit] if isinstance(value, str) else ""


def _clean_permission(value: Any) -> str:
    text = _text(value, 100)
    return text if _PERMISSION.match(text) else ""


def _clean_audiences(value: Any) -> list[str]:
    """Group *names*, deduplicated and bounded. Names, not ids — see the model."""
    if not isinstance(value, list | tuple):
        return []
    kept: list[str] = []
    for item in value:
        name = _text(item, 150)
        if name and name not in kept:
            kept.append(name)
        if len(kept) >= MAX_AUDIENCES:
            break
    return kept


def _clean_args(value: Any) -> list[str]:
    """Positional arguments for a named route.

    Carried through the editor as hidden inputs and never offered as a control. A route
    that needs arguments is a thing somebody wrote in Python, and the point of keeping
    them is that saving the sidebar from the UI must not quietly break it.
    """
    if not isinstance(value, list | tuple):
        return []
    return [_text(str(item), 100) for item in value if str(item).strip()][:MAX_ARGS]


def _clean_target(kind: str, value: Any) -> str | None:
    if kind == "url":
        return clean_url(value)
    text = _text(value if isinstance(value, str) else str(value or ""), 200)
    if kind == "model":
        text = text.lower()
        return text if _MODEL_LABEL.match(text) else None
    if kind == "page":
        return text if _PAGE_SLUG.match(text) else None
    return text if _ROUTE_NAME.match(text) else None


def clean_item(raw: Any) -> dict | None:
    """One stored or posted entry, or ``None`` if it is not one."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in ITEM_KINDS:
        return None
    target = _clean_target(kind, raw.get("target"))
    if target is None:
        return None
    return {
        "kind": kind,
        "target": target,
        "label": _text(raw.get("label"), MAX_LABEL),
        "icon": clean_icon(raw.get("icon")),
        "badge": _text(raw.get("badge"), MAX_BADGE),
        "hidden": bool(raw.get("hidden")),
        "permission": _clean_permission(raw.get("permission")),
        "audiences": _clean_audiences(raw.get("audiences")),
        "args": _clean_args(raw.get("args")) if kind == "route" else [],
    }


def _clean_group(raw: Any, budget: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    items: list[dict] = []
    for entry in raw.get("items") or ():
        if len(items) >= budget:
            break
        item = clean_item(entry)
        if item is not None:
            items.append(item)
    return {
        "label": _text(raw.get("label"), MAX_LABEL),
        "icon": clean_icon(raw.get("icon")),
        "items": items,
    }


def clean_declaration(raw: Any) -> list[dict] | None:
    """A whole stored arrangement, or ``None`` meaning "let the code decide".

    An empty group survives cleaning even though it renders nothing: creating a group
    and then filling it is two steps, and a group that vanished between them would make
    the second step impossible.

    An empty *arrangement* survives too, and is not the same as ``None``. Somebody who
    empties the sidebar has said something — with ``NAV_UNLISTED = "append"`` they get
    the plain per-app list back, which is a reasonable thing to ask for — and turning
    that into "no opinion" would silently reinstate a declaration they had just removed.
    """
    if not isinstance(raw, list | tuple):
        return None
    groups: list[dict] = []
    budget = MAX_ITEMS
    for entry in list(raw)[:MAX_GROUPS]:
        group = _clean_group(entry, budget)
        if group is None:
            continue
        budget -= len(group["items"])
        groups.append(group)
    return groups


# ── the stored arrangement: reading the row ─────────────────────────────────
#
# Cached exactly the way `vantage.siteconfig` caches the settings row, and for the same
# three reasons: nothing may touch the database during app loading or `check`; a missing
# table must resolve to the code rather than raise, or the migration that would create
# it could never run; and one read per request keeps a page's query count constant
# instead of "one extra on whichever request happened to be first".
#
# The row itself, not just the declaration derived from it, is what is cached here,
# because :mod:`vantage.topbar` needs the same row too. The sidebar's stored arrangement
# and the top bar's stored layout are two columns of one row (see both modules'
# docstrings), so a page that renders both must pay for that row once, not once per
# module that asks for it.

_UNSET: Any = object()
_UNAVAILABLE: Any = object()
_row_cache: Any = _UNSET


def _config_model():
    """The model class, or ``None`` if asking is not safe yet."""
    from django.apps import apps

    if not apps.ready:
        return None
    try:
        return apps.get_model("vantage", "NavConfiguration")
    except LookupError:  # pragma: no cover - only when the app is not installed
        return None


def _read_row() -> Any:
    """The stored row, or ``_UNAVAILABLE`` when the database cannot answer."""
    model = _config_model()
    if model is None:
        return _UNAVAILABLE
    try:
        row = model.objects.filter(pk=model.SINGLETON_PK).first()
    except (OperationalError, ProgrammingError):
        # No table yet, or no database. Both mean the same thing here: the code is the
        # whole truth for now.
        return _UNAVAILABLE
    except RuntimeError as exc:
        # pytest-django blocks the connection for tests that did not ask for a
        # database, and rendering a sidebar is not a reason to fail one. Narrowed on
        # the blocker's own message so a real RuntimeError still surfaces.
        if "Database access not allowed" not in str(exc):
            raise
        return _UNAVAILABLE
    return row


def _singleton_row() -> Any:
    """The one ``NavConfiguration`` row for this request, or ``None``.

    Read at most once per request no matter how many of :func:`stored_declaration` and
    :mod:`vantage.topbar`'s ``stored_layout`` ask for it. A miss — no row at all — is
    cached exactly like a hit; an *unavailable database* is not, or a process that
    started before ``migrate`` would never notice the table appearing.
    """
    global _row_cache
    if _row_cache is not _UNSET:
        return _row_cache
    value = _read_row()
    if value is _UNAVAILABLE:
        return None
    _row_cache = value
    return _row_cache


def stored_declaration() -> list[dict] | None:
    """The UI arrangement, or ``None`` when there is none and the code decides."""
    row = _singleton_row()
    return None if row is None else clean_declaration(row.groups)


def stored_known_keys() -> frozenset[str]:
    """Model keys ever claimed by a save of the stored arrangement.

    Empty when there is no row, or when the row predates this field — both read as
    "nothing is known to have been removed", which is the safe direction to be wrong
    in: nothing is dropped from the sidebar that was not explicitly taken out through
    the editor. See :func:`save_declaration` and :func:`_stored_nav`.
    """
    row = _singleton_row()
    return frozenset(row.known_keys or ()) if row is not None else frozenset()


def invalidate_nav() -> None:
    """Forget the shared row. Called on save, on delete, and at the start of each request."""
    global _row_cache
    _row_cache = _UNSET


@receiver(request_started)
def _invalidate_per_request(sender, **kwargs) -> None:
    """One read per request, so a save is visible to every worker on the next one."""
    invalidate_nav()


def _claimed_model_keys(declaration: list[dict] | None) -> set[str]:
    """Every model key an arrangement names, hidden or not.

    Matches exactly what :func:`_stored_nav` treats as "claimed" — a model mentioned
    in the declaration, whichever way it is shown — because this is the record that
    later tells the two kinds of "not mentioned" apart.
    """
    return {
        item["target"]
        for group in declaration or ()
        for item in group.get("items") or ()
        if item.get("kind") == "model"
    }


def save_declaration(declaration: list[dict] | None, *, user=None) -> None:
    """Store an arrangement. ``None`` is not a save — use :func:`reset_declaration`.

    Every model this declaration claims joins :attr:`NavConfiguration.known_keys`,
    which only ever grows. That is what lets a later save tell "this model was never
    placed" (still an unlisted leftover, appended for as long as ``NAV_UNLISTED`` says
    so) apart from "this model was placed once and then explicitly taken out" (stays
    gone) — see :func:`_stored_nav`. A key can only enter this set by having been an
    editable row in the settings screen, which a leftover the user never touched never
    was, so resaving the screen without touching a leftover can never make it vanish.
    """
    model = _config_model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return
    row = model.load()
    row.groups = declaration
    row.known_keys = sorted(set(row.known_keys or ()) | _claimed_model_keys(declaration))
    row.updated_by = user if getattr(user, "is_authenticated", False) else None
    row.save()


def reset_declaration() -> None:
    """Hand the sidebar back to the code by deleting the row.

    Deleting rather than writing today's resolved tree into it. That is the whole
    difference between a reset that works and one that looks like it worked: a stored
    copy of what the code says today stops tracking the code tomorrow.
    """
    model = _config_model()
    if model is None:  # pragma: no cover - only when the app is not installed
        return
    model.objects.filter(pk=model.SINGLETON_PK).delete()


# ── the stored arrangement: rendering it ────────────────────────────────────


def _spec(item: dict) -> dict:
    """One stored item as the declaration spec :func:`_resolve_item` already reads."""
    spec: dict[str, Any] = {
        item["kind"]: item["target"],
        "label": item["label"] or None,
        "icon": item["icon"] or None,
    }
    if item["badge"]:
        spec["badge"] = item["badge"]
    if item["kind"] == "route" and item["args"]:
        spec["args"] = item["args"]
    return spec


def _audience_names(request, user) -> set[str]:
    """The groups this user is in, read once per request.

    Cached on the request because the sidebar is rendered once but this is asked once
    per gated item, and a sidebar is not a place to spend a query per link.
    """
    cached = getattr(request, "_vantage_nav_audiences", None)
    if cached is not None:
        return cached
    names = set(user.groups.values_list("name", flat=True))
    try:
        request._vantage_nav_audiences = names
    except AttributeError:  # pragma: no cover - an exotic request object
        pass
    return names


def may_see(request, item: dict) -> bool:
    """Whether the per-item audience gate lets this request see the entry.

    This narrows and never widens. It runs *in addition to* the model permission check
    that already filtered the panels, so gating an item is a way to tidy somebody's
    sidebar, not a way to grant them anything.

    A superuser passes both halves. ``has_perm`` says so for the permission already,
    and matching that for the group gate is the difference between "the admin hid the
    settings link from the finance team" and "the admin locked themselves out of the
    settings link". With no request at all — ``check``, a test, a management command —
    nothing is gated, which is the same answer the permission layer gives.
    """
    permission = item.get("permission")
    audiences = item.get("audiences")
    if not permission and not audiences:
        return True
    if request is None:
        return True

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if permission and not user.has_perm(permission):
        return False
    return not audiences or bool(set(audiences) & _audience_names(request, user))


def _stored_nav(
    site, request, declaration: list[dict], known_keys: frozenset[str] = frozenset()
) -> list[NavGroup]:
    panels = _visible_panels(site, request)
    panels_by_label = {p.opts.label_lower: p for p in panels}
    pages_by_slug = {p.get_slug(): p for p in _visible_pages(site, request)}

    groups: list[NavGroup] = []
    claimed: set[str] = set()

    for entry in declaration:
        group = NavGroup(label=entry["label"], icon=entry["icon"] or None)
        for stored in entry["items"]:
            if stored["kind"] == "model":
                # Claimed by the *declaration*, not by what resolved out of it. A model
                # that was deliberately hidden, or gated away from this viewer, must
                # not come back through the leftovers below — that would turn "hide
                # this" into "move this to the bottom of the sidebar".
                claimed.add(stored["target"])
            if stored["hidden"] or not may_see(request, stored):
                continue
            item = _resolve_item(_spec(stored), panels_by_label, pages_by_slug)
            if item is not None:
                group.items.append(item)
        if group.items:
            groups.append(group)

    if vantage_settings.NAV_UNLISTED == "append":
        # A panel neither claimed here nor ever claimed by a past save is new since
        # the arrangement began, or has simply never been touched — either way it
        # keeps appearing. A panel that *was* claimed once (it is in ``known_keys``)
        # but is not claimed now was taken out on purpose, through the editor's own
        # "remove" control, and it stays gone rather than resurrecting through the
        # leftovers it would otherwise fall into. See ``save_declaration``.
        leftovers = [
            p
            for p in panels
            if p.opts.label_lower not in claimed and p.opts.label_lower not in known_keys
        ]
        groups.extend(_grouped(leftovers))

    return groups


# ── what the code declares, in the same shape ───────────────────────────────


def _item_from_spec(spec: Any) -> dict | None:
    """One ``VANTAGE["NAV"]`` entry, translated into the stored shape."""
    if isinstance(spec, str):
        spec = {"model": spec}
    if not isinstance(spec, dict):
        return None
    for kind in ITEM_KINDS:
        if kind in spec:
            return clean_item(
                {
                    "kind": kind,
                    "target": spec[kind],
                    "label": spec.get("label"),
                    "icon": spec.get("icon"),
                    "badge": spec.get("badge"),
                    "args": spec.get("args"),
                }
            )
    return None


def _from_declaration(declaration) -> list[dict]:
    groups: list[dict] = []
    for entry in declaration:
        if isinstance(entry, dict) and "items" in entry:
            specs = entry["items"]
            label = entry.get("group") or entry.get("label") or ""
            icon = entry.get("icon")
        else:
            specs = [entry]
            label, icon = "", None
        items = [item for item in (_item_from_spec(spec) for spec in specs) if item]
        if items:
            groups.append(
                {"label": _text(str(label), MAX_LABEL), "icon": clean_icon(icon), "items": items}
            )
    return groups


def _from_groups(groups: list[NavGroup], site, request) -> list[dict]:
    """The inferred sidebar, translated into the stored shape.

    Recognised by key rather than rebuilt: :func:`_derived_nav` already knows how to
    bucket and sort, and a second implementation of that would be a second answer to
    "which group is this model in".
    """
    labels = {p.opts.label_lower for p in _visible_panels(site, request)}
    slugs = {p.get_slug() for p in _visible_pages(site, request)}

    out: list[dict] = []
    for group in groups:
        items: list[dict] = []
        for item in group.items:
            if item.key in labels:
                entry = clean_item({"kind": "model", "target": item.key})
            elif item.key.startswith("page:") and item.key[5:] in slugs:
                entry = clean_item({"kind": "page", "target": item.key[5:]})
            else:  # pragma: no cover - the derived nav emits nothing else
                entry = None
            if entry is not None:
                items.append(entry)
        if items:
            out.append(
                {
                    "label": _text(str(group.label), MAX_LABEL),
                    "icon": clean_icon(group.icon),
                    "items": items,
                }
            )
    return out


def code_declaration(site, request=None) -> list[dict]:
    """The sidebar the code declares, in the stored shape.

    Two jobs, and it has to be the same answer for both or the screen lies: it is what
    the editor is seeded with before anybody has arranged anything, and it is what Reset
    goes back to.
    """
    declaration = vantage_settings.NAV
    if declaration:
        return _from_declaration(declaration)
    return _from_groups(_derived_nav(site, request), site, request)


# ── the editor's view model ─────────────────────────────────────────────────
#
# Built in Python rather than in the template for the same reason `listprefs.panel_context`
# is: every menu on the screen is an allowlist, and the list that draws a control has to
# be the list that validates what comes back from it. A picker offering an icon that
# `clean_icon` would then drop is a control that silently does nothing.
#
# One thing to be explicit about, because it is a judgement rather than a rule. The
# editor is drawn from the panels *this editor* may see, so it never shows a colleague a
# model they have no rights to. An item naming something they cannot see is kept as an
# opaque, unavailable row rather than dropped, because dropping it would mean opening
# the screen and pressing Save quietly deleted part of somebody else's sidebar. The
# screen is superuser-only by default (``SETTINGS_PERMISSION``), so in the usual case
# nothing is opaque and nothing is at risk either way.


@dataclass(frozen=True)
class EditorItem:
    """One row of the editor: what it points at, and what it would look like."""

    uid: str
    kind: str
    kind_label: Any
    target: str
    #: What is in the label box. Empty means "use the generated name", which is shown
    #: as the box's placeholder — the same bargain the settings screen makes for every
    #: field it can inherit.
    label: str
    fallback: str
    icon: str
    fallback_icon: str
    badge: str
    hidden: bool
    permission: str
    audiences: list[str]
    args: list[str]
    url: str
    #: Whether the target resolves right now. A stored item for an unregistered model,
    #: an unmounted route or a page this user cannot open is shown, greyed, and kept.
    available: bool
    external: bool

    @property
    def display(self) -> str:
        return self.label or self.fallback or self.target

    @property
    def gated(self) -> bool:
        return bool(self.permission or self.audiences)


@dataclass(frozen=True)
class EditorGroup:
    uid: str
    label: str
    icon: str
    items: list[EditorItem]


def _editor_item(uid: str, raw: dict, panels: dict, pages: dict) -> EditorItem:
    kind, target = raw["kind"], raw["target"]
    fallback, fallback_icon, url, available = "", "box", "", False

    if kind in ("model", "page"):
        registered = panels.get(target) if kind == "model" else pages.get(target)
        build = _panel_item if kind == "model" else _page_item
        resolved = build(registered) if registered is not None else None
        if resolved is not None:
            available = True
            fallback, fallback_icon, url = resolved.label, resolved.icon or "box", resolved.url
    elif kind == "route":
        fallback = target
        try:
            url = reverse(target, args=tuple(raw["args"]))
        except NoReverseMatch:
            url = ""
        available = bool(url)
    else:
        fallback, url, available = target, target, True

    return EditorItem(
        uid=uid,
        kind=kind,
        kind_label=_KIND_LABELS.get(kind, kind),
        target=target,
        label=raw["label"],
        fallback=str(fallback),
        icon=raw["icon"],
        fallback_icon=fallback_icon,
        badge=raw["badge"],
        hidden=raw["hidden"],
        permission=raw["permission"],
        audiences=list(raw["audiences"]),
        args=list(raw["args"]),
        url=url,
        available=available,
        external=kind == "url" and is_external(target),
    )


#: What each kind is called on the screen. Whole words rather than the stored key: the
#: key is an identifier, and capitalising an identifier produces an English word wearing
#: English capitalisation in every language the panel is read in.
_KIND_LABELS: dict[str, Any] = {
    "model": _("Model"),
    "page": _("Page"),
    "route": _("Route"),
    "url": _("Link"),
}


def editor_context(site, request) -> dict:
    """Everything ``vantage/settings_nav.html`` renders."""
    stored = stored_declaration()
    declaration = stored if stored is not None else code_declaration(site, request)

    panels = {p.opts.label_lower: p for p in _visible_panels(site, request)}
    pages = {p.get_slug(): p for p in _visible_pages(site, request)}

    groups = [
        EditorGroup(
            uid=f"g{index}",
            label=entry["label"],
            icon=entry["icon"],
            items=[
                _editor_item(f"i{index}-{position}", raw, panels, pages)
                for position, raw in enumerate(entry["items"])
            ],
        )
        for index, entry in enumerate(declaration)
    ]

    listed = {
        item.target for group in groups for item in group.items if item.kind == "model"
    }
    # A key claimed by some earlier save and not claimed now was removed on purpose —
    # see ``save_declaration`` and ``_stored_nav`` — so it belongs in neither this list
    # nor the leftovers the sidebar itself renders. Only asked when there is a stored
    # arrangement to have removed anything from; the code's own declaration has no
    # history to consult.
    removed = (stored_known_keys() - listed) if stored is not None else frozenset()
    return {
        "overridden": stored is not None,
        "declared_in_code": bool(vantage_settings.NAV),
        "groups": groups,
        "icons": icon_names(),
        "models": sorted(
            (
                {"value": label, "label": str(panel.get_label_plural())}
                for label, panel in panels.items()
            ),
            key=lambda entry: entry["label"].lower(),
        ),
        "pages": [
            {"value": slug, "label": str(page.get_title())} for slug, page in sorted(pages.items())
        ],
        "audiences": _audience_choices(),
        "unlisted": vantage_settings.NAV_UNLISTED,
        # Named so the screen can say what happens to a model nobody has placed, which
        # is the question "where did my new model go" asked before it is asked.
        "unlisted_models": sorted(
            str(panel.get_label_plural())
            for label, panel in panels.items()
            if label not in listed and label not in removed
        ),
    }


def _audience_choices() -> list[str]:
    """Every group a sidebar item can be limited to. Empty when there is no database."""
    from django.contrib.auth.models import Group

    try:
        return list(Group.objects.order_by("name").values_list("name", flat=True)[:200])
    except (OperationalError, ProgrammingError):  # pragma: no cover - no table yet
        return []
    except RuntimeError as exc:  # pragma: no cover - pytest-django's connection block
        if "Database access not allowed" not in str(exc):
            raise
        return []


# ── the editor's POST ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class EditResult:
    """What one save of the editor produced, and anything it had to refuse."""

    declaration: list[dict]
    errors: list[Any] = field(default_factory=list)


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _getlist(data, name: str) -> list:
    getter = getattr(data, "getlist", None)
    if getter is not None:
        return getter(name)
    value = data.get(name)
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def apply_edits(data) -> EditResult:
    """Rebuild the whole arrangement from a posted editor form.

    Takes the POST and nothing else. There is deliberately no registry or request in
    this signature: an arrangement is presentation, every target is re-checked against
    the live registry when the sidebar is *rendered*, and validating twice against two
    different snapshots is how a saved item quietly stops existing.

    Order arrives as a number per row and a group per row, which is what makes the
    screen work with scripting switched off and what the drag handler rewrites rather
    than replaces — the same arrangement the per-list rail uses, so the two can never
    disagree about which one the server believes.

    Nothing is trusted. Every target, icon, permission and URL goes back through the
    same validators a stored row does, and the one refusal a person needs to hear about
    — a link this panel will not point at — is returned rather than swallowed.
    """
    errors: list[Any] = []
    groups: dict[str, dict] = {}

    for index, uid in enumerate(_getlist(data, "nav_group_uid")):
        if uid in groups or data.get(f"nav_group_delete__{uid}"):
            continue
        groups[uid] = {
            "label": _text(data.get(f"nav_group_label__{uid}"), MAX_LABEL),
            "icon": clean_icon(data.get(f"nav_group_icon__{uid}")),
            "items": [],
            "sort": (_as_int(data.get(f"nav_group_pos__{uid}"), index + 1), index),
        }

    added_group = _text(data.get("nav_new_group"), MAX_LABEL)
    if added_group:
        # Appended rather than inserted: a new group with no position box yet has not
        # been given a place, and the end is the only place that is not a guess.
        groups["new"] = {
            "label": added_group,
            "icon": clean_icon(data.get("nav_new_group_icon")),
            "items": [],
            "sort": (_LAST, _LAST),
        }

    for index, uid in enumerate(_getlist(data, "nav_item_uid")):
        if data.get(f"nav_item_remove__{uid}"):
            continue
        bucket = groups.get(data.get(f"nav_item_group__{uid}") or "")
        if bucket is None:
            # Its group was deleted in this same save. The item goes with it; a model
            # item reappears through NAV_UNLISTED, which is what the screen says.
            continue
        item = clean_item(
            {
                "kind": data.get(f"nav_item_kind__{uid}"),
                "target": data.get(f"nav_item_target__{uid}"),
                "label": data.get(f"nav_item_label__{uid}"),
                "icon": data.get(f"nav_item_icon__{uid}"),
                "badge": data.get(f"nav_item_badge__{uid}"),
                "hidden": bool(data.get(f"nav_item_hidden__{uid}")),
                "permission": data.get(f"nav_item_permission__{uid}"),
                "audiences": _getlist(data, f"nav_item_audience__{uid}"),
                "args": _getlist(data, f"nav_item_arg__{uid}"),
            }
        )
        if item is not None:
            bucket["items"].append(
                ((_as_int(data.get(f"nav_item_pos__{uid}"), index + 1), index), item)
            )

    added, error = _added_item(data)
    if error is not None:
        errors.append(error)
    if added is not None:
        target = groups.get(data.get("nav_add_group") or "")
        if target is None:
            target = next(iter(sorted(groups.values(), key=lambda g: g["sort"])), None)
        if target is None:
            errors.append(_("There is no group to add that to yet. Create one first."))
        else:
            target["items"].append(((_LAST, _LAST), added))

    declaration = [
        {
            "label": group["label"],
            "icon": group["icon"],
            "items": [item for _sort, item in sorted(group["items"], key=lambda pair: pair[0])],
        }
        for group in sorted(groups.values(), key=lambda group: group["sort"])
    ]
    return EditResult(declaration=clean_declaration(declaration) or [], errors=errors)


#: Sorts after every real position, whatever anybody typed in a number box.
_LAST = 1_000_000


def _added_item(data) -> tuple[dict | None, Any]:
    """The one item the "Add" form may contribute, and why it was refused if it was."""
    kind = data.get("nav_add_kind")
    if kind not in ITEM_KINDS:
        return None, None

    raw = data.get(f"nav_add_{kind}") or ""
    if not str(raw).strip():
        return None, None

    item = clean_item(
        {
            "kind": kind,
            "target": raw,
            "label": data.get("nav_add_label"),
            "icon": data.get("nav_add_icon"),
        }
    )
    if item is None:
        if kind == "url":
            return None, _(
                "%(url)s was not added. A link must be a site path starting with “/”, or "
                "an http, https or mailto address — anything else, a javascript: URL "
                "most of all, is refused."
            ) % {"url": str(raw)[:100]}
        return None, _("That item could not be added.")
    return item, None


# ── entry points ────────────────────────────────────────────────────────────


def build_nav(site, request=None) -> list[NavGroup]:
    """The sidebar for this request, resolved through all three layers."""
    stored = stored_declaration()
    if stored is not None:
        return _stored_nav(site, request, stored, stored_known_keys())
    declaration = vantage_settings.NAV
    if declaration:
        return _declared_nav(site, request, declaration)
    return _derived_nav(site, request)


def check_nav(app_configs, **kwargs) -> list:
    """Report malformed or unresolvable ``NAV`` entries at ``manage.py check`` time.

    A nav entry naming a model that was never registered is almost always a typo, and
    finding it at startup beats noticing the link is missing three weeks later.
    """
    from django.core.checks import Error, Warning

    declaration = vantage_settings.NAV
    if not declaration:
        return []

    messages: list[Any] = []
    for entry in declaration:
        specs = entry["items"] if isinstance(entry, dict) and "items" in entry else [entry]
        for spec in specs:
            normalised = {"model": spec} if isinstance(spec, str) else spec
            if not isinstance(normalised, dict):
                messages.append(
                    Error(
                        f"VANTAGE['NAV'] contains {spec!r}, which is not a dict or a "
                        "model label.",
                        id="vantage.E020",
                    )
                )
                continue
            if not {"model", "page", "route", "url"} & set(normalised):
                messages.append(
                    Error(
                        f"The VANTAGE['NAV'] item {normalised!r} needs one of 'model', "
                        "'page', 'route' or 'url'.",
                        id="vantage.E021",
                    )
                )
                continue
            if "model" in normalised:
                label = str(normalised["model"])
                try:
                    django_apps.get_model(label)
                except (LookupError, ValueError):
                    messages.append(
                        Warning(
                            f"VANTAGE['NAV'] refers to the model {label!r}, which does "
                            "not exist.",
                            hint="Use 'app_label.ModelName'.",
                            id="vantage.W020",
                        )
                    )
    return messages


def resolve_theme(request) -> str:
    """The theme this request should render with.

    A cookie set by the toggle wins over the project default. The inline no-flash
    script also reads localStorage, which covers the very first paint before any
    cookie exists.
    """
    default = vantage_settings.THEME
    if not vantage_settings.THEME_TOGGLE:
        return default
    chosen = request.COOKIES.get("vantage-theme") if request else None
    return chosen if chosen in ("light", "dark") else default
