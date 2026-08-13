"""Deriving a complete, accessible theme from one brand colour.

A site owner supplies ``BRAND_COLOR = "#0ea5e9"`` and expects the panel to look like
theirs. Doing that well means more than tinting a button: every interactive surface
needs a matching hover, a focus ring, a subtle accent, and — the part that is usually
got wrong — a foreground colour that is actually readable on top of it, in both light
and dark themes, for *any* hue the owner might pick.

Working in OKLCH is what makes this tractable. It is perceptually uniform, so holding
lightness constant while changing hue produces colours that genuinely look equally
light, which sRGB emphatically does not. That lets us pick lightness *targets* per
role and per theme and trust them across the hue circle.

Correctness here is asserted numerically rather than eyeballed: the derived
primary/foreground pair must clear WCAG AA at 4.5:1 for every input hue, and the tests
sweep the circle to prove it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = [
    "OKLCH",
    "contrast_ratio",
    "derive_palette",
    "hex_to_oklch",
    "oklch_to_hex",
    "parse_hex",
]

HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass(frozen=True)
class OKLCH:
    """Lightness 0-1, chroma 0-~0.4, hue in degrees."""

    l: float  # noqa: E741 — the channel is called L; renaming it would be worse
    c: float
    h: float

    def with_l(self, lightness: float) -> OKLCH:
        return OKLCH(lightness, self.c, self.h)

    def with_c(self, chroma: float) -> OKLCH:
        return OKLCH(self.l, chroma, self.h)

    def css(self) -> str:
        return f"oklch({self.l:.4f} {self.c:.4f} {self.h:.2f})"


# ── sRGB transfer function ──────────────────────────────────────────────────


def _to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _from_linear(channel: float) -> float:
    if channel <= 0.0031308:
        return channel * 12.92
    return 1.055 * (channel ** (1 / 2.4)) - 0.055


# ── conversions ─────────────────────────────────────────────────────────────


def parse_hex(value: str) -> tuple[float, float, float]:
    """``"#0ea5e9"`` or ``"#0ae"`` to linear-light RGB in 0-1."""
    match = HEX.match(value.strip())
    if not match:
        raise ValueError(f"{value!r} is not a hex colour like '#0ea5e9'.")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    srgb = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(_to_linear(c) for c in srgb)  # type: ignore[return-value]


def _linear_to_oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    lms_l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    lms_m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    lms_s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b

    l_ = math.copysign(abs(lms_l) ** (1 / 3), lms_l)
    m_ = math.copysign(abs(lms_m) ** (1 / 3), lms_m)
    s_ = math.copysign(abs(lms_s) ** (1 / 3), lms_s)

    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _oklab_to_linear(lightness: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b

    lms_l, lms_m, lms_s = l_**3, m_**3, s_**3

    return (
        4.0767416621 * lms_l - 3.3077115913 * lms_m + 0.2309699292 * lms_s,
        -1.2684380046 * lms_l + 2.6097574011 * lms_m - 0.3413193965 * lms_s,
        -0.0041960863 * lms_l - 0.7034186147 * lms_m + 1.7076147010 * lms_s,
    )


def hex_to_oklch(value: str) -> OKLCH:
    lightness, a, b = _linear_to_oklab(*parse_hex(value))
    chroma = math.hypot(a, b)
    hue = math.degrees(math.atan2(b, a)) % 360
    return OKLCH(lightness, chroma, hue)


def _oklch_to_linear(color: OKLCH) -> tuple[float, float, float]:
    radians = math.radians(color.h)
    return _oklab_to_linear(color.l, color.c * math.cos(radians), color.c * math.sin(radians))


def _in_gamut(rgb: tuple[float, float, float], tolerance: float = 1e-4) -> bool:
    return all(-tolerance <= c <= 1 + tolerance for c in rgb)


def clamp_to_gamut(color: OKLCH) -> OKLCH:
    """Reduce chroma until the colour is representable in sRGB.

    Holding lightness and hue while giving up chroma is the right trade: a slightly
    duller colour still reads as the brand, whereas a shifted lightness breaks the
    contrast guarantees this module exists to provide.
    """
    if _in_gamut(_oklch_to_linear(color)):
        return color

    low, high = 0.0, color.c
    for _ in range(24):  # ~1e-7 precision, cheap and deterministic
        mid = (low + high) / 2
        if _in_gamut(_oklch_to_linear(color.with_c(mid))):
            low = mid
        else:
            high = mid
    return color.with_c(low)


def oklch_to_hex(color: OKLCH) -> str:
    rgb = _oklch_to_linear(clamp_to_gamut(color))
    channels = [max(0, min(255, round(_from_linear(max(0.0, min(1.0, c))) * 255))) for c in rgb]
    return "#" + "".join(f"{c:02x}" for c in channels)


# ── contrast ────────────────────────────────────────────────────────────────


def _relative_luminance(color: OKLCH) -> float:
    r, g, b = (max(0.0, min(1.0, c)) for c in _oklch_to_linear(clamp_to_gamut(color)))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: OKLCH, b: OKLCH) -> float:
    """WCAG 2.x contrast ratio between two colours, 1.0-21.0."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ── palette derivation ──────────────────────────────────────────────────────

WCAG_AA = 4.5

WHITE = OKLCH(1.0, 0.0, 0.0)
BLACK = OKLCH(0.0, 0.0, 0.0)

#: Where each theme wants the primary surface to sit on the lightness axis. Light
#: themes need a mid-dark primary to carry white text; dark themes need a lighter one
#: so the button reads as raised against a near-black background.
LIGHT_TARGET = 0.55
DARK_TARGET = 0.70


def _best_foreground(background: OKLCH) -> tuple[OKLCH, float]:
    """Whichever of near-white or near-black reads better on ``background``."""
    # Not pure white/black: a hint of the brand hue keeps the pairing from looking
    # like an unstyled default, and costs almost nothing in contrast.
    white = OKLCH(0.985, min(background.c * 0.06, 0.01), background.h)
    black = OKLCH(0.16, min(background.c * 0.20, 0.03), background.h)
    on_white = contrast_ratio(background, white)
    on_black = contrast_ratio(background, black)
    return (white, on_white) if on_white >= on_black else (black, on_black)


def accessible_pair(seed: OKLCH, target: float) -> tuple[OKLCH, OKLCH]:
    """A primary at roughly ``target`` lightness, plus a foreground that clears AA.

    Starts at the target and walks lightness away from the midpoint until the best
    available foreground reaches 4.5:1. Some hues — saturated yellows especially —
    simply cannot hold white text at a mid lightness, so the primary has to move.
    Moving it is the correct failure mode; shipping an unreadable button is not.
    """
    primary = clamp_to_gamut(seed.with_l(target))
    foreground, ratio = _best_foreground(primary)
    if ratio >= WCAG_AA:
        return primary, foreground

    # Try both directions and take whichever reaches AA with the smaller move, so the
    # result stays as close to the requested lightness as the constraint allows.
    best: tuple[float, OKLCH, OKLCH] | None = None
    for direction in (-1, 1):
        for step in range(1, 61):
            lightness = target + direction * step * 0.01
            if not 0.0 <= lightness <= 1.0:
                break
            candidate = clamp_to_gamut(seed.with_l(lightness))
            fg, candidate_ratio = _best_foreground(candidate)
            if candidate_ratio >= WCAG_AA:
                distance = step
                if best is None or distance < best[0]:
                    best = (distance, candidate, fg)
                break

    if best is not None:
        return best[1], best[2]

    # Unreachable for any real sRGB hue, but never return an unreadable pair.
    return primary, (WHITE if _relative_luminance(primary) < 0.18 else BLACK)


def derive_palette(brand: str, *, dark: bool = False) -> dict[str, str]:
    """Every brand-derived token, as CSS ``oklch()`` strings.

    Only the tokens that depend on the brand colour are produced here; the neutral
    greys are fixed in the stylesheet. Returning a dict of CSS strings rather than
    objects keeps the template layer free of colour maths.
    """
    seed = clamp_to_gamut(hex_to_oklch(brand))
    target = DARK_TARGET if dark else LIGHT_TARGET
    primary, foreground = accessible_pair(seed, target)

    # Hover moves away from the page background so the change is legible in both
    # themes: darker on light, lighter on dark.
    hover_shift = 0.05 if dark else -0.05
    hover = clamp_to_gamut(primary.with_l(max(0.0, min(1.0, primary.l + hover_shift))))

    # A heavily desaturated tint of the brand, for selected rows and subtle fills.
    accent_l = 0.26 if dark else 0.965
    accent = clamp_to_gamut(OKLCH(accent_l, min(seed.c * 0.22, 0.04), seed.h))
    accent_fg = OKLCH(0.985, 0.0, seed.h) if dark else OKLCH(0.21, min(seed.c * 0.3, 0.05), seed.h)

    return {
        "primary": primary.css(),
        "primary-foreground": foreground.css(),
        "primary-hover": hover.css(),
        "ring": primary.with_l(min(1.0, primary.l + (0.06 if dark else 0.0))).css(),
        "accent": accent.css(),
        "accent-foreground": accent_fg.css(),
    }
