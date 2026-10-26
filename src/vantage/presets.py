"""UI theme presets: whole looks, selectable the way shadcn/ui's themes are.

A preset is a *delta* over the token contract in :mod:`vantage.theme`, not a second
stylesheet. It can change the neutral scale for light and dark, supply a default primary
colour, and optionally the corner radius, the shadow scale, the border width and the font
stack. Anything it does not say is inherited from the ``vantage`` preset, which is the
original look and the only preset that says nothing at all. That is what lets a custom
preset be three lines long, and what makes "``vantage`` is byte-identical to before" a
property of the code rather than of a snapshot.

Two precedence rules matter more than the rest:

* **An owner's explicit ``BRAND_COLOR`` always wins over a preset's primary.** A preset's
  primary is a *default*. Someone who set their brand colour wants their brand colour;
  switching preset must restyle the page, not silently rebrand it. The same holds for an
  explicit ``BRAND_RADIUS``. "Explicit" is detected in :mod:`vantage.conf`, because a value
  that merely equals the default (``#6366f1``) is indistinguishable from none.
* **A preset never moves a contrast pair.** Every preset is tested light and dark for the
  pairs a reader depends on (see ``tests/test_presets.py``); a custom preset is checked by
  the same pairs when you run ``manage.py check``.

The registry is read lazily. This module is imported by :mod:`vantage.conf` for validation,
so it must not import ``vantage_settings`` at module scope.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext_lazy as _

__all__ = [
    "BUILTIN_PRESETS",
    "DEFAULT_PRESET",
    "NEUTRAL_TOKENS",
    "Preset",
    "get_presets",
    "validate_ui_themes",
]

DEFAULT_PRESET = "vantage"

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

#: Tokens a preset may set on a neutral scale, i.e. every one ``theme`` defines apart from
#: the brand-derived ones. Kept as a literal so a typo in a custom preset is reported by
#: name rather than silently emitted as an unused custom property.
NEUTRAL_TOKENS: tuple[str, ...] = (
    "background",
    "foreground",
    "card",
    "card-foreground",
    "popover",
    "popover-foreground",
    "secondary",
    "secondary-foreground",
    "muted",
    "muted-foreground",
    "destructive",
    "destructive-foreground",
    "success",
    "success-foreground",
    "warning",
    "warning-foreground",
    "border",
    "input",
    "sidebar",
    "sidebar-foreground",
    "sidebar-border",
)

#: The tokens ``derive_palette`` produces from a brand colour; a preset that wants a
#: primary that is *not* a derived one (shadcn's near-black) sets them per mode instead.
BRAND_TOKENS: tuple[str, ...] = (
    "primary",
    "primary-foreground",
    "primary-hover",
    "ring",
    "accent",
    "accent-foreground",
)

#: The three rungs of the shadow scale the stylesheet reads.
SHADOW_KEYS: tuple[str, ...] = ("sm", "md", "lg")

_KNOWN_SPEC_KEYS = frozenset(
    {
        "label",
        "description",
        "light",
        "dark",
        "primary",
        "light_brand",
        "dark_brand",
        "radius",
        "shadows",
        "border_width",
        "font",
        "extras",
    }
)


@dataclass(frozen=True)
class Preset:
    """One selectable look. Every field except ``name`` and ``label`` may be omitted."""

    name: str
    label: str
    description: str = ""
    #: Overrides over the ``vantage`` neutrals, per mode.
    light: dict[str, str] = field(default_factory=dict)
    dark: dict[str, str] = field(default_factory=dict)
    #: A hex colour the brand tokens are derived from when the owner set no BRAND_COLOR.
    primary: str | None = None
    #: Brand tokens set outright, per mode, for a primary that is not derived (it wins
    #: over ``primary`` for the tokens it names).
    light_brand: dict[str, str] = field(default_factory=dict)
    dark_brand: dict[str, str] = field(default_factory=dict)
    radius: str | None = None
    #: ``{"sm": ..., "md": ..., "lg": ...}`` box-shadow values; ``"none"`` flattens.
    shadows: dict[str, str] = field(default_factory=dict)
    border_width: str | None = None
    #: A ``font-family`` value for ``--font-sans``.
    font: str | None = None
    #: Any further custom properties, without the leading ``--``.
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def has_extras(self) -> bool:
        """Whether this preset sets anything the static stylesheet defines itself."""
        return bool(self.shadows or self.border_width or self.font or self.extras)

    def layer(self) -> dict[str, str]:
        """The custom properties emitted once, outside either colour mode."""
        out: dict[str, str] = {}
        for key, name in zip(SHADOW_KEYS, ("shadow-sm", "shadow", "shadow-lg"), strict=True):
            if key in self.shadows:
                out[name] = self.shadows[key]
        if self.border_width:
            out["border-width"] = self.border_width
        if self.font:
            out["font-sans"] = self.font
        out.update(self.extras)
        return out


# ── building blocks for the built-ins ───────────────────────────────────────


def _scale(hue: float, c: float) -> tuple[dict[str, str], dict[str, str]]:
    """Light and dark neutrals for one hue family, as overrides over ``vantage``.

    ``c`` is the base chroma. The same structure as the vantage neutrals - the lightness
    steps are the ones its contrast tests already vouch for - with the hue and the chroma
    swapped, which is all that separates slate from stone from zinc.
    """

    def ok(lightness: float, mult: float) -> str:
        chroma = round(c * mult, 4)
        return f"oklch({lightness} {chroma:g} {hue:g})"

    light = {
        "foreground": ok(0.145, 1),
        "card-foreground": ok(0.145, 1),
        "popover-foreground": ok(0.145, 1),
        "secondary": ok(0.97, 0.6),
        "secondary-foreground": ok(0.21, 1.5),
        "muted": ok(0.97, 0.6),
        "muted-foreground": ok(0.5, 3),
        "border": ok(0.92, 1),
        "input": ok(0.6, 1.5),
        "sidebar": ok(0.985, 0.5),
        "sidebar-foreground": ok(0.145, 1),
        "sidebar-border": ok(0.92, 1),
    }
    dark = {
        "background": ok(0.145, 1),
        "card": ok(0.19, 1.2),
        "popover": ok(0.19, 1.2),
        "secondary": ok(0.26, 1.5),
        "muted": ok(0.26, 1.5),
        "muted-foreground": ok(0.72, 3),
        "input": ok(0.58, 1.5),
        "sidebar": ok(0.17, 1),
    }
    return light, dark


#: shadcn/ui's chrome: modest radius, one hairline shadow, nothing floating.
_SHADCN_SHADOWS = {
    "sm": "0 1px 2px 0 oklch(0 0 0 / 0.05)",
    "md": "0 1px 2px 0 oklch(0 0 0 / 0.05)",
    "lg": "0 4px 6px -1px oklch(0 0 0 / 0.1), 0 2px 4px -2px oklch(0 0 0 / 0.1)",
}

#: A near-black primary in light and a near-white one in dark, which is shadcn's neutral
#: theme and the reason its buttons look the way they do. Hover is a step towards the
#: page, ring a mid grey, accent the same tone as muted.
_INK_LIGHT = {
    "primary": "oklch(0.205 0 0)",
    "primary-foreground": "oklch(0.985 0 0)",
    "primary-hover": "oklch(0.3 0 0)",
    "ring": "oklch(0.55 0 0)",
    "accent": "oklch(0.97 0 0)",
    "accent-foreground": "oklch(0.205 0 0)",
}
_INK_DARK = {
    "primary": "oklch(0.922 0 0)",
    "primary-foreground": "oklch(0.205 0 0)",
    "primary-hover": "oklch(0.85 0 0)",
    "ring": "oklch(0.62 0 0)",
    "accent": "oklch(0.269 0 0)",
    "accent-foreground": "oklch(0.985 0 0)",
}


def _ink(hue: float, c: float) -> tuple[dict[str, str], dict[str, str]]:
    """The near-black / near-white primary, tinted to a base colour's hue."""

    def tint(token_set: dict[str, str]) -> dict[str, str]:
        out = {}
        for name, value in token_set.items():
            lightness = value.split()[0].removeprefix("oklch(")
            out[name] = f"oklch({lightness} {c:g} {hue:g})" if c else value
        return out

    return tint(_INK_LIGHT), tint(_INK_DARK)


def _base(name: str, label: str, description: str, hue: float, c: float, **kw: Any) -> Preset:
    light, dark = _scale(hue, c)
    ink_light, ink_dark = _ink(hue, c)
    return Preset(
        name=name,
        label=label,
        description=description,
        light=light,
        dark=dark,
        light_brand=ink_light,
        dark_brand=ink_dark,
        radius="0.625rem",
        shadows=_SHADCN_SHADOWS,
        **kw,
    )


def _accent(name: str, label: str, description: str, primary: str) -> Preset:
    """shadcn's coloured themes: the neutral base with one hue for every active state."""
    light, dark = _scale(0, 0)
    return Preset(
        name=name,
        label=label,
        description=description,
        light=light,
        dark=dark,
        primary=primary,
        radius="0.625rem",
        shadows=_SHADCN_SHADOWS,
    )


def _shadcn() -> Preset:
    light, dark = _scale(0, 0)
    # shadcn's dark surfaces step by lightness alone, one notch each.
    dark.update(
        {
            "card": "oklch(0.205 0 0)",
            "popover": "oklch(0.205 0 0)",
            "secondary": "oklch(0.269 0 0)",
            "muted": "oklch(0.269 0 0)",
            "sidebar": "oklch(0.205 0 0)",
        }
    )
    ink_light, ink_dark = _ink(0, 0)
    return Preset(
        name="shadcn",
        label="shadcn",
        description=_("shadcn/ui's stock neutral theme: grey, near-black buttons, flat."),
        light=light,
        dark=dark,
        light_brand=ink_light,
        dark_brand=ink_dark,
        radius="0.625rem",
        shadows=_SHADCN_SHADOWS,
    )


BUILTIN_PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset(
            name=DEFAULT_PRESET,
            label="Vantage",
            description=_("The original look: cool zinc greys and your brand colour."),
        ),
        _shadcn(),
        _base("slate", "Slate", _("Blue-grey neutrals with a near-black primary."), 257, 0.02),
        _base("stone", "Stone", _("Warm grey neutrals with a near-black primary."), 60, 0.01),
        _base("zinc", "Zinc", _("Cool grey neutrals with a near-black primary."), 286, 0.006),
        _accent("rose", "Rose", _("Neutral greys, rose accents."), "#e11d48"),
        _accent("blue", "Blue", _("Neutral greys, blue accents."), "#2563eb"),
        _accent("green", "Green", _("Neutral greys, green accents."), "#16a34a"),
        _accent("violet", "Violet", _("Neutral greys, violet accents."), "#7c3aed"),
        _accent("orange", "Orange", _("Neutral greys, orange accents."), "#ea580c"),
    )
}


# ── custom presets ──────────────────────────────────────────────────────────


def _suggest(word: str, options) -> str:
    close = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return f" Did you mean {close[0]!r}?" if close else ""


def _check_tokens(where: str, tokens: Any, allowed: tuple[str, ...]) -> list[str]:
    if not isinstance(tokens, dict):
        return [f"{where} must be a dict of token -> CSS value."]
    problems = []
    for key, value in tokens.items():
        if key not in allowed:
            problems.append(f"{where}: {key!r} is not a theme token.{_suggest(str(key), allowed)}")
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"{where}[{key!r}] must be a non-empty CSS value.")
        elif any(ch in value for ch in "{};<>"):
            problems.append(f"{where}[{key!r}] must be a single CSS value, not a rule.")
    return problems


def _problems(name: Any, spec: Any) -> list[str]:
    """Everything wrong with one custom preset, as sentences; empty when it is valid."""
    from vantage.color import HEX

    if not isinstance(name, str) or not _NAME.match(name):
        return [
            f"UI_THEMES key {name!r} must be lowercase letters, digits, '-' or '_', "
            "starting with a letter."
        ]
    where = f"UI_THEMES[{name!r}]"
    if not isinstance(spec, dict):
        return [f"{where} must be a dict."]

    out = []
    for key in spec:
        if key not in _KNOWN_SPEC_KEYS:
            out.append(
                f"{where}: {key!r} is not a preset key.{_suggest(str(key), _KNOWN_SPEC_KEYS)}"
            )
    for mode in ("light", "dark"):
        if mode in spec:
            out += _check_tokens(f"{where}[{mode!r}]", spec[mode], NEUTRAL_TOKENS)
    for mode in ("light_brand", "dark_brand"):
        if mode in spec:
            out += _check_tokens(f"{where}[{mode!r}]", spec[mode], BRAND_TOKENS)
    if "shadows" in spec:
        out += _check_tokens(f"{where}['shadows']", spec["shadows"], SHADOW_KEYS)
    if spec.get("primary") is not None and not (
        isinstance(spec["primary"], str) and HEX.match(spec["primary"])
    ):
        out.append(f"{where}['primary'] must be a hex colour like '#2563eb'.")
    for key in ("radius", "border_width", "font", "label", "description"):
        if key in spec and spec[key] is not None and not isinstance(spec[key], str):
            out.append(f"{where}[{key!r}] must be a string.")
    if "extras" in spec:
        if not isinstance(spec["extras"], dict):
            out.append(f"{where}['extras'] must be a dict.")
        else:
            for k, v in spec["extras"].items():
                if not (isinstance(k, str) and re.match(r"^[a-z][a-z0-9-]*$", k)):
                    out.append(f"{where}['extras'] name {k!r} must be lowercase with dashes.")
                elif not isinstance(v, str) or any(ch in v for ch in "{};<>"):
                    out.append(f"{where}['extras'][{k!r}] must be a single CSS value.")
    return out


def validate_ui_themes(custom: Any, chosen: Any) -> list[str]:
    """Problems with ``UI_THEMES`` and with ``UI_THEME`` naming something that exists."""
    if not isinstance(custom, dict):
        return [f"VANTAGE['UI_THEMES'] must be a dict, got {type(custom).__name__}."]
    problems = []
    for name, spec in custom.items():
        problems += _problems(name, spec)
    known = {**BUILTIN_PRESETS, **dict.fromkeys(custom)}
    if chosen not in known:
        problems.append(
            f"VANTAGE['UI_THEME'] must name a preset ({', '.join(sorted(known))}); "
            f"got {chosen!r}.{_suggest(str(chosen), known)}"
        )
    return problems


def _from_spec(name: str, spec: dict[str, Any]) -> Preset:
    inherited = BUILTIN_PRESETS[DEFAULT_PRESET]
    return Preset(
        name=name,
        label=spec.get("label") or name.replace("-", " ").replace("_", " ").title(),
        description=spec.get("description") or "",
        # Partial presets inherit from vantage: only what is named is overridden.
        light={**inherited.light, **spec.get("light", {})},
        dark={**inherited.dark, **spec.get("dark", {})},
        primary=spec.get("primary"),
        light_brand=dict(spec.get("light_brand", {})),
        dark_brand=dict(spec.get("dark_brand", {})),
        radius=spec.get("radius"),
        shadows=dict(spec.get("shadows", {})),
        border_width=spec.get("border_width"),
        font=spec.get("font"),
        extras=dict(spec.get("extras", {})),
    )


def get_presets() -> dict[str, Preset]:
    """Built-ins plus ``VANTAGE['UI_THEMES']``; a custom name replaces a built-in."""
    from vantage.conf import vantage_settings

    custom = vantage_settings.UI_THEMES or {}
    return {**BUILTIN_PRESETS, **{name: _from_spec(name, spec) for name, spec in custom.items()}}
