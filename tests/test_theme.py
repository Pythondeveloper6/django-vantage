"""Theme tokens, and the cascade rules that make a toggle work in both directions."""

import re

import pytest
from django.test import override_settings

from vantage.color import WCAG_AA, contrast_ratio
from vantage.theme import DARK_NEUTRALS, LIGHT_NEUTRALS, build_tokens, render_theme_css

OKLCH_CALL = re.compile(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)")


def _oklch(value: str):
    from vantage.color import OKLCH

    match = OKLCH_CALL.match(value)
    assert match, value
    return OKLCH(float(match.group(1)), float(match.group(2)), float(match.group(3)))


class TestBuildTokens:
    def test_light_and_dark_expose_the_same_token_names(self):
        """A component reads one name; both themes must answer to it."""
        assert set(build_tokens(dark=False)) == set(build_tokens(dark=True))

    def test_neutrals_do_not_move_with_the_brand(self):
        indigo = build_tokens("#6366f1")
        red = build_tokens("#ef4444")
        for name in LIGHT_NEUTRALS:
            assert indigo[name] == red[name], name

    def test_brand_tokens_do_move_with_the_brand(self):
        assert build_tokens("#6366f1")["primary"] != build_tokens("#ef4444")["primary"]

    def test_uses_the_configured_brand_by_default(self):
        with override_settings(VANTAGE={"BRAND_COLOR": "#ef4444"}):
            assert build_tokens() == build_tokens("#ef4444")

    def test_every_role_a_component_needs_is_present(self):
        tokens = build_tokens()
        for name in (
            "background",
            "foreground",
            "card",
            "muted",
            "muted-foreground",
            "border",
            "input",
            "ring",
            "primary",
            "primary-foreground",
            "destructive",
            "sidebar",
        ):
            assert name in tokens, name

    def test_dark_background_is_dark_and_light_background_is_light(self):
        assert _oklch(build_tokens(dark=True)["background"]).l < 0.3
        assert _oklch(build_tokens(dark=False)["background"]).l > 0.9


class TestNeutralContrast:
    """Body text and secondary text must be readable on their own surfaces."""

    @pytest.mark.parametrize("dark", [False, True])
    @pytest.mark.parametrize(
        ("fg", "bg"),
        [
            ("foreground", "background"),
            ("card-foreground", "card"),
            ("muted-foreground", "muted"),
            ("secondary-foreground", "secondary"),
            ("sidebar-foreground", "sidebar"),
            ("destructive-foreground", "destructive"),
            ("success-foreground", "success"),
            ("warning-foreground", "warning"),
            ("popover-foreground", "popover"),
            ("primary-foreground", "primary"),
        ],
    )
    def test_pair_clears_aa(self, dark, fg, bg):
        tokens = build_tokens(dark=dark)
        assert contrast_ratio(_oklch(tokens[fg]), _oklch(tokens[bg])) >= WCAG_AA


class TestRenderThemeCss:
    def test_light_is_defined_on_bare_root(self):
        """A page with no theme attribute must still render correctly."""
        css = render_theme_css()
        root = css.split("@media")[0]
        assert root.startswith(":root {")
        assert "--background:" in root

    def test_system_dark_does_not_override_an_explicit_light_choice(self):
        css = render_theme_css()
        assert "@media (prefers-color-scheme: dark)" in css
        assert ':root:not([data-theme="light"])' in css

    def test_explicit_dark_wins_in_both_directions(self):
        assert ':root[data-theme="dark"] {' in render_theme_css()

    def test_no_colour_is_defined_only_inside_the_media_query(self):
        """The bug that makes a toggle work one way and not the other."""
        css = render_theme_css()
        base = css.split("@media")[0]
        explicit = css.split(':root[data-theme="dark"] {')[1]
        for name in DARK_NEUTRALS:
            assert f"--{name}:" in base, name
            assert f"--{name}:" in explicit, name

    def test_radius_is_emitted(self):
        assert "--radius: 0.5rem;" in render_theme_css()

    @override_settings(VANTAGE={"BRAND_RADIUS": "1rem"})
    def test_radius_follows_the_setting(self):
        assert "--radius: 1rem;" in render_theme_css()

    def test_an_invalid_brand_colour_fails_loudly(self):
        with pytest.raises(ValueError, match="hex colour"):
            render_theme_css("chartreuse")
