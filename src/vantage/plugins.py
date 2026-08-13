"""Plugins — packages that extend the panel by being installed.

The target is that ``pip install vantage-celery`` adds a queue page, a nav entry, a
dashboard card and a couple of actions, and the project changes nothing. That is what
an entry point buys: the package declares where its plugin lives, and Vantage finds it
without the project maintaining a list::

    # in the plugin's pyproject.toml
    [project.entry-points."vantage.plugins"]
    celery = "vantage_celery:CeleryPlugin"

::

    # in the plugin
    from vantage.plugins import VantagePlugin

    class CeleryPlugin(VantagePlugin):
        name = "celery"
        version = "1.2.0"

        def register(self, site):
            site.register_page(QueuePage)

Four rules, each of which is the difference between a plugin system and a liability:

**A broken plugin is never fatal.** Loading happens in ``AppConfig.ready()``, and an
exception there means ``manage.py`` does not run — no shell, no migrate, no way to
uninstall the thing that broke. So a plugin that raises is caught, skipped, and
reported through the checks framework, where it is loud without being terminal.

**Loading is idempotent.** ``ready()`` can run more than once (``runserver``'s
reloader, ``override_settings`` in tests, a second ``django.setup()``), and a plugin
that registers a page each time would give a project three copies of it.

**The project has the final say.** ``VANTAGE["PLUGINS"]`` is an allowlist and
``VANTAGE["PLUGINS_EXCLUDE"]`` a denylist, both matched against the entry-point name or
the plugin's own ``name``. Installing a package must be reversible without uninstalling
it, particularly when the reason for turning it off is that it is broken.

**Entry points are not the only door.** ``register_plugin(MyPlugin)`` from a
``panel.py`` covers the project that vendors a plugin, or would simply rather see its
extensions written down.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any
from weakref import WeakKeyDictionary

from django.core.checks import Error, Warning

__all__ = [
    "ENTRY_POINT_GROUP",
    "PluginError",
    "VantagePlugin",
    "check_plugins",
    "load_plugins",
    "loaded_plugins",
    "plugin_errors",
    "register_plugin",
    "reset",
]

logger = logging.getLogger("vantage.plugins")

#: The entry-point group a distribution declares to be discovered.
ENTRY_POINT_GROUP = "vantage.plugins"


class VantagePlugin:
    """Base class for a plugin. Override :meth:`register` and set a ``name``."""

    #: Stable identifier. It is what ``VANTAGE["PLUGINS"]`` matches on and what a check
    #: error names, so it should not change between releases of the plugin.
    name: str = ""

    #: Informational: shown by ``manage.py vantage_plugins``-style introspection and in
    #: check messages, so a bug report says which version misbehaved.
    version: str = ""

    def register(self, site) -> None:
        """Contribute to ``site``. Called once per site, at startup.

        Registering pages, commands, panels and slot contributions all belong here.
        Touching the database does not: this runs inside ``AppConfig.ready()``, where
        a query breaks ``migrate`` on a fresh install.
        """

    @classmethod
    def get_name(cls) -> str:
        return cls.name or cls.__name__

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        version = f" {self.version}" if self.version else ""
        return f"<{type(self).__name__} {self.get_name()}{version}>"


@dataclass(frozen=True)
class PluginError:
    """A plugin that could not be loaded, kept so a check can report it."""

    name: str
    message: str
    detail: str = ""
    source: str = ""


#: Loaded plugins per site. Weak, so a site built inside a test does not keep its
#: plugins — or itself — alive for the rest of the run.
_loaded: WeakKeyDictionary = WeakKeyDictionary()

#: Names already tried per site, successful or not, so a failure is not retried into a
#: duplicate registration.
_attempted: WeakKeyDictionary = WeakKeyDictionary()

#: Failures, by name. Global rather than per-site: a plugin that cannot be imported
#: failed for every site, and reporting it once is what a reader wants.
_errors: dict[str, PluginError] = {}

#: Entry-point names seen during the last discovery, so an allowlist entry that matches
#: nothing can be reported as the typo it almost certainly is.
_discovered: set[str] = set()
_discovery_ran = False


# ── loading ─────────────────────────────────────────────────────────────────


def load_plugins(site=None, *, group: str = ENTRY_POINT_GROUP) -> list[VantagePlugin]:
    """Discover and register every allowed plugin against ``site``.

    Returns the plugins loaded by *this* call. Already-loaded plugins are skipped
    rather than re-registered, so calling it twice is a no-op.
    """
    global _discovery_ran

    site = _resolve_site(site)
    loaded: list[VantagePlugin] = []

    for entry in _entry_points(group):
        _discovered.add(entry.name)
        if not _permitted(entry.name):
            continue

        candidate = _guard(entry.name, str(entry.value), lambda e=entry: e.load())
        if candidate is None:
            continue

        plugin = _guard(entry.name, str(entry.value), lambda c=candidate: _coerce(c))
        if plugin is None:
            continue

        installed = _install(plugin, site, source=str(entry.value))
        if installed is not None:
            loaded.append(installed)

    _discovery_ran = True
    return loaded


def register_plugin(plugin: Any, site=None) -> VantagePlugin | None:
    """Register a plugin the project names itself, instead of discovering it.

    Loads immediately rather than queueing, because the natural place to call this is a
    ``panel.py`` — which is imported *after* startup discovery has already run.
    """
    site = _resolve_site(site)
    coerced = _guard(_name_of(plugin), repr(plugin), lambda: _coerce(plugin))
    if coerced is None:
        return None
    return _install(coerced, site, source=repr(plugin))


def _install(plugin: VantagePlugin, site, *, source: str) -> VantagePlugin | None:
    """Run one plugin's ``register`` hook, once per site, catching anything it throws."""
    name = plugin.get_name()
    if not _permitted(name):
        return None

    registered = _loaded.setdefault(site, {})
    attempted = _attempted.setdefault(site, set())
    if name in attempted:
        # Idempotence, not a warning: a second ready() is normal, and a project that
        # both installs and explicitly registers a plugin meant to end up with one.
        # Failures count as attempts too — a hook that raised halfway through has
        # already registered whatever it managed to, and running it again would
        # duplicate exactly that part.
        return None

    attempted.add(name)
    if _guard(name, source, lambda: plugin.register(site) or True) is None:
        return None

    registered[name] = plugin
    _errors.pop(name, None)
    logger.debug("Vantage loaded plugin %r from %s.", name, source)
    return plugin


def loaded_plugins(site=None) -> list[VantagePlugin]:
    """Plugins successfully registered against ``site``, in load order."""
    return list(_loaded.get(_resolve_site(site), {}).values())


def plugin_errors() -> list[PluginError]:
    return sorted(_errors.values(), key=lambda err: err.name)


def reset(site=None) -> None:
    """Forget what has been loaded. For tests; nothing in production needs it."""
    global _discovery_ran
    if site is None:
        _loaded.clear()
        _attempted.clear()
        _errors.clear()
        _discovered.clear()
        _discovery_ran = False
    else:
        _loaded.pop(site, None)
        _attempted.pop(site, None)


# ── filtering ───────────────────────────────────────────────────────────────


def _permitted(name: str) -> bool:
    """Whether the project's allowlist and denylist admit this plugin.

    The denylist wins. Turning something off is the urgent operation — usually because
    it is breaking a deploy — and it should not require also finding and editing an
    allowlist somewhere else in the same file.
    """
    lowered = str(name).lower()
    excluded = _setting("PLUGINS_EXCLUDE") or ()
    if lowered in {str(item).lower() for item in excluded}:
        return False

    allowed = _setting("PLUGINS")
    if allowed is None:
        return True
    return lowered in {str(item).lower() for item in allowed}


def _setting(name: str):
    """Read a plugin setting, tolerating a settings layer that has not heard of it.

    ``vantage_settings`` rejects unknown keys by design, so this asks with a fallback
    exactly as :func:`vantage.importer.limit` does: discovery works with its defaults
    today, and honours ``VANTAGE["PLUGINS"]`` the moment that name is added to
    :data:`vantage.conf.DEFAULTS`, with no change here.
    """
    from vantage.conf import vantage_settings

    return getattr(vantage_settings, name, None)


def _entry_points(group: str):
    try:
        return sorted(entry_points(group=group), key=lambda entry: entry.name)
    except Exception:  # pragma: no cover - a broken installed distribution
        logger.exception("Vantage could not read the %r entry-point group.", group)
        return []


def _coerce(candidate: Any) -> VantagePlugin:
    """Accept a class, an instance, or a bare ``register(site)`` callable.

    An entry point pointing at a function is the shortest possible plugin, and refusing
    it would buy nothing: the base class exists for the metadata, not for ceremony.
    """
    if isinstance(candidate, VantagePlugin):
        return candidate
    if isinstance(candidate, type) and issubclass(candidate, VantagePlugin):
        return candidate()
    if isinstance(candidate, type) and callable(getattr(candidate, "register", None)):
        # A duck-typed plugin class: same shape, different base, still fine.
        return candidate()
    if callable(getattr(candidate, "register", None)):
        return candidate
    if callable(candidate):
        return _FunctionPlugin(candidate)
    raise TypeError(
        f"{candidate!r} is not a Vantage plugin. Expected a VantagePlugin subclass, an "
        f"instance of one, or a callable taking the site."
    )


class _FunctionPlugin(VantagePlugin):
    """Wrapper letting ``def setup(site): ...`` be a whole plugin."""

    def __init__(self, func) -> None:
        self.func = func
        self.name = getattr(func, "__name__", "plugin")
        self.version = ""

    def register(self, site) -> None:
        self.func(site)


def _name_of(candidate: Any) -> str:
    for attr in ("name", "__name__", "__qualname__"):
        value = getattr(candidate, attr, None)
        if value:
            return str(value)
    return repr(candidate)


def _guard(name: str, source: str, work):
    """Run one step of loading. Failure is recorded and reported, never raised.

    ``Exception`` is the right breadth: this executes third-party code during startup,
    and the failure mode being avoided is a management command that cannot run at all.
    """
    try:
        return work()
    except Exception as exc:
        _errors[name] = PluginError(
            name=name,
            message=f"{type(exc).__name__}: {exc}",
            detail=traceback.format_exc(),
            source=source,
        )
        logger.exception("Vantage plugin %r (%s) failed to load.", name, source)
        return None


def _resolve_site(site):
    if site is not None:
        return site
    from vantage.sites import site as default_site

    return default_site


# ── checks ──────────────────────────────────────────────────────────────────


def check_plugins(app_configs, **kwargs) -> list:
    """Report plugins that failed to load, and allowlist entries that match nothing.

    This is the other half of "never fatal": swallowing the exception is only
    acceptable if something else says loudly that it happened.
    """
    messages: list = [
        Error(
            f"The Vantage plugin {err.name!r} failed to load and was skipped.",
            hint=(
                f"{err.message}\nDeclared at {err.source}.\nFix the plugin, or add "
                f"{err.name!r} to VANTAGE['PLUGINS_EXCLUDE'] to run without it."
            ),
            id="vantage.E030",
        )
        for err in plugin_errors()
    ]

    if _discovery_ran:
        allowed = _setting("PLUGINS")
        known = {name.lower() for name in _discovered}
        known |= {
            plugin.get_name().lower()
            for plugins in _loaded.values()
            for plugin in plugins.values()
        }
        for entry in allowed or ():
            if str(entry).lower() not in known:
                messages.append(
                    Warning(
                        f"VANTAGE['PLUGINS'] lists {entry!r}, which no installed "
                        f"package provides.",
                        hint="Nothing was loaded for it. Check the spelling, or that "
                        "the distribution providing it is installed.",
                        id="vantage.W030",
                    )
                )

    return messages
