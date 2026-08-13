"""The command palette's registry — every action ⌘K can offer.

A palette is a promise that anything reachable by clicking is reachable by typing, and
the only honest way to keep that promise is to build the list from the same facts the
sidebar is built from rather than from a hand-maintained copy of it. So navigation
commands are derived from the registry and from the registered pages, every one of
them filtered through the permission layer on the way past.

That filtering is not cosmetic. A palette is *read* as a menu of what you are allowed
to do; listing "Delete customers" to someone who cannot delete customers is worse than
useless, because they will try. Nothing in this module emits a command without first
asking whether the user could perform it by hand — including the ones a project
registers itself, which get a ``permission`` of their own for the purpose.

Commands are shipped to the browser as JSON and filtered there, because a palette that
round-trips on every keystroke of "das" to find "Dashboard" is not a palette. Record
search is the opposite case and lives in :mod:`vantage.search`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from typing import Any
from weakref import WeakKeyDictionary

from django.http import JsonResponse
from django.urls import NoReverseMatch, reverse

from vantage.conf import vantage_settings
from vantage.permissions import get_permissions
from vantage.views import SiteView

__all__ = [
    "Command",
    "CommandRegistry",
    "CommandsView",
    "build_commands",
    "build_urls",
    "icon_paths",
    "register_command",
    "registry_for",
]

#: Sections in the order they read best: where you can go, what you can make, what you
#: can do. Anything a project invents sorts after these, alphabetically.
SECTION_ORDER = ("Go to", "Create", "Actions")


@dataclass(frozen=True, slots=True)
class Command:
    """One row in the palette.

    Either ``url`` or ``callback`` — a command that does nothing is a bug worth
    hearing about at import time rather than discovering as a dead row in the UI.
    ``callback`` names a client-side handler registered with
    ``window.vantage.palette.handle(name, fn)``; anything that needs the server should
    be a ``url``.
    """

    id: str
    label: str
    hint: str = ""
    icon: str = "box"
    url: str | None = None
    callback: str | None = None
    keywords: tuple[str, ...] = ()
    section: str = "Actions"
    order: int = 0

    #: A Django permission string, or a callable taking the request. Mirrors
    #: :attr:`vantage.pages.Page.permission` so there is one thing to learn.
    permission: str | Callable[[Any], bool] | None = None

    def __post_init__(self) -> None:
        if bool(self.url) == bool(self.callback):
            raise ValueError(
                f"Command {self.id!r} needs exactly one of 'url' or 'callback'."
            )
        if not isinstance(self.keywords, tuple):
            object.__setattr__(self, "keywords", tuple(self.keywords))

    def is_visible(self, request) -> bool:
        if self.permission is None:
            return True
        if callable(self.permission):
            return bool(self.permission(request))
        user = getattr(request, "user", None)
        return bool(user and user.has_perm(str(self.permission)))

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "hint": self.hint,
            "icon": self.icon,
            "url": self.url,
            "callback": self.callback,
            "keywords": list(self.keywords),
            "section": self.section,
        }


class CommandRegistry:
    """The commands one site was told about by hand."""

    def __init__(self) -> None:
        self._commands: list[Command] = []

    def register(self, command: Command | None = None, /, **kwargs) -> Command:
        if command is None:
            command = Command(**kwargs)
        # Last registration wins, so a project can replace a command it does not like
        # by re-registering its id rather than by reaching into this list.
        self._commands = [existing for existing in self._commands if existing.id != command.id]
        self._commands.append(command)
        return command

    def unregister(self, command_id: str) -> None:
        self._commands = [c for c in self._commands if c.id != command_id]

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands)

    def __len__(self) -> int:
        return len(self._commands)


#: Keyed weakly, exactly like ``sites.all_sites``, so a site built inside a test does
#: not keep its commands alive forever.
_registries: WeakKeyDictionary = WeakKeyDictionary()


def registry_for(site) -> CommandRegistry:
    """The hand-registered commands for ``site``, created on first use."""
    registry = _registries.get(site)
    if registry is None:
        registry = CommandRegistry()
        _registries[site] = registry
    return registry


def register_command(site, command: Any = None, /, **kwargs) -> Any:
    """Add a command to ``site``. The hook behind ``site.register_command``.

    Three shapes, because all three read naturally at a call site::

        site.register_command(id="docs", label="Open the docs", url="https://…")
        site.register_command(Command(id="docs", label="Open the docs", url="…"))

        @site.register_command
        class FlushCache:
            id = "cache-flush"
            label = "Flush the cache"
            url = "/ops/flush/"
            permission = "ops.flush_cache"

    The decorator form hands the class back untouched, which is what makes it usable
    as a decorator at all — same contract as ``@site.register_page``.
    """
    registry = registry_for(site)
    if command is None:
        return registry.register(**kwargs)
    if isinstance(command, Command):
        return registry.register(command)
    if isinstance(command, type):
        registry.register(_from_class(command))
        return command
    raise TypeError(
        f"register_command takes a Command, a class describing one, or keyword "
        f"arguments; got {command!r}."
    )


def _from_class(cls: type) -> Command:
    """Read a ``Command`` off a plain declarative class."""
    known = {f.name for f in fields(Command)}
    given = {name: getattr(cls, name) for name in known if hasattr(cls, name)}
    given.setdefault("id", cls.__name__.lower())
    given.setdefault("label", cls.__name__)
    return Command(**given)


# ── the derived commands ────────────────────────────────────────────────────


def build_commands(site, request) -> list[Command]:
    """Every command this request may see, in display order."""
    commands = [
        *_global_commands(site, request),
        *_navigation_commands(site, request),
        *_create_commands(site, request),
        *(c for c in registry_for(site) if c.is_visible(request)),
    ]

    seen: set[str] = set()
    unique = []
    for command in commands:
        if command.id in seen:
            continue
        seen.add(command.id)
        unique.append(command)

    unique.sort(key=lambda c: (_section_rank(c.section), c.section, c.order, c.label.lower()))
    return unique


def _section_rank(section: str) -> tuple[int, str]:
    try:
        return (0, f"{SECTION_ORDER.index(section):02d}")
    except ValueError:
        return (1, section.lower())


def _global_commands(site, request) -> list[Command]:
    commands = [
        Command(
            id="go:dashboard",
            label="Dashboard",
            hint=vantage_settings.SITE_TITLE,
            icon="layout-dashboard",
            url=_reverse("vantage:dashboard"),
            keywords=("home", "overview", "start"),
            section="Go to",
            order=-100,
        )
    ]
    if vantage_settings.THEME_TOGGLE:
        commands.append(
            Command(
                id="theme:toggle",
                label="Toggle theme",
                hint="Light / dark",
                icon="moon",
                callback="theme.toggle",
                keywords=("dark", "light", "appearance", "colour", "color"),
                section="Actions",
            )
        )
    commands.append(
        Command(
            id="go:search",
            label="Search everything",
            hint="Full results page",
            icon="search",
            url=_reverse("vantage:search"),
            keywords=("find", "lookup", "records"),
            section="Go to",
            order=-90,
        )
    )
    return [c for c in commands if c.url or c.callback]


def _navigation_commands(site, request) -> list[Command]:
    """Every list page and every custom page the user may actually open.

    Built from the registry rather than from :func:`vantage.nav.build_nav` on purpose.
    A declared ``NAV`` is presentation — it may hide a model from the sidebar to keep
    it short — but the palette is how people find the things the sidebar left out.
    """
    permissions = get_permissions()
    commands = []

    for panel in permissions.visible_panels(request, list(site.panels())):
        url = _reverse(panel.list_url_name)
        if url is None:
            continue
        commands.append(
            Command(
                id=f"go:{panel.slug}",
                label=panel.get_label_plural(),
                hint=panel.get_group(),
                icon=panel.get_icon(),
                url=url,
                keywords=(panel.opts.app_label, panel.opts.model_name, panel.get_label()),
                section="Go to",
                order=panel.order,
            )
        )

    for page_class in site.pages():
        # Instantiated because ``has_permission`` is an instance method that a page is
        # expected to override — asking the class would silently skip every override.
        if not page_class(site).has_permission(request):
            continue
        url = _reverse(f"vantage:page-{page_class.get_slug()}")
        if url is None:
            continue
        commands.append(
            Command(
                id=f"go:page-{page_class.get_slug()}",
                label=page_class.get_title(),
                hint=getattr(page_class, "group", None) or "Pages",
                icon=getattr(page_class, "icon", None) or "file-text",
                url=url,
                keywords=(page_class.get_slug(),),
                section="Go to",
                order=getattr(page_class, "order", 0),
            )
        )

    return commands


def _create_commands(site, request) -> list[Command]:
    if vantage_settings.READ_ONLY:
        return []
    permissions = get_permissions()
    commands = []
    for panel in site.panels():
        if not permissions.has_model_perm(request, panel.model, "add"):
            continue
        url = _reverse(f"vantage:{panel.slug}-add")
        if url is None:
            continue
        commands.append(
            Command(
                id=f"add:{panel.slug}",
                label=f"New {panel.get_label().lower()}",
                hint=panel.get_label_plural(),
                icon="plus",
                url=url,
                keywords=("create", "add", "new", panel.opts.model_name),
                section="Create",
                order=panel.order,
            )
        )
    return commands


def icon_paths(names) -> dict[str, str]:
    """The SVG path data for ``names``, for a client that has to draw its own rows.

    The palette builds its list in JavaScript, and the icon set lives in
    :mod:`vantage.templatetags.vantage`. Shipping the handful of paths a payload
    actually uses keeps the icons single-homed in Python rather than forking a second
    copy into a stylesheet or a sprite nobody remembers to update.
    """
    from vantage.templatetags.vantage import ICONS

    return {name: ICONS[name] for name in sorted(set(names)) if name in ICONS}


def _reverse(name: str, args: tuple = ()) -> str | None:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:  # pragma: no cover - only when urls are not mounted
        return None


# ── view ────────────────────────────────────────────────────────────────────


class CommandsView(SiteView):
    """The palette's command list, fetched once per page and filtered in the browser.

    A separate endpoint rather than a blob inlined into every response: the list is
    identical for every page a given user visits, so paying for it once — off the
    critical path, on idle — beats paying for it on every navigation.
    """

    def get(self, request, *args, **kwargs):
        commands = [command.as_dict() for command in build_commands(self.site, request)]
        response = JsonResponse(
            {
                "commands": commands,
                "sections": list(SECTION_ORDER),
                "icons": icon_paths(command["icon"] for command in commands),
            }
        )
        # Permission-scoped, exactly like the search payload.
        response["Cache-Control"] = "no-store"
        return response


def build_urls(site) -> list:
    """The three routes global search and the palette need.

    Under the ``_/`` prefix that :mod:`vantage.urls` already reserves for panel
    machinery, so they can never collide with an app label.
    """
    from django.urls import path

    from vantage import search

    return [
        path("_/search/", search.SearchView.as_view(site=site), name="search"),
        path("_/search.json", search.SearchJSONView.as_view(site=site), name="search-json"),
        path("_/commands.json", CommandsView.as_view(site=site), name="commands"),
    ]
