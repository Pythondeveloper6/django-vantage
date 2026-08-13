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
    "ModelPanel",
    "VantageSite",
    "__version__",
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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
