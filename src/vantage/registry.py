"""The model registry — how models become administrable.

Auto-registration is the default, and it is meant to be genuinely usable rather than a
placeholder: a project that never writes a ``panel.py`` still gets every model, minus
the framework plumbing nobody wants to browse.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.db import models

from vantage.conf import vantage_settings
from vantage.panels import ModelPanel

if TYPE_CHECKING:
    from vantage.sites import VantageSite


class AlreadyRegistered(Exception):
    """Raised when a model is registered twice."""


class NotRegistered(Exception):
    """Raised when a model is expected in the registry and is not there."""


#: Framework tables that are noise in an admin panel. ``auth.User`` and ``auth.Group``
#: are deliberately absent — they are among the most useful things to administer.
DEFAULT_EXCLUDE: frozenset[str] = frozenset(
    {
        "sessions.session",
        "contenttypes.contenttype",
        "auth.permission",
        "admin.logentry",
        # Editable only through the settings screen, which gates on superuser.
        # A generic CRUD form for it would route around that gate.
        "vantage.siteconfiguration",
        # An editable audit log is not an audit log, and a generic CRUD form for
        # a saved view routes around every permission rule in savedviews.py.
        "vantage.vantagelogentry",
        "vantage.savedview",
        "vantage.savedviewgrant",
        "vantage.savedviewuserstate",
        "vantage.listpreference",
        "vantage.vantagejob",
        # Undo snapshots hold deleted rows verbatim, secrets included, and redeeming
        # one is a capability bound to a user and a session. A CRUD form would print
        # the first and route around the second.
        "vantage.undoentry",
    }
)


class Registry:
    """Maps models to their panel instances."""

    def __init__(self, site: VantageSite | None = None) -> None:
        self.site = site
        self._panels: dict[type[models.Model], ModelPanel] = {}

    # ── registration ────────────────────────────────────────────────────────

    def register(
        self,
        model: type[models.Model],
        panel_class: type[ModelPanel] | None = None,
    ) -> ModelPanel:
        """Register ``model``, optionally with an explicit panel class."""
        _check_registerable(model)
        if model in self._panels:
            raise AlreadyRegistered(f"{model._meta.label} is already registered with Vantage.")
        panel = (panel_class or ModelPanel)(model, self.site)
        self._panels[model] = panel
        return panel

    def register_decorator(self, model: type[models.Model]):
        """Return a class decorator that registers the decorated panel for ``model``."""

        def wrapper(panel_class: type[ModelPanel]) -> type[ModelPanel]:
            self.register(model, panel_class)
            return panel_class

        return wrapper

    def unregister(self, model: type[models.Model]) -> None:
        if model not in self._panels:
            raise NotRegistered(f"{model._meta.label} is not registered with Vantage.")
        del self._panels[model]

    # ── lookup ──────────────────────────────────────────────────────────────

    def is_registered(self, model: type[models.Model]) -> bool:
        return model in self._panels

    def get_panel(self, model: type[models.Model]) -> ModelPanel:
        try:
            return self._panels[model]
        except KeyError:
            raise NotRegistered(f"{model._meta.label} is not registered with Vantage.") from None

    def panels(self) -> Iterator[ModelPanel]:
        """Every panel, in a stable order (app label, then model name)."""
        yield from sorted(
            self._panels.values(),
            key=lambda p: (p.opts.app_label, p.opts.model_name),
        )

    def __len__(self) -> int:
        return len(self._panels)

    # ── auto-registration ───────────────────────────────────────────────────

    def autoregister(self) -> None:
        """Register every eligible model that has no explicit panel already.

        Never clobbers an existing registration: an explicit ``panel.py`` always wins
        over inference, whichever ran first.
        """
        if not vantage_settings.AUTO_REGISTER:
            return
        for model in django_apps.get_models():
            if model in self._panels or not self.is_eligible(model):
                continue
            self.register(model)

    def is_eligible(self, model: type[models.Model]) -> bool:
        """Whether auto-registration should pick this model up."""
        opts = model._meta
        # ``auto_created`` covers implicit M2M through-tables, which nobody wants to
        # browse. Proxy models are deliberately kept: they are a normal way to give a
        # model a second, differently-configured presence in the panel.
        if opts.abstract or opts.auto_created:
            return False

        label = opts.label_lower  # "app_label.modelname"
        app_label = opts.app_label

        include_only = vantage_settings.INCLUDE_ONLY
        if include_only is not None:
            return _matches(label, app_label, include_only)

        if label in DEFAULT_EXCLUDE:
            return False
        return not _matches(label, app_label, vantage_settings.EXCLUDE)


def _matches(label: str, app_label: str, patterns) -> bool:
    """A pattern is either an app label ('blog') or a model label ('blog.Post')."""
    for pattern in patterns or ():
        candidate = str(pattern).lower()
        if candidate in (label, app_label):
            return True
    return False


def _check_registerable(model) -> None:
    if not (isinstance(model, type) and issubclass(model, models.Model)):
        raise TypeError(f"{model!r} is not a Django model.")
    if model._meta.abstract:
        raise TypeError(f"{model._meta.label} is abstract and cannot be registered with Vantage.")
