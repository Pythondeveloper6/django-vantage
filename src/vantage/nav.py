"""Building the sidebar.

Two modes. By default navigation is *derived*: registering a model is enough to make
it reachable, grouped by app. Setting ``VANTAGE["NAV"]`` switches to *declared*: the
site owner states the groups, their order, which items go in them, what those items
are called, and any extra links they want alongside.

Declared navigation is presentation only. It can reorder, rename and hide, but it can
never grant access — every item is still filtered through the permission layer, so a
nav entry for a model the user cannot open simply does not render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.apps import apps as django_apps
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings

__all__ = ["NavGroup", "NavItem", "build_nav", "check_nav", "resolve_theme"]


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


# ── entry points ────────────────────────────────────────────────────────────


def build_nav(site, request=None) -> list[NavGroup]:
    """The sidebar for this request."""
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
