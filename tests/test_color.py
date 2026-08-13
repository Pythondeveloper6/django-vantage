"""Contrast is asserted numerically, because "looks fine to me" is not a guarantee.

The site owner picks the brand colour, not us. Whatever they pick must produce a
readable panel, so the sweeps below cover the whole hue circle rather than a few
colours that happen to work.
"""

import pytest

from vantage.color import (
    OKLCH,
    WCAG_AA,
    accessible_pair,
    clamp_to_gamut,
    contrast_ratio,
    derive_palette,
    hex_to_oklch,
    oklch_to_hex,
    parse_hex,
)

#: A spread of real brand colours, deliberately including the awkward ones.
BRANDS = [
    "#6366f1",  # indigo, the default
    "#0ea5e9",  # sky
    "#ef4444",  # red
    "#22c55e",  # green
    "#eab308",  # yellow — the classic white-text failure
    "#f97316",  # orange
    "#a855f7",  # purple
    "#ec4899",  # pink
    "#14b8a6",  # teal
    "#000000",  # black
    "#ffffff",  # white
    "#808080",  # mid grey, zero chroma
]


class TestParsing:
    def test_six_digit_hex(self):
        assert parse_hex("#ffffff") == (1.0, 1.0, 1.0)

    def test_three_digit_hex(self):
        assert parse_hex("#fff") == parse_hex("#ffffff")

    def test_hash_is_optional(self):
        assert parse_hex("0ea5e9") == parse_hex("#0ea5e9")

    def test_case_insensitive(self):
        assert parse_hex("#0EA5E9") == parse_hex("#0ea5e9")

    @pytest.mark.parametrize("bad", ["", "#12", "#12345", "nope", "#gggggg", "rgb(1,2,3)"])
    def test_rejects_junk(self, bad):
        with pytest.raises(ValueError, match="hex colour"):
            parse_hex(bad)


class TestConversionRoundTrip:
    @pytest.mark.parametrize("value", BRANDS)
    def test_hex_survives_a_round_trip(self, value):
        assert oklch_to_hex(hex_to_oklch(value)) == value.lower()

    def test_white_is_fully_light(self):
        assert hex_to_oklch("#ffffff").l == pytest.approx(1.0, abs=1e-3)

    def test_black_is_fully_dark(self):
        assert hex_to_oklch("#000000").l == pytest.approx(0.0, abs=1e-3)

    def test_grey_has_no_chroma(self):
        assert hex_to_oklch("#808080").c == pytest.approx(0.0, abs=1e-3)

    def test_equal_lightness_across_hues_is_perceptually_equal(self):
        """The property that makes OKLCH worth using instead of HSL."""
        lightnesses = [
            hex_to_oklch(oklch_to_hex(OKLCH(0.6, 0.1, hue))).l for hue in range(0, 360, 30)
        ]
        assert max(lightnesses) - min(lightnesses) < 0.02


class TestGamutClamping:
    def test_an_impossible_chroma_is_reduced(self):
        absurd = OKLCH(0.6, 0.5, 250)  # far outside sRGB
        clamped = clamp_to_gamut(absurd)
        assert clamped.c < absurd.c
        assert clamped.l == absurd.l  # lightness is preserved
        assert clamped.h == absurd.h  # so is hue

    def test_an_in_gamut_colour_is_untouched(self):
        colour = hex_to_oklch("#0ea5e9")
        assert clamp_to_gamut(colour) == colour

    def test_clamped_colours_render_without_error(self):
        for hue in range(0, 360, 15):
            assert oklch_to_hex(OKLCH(0.6, 0.4, hue)).startswith("#")


class TestContrastRatio:
    def test_black_on_white_is_maximal(self):
        ratio = contrast_ratio(hex_to_oklch("#000"), hex_to_oklch("#fff"))
        assert ratio == pytest.approx(21, abs=0.1)

    def test_a_colour_against_itself_is_one(self):
        colour = hex_to_oklch("#0ea5e9")
        assert contrast_ratio(colour, colour) == pytest.approx(1.0, abs=1e-6)

    def test_symmetric(self):
        a, b = hex_to_oklch("#0ea5e9"), hex_to_oklch("#fff")
        assert contrast_ratio(a, b) == pytest.approx(contrast_ratio(b, a))


class TestAccessiblePair:
    @pytest.mark.parametrize("hue", range(0, 360, 10))
    def test_every_hue_clears_aa_in_a_light_theme(self, hue):
        primary, foreground = accessible_pair(OKLCH(0.6, 0.18, hue), 0.55)
        assert contrast_ratio(primary, foreground) >= WCAG_AA

    @pytest.mark.parametrize("hue", range(0, 360, 10))
    def test_every_hue_clears_aa_in_a_dark_theme(self, hue):
        primary, foreground = accessible_pair(OKLCH(0.6, 0.18, hue), 0.70)
        assert contrast_ratio(primary, foreground) >= WCAG_AA

    def test_lightness_is_kept_when_it_already_works(self):
        primary, _ = accessible_pair(hex_to_oklch("#6366f1"), 0.55)
        assert primary.l == pytest.approx(0.55, abs=1e-6)

    def test_lightness_moves_only_as_far_as_it_must(self):
        """Saturated yellow cannot hold white text at 0.55; it should shift, not leap."""
        primary, foreground = accessible_pair(hex_to_oklch("#eab308"), 0.55)
        assert contrast_ratio(primary, foreground) >= WCAG_AA
        assert abs(primary.l - 0.55) <= 0.30

    def test_a_greyscale_seed_still_produces_a_readable_pair(self):
        primary, foreground = accessible_pair(OKLCH(0.5, 0.0, 0.0), 0.55)
        assert contrast_ratio(primary, foreground) >= WCAG_AA


class TestDerivePalette:
    @pytest.mark.parametrize("brand", BRANDS)
    @pytest.mark.parametrize("dark", [False, True])
    def test_primary_and_foreground_clear_aa(self, brand, dark):
        """The guarantee the whole module exists to provide."""
        palette = derive_palette(brand, dark=dark)
        primary = _parse_css_oklch(palette["primary"])
        foreground = _parse_css_oklch(palette["primary-foreground"])
        assert contrast_ratio(primary, foreground) >= WCAG_AA, brand

    @pytest.mark.parametrize("brand", BRANDS)
    @pytest.mark.parametrize("dark", [False, True])
    def test_accent_and_its_foreground_clear_aa(self, brand, dark):
        palette = derive_palette(brand, dark=dark)
        accent = _parse_css_oklch(palette["accent"])
        foreground = _parse_css_oklch(palette["accent-foreground"])
        assert contrast_ratio(accent, foreground) >= WCAG_AA, brand

    def test_every_expected_token_is_present(self):
        palette = derive_palette("#0ea5e9")
        assert set(palette) == {
            "primary",
            "primary-foreground",
            "primary-hover",
            "ring",
            "accent",
            "accent-foreground",
        }

    def test_values_are_css_oklch_strings(self):
        for value in derive_palette("#0ea5e9").values():
            assert value.startswith("oklch(") and value.endswith(")")

    @pytest.mark.parametrize("brand", BRANDS)
    def test_hover_is_distinguishable_from_primary(self, brand):
        palette = derive_palette(brand)
        primary = _parse_css_oklch(palette["primary"])
        hover = _parse_css_oklch(palette["primary-hover"])
        assert abs(primary.l - hover.l) > 0.01, brand

    def test_hover_darkens_on_light_and_lightens_on_dark(self):
        light = derive_palette("#0ea5e9", dark=False)
        dark = derive_palette("#0ea5e9", dark=True)
        assert _parse_css_oklch(light["primary-hover"]).l < _parse_css_oklch(light["primary"]).l
        assert _parse_css_oklch(dark["primary-hover"]).l > _parse_css_oklch(dark["primary"]).l

    def test_dark_theme_primary_is_lighter_than_light_theme_primary(self):
        """A dark-theme button has to read as raised against a near-black page."""
        light = _parse_css_oklch(derive_palette("#0ea5e9", dark=False)["primary"])
        dark = _parse_css_oklch(derive_palette("#0ea5e9", dark=True)["primary"])
        assert dark.l > light.l

    def test_accent_stays_subtle(self):
        """The accent is a background tint, not a second primary."""
        light = _parse_css_oklch(derive_palette("#ef4444", dark=False)["accent"])
        assert light.l > 0.9
        dark = _parse_css_oklch(derive_palette("#ef4444", dark=True)["accent"])
        assert dark.l < 0.4

    def test_the_brand_hue_is_preserved(self):
        seed = hex_to_oklch("#ef4444")
        primary = _parse_css_oklch(derive_palette("#ef4444")["primary"])
        assert abs(primary.h - seed.h) < 1.0

    def test_rejects_an_invalid_brand_colour(self):
        with pytest.raises(ValueError, match="hex colour"):
            derive_palette("not-a-colour")


def _parse_css_oklch(value: str) -> OKLCH:
    body = value.removeprefix("oklch(").removesuffix(")")
    lightness, chroma, hue = (float(part) for part in body.split())
    return OKLCH(lightness, chroma, hue)
