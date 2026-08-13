"""Custom pages: anything that is not a model.

A reporting screen, an operations console, a settings form, a health check. The bar
this API has to clear is that adding one should feel no heavier than writing a
``ModelPanel`` — a class, a title, and a method that returns some data:

    @site.register_page
    class RevenuePage(Page):
        title = "Revenue"
        icon = "trending-up"

        def get_context(self, request):
            return {"total": Order.objects.total_revenue()}

Everything else has a default. The URL comes from the class name, the template falls
back to a generic renderer that displays whatever ``get_context`` returned, and the
nav entry appears on its own. A page with no template at all still produces a
presentable screen, which is what keeps the first one cheap to write.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["HealthPage", "Page", "SchemaPage", "page_slug"]

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def page_slug(cls: type) -> str:
    """``RevenueReportPage`` -> ``revenue-report``.

    The trailing ``Page`` is dropped because every class has it and repeating it in
    every URL would be noise.
    """
    name = cls.__name__
    if name.endswith("Page") and len(name) > 4:
        name = name[:-4]
    return _CAMEL.sub("-", name).lower().replace("_", "-")


class Page:
    """Base class for a custom panel page."""

    #: Shown in the nav and as the page heading. Required in practice; defaults to
    #: the slug so a page that forgets it is still usable rather than broken.
    title: str = ""
    description: str = ""
    icon: str | None = None
    group: str | None = None
    order: int = 0

    #: URL segment, relative to where the site is mounted. Defaults to the slug.
    path: str | None = None

    #: An explicit slug, if the derived one is not what you want.
    slug_override: str | None = None

    #: A Django permission string, or a callable taking the request. ``None`` means
    #: any staff user who can reach the panel can open the page.
    permission: str | Any | None = None

    #: Falls back to ``vantage/pages/<slug>.html``, then to the generic renderer.
    template_name: str | None = None

    def __init__(self, site=None) -> None:
        self.site = site

    # ── identity ────────────────────────────────────────────────────────────

    @classmethod
    def get_slug(cls) -> str:
        return cls.slug_override or page_slug(cls)

    @classmethod
    def get_path(cls) -> str:
        path = cls.path if cls.path is not None else f"{cls.get_slug()}/"
        return path.lstrip("/")

    @classmethod
    def get_title(cls) -> str:
        return cls.title or cls.get_slug().replace("-", " ").capitalize()

    # ── access ──────────────────────────────────────────────────────────────

    def has_permission(self, request) -> bool:
        """Whether this user may open the page.

        A callable receives the request, so a page can be gated on anything — a
        feature flag, group membership, the time of day — without inventing a
        permission just to express it.
        """
        if self.permission is None:
            return True
        if callable(self.permission):
            return bool(self.permission(request))
        return request.user.has_perm(str(self.permission))

    # ── content ─────────────────────────────────────────────────────────────

    def get_context(self, request) -> dict:
        """Everything the page needs. Override this; it is the whole point."""
        return {}

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        # The project's own template wins if it exists; otherwise the generic
        # renderer displays whatever get_context returned.
        return [f"vantage/pages/{self.get_slug()}.html", "vantage/page.html"]

    def get_breadcrumbs(self) -> list[tuple[str, str | None]]:
        return [(self.get_title(), None)]


class SchemaPage(Page):
    """The relationship map, shipped as a built-in page.

    Registered by the app config rather than left for projects to add, because a
    schema view is only useful if it is always there — one a user has to switch on is
    one they never remember exists.
    """

    title = "Schema"
    icon = "share-2"
    group = "System"
    order = 90
    path = "_/schema/"
    template_name = "vantage/pages/schema.html"

    def get_context(self, request):
        from vantage.erd import build_graph

        return {"graph": build_graph(self.site, request)}


class HealthPage(Page):
    """System diagnostics. Superusers only — the detail is deployment information."""

    title = "System"
    icon = "activity"
    group = "System"
    order = 95
    path = "_/health/"
    template_name = "vantage/pages/health.html"
    permission = staticmethod(lambda request: request.user.is_superuser)

    def get_context(self, request):
        from vantage.health import run_checks

        checks = run_checks()
        return {
            "checks": checks,
            "worst": next(
                (s for s in ("fail", "warn") if any(c.status == s for c in checks)), "ok"
            ),
        }
