"""UI theme presets: the original look stays byte-identical, every preset stays legible."""

import re
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse

from vantage.color import OKLCH, WCAG_AA, contrast_ratio
from vantage.conf import vantage_settings
from vantage.presets import BUILTIN_PRESETS, get_presets
from vantage.theme import build_tokens, render_theme_css, swatch_css

GOLDEN = Path(__file__).parent / "golden" / "vantage_theme.css"
STATIC = Path(__file__).parent.parent / "src" / "vantage" / "static" / "vantage"
OKLCH_CALL = re.compile(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)")
NAMES = list(BUILTIN_PRESETS)


def _c(value: str) -> OKLCH:
    match = OKLCH_CALL.match(value)
    assert match, value
    return OKLCH(*(float(g) for g in match.groups()))


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("staffer", "staff@example.com", "x")


@pytest.fixture
def client_in(client, staff):
    client.force_login(staff)
    return client


class TestVantageIsUnchanged:
    def test_css_is_byte_identical_to_the_pre_preset_output(self):
        """The default look is a promise; this is the golden copy it is held to."""
        assert render_theme_css("#6366f1", "0.5rem") == GOLDEN.read_text()

    def test_default_settings_render_the_same_thing(self):
        assert render_theme_css() == GOLDEN.read_text()

    def test_vantage_emits_nothing_beyond_the_tokens(self):
        assert ":root:root" not in render_theme_css()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("name", NAMES)
class TestEveryPresetIsLegible:
    def _tokens(self, name, dark):
        return build_tokens(dark=dark, preset=name)

    def test_same_token_names_as_vantage(self, name, dark):
        assert set(self._tokens(name, dark)) == set(build_tokens(dark=dark, preset="vantage"))

    @pytest.mark.parametrize(
        ("fg", "bg"),
        [
            ("foreground", "background"),
            ("card-foreground", "card"),
            ("popover-foreground", "popover"),
            ("muted-foreground", "background"),
            ("muted-foreground", "muted"),
            ("muted-foreground", "card"),
            ("secondary-foreground", "secondary"),
            ("primary-foreground", "primary"),
            ("primary-foreground", "primary-hover"),
            ("accent-foreground", "accent"),
            ("sidebar-foreground", "sidebar"),
            ("destructive-foreground", "destructive"),
            ("success-foreground", "success"),
            ("warning-foreground", "warning"),
        ],
    )
    def test_text_pairs_clear_aa(self, name, dark, fg, bg):
        t = self._tokens(name, dark)
        assert contrast_ratio(_c(t[fg]), _c(t[bg])) >= WCAG_AA, (name, dark, fg, bg)

    @pytest.mark.parametrize("surface", ["background", "card", "muted", "secondary", "popover"])
    def test_input_border_is_3_to_1_against_every_surface(self, name, dark, surface):
        t = self._tokens(name, dark)
        assert contrast_ratio(_c(t["input"]), _c(t[surface])) >= 3.0, (name, dark, surface)

    def test_surfaces_stay_on_their_side_of_the_midpoint(self, name, dark):
        lightness = _c(self._tokens(name, dark)["background"]).l
        assert (lightness < 0.3) if dark else (lightness > 0.9)


class TestShadcn:
    def test_primary_is_near_black_in_light_and_near_white_in_dark(self):
        assert _c(build_tokens(preset="shadcn")["primary"]).l < 0.3
        assert _c(build_tokens(dark=True, preset="shadcn")["primary"]).l > 0.85

    def test_neutrals_are_achromatic(self):
        for dark in (False, True):
            t = build_tokens(dark=dark, preset="shadcn")
            for name in ("background", "foreground", "muted", "border", "primary"):
                assert _c(t[name]).c == 0, name

    def test_radius_and_flat_shadows(self):
        css = render_theme_css(preset="shadcn")
        assert "--radius: 0.625rem;" in css
        assert "--shadow-sm: 0 1px 2px 0" in css
        assert "--shadow-lg: 0 4px 6px" in css


class TestPrecedence:
    def test_an_accent_preset_supplies_its_primary_by_default(self):
        assert build_tokens(preset="rose")["primary"] != build_tokens(preset="blue")["primary"]

    @override_settings(VANTAGE={"BRAND_COLOR": "#ef4444"})
    def test_an_explicit_brand_colour_beats_every_preset(self):
        expected = build_tokens("#ef4444", preset="vantage")["primary"]
        for name in NAMES:
            assert build_tokens(preset=name)["primary"] == expected, name

    @override_settings(VANTAGE={"BRAND_COLOR": "#6366f1"})
    def test_explicit_means_set_not_different(self):
        """Choosing the default colour on purpose is still choosing."""
        assert vantage_settings.is_explicit("BRAND_COLOR")
        assert build_tokens(preset="shadcn")["primary"] == build_tokens("#6366f1")["primary"]

    def test_unset_brand_is_not_explicit(self):
        assert not vantage_settings.is_explicit("BRAND_COLOR")

    @override_settings(VANTAGE={"BRAND_RADIUS": "0.25rem"})
    def test_an_explicit_radius_beats_the_preset(self):
        assert "--radius: 0.25rem;" in render_theme_css(preset="shadcn")

    def test_a_brand_argument_beats_the_preset(self):
        assert (
            build_tokens("#ef4444", preset="shadcn")["primary"]
            == (build_tokens("#ef4444")["primary"])
        )

    def test_neutrals_follow_the_preset_even_with_a_brand_colour(self):
        with override_settings(VANTAGE={"BRAND_COLOR": "#ef4444"}):
            assert build_tokens(preset="stone")["background"] == "oklch(1 0 0)"
            assert build_tokens(preset="stone")["muted"] != build_tokens(preset="vantage")["muted"]


class TestSettings:
    @override_settings(VANTAGE={"UI_THEME": "shadcn"})
    def test_ui_theme_selects_the_site_default(self):
        assert "--radius: 0.625rem;" in render_theme_css()

    @override_settings(VANTAGE={"UI_THEME": "shadcm"})
    def test_a_typo_is_rejected_with_a_suggestion(self):
        with pytest.raises(ImproperlyConfigured, match="Did you mean 'shadcn'"):
            vantage_settings.UI_THEME  # noqa: B018

    @override_settings(VANTAGE={"UI_THEMES": {"Bad Name": {}}})
    def test_a_custom_name_is_validated(self):
        with pytest.raises(ImproperlyConfigured, match="lowercase"):
            vantage_settings.UI_THEME  # noqa: B018

    @override_settings(VANTAGE={"UI_THEMES": {"mine": {"light": {"backgroud": "red"}}}})
    def test_a_custom_token_typo_is_suggested(self):
        with pytest.raises(ImproperlyConfigured, match="Did you mean 'background'"):
            vantage_settings.UI_THEME  # noqa: B018

    @override_settings(VANTAGE={"UI_THEMES": {"mine": {"raduis": "1rem"}}})
    def test_an_unknown_preset_key_is_suggested(self):
        with pytest.raises(ImproperlyConfigured, match="Did you mean 'radius'"):
            vantage_settings.UI_THEME  # noqa: B018

    @override_settings(VANTAGE={"UI_THEMES": {"mine": {"extras": {"x": "red; } body {"}}}})
    def test_a_value_cannot_smuggle_a_rule(self):
        with pytest.raises(ImproperlyConfigured, match="single CSS value"):
            vantage_settings.UI_THEME  # noqa: B018

    @override_settings(
        VANTAGE={
            "UI_THEME": "mine",
            "UI_THEMES": {
                "mine": {
                    "label": "Mine",
                    "light": {"background": "oklch(0.98 0.01 90)"},
                    "primary": "#0d9488",
                    "radius": "0",
                    "font": "Georgia, serif",
                    "border_width": "2px",
                    "shadows": {"lg": "none"},
                }
            },
        }
    )
    def test_a_custom_preset_inherits_from_vantage(self):
        presets = get_presets()
        assert presets["mine"].label == "Mine"
        light = build_tokens()
        assert light["background"] == "oklch(0.98 0.01 90)"
        # Unnamed tokens come from vantage, not from nowhere.
        assert light["foreground"] == build_tokens(preset="vantage")["foreground"]
        css = render_theme_css()
        assert "--radius: 0;" in css
        assert "--font-sans: Georgia, serif;" in css
        assert "--border-width: 2px;" in css
        assert "--shadow-lg: none;" in css

    @override_settings(VANTAGE={"UI_THEMES": {"shadcn": {"radius": "0"}}})
    def test_a_custom_name_replaces_a_builtin(self):
        assert get_presets()["shadcn"].radius == "0"


class TestPerUserChoice:
    def test_the_picker_posts_a_cookie(self, client_in):
        response = client_in.post(reverse("vantage:theme"), {"ui_theme": "rose"})
        assert response.status_code == 204
        assert client_in.cookies["vantage-ui-theme"].value == "rose"

    def test_choosing_a_look_does_not_touch_light_dark(self, client_in):
        client_in.cookies["vantage-theme"] = "dark"
        client_in.post(reverse("vantage:theme"), {"ui_theme": "rose"})
        assert client_in.cookies["vantage-theme"].value == "dark"

    def test_choosing_light_dark_does_not_touch_the_look(self, client_in):
        client_in.post(reverse("vantage:theme"), {"ui_theme": "rose"})
        client_in.post(reverse("vantage:theme"), {"theme": "dark"})
        assert client_in.cookies["vantage-ui-theme"].value == "rose"

    def test_an_unknown_preset_is_not_stored(self, client_in):
        client_in.post(reverse("vantage:theme"), {"ui_theme": "chartreuse"})
        assert "vantage-ui-theme" not in client_in.cookies

    @override_settings(VANTAGE={"UI_THEME_TOGGLE": False})
    def test_toggle_off_stores_nothing_and_ignores_a_stale_cookie(self, client_in):
        client_in.post(reverse("vantage:theme"), {"ui_theme": "rose"})
        assert "vantage-ui-theme" not in client_in.cookies
        client_in.cookies["vantage-ui-theme"] = "rose"
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-ui-theme="vantage"' in body

    def test_the_cookie_drives_the_first_paint(self, client_in):
        """Server-rendered, so there is no flash to prevent."""
        client_in.cookies["vantage-ui-theme"] = "shadcn"
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-ui-theme="shadcn"' in body
        assert "--radius: 0.625rem;" in body

    def test_a_removed_preset_cookie_falls_back(self, client_in):
        client_in.cookies["vantage-ui-theme"] = "gone"
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-ui-theme="vantage"' in body

    def test_only_the_active_preset_is_in_the_page(self, client_in):
        client_in.cookies["vantage-ui-theme"] = "shadcn"
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        rose = build_tokens(preset="rose")["primary"]
        assert rose not in body
        assert body.count("--background:") == 3  # light, system dark, explicit dark


class TestPreviewEndpoint:
    def test_serves_one_presets_css(self, client_in):
        response = client_in.get(reverse("vantage:ui_theme_css", args=["rose"]))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/css")
        assert response.content.decode() == render_theme_css(preset="rose")
        assert len(response.content) < 8000

    def test_unknown_preset_is_404(self, client_in):
        assert client_in.get(reverse("vantage:ui_theme_css", args=["nope"])).status_code == 404

    def test_anonymous_is_sent_to_login(self, client):
        response = client.get(reverse("vantage:ui_theme_css", args=["rose"]))
        assert response.status_code == 302


class TestAccountPicker:
    def test_every_preset_is_a_card(self, client_in):
        body = client_in.get(reverse("vantage:account")).content.decode()
        for name in NAMES:
            assert (
                f'<button class="v-uitheme-card" type="submit" name="ui_theme" value="{name}"'
                in body
            )
        assert body.count('class="v-uitheme-card"') == len(NAMES)

    def test_current_card_is_pressed(self, client_in):
        client_in.cookies["vantage-ui-theme"] = "violet"
        body = client_in.get(reverse("vantage:account")).content.decode()
        card = re.search(r'<button[^>]*data-ui-theme-choice="violet"[^>]*>', body).group(0)
        assert 'aria-pressed="true"' in card

    @override_settings(VANTAGE={"UI_THEME_TOGGLE": False})
    def test_hidden_when_toggle_is_off(self, client_in):
        body = client_in.get(reverse("vantage:account")).content.decode()
        assert "data-ui-theme-picker" not in body

    def test_swatches_cover_both_modes_without_inline_styles(self):
        css = swatch_css()
        assert 'data-theme="dark"' in css and "prefers-color-scheme: dark" in css
        assert "--sw-primary" in css

    def test_query_count_does_not_grow_with_presets(self, client_in, django_assert_max_num_queries):
        client_in.get(reverse("vantage:account"))
        with django_assert_max_num_queries(12):
            client_in.get(reverse("vantage:account"))


class TestSiteSettingsScreen:
    def test_a_stored_look_drives_the_page(self, client_in):
        from vantage.models import SiteConfiguration
        from vantage.siteconfig import invalidate

        SiteConfiguration.objects.create(ui_theme="shadcn")
        invalidate()
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert 'data-ui-theme="shadcn"' in body

    def test_an_invented_look_is_refused(self, client_in):
        from vantage.settingsview import SiteSettingsForm

        form = SiteSettingsForm({"ui_theme": "chartreuse"})
        assert not form.is_valid()
        assert "ui_theme" in form.errors

    def test_a_stored_brand_colour_also_counts_as_explicit(self, client_in):
        from vantage.models import SiteConfiguration
        from vantage.siteconfig import invalidate

        SiteConfiguration.objects.create(brand_color="#6366f1")
        invalidate()
        assert vantage_settings.is_explicit("BRAND_COLOR")


class TestNoHardcodedColours:
    """Every colour comes from a token, or a preset cannot restyle it."""

    FORBIDDEN = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")

    @pytest.mark.parametrize("path", sorted(STATIC.glob("*.css")), ids=lambda p: p.name)
    def test_stylesheet_has_no_literal_colours(self, path):
        text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        assert not self.FORBIDDEN.findall(text), path.name

    @pytest.mark.parametrize("path", sorted(STATIC.glob("*.css")), ids=lambda p: p.name)
    def test_only_black_scrims_and_shadows_use_oklch(self, path):
        """Pure-black overlays and shadows are theme-neutral; anything else is a token."""
        text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
        for call in re.findall(r"oklch\([^)]*\)", text):
            assert call.startswith("oklch(0 0 0 /"), (path.name, call)
