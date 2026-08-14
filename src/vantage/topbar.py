"""Building the top bar, and the identity that sits at the end of it.

Two layers, resolved narrowest first — the order this package uses everywhere::

    NavConfiguration.topbar row  >  the built-in arrangement

There is no ``VANTAGE["TOPBAR"]`` between them on purpose. The sidebar has a Python
declaration because a sidebar is a map of the *registry* and a developer is the person
who knows it; the top bar is four controls and whatever links the site owner wants
beside them, which is a decision the person looking at the panel is better placed to
make than the person who deployed it. If a declaration is ever wanted it slots in
between these two without moving anything: :func:`build_topbar` already takes the
layout as an argument.

**The stored layout can reorder, hide and add — it can never grant access.** A stored
item naming a route is reversed at render time and dropped if the route is gone; one
naming a custom page is checked against that page's own ``has_permission`` for *this*
request, so a link written by a superuser never tells a colleague that a page exists.
An external URL is re-validated against :data:`SAFE_SCHEMES` on the way out as well as
on the way in, because a row can be written by something other than the editor form.

**Nothing here is a foreign key**, for the reason stated in :mod:`vantage.models`: an
item names a route as the text ``"vantage:jobs"`` and a page as its slug, so removing
either leaves an inert entry the editor can show and delete.

The user menu's initials are the other half of this module. They are derived from
fields the user controls, which makes them two things at once: a string that must never
reach the page as HTML — hence a plain ``str`` return and template autoescaping, never
``mark_safe`` — and a string in an unknown script, which is why "the first letter" is
computed as a grapheme rather than as ``name[0]``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from django.urls import NoReverseMatch, reverse
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings

__all__ = [
    "BUILTIN_KEYS",
    "DEFAULT_ORDER",
    "ITEM_KINDS",
    "REQUIRED_KEYS",
    "SAFE_SCHEMES",
    "Topbar",
    "TopbarSlot",
    "build_topbar",
    "clean_layout",
    "clean_url",
    "default_layout",
    "display_name",
    "editor_slots",
    "initials",
    "invalidate_topbar",
    "reset_layout",
    "save_layout",
    "stored_layout",
    "topbar_context",
]

#: The controls the panel ships with, in the order they are offered in the editor.
#: ``user`` is in the list but can never be hidden — see :func:`clean_layout`.
BUILTIN_KEYS: tuple[str, ...] = ("environment", "search", "theme", "user")

#: The arrangement with nothing stored. ``environment`` is present but rendered as the
#: full-width banner rather than at its position here; see :attr:`Topbar.banner`.
DEFAULT_ORDER: tuple[str, ...] = ("environment", "search", "theme", "user")

#: The one control that has no "off". Hiding sign-out from the only place it appears
#: would lock a session open until the cookie expired, and no arrangement is worth
#: that. The editor does not offer it and :func:`clean_layout` puts it back.
REQUIRED_KEYS: frozenset[str] = frozenset({"user"})

#: What a custom item can point at. Deliberately three, and deliberately not "model":
#: a model already has a sidebar entry, and a top bar with eight of them is the strip
#: this feature exists to get rid of.
ITEM_KINDS: tuple[tuple[str, Any], ...] = (
    ("route", _("A page in this panel")),
    ("page", _("A custom page")),
    ("url", _("An external link")),
)

#: Schemes an external item may use. An allowlist rather than a ``javascript:`` denylist
#: because the denylist is the one that gets bypassed: ``java\tscript:``, ``JaVaScRiPt:``,
#: ``data:text/html``, ``vbscript:`` and a dozen encodings of each are all one entry on
#: a list nobody remembered to extend.
SAFE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Separators inside one word of a name or a username. ``ada.lovelace``, ``ada_lovelace``
#: and ``anne-marie`` are all two initials; a space is handled by ``split()`` above this.
_WORD_SPLIT = re.compile(r"[\s._+\-]+")

#: Unicode marks that belong to the character before them. Taking ``name[0]`` of a
#: decomposed "É" yields a bare "E" and drops the accent; of a Devanagari syllable it
#: yields the consonant without its vowel sign. Neither is the first letter.
_COMBINING = frozenset({"Mn", "Mc", "Me"})


# ── slots ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TopbarSlot:
    """One thing in the bar, resolved for one request."""

    key: str
    #: ``"search"``, ``"theme"``, ``"environment"``, ``"user"`` or ``"link"``.
    kind: str
    label: str = ""
    url: str = ""
    icon: str | None = None
    external: bool = False


@dataclass(frozen=True)
class Topbar:
    """The bar for one request."""

    slots: tuple[TopbarSlot, ...] = ()
    #: The environment text, when it is being rendered as the full-width banner rather
    #: than as a chip in the bar. Empty otherwise, including when it is switched off.
    banner: str = ""
    #: Whether this request may open the arrangement screen. Carried here rather than
    #: as a separate context key so the one thing that draws the bar knows everything
    #: the bar needs, including whether to offer the link that changes it.
    may_arrange: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience for templates
        return bool(self.slots or self.banner)


# ── the stored layout ───────────────────────────────────────────────────────
#
# Storage is one JSON column on the sidebar's own singleton row. The top bar and the
# sidebar are one arrangement made by one person in one sitting, so they are one row:
# a second singleton would be a second thing to reset, a second thing to migrate and a
# second query on every page that renders the shell.
#
# The row itself is read through :mod:`vantage.nav`'s cache rather than queried again
# here — see that module's "reading the row" section. It already reads at most once per
# request, already caches a miss (no row at all) the same way a hit is cached, and
# already stays quiet when the table does not exist yet. A second, separate cache in
# this module would either duplicate that machinery or, worse, disagree with it about
# whether the database answered this request yet.


#: The model and column the layout lives in. Named once so the reconciliation is one
#: line if the sidebar's storage ever moves.
LAYOUT_MODEL = ("vantage", "NavConfiguration")
LAYOUT_FIELD = "topbar"


def _model():
    """The storage model, or ``None`` when asking is not safe yet."""
    from django.apps import apps

    if not apps.ready:
        return None
    try:
        return apps.get_model(*LAYOUT_MODEL)
    except LookupError:  # pragma: no cover - only when the app is not installed
        return None


def stored_layout() -> dict | None:
    """The saved arrangement, or ``None`` when the code decides.

    ``None`` and ``{}`` are different answers and stay different: no row means the
    built-in arrangement, an empty dict means somebody saved an arrangement that
    happens to hold no custom items.

    Reads :func:`vantage.nav._singleton_row`, the same row
    :func:`vantage.nav.stored_declaration` reads, so a page that renders both the
    sidebar and the top bar pays for that row once. Failure-tolerant by inheritance:
    that shared reader already stays quiet before ``migrate`` and in tests that never
    asked for a database, for the reasons :mod:`vantage.siteconfig` states.
    """
    from vantage.nav import _singleton_row

    row = _singleton_row()
    raw = getattr(row, LAYOUT_FIELD, None) if row is not None else None
    return clean_layout(raw) if isinstance(raw, dict) else None


def invalidate_topbar() -> None:
    """Drop the shared cached row. Saving must change the next page, not the next restart.

    A thin alias for :func:`vantage.nav.invalidate_nav`. This module keeps no cache of
    its own to drop — :func:`stored_layout` derives everything from the row that
    function already caches — but a caller that writes to this row has no reason to
    know that, so it still has something of this module's own to call afterwards.
    """
    from vantage.nav import invalidate_nav

    invalidate_nav()


def save_layout(layout: dict, user=None) -> None:
    """Store an arrangement, cleaned. Raises ``LookupError`` if the column is absent."""
    model = _model()
    if model is None:  # pragma: no cover - only when the app is not installed
        raise LookupError("vantage.NavConfiguration is not installed.")
    row = model.load()
    if not hasattr(row, LAYOUT_FIELD):  # pragma: no cover - reconciliation guard
        raise LookupError(
            f"{LAYOUT_MODEL[1]} has no {LAYOUT_FIELD!r} column; run migrations."
        )
    setattr(row, LAYOUT_FIELD, clean_layout(layout))
    if user is not None and getattr(user, "is_authenticated", False):
        row.updated_by = user
    row.save()
    invalidate_topbar()


def reset_layout() -> None:
    """Hand the top bar back to the code.

    Writes ``NULL`` rather than the built-in arrangement, so a later change to the
    defaults reaches a panel that was reset — freezing today's answer into the row
    would make Reset a slow way of pinning the past.
    """
    model = _model()
    if model is None:  # pragma: no cover
        return
    row = model.objects.filter(pk=model.SINGLETON_PK).first()
    if row is None or not hasattr(row, LAYOUT_FIELD):
        return
    setattr(row, LAYOUT_FIELD, None)
    row.save()
    invalidate_topbar()


# ── cleaning ────────────────────────────────────────────────────────────────


def default_layout() -> dict:
    """The built-in arrangement, as a layout the editor can round-trip."""
    return {"order": list(DEFAULT_ORDER), "hidden": [], "banner": True, "items": []}


def clean_url(raw: Any) -> tuple[str, bool] | None:
    """``(url, is_external)``, or ``None`` for anything that must not become an href.

    Applied on the way *in* from the form and again on the way *out* of the database,
    because a row can be written by a data migration, a shell, or an older version of
    this function.
    """
    text = str(raw or "").strip()
    if not text:
        return None

    # Control characters and stray whitespace are stripped before the scheme is read,
    # or "java\tscript:alert(1)" — which browsers happily execute — passes as a
    # relative path with no scheme at all.
    cleaned = "".join(char for char in text if ord(char) > 0x20 and char != "\x7f")
    if not cleaned:
        return None

    if cleaned.startswith("//"):
        # Protocol-relative. External, and it inherits the panel's own scheme.
        return cleaned, True
    if cleaned.startswith("/"):
        return cleaned, False

    scheme, separator, _rest = cleaned.partition(":")
    if not separator:
        # A bare "example.com" would be resolved by the browser as a relative path,
        # which is never what was meant. Ask for the scheme rather than guess it.
        return None
    if scheme.lower() in SAFE_SCHEMES:
        return cleaned, True
    return None


def _clean_item(raw: Any, seen: set[str]) -> dict | None:
    """One custom item, or ``None`` if it cannot be stored."""
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind") or "").strip()
    if kind not in {key for key, _label in ITEM_KINDS}:
        return None

    target = str(raw.get("target") or "").strip()
    if not target:
        return None
    if kind == "url" and clean_url(target) is None:
        return None

    identifier = str(raw.get("id") or "").strip()
    # A hand-written id is kept when it is usable and replaced when it is not: the id
    # goes into a DOM id and into "item:<id>" in the order list, so it may not carry
    # a colon, a space or anything a selector would have to escape.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", identifier) or identifier in seen:
        identifier = _fresh_id(seen)
    seen.add(identifier)

    icon = str(raw.get("icon") or "").strip() or None
    return {
        "id": identifier,
        "kind": kind,
        "target": target,
        "label": str(raw.get("label") or "").strip()[:60],
        "icon": icon,
    }


def _fresh_id(seen: set[str]) -> str:
    index = 1
    while f"i{index}" in seen:
        index += 1
    return f"i{index}"


def clean_layout(raw: Any) -> dict:
    """Coerce anything into a layout this module will render.

    Three properties hold whatever arrives:

    * every built-in control is accounted for, so a layout written before a control
      existed does not make it unreachable;
    * ``user`` is never hidden, so sign-out is never lost;
    * every custom item has a usable id, and every id appears at most once.
    """
    source = raw if isinstance(raw, dict) else {}

    seen: set[str] = set()
    items: list[dict] = []
    for entry in source.get("items") or ():
        item = _clean_item(entry, seen)
        if item is not None:
            items.append(item)
    known = {f"item:{item['id']}" for item in items}

    hidden: list[str] = []
    for key in source.get("hidden") or ():
        key = str(key)
        if key in REQUIRED_KEYS:
            continue
        if (key in BUILTIN_KEYS or key in known) and key not in hidden:
            hidden.append(key)

    order: list[str] = []
    for key in source.get("order") or ():
        key = str(key)
        if (key in BUILTIN_KEYS or key in known) and key not in order:
            order.append(key)

    # Anything real but unmentioned is appended rather than dropped. Registering a new
    # control, or adding an item by hand, must never be silently invisible — the same
    # rule NAV_UNLISTED states for the sidebar.
    for key in (*BUILTIN_KEYS, *(f"item:{item['id']}" for item in items)):
        if key not in order and key not in hidden:
            order.append(key)

    return {
        "order": order,
        "hidden": hidden,
        "banner": bool(source.get("banner", True)),
        "items": items,
    }


# ── building ────────────────────────────────────────────────────────────────


def _page_url(site, request, slug: str) -> str | None:
    """The URL of a custom page, if this request may open it.

    ``Page.has_permission`` is asked here and not only at the page itself: linking to
    a page that 403s on click is worse than omitting the link, and for a page gated on
    something sensitive the label in the bar is itself a disclosure.
    """
    if site is None:
        return None
    for page in site.pages():
        if page.get_slug() != slug:
            continue
        if request is not None and not page(site).has_permission(request):
            return None
        try:
            return reverse(f"vantage:page-{slug}")
        except NoReverseMatch:  # pragma: no cover - a registered page always reverses
            return None
    return None


def _link_slot(site, request, item: dict) -> TopbarSlot | None:
    kind, target = item["kind"], item["target"]

    if kind == "route":
        try:
            url = reverse(target)
        except NoReverseMatch:
            # The route was renamed or its app was removed. An inert row the editor
            # still lists and can delete beats a 500 on every page in the panel.
            return None
        external = False
    elif kind == "page":
        url = _page_url(site, request, target)
        if url is None:
            return None
        external = False
    else:
        resolved = clean_url(target)
        if resolved is None:
            return None
        url, external = resolved

    return TopbarSlot(
        key=f"item:{item['id']}",
        kind="link",
        label=item["label"] or target,
        url=url,
        icon=item["icon"],
        external=external,
    )


def build_topbar(site=None, request=None, layout: dict | None = None) -> Topbar:
    """The bar for this request.

    ``layout`` is normally left out and read from the database. It is a parameter so
    the editor can render a preview of an arrangement that has not been saved, and so
    the resolution can be tested without a row.
    """
    resolved = clean_layout(layout if layout is not None else stored_layout())
    items = {item["id"]: item for item in resolved["items"]}
    hidden = set(resolved["hidden"])

    environment = str(vantage_settings.ENVIRONMENT or "")
    # The banner is the environment's home. It is the loudest thing on the page and it
    # is loud on purpose, so moving it into the bar is a choice somebody makes, not
    # something that happens to them the first time they save an arrangement.
    as_banner = resolved["banner"] and "environment" not in hidden

    slots: list[TopbarSlot] = []
    for key in resolved["order"]:
        if key in hidden:
            continue
        if key == "search":
            slots.append(TopbarSlot(key=key, kind="search"))
        elif key == "theme":
            if vantage_settings.THEME_TOGGLE:
                slots.append(TopbarSlot(key=key, kind="theme"))
        elif key == "environment":
            if environment and not as_banner:
                slots.append(TopbarSlot(key=key, kind="environment", label=environment))
        elif key == "user":
            slots.append(TopbarSlot(key=key, kind="user"))
        elif key.startswith("item:"):
            item = items.get(key[5:])
            slot = _link_slot(site, request, item) if item else None
            if slot is not None:
                slots.append(slot)

    return Topbar(
        slots=tuple(slots),
        banner=environment if (as_banner and environment) else "",
        may_arrange=_may_arrange(request),
    )


def _may_arrange(request) -> bool:
    """Whether to offer the "customise" link, asked of the settings gate.

    The same permission, not a second one: arranging the bar and rebranding the site
    are the same job done by the same person, and a second rule would be a second
    thing to get wrong. Imported inside the function because ``settingsview`` imports
    ``views``, which imports this module.
    """
    if request is None:
        return False
    from vantage.settingsview import may_edit_settings

    try:
        return may_edit_settings(request)
    except Exception:  # pragma: no cover - a custom SETTINGS_PERMISSION callable
        # The header is never the right place to raise. A missing link is recoverable;
        # a 500 on every page in the panel because someone's rule threw is not.
        return False


def topbar_context(site=None, request=None) -> dict:
    """Everything the shell needs to draw the end of the header.

    One function for both callers — :class:`vantage.views.SiteView` and
    :func:`vantage.authviews.shell_context` — because the password-change and error
    pages render the same header, and a header that loses its sign-out button on a 404
    is a header with two implementations.
    """
    user = getattr(request, "user", None)
    return {
        "topbar": build_topbar(site, request),
        "account_name": display_name(user),
        "account_initials": initials(user),
    }


# ── who is signed in ────────────────────────────────────────────────────────


def display_name(user) -> str:
    """The name to put at the top of the menu: a real name if there is one."""
    if user is None:
        return ""
    full = _full_name(user)
    return full or _username(user)


def _full_name(user) -> str:
    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    joined = " ".join(part for part in (first, last) if part)
    if joined:
        return joined
    # A custom user model may implement get_full_name() over columns of its own.
    getter = getattr(user, "get_full_name", None)
    return str(getter() or "").strip() if callable(getter) else ""


def _username(user) -> str:
    getter = getattr(user, "get_username", None)
    if callable(getter):
        return str(getter() or "").strip()
    return str(getattr(user, "username", "") or "").strip()


def _first_grapheme(word: str) -> str:
    """The first letter of one word, including whatever attaches to it.

    Not ``word[0]``: a decomposed "Élodie" starts with a bare "E" and a combining
    acute, "नमस्ते" starts with a consonant whose vowel sign is the code point after
    it, and a leading quote or bracket is not a letter at all.
    """
    for index, char in enumerate(word):
        if not char.isalnum():
            continue
        cluster = char
        for following in word[index + 1 :]:
            if unicodedata.category(following) not in _COMBINING:
                break
            cluster += following
        return cluster
    return ""


def _upper(text: str) -> str:
    """Uppercase, with the one language where ``str.upper`` changes the letter.

    Turkish and Azerbaijani have a dotted and a dotless i, and Python's ``upper`` is
    not locale-aware: it turns "i" into "I", which in those languages is a different
    letter from the one the person typed.
    """
    language = (get_language() or "").lower().split("-")[0]
    if language in ("tr", "az"):
        # Turkish and Azeri have two separate i letters, dotted and dotless, and
        # they uppercase to different characters. Ruff reads the dotless one as a
        # lookalike typo for ASCII i; it is the character actually meant, so the
        # suppression below is the correct answer rather than a way around the rule.
        text = text.replace("i", "\u0130").replace("\u0131", "I")
    return text.upper()


def initials(user) -> str:
    """One or two letters for the avatar, or ``""`` when there is nothing to show.

    Always a plain ``str``. It is built from ``first_name``, ``last_name`` and the
    username — three fields the user themselves can usually edit — so it is escaped by
    the template like any other value and is never marked safe.

    The awkward cases, all of which are ordinary rather than hypothetical:

    * one word — "Prince" is ``P``, not ``PR``, because a second letter of a single
      name is an invention;
    * an email address in the name or as the username — "ada.lovelace@example.com" is
      ``AL``, taken from the local part, never ``A@``;
    * a script where a letter is more than one code point — see :func:`_first_grapheme`;
    * nothing usable at all — an empty name, a username of "..." or one made only of
      emoji. The caller shows a generic mark; a "?" in a circle is not a name.
    """
    source = display_name(user)
    if not source:
        return ""

    if "@" in source:
        # An email address as a name. The domain is the same for a whole company, so
        # "AE" for everyone at example.com is worse than no initials at all.
        source = source.split("@", 1)[0]

    graphemes = [
        grapheme
        for grapheme in (_first_grapheme(word) for word in _WORD_SPLIT.split(source))
        if grapheme
    ]
    if not graphemes:
        return ""
    if len(graphemes) == 1:
        return _upper(graphemes[0])
    # First and last: "Ada Byron Lovelace" is AL, which is how she signed things.
    return _upper(graphemes[0] + graphemes[-1])


# ── the editor's raw material ───────────────────────────────────────────────


@dataclass
class EditorSlot:
    """One row in the editor: what it is, what it is called, whether it shows."""

    key: str
    label: Any
    description: Any = ""
    visible: bool = True
    builtin: bool = True
    removable: bool = False
    lockable: bool = True
    item: dict = field(default_factory=dict)
    #: Set when a stored item no longer resolves — a renamed route, a page that was
    #: unregistered. Listed so it can be deleted rather than silently ignored.
    broken: bool = False


#: What each built-in control is called in the editor, and what it does.
BUILTIN_LABELS: dict[str, tuple[Any, Any]] = {
    "search": (_("Search"), _("The search box, and the ⌘K command palette behind it.")),
    "theme": (_("Theme toggle"), _("Switches one person between light and dark.")),
    "environment": (
        _("Environment banner"),
        _("Only shows when an environment name is set in the settings."),
    ),
    "user": (
        _("Account menu"),
        _("Who you are signed in as, your password, the theme, and sign out."),
    ),
}


def editor_slots(site=None, request=None, layout: dict | None = None) -> list[EditorSlot]:
    """The layout as rows to draw, in stored order, hidden ones included."""
    resolved = clean_layout(layout if layout is not None else stored_layout())
    items = {item["id"]: item for item in resolved["items"]}
    hidden = set(resolved["hidden"])

    rows: list[EditorSlot] = []
    for key in (*resolved["order"], *resolved["hidden"]):
        if any(row.key == key for row in rows):
            continue
        if key in BUILTIN_KEYS:
            label, description = BUILTIN_LABELS[key]
            rows.append(
                EditorSlot(
                    key=key,
                    label=label,
                    description=description,
                    visible=key not in hidden,
                    lockable=key not in REQUIRED_KEYS,
                )
            )
        elif key.startswith("item:") and key[5:] in items:
            item = items[key[5:]]
            rows.append(
                EditorSlot(
                    key=key,
                    label=item["label"] or item["target"],
                    description=item["target"],
                    visible=key not in hidden,
                    builtin=False,
                    removable=True,
                    item=item,
                    broken=_link_slot(site, request, item) is None,
                )
            )
    return rows
