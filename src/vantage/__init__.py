"""django-vantage — a smart, beautiful admin panel for Django.

Zero config to start::

    INSTALLED_APPS = ["vantage", ...]
    urlpatterns = [path("admin/", site.urls)]

One line to override anything::

    @register(Post)
    class PostPanel(ModelPanel):
        list_display = ["title", "author", "status"]
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

__all__ = [
    "Chart",
    "ChartCard",
    "Dashboard",
    "ListCard",
    "ModelPanel",
    "Page",
    "Stat",
    "VantageSite",
    "__version__",
    "action",
    "register",
    "site",
]


def register(model):
    """Class decorator registering a :class:`ModelPanel` for ``model``.

    ::

        @register(Post)
        class PostPanel(ModelPanel):
            list_display = ["title"]
    """
    from vantage.sites import site

    return site.registry.register_decorator(model)


def __getattr__(name: str):
    """Lazy re-exports, so importing ``vantage`` never touches Django settings."""
    if name == "site":
        from vantage.sites import site

        return site
    if name == "VantageSite":
        from vantage.sites import VantageSite

        return VantageSite
    if name == "ModelPanel":
        from vantage.panels import ModelPanel

        return ModelPanel
    if name in (
        "Dashboard",
        "StatCard",
        "ChartCard",
        "TableCard",
        "ListCard",
        "TemplateCard",
    ):
        from vantage import dashboards

        return getattr(dashboards, name)
    if name == "action":
        from vantage.actions import action

        return action
    if name == "Page":
        from vantage.pages import Page

        return Page
    if name in ("Stat", "Chart"):
        from vantage import stats

        return getattr(stats, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
