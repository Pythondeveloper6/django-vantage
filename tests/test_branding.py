"""Brand identity, asserted on the pages a browser receives.

The resolution of each brand setting is covered in test_conf.py and test_siteconfig.py;
this file is about the last step, the one that was missing for ``LOGO_DARK``: a value
that resolves correctly and is then rendered nowhere looks configured and does nothing.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

LOGO = "https://cdn.example.com/acme-light.png"
LOGO_DARK = "https://cdn.example.com/acme-dark.png"


@pytest.fixture(autouse=True)
def _urls(settings):
    # The urlconf that mounts both the panel and the sign-in views.
    settings.ROOT_URLCONF = "tests.test_auth_pages"


@pytest.fixture
def signed_in(client, db):
    client.force_login(User.objects.create_superuser("brand", "brand@example.com", "x"))
    return client


def _dashboard(client) -> str:
    return client.get(reverse("vantage:dashboard")).content.decode()


def _sidebar_brand(html: str) -> str:
    return re.search(r'<a class="v-brand".*?</a>', html, re.DOTALL).group(0)


def _imgs(fragment: str) -> list[str]:
    return re.findall(r"<img[^>]*>", fragment)


class TestLogos:
    def test_the_logo_replaces_the_title_in_the_sidebar(self, signed_in):
        with override_settings(VANTAGE={"LOGO": LOGO, "SITE_TITLE": "Acme"}):
            brand = _sidebar_brand(_dashboard(signed_in))
        (img,) = _imgs(brand)
        assert f'src="{LOGO}"' in img
        assert 'alt="Acme"' in img

    def test_a_distinct_dark_logo_is_rendered_and_marked_for_the_swap(self, signed_in):
        with override_settings(VANTAGE={"LOGO": LOGO, "LOGO_DARK": LOGO_DARK}):
            brand = _sidebar_brand(_dashboard(signed_in))
        light, dark = _imgs(brand)
        assert f'src="{LOGO}"' in light and "data-logo-light" in light
        assert f'src="{LOGO_DARK}"' in dark and "data-logo-dark" in dark

    def test_no_dark_logo_means_one_image_and_no_swap(self, signed_in):
        """LOGO_DARK falls back to LOGO; rendering it twice would be two identical
        images, one of them hidden, for nothing."""
        with override_settings(VANTAGE={"LOGO": LOGO}):
            brand = _sidebar_brand(_dashboard(signed_in))
        (img,) = _imgs(brand)
        assert "data-logo-light" not in img

    def test_the_stylesheet_swaps_on_both_dark_conditions(self):
        """Pinned dark and device-dark-while-unpinned, mirroring the token blocks."""
        from pathlib import Path

        css = (
            Path(__file__).resolve().parent.parent / "src/vantage/static/vantage/vantage.css"
        ).read_text()
        assert ':root[data-theme="dark"] [data-logo-dark]' in css
        assert re.search(
            r"prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\) "
            r"\[data-logo-light\]",
            css,
        )

    @pytest.mark.parametrize("url", ["/accounts/login/"])
    def test_the_sign_in_page_carries_both_logos(self, client, db, url):
        with override_settings(VANTAGE={"LOGO": LOGO, "LOGO_DARK": LOGO_DARK}):
            body = client.get(url).content.decode()
        brand = re.search(r'<div class="v-auth-brand">.*?</div>', body, re.DOTALL).group(0)
        assert len(_imgs(brand)) == 2
        assert "data-logo-dark" in brand

    def test_the_collapsed_sidebar_still_has_a_mark_beside_a_logo(self, signed_in):
        """Collapsed, the logo is hidden for want of room; without the tile the brand
        strip would be blank."""
        with override_settings(VANTAGE={"LOGO": LOGO, "SITE_TITLE": "Acme Corporation"}):
            brand = _sidebar_brand(_dashboard(signed_in))
        assert re.search(r"<span[^>]*data-brand-collapsed[^>]*>AC</span>", brand)

    def test_the_brand_link_is_named_however_the_sidebar_is_drawn(self, signed_in):
        with override_settings(VANTAGE={"SITE_TITLE": "Acme"}):
            brand = _sidebar_brand(_dashboard(signed_in))
        assert 'aria-label="Acme"' in brand


class TestHeadAssets:
    def test_the_favicon_is_linked_on_panel_and_sign_in_pages(self, signed_in):
        from django.test import Client

        icon = "https://cdn.example.com/favicon.png"
        with override_settings(VANTAGE={"FAVICON": icon}):
            # A fresh client: the signed-in one would be redirected off the login page.
            sign_in = Client().get("/accounts/login/").content.decode()
            for body in (_dashboard(signed_in), sign_in):
                assert f'<link rel="icon" href="{icon}">' in body

    def test_no_favicon_link_without_a_favicon(self, signed_in):
        assert 'rel="icon"' not in _dashboard(signed_in)

    def test_custom_css_loads_after_the_panels_own(self, signed_in):
        with override_settings(VANTAGE={"CUSTOM_CSS": "/static/acme/brand.css"}):
            body = _dashboard(signed_in)
        assert body.index("vantage/vantage.css") < body.index("/static/acme/brand.css")

    def test_the_radius_reaches_the_page(self, signed_in):
        with override_settings(VANTAGE={"BRAND_RADIUS": "12px"}):
            assert "--radius: 12px" in _dashboard(signed_in)


class TestThemeToggleOff:
    def test_the_saved_choice_is_not_applied_when_users_may_not_choose(self, signed_in):
        """The server ignores the theme cookie when THEME_TOGGLE is off; the inline
        script must not resurrect a choice from localStorage either."""
        with override_settings(VANTAGE={"THEME_TOGGLE": False, "THEME": "dark"}):
            body = _dashboard(signed_in)
        assert "localStorage" not in body.split("</head>", 1)[0]
        assert re.search(r'<html [^>]*data-theme="dark"', body)

    def test_the_saved_choice_is_applied_when_they_may(self, signed_in):
        assert "localStorage.getItem('vantage-theme')" in _dashboard(signed_in)
