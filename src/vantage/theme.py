"""Turning settings into the CSS custom properties every component reads.

The token contract is shadcn/ui's, because it is well-designed and widely understood:
a small set of semantic role names (``--background``, ``--foreground``, ``--primary``,
``--muted``, ``--border``, ``--ring``) rather than a palette of colour names. No
component ever hard-codes a colour, so the whole appearance is controlled from here.

Neutrals are fixed per *preset* (see :mod:`vantage.presets`); within a preset only the
brand-derived tokens vary with ``BRAND_COLOR``. That
split matters: a site owner supplying one colour should get a branded panel, not a
panel where their colour has leaked into the page background and the table borders.

Both themes are always emitted. Light lives on bare ``:root`` so a page with no theme
information still renders correctly; dark is applied under both
``:root[data-theme="dark"]`` (an explicit user choice) and a
``prefers-color-scheme`` media query guarded with ``:not([data-theme="light"])``
(the system default). Defining a colour only inside a media query is the bug that
makes a theme toggle work in one direction and not the other.
"""

from __future__ import annotations

from vantage.color import derive_palette
from vantage.conf import vantage_settings
from vantage.presets import DEFAULT_PRESET, Preset, get_presets

__all__ = ["build_tokens", "render_theme_css", "resolve_preset", "swatch_css"]

#: Neutral scale for the light theme. Very slightly cool rather than pure grey, which
#: is what stops a panel looking like an unstyled document.
LIGHT_NEUTRALS: dict[str, str] = {
    "background": "oklch(1 0 0)",
    "foreground": "oklch(0.145 0.004 285)",
    "card": "oklch(1 0 0)",
    "card-foreground": "oklch(0.145 0.004 285)",
    "popover": "oklch(1 0 0)",
    "popover-foreground": "oklch(0.145 0.004 285)",
    "secondary": "oklch(0.97 0.002 285)",
    "secondary-foreground": "oklch(0.21 0.006 285)",
    "muted": "oklch(0.97 0.002 285)",
    "muted-foreground": "oklch(0.53 0.014 285)",
    "destructive": "oklch(0.577 0.245 27)",
    "destructive-foreground": "oklch(0.985 0 0)",
    "success": "oklch(0.52 0.15 150)",
    "success-foreground": "oklch(0.985 0 0)",
    "warning": "oklch(0.68 0.16 70)",
    "warning-foreground": "oklch(0.21 0.02 70)",
    "border": "oklch(0.92 0.004 286)",
    # A form field's boundary is how it is found (WCAG 1.4.11): 3:1 against every
    # surface a field sits on, muted included. --border stays light; it only separates.
    "input": "oklch(0.62 0.004 286)",
    "sidebar": "oklch(0.985 0.002 285)",
    "sidebar-foreground": "oklch(0.145 0.004 285)",
    "sidebar-border": "oklch(0.92 0.004 286)",
}

#: Dark theme neutrals. Not pure black — a near-black surface with a trace of the
#: same cool hue keeps elevation readable and avoids the smeary look of #000 on OLED.
DARK_NEUTRALS: dict[str, str] = {
    "background": "oklch(0.145 0.004 285)",
    "foreground": "oklch(0.985 0 0)",
    "card": "oklch(0.19 0.005 285)",
    "card-foreground": "oklch(0.985 0 0)",
    "popover": "oklch(0.19 0.005 285)",
    "popover-foreground": "oklch(0.985 0 0)",
    "secondary": "oklch(0.26 0.006 286)",
    "secondary-foreground": "oklch(0.985 0 0)",
    "muted": "oklch(0.26 0.006 286)",
    "muted-foreground": "oklch(0.705 0.015 286)",
    "destructive": "oklch(0.56 0.21 25)",
    "destructive-foreground": "oklch(0.985 0 0)",
    "success": "oklch(0.68 0.16 150)",
    "success-foreground": "oklch(0.16 0.02 150)",
    "warning": "oklch(0.78 0.15 75)",
    "warning-foreground": "oklch(0.18 0.02 75)",
    "border": "oklch(1 0 0 / 12%)",
    # Opaque, unlike --border: a translucent boundary's contrast depends on whatever
    # is behind it, so it cannot be guaranteed — and at 16% it measured far below 3:1.
    "input": "oklch(0.56 0.006 286)",
    "sidebar": "oklch(0.17 0.004 285)",
    "sidebar-foreground": "oklch(0.985 0 0)",
    "sidebar-border": "oklch(1 0 0 / 10%)",
}


def resolve_preset(preset: str | Preset | None = None) -> Preset:
    """A preset by name (or as given), defaulting to the configured ``UI_THEME``.

    An unknown name falls back to the configured one rather than raising: the name can
    come from a cookie, and a preset a project has since removed must not break a page.
    """
    if isinstance(preset, Preset):
        return preset
    presets = get_presets()
    if preset in presets:
        return presets[preset]
    return presets.get(vantage_settings.UI_THEME, presets[DEFAULT_PRESET])


def build_tokens(
    brand: str | None = None, *, dark: bool = False, preset: str | Preset | None = None
) -> dict[str, str]:
    """Every colour token for one theme, keyed without the leading ``--``.

    Precedence for the brand tokens: a ``brand`` argument, then an explicit
    ``BRAND_COLOR``, then the preset's own primary, then the default ``BRAND_COLOR``.
    """
    chosen = resolve_preset(preset)
    neutrals = {
        **(DARK_NEUTRALS if dark else LIGHT_NEUTRALS),
        **(chosen.dark if dark else chosen.light),
    }

    if brand is not None or vantage_settings.is_explicit("BRAND_COLOR"):
        return {**neutrals, **derive_palette(brand or vantage_settings.BRAND_COLOR, dark=dark)}

    derived = derive_palette(chosen.primary or vantage_settings.BRAND_COLOR, dark=dark)
    return {**neutrals, **derived, **(chosen.dark_brand if dark else chosen.light_brand)}


def _block(selector: str, tokens: dict[str, str], *, radius: str | None = None) -> str:
    lines = [f"{selector} {{"]
    if radius is not None:
        lines.append(f"  --radius: {radius};")
    lines.extend(f"  --{name}: {value};" for name, value in tokens.items())
    lines.append("}")
    return "\n".join(lines)


def render_theme_css(
    brand: str | None = None,
    radius: str | None = None,
    preset: str | Preset | None = None,
) -> str:
    """The complete themed token stylesheet, ready to inline into ``<head>``.

    Inlined rather than served as a file because the tokens depend on project settings:
    a static asset would have to be regenerated on every settings change, and would be
    wrong for any project running more than one brand.

    Only the active preset is emitted, so choosing a preset costs a page nothing it did
    not already carry. The live preview on the account page asks for another preset's
    stylesheet through :class:`vantage.views.UIThemeCssView`, which calls this too.
    """
    chosen = resolve_preset(preset)
    light = build_tokens(brand, dark=False, preset=chosen)
    dark = build_tokens(brand, dark=True, preset=chosen)

    if radius is None:
        explicit = vantage_settings.is_explicit("BRAND_RADIUS")
        radius = vantage_settings.BRAND_RADIUS if explicit or not chosen.radius else chosen.radius

    parts = [
        # Light on bare :root — a page with no theme attribute still renders.
        _block(":root", light, radius=radius),
        # System preference, but never overriding an explicit light choice.
        "@media (prefers-color-scheme: dark) {",
        _block('  :root:not([data-theme="light"])', dark),
        "}",
        # An explicit dark choice wins in both directions.
        _block(':root[data-theme="dark"]', dark),
    ]
    layer = chosen.layer()
    if layer:
        # Not mode-dependent, and doubled so it beats the static stylesheet's own
        # ``:root`` defaults (shadows, font) despite being emitted before it.
        parts.append(_block(":root:root", layer))
    return "\n".join(parts)


def swatch_css(presets: dict[str, Preset] | None = None) -> str:
    """Per-preset colour variables for the picker's cards, in both modes.

    Scoped by attribute rather than inlined as ``style=`` because the panel's CSP
    forbids inline style attributes. Small: four colours per preset per mode.
    """
    presets = presets if presets is not None else get_presets()

    def rules(selector_for, dark: bool) -> list[str]:
        out = []
        for name, preset in presets.items():
            tokens = build_tokens(dark=dark, preset=preset)
            body = " ".join(
                f"--sw-{label}: {tokens[token]};"
                for label, token in (
                    ("bg", "background"),
                    ("card", "card"),
                    ("fg", "muted-foreground"),
                    ("border", "border"),
                    ("primary", "primary"),
                    ("on", "primary-foreground"),
                )
            )
            out.append(f"{selector_for(name)} {{ {body} }}")
        return out

    card = '.v-uitheme-card[data-ui-theme-choice="{}"]'
    return "\n".join(
        [
            *rules(lambda n: card.format(n), dark=False),
            "@media (prefers-color-scheme: dark) {",
            *rules(lambda n: f':root:not([data-theme="light"]) {card.format(n)}', dark=True),
            "}",
            *rules(lambda n: f':root[data-theme="dark"] {card.format(n)}', dark=True),
        ]
    )


#: Stamps the saved theme onto <html> before first paint. Without this the page
#: renders light, then flips — the "theme flash" every dark-mode implementation hits.
#: It has to be inline and blocking in <head>; a deferred script is too late.
NO_FLASH_SCRIPT = """
(function () {
  try {
    var saved = localStorage.getItem('vantage-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {}
})();
""".strip()


def no_flash_script() -> str:
    """The inline script for ``<head>``, or nothing when users may not choose a theme.

    With ``THEME_TOGGLE`` off the server ignores the theme cookie, but this script would
    still apply whatever a user saved in localStorage back when the toggle was on, and
    the documented promise — everyone gets ``THEME`` — would quietly not hold for them.
    """
    return NO_FLASH_SCRIPT if vantage_settings.THEME_TOGGLE else ""
