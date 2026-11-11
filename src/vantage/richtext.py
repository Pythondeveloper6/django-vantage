"""Rich-text values: an allowlist sanitiser, and the one place its policy lives.

A rich-text field stores HTML, which makes it the only field in the panel whose value is
markup rather than text. The rule this module enforces is that markup is never trusted
because of where it came from. It is cleaned when it is saved, so the database holds
nothing dangerous that the panel wrote, and cleaned again every time it is drawn, so
that a row written by anything else (a script, a data migration, another application on
the same table) cannot put a script on the page either.

The cleaning is an allowlist, delegated to ``nh3`` (the Rust ``ammonia`` library).
Anything not named here is removed: unknown tags are unwrapped, ``script`` and ``style``
are dropped with their contents, every ``on*`` handler, ``style`` attribute and
non-allowlisted URL scheme is stripped.

A project widens the policy with ``VANTAGE["RICHTEXT_EXTRA_TAGS"]``,
``["RICHTEXT_EXTRA_ATTRIBUTES"]`` and ``["RICHTEXT_URL_SCHEMES"]``. Widening has a floor:
:func:`validate_policy` refuses active content (``script``, ``iframe``, ``object``, ...),
``style`` and event-handler attributes whatever the project asks for.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from typing import Any

import nh3
from django.core.exceptions import ImproperlyConfigured
from django.utils.html import strip_tags
from django.utils.safestring import SafeString, mark_safe

__all__ = [
    "BASE_ATTRIBUTES",
    "BASE_TAGS",
    "BASE_URL_SCHEMES",
    "FORBIDDEN_TAGS",
    "is_effectively_empty",
    "render_richtext",
    "resolve_policy",
    "sanitize_html",
    "to_text",
    "validate_policy",
]

#: What the editor can produce: paragraphs, line breaks, bold, italic, underline,
#: strikethrough, inline code and code blocks, block quotes, lists, links and headings.
BASE_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "code",
        "pre",
        "blockquote",
        "ul",
        "ol",
        "li",
        "a",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

#: Attributes per tag. Only a link carries any; ``rel`` is set by the sanitiser itself.
BASE_ATTRIBUTES: dict[str, frozenset[str]] = {"a": frozenset({"href", "title"})}

#: URL schemes a link may use. Relative URLs are allowed; ``javascript:``, ``data:`` and
#: ``vbscript:`` are not, because they are not on this list.
BASE_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "tel"})

#: Tags no project setting can switch on: each runs code, loads a document or reshapes
#: the page around it.
FORBIDDEN_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "base",
        "link",
        "meta",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "svg",
        "math",
        "template",
        "noscript",
        "title",
        "xmp",
        "noembed",
        "noframes",
        "plaintext",
        "listing",
        "portal",
        "marquee",
    }
)

#: Attributes no project setting can switch on. ``style`` is a CSS injection and
#: click-jacking surface; the ``on*`` family are script; the rest rewrite how the
#: surrounding page treats the element.
FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "style",
        "srcdoc",
        "formaction",
        "id",
        "name",
        "class",
        # URL-valued attributes the sanitiser does not scheme-check.
        "srcset",
        "background",
        "poster",
        "ping",
        "action",
        "data",
        "xlink:href",
        "xlinkhref",
        "href:xlink",
        "dynsrc",
        "lowsrc",
        "manifest",
        "codebase",
        "archive",
        "usemap",
        # Focus, editing and UI hooks that change how the page treats the element.
        "autofocus",
        "contenteditable",
        "tabindex",
        "accesskey",
        "draggable",
        "is",
        "slot",
        "popover",
        "popovertarget",
        "inert",
    }
)

#: Schemes that execute or embed content.
FORBIDDEN_SCHEMES: frozenset[str] = frozenset({"javascript", "data", "vbscript", "blob", "file"})

#: Tags whose content is dropped with the tag instead of being left behind as text.
CLEAN_CONTENT_TAGS: frozenset[str] = frozenset({"script", "style"})

_LINK_REL = "noopener noreferrer nofollow"
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_ATTR_NAME = re.compile(r"^[a-z][a-z0-9:_-]*$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*$")


def validate_policy(
    tags: Any, attributes: Any, schemes: Any
) -> None:  # raises ImproperlyConfigured
    """Check the three project-supplied settings; raise on anything unsafe or malformed."""
    if not isinstance(tags, (list, tuple, set, frozenset)):
        raise ImproperlyConfigured("VANTAGE['RICHTEXT_EXTRA_TAGS'] must be a list of tag names.")
    for tag in tags:
        if not isinstance(tag, str) or not _NAME.match(tag):
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_EXTRA_TAGS'] has an invalid tag name: {tag!r}."
            )
        if tag in FORBIDDEN_TAGS:
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_EXTRA_TAGS'] cannot allow <{tag}>: it runs code or "
                "loads content, and no setting may switch that on."
            )

    if not isinstance(attributes, dict):
        raise ImproperlyConfigured(
            "VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'] must be a dict of tag name to a list "
            'of attribute names, e.g. {"a": ["target"]}.'
        )
    for tag, names in attributes.items():
        if tag != "*" and (not isinstance(tag, str) or not _NAME.match(tag)):
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'] has an invalid tag key: {tag!r}."
            )
        if tag in FORBIDDEN_TAGS:
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'] cannot configure <{tag}>."
            )
        if not isinstance(names, (list, tuple, set, frozenset)):
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'][{tag!r}] must be a list of names."
            )
        for name in names:
            if not isinstance(name, str) or not _ATTR_NAME.match(name.lower()):
                raise ImproperlyConfigured(
                    f"VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'] has an invalid attribute: {name!r}."
                )
            lowered = name.lower()
            if lowered.startswith("on") or lowered in FORBIDDEN_ATTRIBUTES:
                raise ImproperlyConfigured(
                    f"VANTAGE['RICHTEXT_EXTRA_ATTRIBUTES'] cannot allow {name!r}: "
                    "event handlers, style, URL-valued and focus/UI attributes are never "
                    "permitted."
                )

    if not isinstance(schemes, (list, tuple, set, frozenset)):
        raise ImproperlyConfigured("VANTAGE['RICHTEXT_URL_SCHEMES'] must be a list of schemes.")
    for scheme in schemes:
        if not isinstance(scheme, str) or not _SCHEME.match(scheme):
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_URL_SCHEMES'] has an invalid scheme: {scheme!r}."
            )
        if scheme in FORBIDDEN_SCHEMES:
            raise ImproperlyConfigured(
                f"VANTAGE['RICHTEXT_URL_SCHEMES'] cannot allow {scheme}: URLs."
            )


def resolve_policy() -> tuple[
    frozenset[str], tuple[tuple[str, frozenset[str]], ...], frozenset[str]
]:
    """The effective ``(tags, attributes, schemes)``, as hashable values."""
    from vantage.conf import vantage_settings

    tags = BASE_TAGS | frozenset(vantage_settings.RICHTEXT_EXTRA_TAGS)

    merged: dict[str, set[str]] = {tag: set(names) for tag, names in BASE_ATTRIBUTES.items()}
    for tag, names in vantage_settings.RICHTEXT_EXTRA_ATTRIBUTES.items():
        merged.setdefault(tag, set()).update(name.lower() for name in names)
    attributes = tuple(sorted((tag, frozenset(names)) for tag, names in merged.items()))

    schemes = BASE_URL_SCHEMES | frozenset(vantage_settings.RICHTEXT_URL_SCHEMES)
    return tags, attributes, schemes


@lru_cache(maxsize=8)
def _cleaner(
    tags: frozenset[str],
    attributes: tuple[tuple[str, frozenset[str]], ...],
    schemes: frozenset[str],
) -> nh3.Cleaner:
    attribute_map = {tag: set(names) for tag, names in attributes}
    # nh3 manages ``rel`` itself when ``link_rel`` is given, and refuses it in the map.
    if "a" in attribute_map:
        attribute_map["a"].discard("rel")
    return nh3.Cleaner(
        tags=set(tags),
        attributes=attribute_map,
        url_schemes=set(schemes),
        link_rel=_LINK_REL,
        strip_comments=True,
        clean_content_tags=set(CLEAN_CONTENT_TAGS),
    )


def sanitize_html(value: Any) -> str:
    """``value`` as HTML that contains only the allowlisted markup.

    ``None`` and non-strings become ``""`` or their string form respectively; the result
    is a plain ``str``, never marked safe, so it is still escaped anywhere it is dropped
    into a template without going through :func:`render_richtext`.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        return ""
    return _cleaner(*resolve_policy()).clean(text)


def to_text(value: Any) -> str:
    """The visible text of ``value``, for a table cell or a search snippet."""
    cleaned = sanitize_html(value)
    # Block boundaries become spaces so "<p>a</p><p>b</p>" does not read "ab".
    spaced = re.sub(r"</(p|li|h[1-6]|blockquote|pre)>|<br\s*/?>", " ", cleaned)
    return " ".join(html.unescape(strip_tags(spaced)).replace("\xa0", " ").split())


def is_effectively_empty(value: str) -> bool:
    """True when ``value`` has no text, which is what an emptied editor leaves behind."""
    return not to_text(value).strip()


def render_richtext(value: Any) -> SafeString:
    """``value`` sanitised and marked safe: the only way stored markup reaches a page."""
    cleaned = sanitize_html(value)
    return mark_safe(cleaned)  # noqa: S308 - the output of the allowlist sanitiser above
