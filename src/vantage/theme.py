"""Turning settings into the CSS custom properties every component reads.

The token contract is shadcn/ui's, because it is well-designed and widely understood:
a small set of semantic role names (``--background``, ``--foreground``, ``--primary``,
``--muted``, ``--border``, ``--ring``) rather than a palette of colour names. No
component ever hard-codes a colour, so the whole appearance is controlled from here.

Neutrals are fixed; only the brand-derived tokens vary with ``BRAND_COLOR``. That
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

__all__ = ["build_tokens", "render_theme_css"]

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
    "input": "oklch(0.92 0.004 286)",
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
    "input": "oklch(1 0 0 / 16%)",
    "sidebar": "oklch(0.17 0.004 285)",
    "sidebar-foreground": "oklch(0.985 0 0)",
    "sidebar-border": "oklch(1 0 0 / 10%)",
}


def build_tokens(brand: str | None = None, *, dark: bool = False) -> dict[str, str]:
    """Every colour token for one theme, keyed without the leading ``--``."""
    brand = brand or vantage_settings.BRAND_COLOR
    neutrals = DARK_NEUTRALS if dark else LIGHT_NEUTRALS
    return {**neutrals, **derive_palette(brand, dark=dark)}


def _block(selector: str, tokens: dict[str, str], *, radius: str | None = None) -> str:
    lines = [f"{selector} {{"]
    if radius is not None:
        lines.append(f"  --radius: {radius};")
    lines.extend(f"  --{name}: {value};" for name, value in tokens.items())
    lines.append("}")
    return "\n".join(lines)


def render_theme_css(brand: str | None = None, radius: str | None = None) -> str:
    """The complete themed token stylesheet, ready to inline into ``<head>``.

    Inlined rather than served as a file because the tokens depend on project settings:
    a static asset would have to be regenerated on every settings change, and would be
    wrong for any project running more than one brand.
    """
    brand = brand or vantage_settings.BRAND_COLOR
    radius = radius or vantage_settings.BRAND_RADIUS
    light = build_tokens(brand, dark=False)
    dark = build_tokens(brand, dark=True)

    return "\n".join(
        [
            # Light on bare :root — a page with no theme attribute still renders.
            _block(":root", light, radius=radius),
            # System preference, but never overriding an explicit light choice.
            "@media (prefers-color-scheme: dark) {",
            _block('  :root:not([data-theme="light"])', dark),
            "}",
            # An explicit dark choice wins in both directions.
            _block(':root[data-theme="dark"]', dark),
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
