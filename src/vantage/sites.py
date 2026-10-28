"""``VantageSite`` — the object a project configures and mounts.

``vantage.site`` is the default instance. Projects needing a second, separately-scoped
panel (a staff panel and a partner panel, say) instantiate ``VantageSite`` again and
mount it at another URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from weakref import WeakSet

from django.db import models

from vantage.conf import vantage_settings
from vantage.registry import Registry

if TYPE_CHECKING:
    from vantage.panels import ModelPanel

#: Every live site, so system checks can validate all of them. A ``WeakSet`` so
#: sites created inside tests are collected rather than accumulating.
all_sites: WeakSet[VantageSite] = WeakSet()


class VantageSite:
    """A mountable admin panel: a registry, a navigation tree, and custom pages."""

    def __init__(self, name: str = "vantage") -> None:
        self.name = name
        self.registry = Registry(site=self)
        self._pages: list[type] = []
        all_sites.add(self)

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

    # ── custom commands ─────────────────────────────────────────────────────

    def register_command(self, command=None, **kwargs):
        """Add an entry to the command palette.

        Accepts a ``Command``, keyword arguments, or a decorated class — the same
        three shapes ``register_page`` supports, so the two feel alike.
        """
        from vantage.palette import register_command

        return register_command(self, command, **kwargs)

    def commands(self, request):
        from vantage.palette import build_commands

        return build_commands(self, request)

    # ── mounting ────────────────────────────────────────────────────────────

    @property
    def title(self) -> str:
        return vantage_settings.SITE_TITLE

    def get_urls(self) -> list:
        from vantage import urls as vantage_urls

        return vantage_urls.build(self)

    @property
    def urls(self):
        from vantage import twofactor

        # Here as well as in vantage.urls.build: a subclass's get_urls() may add routes
        # after super()'s, and those are behind the two-factor gate too.
        return twofactor.guard_patterns(self.get_urls()), "vantage", self.name


#: The default site. Import this, not the class, unless you need a second panel.
site = VantageSite()
