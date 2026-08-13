"""``VantageSite`` — the object a project configures and mounts.

``vantage.site`` is the default instance. Projects needing a second, separately-scoped
panel (a staff panel and a partner panel, say) instantiate ``VantageSite`` again and
mount it at another URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

from vantage.conf import vantage_settings
from vantage.registry import Registry

if TYPE_CHECKING:
    from vantage.panels import ModelPanel


class VantageSite:
    """A mountable admin panel: a registry, a navigation tree, and custom pages."""

    def __init__(self, name: str = "vantage") -> None:
        self.name = name
        self.registry = Registry(site=self)
        self._pages: list[type] = []

    @property
    def _registry(self) -> dict:
        """Django's ``autodiscover_modules(register_to=...)`` contract.

        It snapshots ``_registry`` before importing each ``panel.py`` and restores it
        if the import raises, so a broken panel module cannot leave the site half
        registered. ``AdminSite`` satisfies the same contract.
        """
        return self.registry._panels

    @_registry.setter
    def _registry(self, value: dict) -> None:
        self.registry._panels = value

    # ── model registration (delegates to the registry) ──────────────────────

    def register(self, model, panel_class=None):
        return self.registry.register(model, panel_class)

    def unregister(self, model: type[models.Model]) -> None:
        self.registry.unregister(model)

    def is_registered(self, model: type[models.Model]) -> bool:
        return self.registry.is_registered(model)

    def get_panel(self, model: type[models.Model]) -> ModelPanel:
        return self.registry.get_panel(model)

    def panels(self):
        return self.registry.panels()

    # ── custom pages ────────────────────────────────────────────────────────

    def register_page(self, page_class: type) -> type:
        """Class decorator registering a custom :class:`~vantage.pages.Page`."""
        self._pages.append(page_class)
        return page_class

    def pages(self) -> list[type]:
        return list(self._pages)

    # ── mounting ────────────────────────────────────────────────────────────

    @property
    def title(self) -> str:
        return vantage_settings.SITE_TITLE

    def get_urls(self) -> list:
        from vantage import urls as vantage_urls

        return vantage_urls.build(self)

    @property
    def urls(self):
        return self.get_urls(), "vantage", self.name


#: The default site. Import this, not the class, unless you need a second panel.
site = VantageSite()
