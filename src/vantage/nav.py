"""Building the sidebar from the registry.

Navigation is derived, not declared: registering a model is enough to make it
reachable. A project that wants a different structure sets ``VANTAGE["NAV"]``, and a
panel that wants a different home sets ``group`` and ``order``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import NoReverseMatch, reverse

from vantage.conf import vantage_settings

__all__ = ["NavGroup", "NavItem", "build_nav"]


@dataclass
class NavItem:
    label: str
    url: str
    icon: str | None = None
    key: str = ""

    def is_current(self, path: str) -> bool:
        """Whether this item covers the given path.

        Prefix matching so a detail page keeps its list highlighted, but the panel
        root is compared exactly or it would match everything.
        """
        if not self.url:
            return False
        if self.url.rstrip("/") == "":
            return path.rstrip("/") == ""
        return path == self.url or path.startswith(self.url)


@dataclass
class NavGroup:
    label: str
    items: list[NavItem] = field(default_factory=list)


def build_nav(site, request=None) -> list[NavGroup]:
    """Group every visible panel and custom page for the sidebar."""
    groups: dict[str, NavGroup] = {}
    order: dict[str, int] = {}

    def bucket(label: str, weight: int) -> NavGroup:
        if label not in groups:
            groups[label] = NavGroup(label=label)
            order[label] = weight
        order[label] = min(order[label], weight)
        return groups[label]

    for panel in site.panels():
        try:
            url = reverse(f"vantage:{panel.slug}-list")
        except NoReverseMatch:  # pragma: no cover - only if urls are not mounted
            continue
        bucket(panel.get_group(), panel.order).items.append(
            NavItem(
                label=panel.get_label_plural(),
                url=url,
                icon=panel.get_icon(),
                key=panel.slug,
            )
        )

    for page in site.pages():
        try:
            url = reverse(f"vantage:page-{page.slug}")
        except NoReverseMatch:  # pragma: no cover
            continue
        bucket(getattr(page, "group", None) or "Pages", getattr(page, "order", 0)).items.append(
            NavItem(label=page.title, url=url, icon=getattr(page, "icon", None), key=page.slug)
        )

    for group in groups.values():
        group.items.sort(key=lambda item: item.label.lower())

    return [groups[label] for label in sorted(groups, key=lambda label: (order[label], label))]


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
