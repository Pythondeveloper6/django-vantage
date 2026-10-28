# ruff: noqa: F811
"""The phone layout, in a real viewport.

Reuses ``tests/test_browser.py``'s fixtures (and its urlconf, through ``_mounted``), so
the same rules apply: database rows first, Chromium second, waits on conditions rather
than sleeps. Everything here runs at 390x844, an iPhone-sized touch screen.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from tests.test_browser import _mounted, authed_page, pw, sample_data, staff_cookie  # noqa: F401
from vantage.presets import BUILTIN_PRESETS

pytestmark = pytest.mark.browser

PHONE = {"width": 390, "height": 844}


@pytest.fixture
def phone(sample_data, staff_cookie, pw, live_server):
    """A signed-in touch device at phone width."""
    context = pw.new_context(viewport=PHONE, has_touch=True, is_mobile=True)
    context.add_cookies([{**staff_cookie, "url": live_server.url}])
    pg = context.new_page()
    pg.base = live_server.url
    yield pg
    context.close()


def goto(pg, name, **kwargs):
    pg.goto(pg.base + reverse(name, kwargs=kwargs or None))
    pg.wait_for_load_state("networkidle")


class TestNoSidewaysScroll:
    @pytest.mark.parametrize(
        "name",
        [
            "vantage:dashboard",
            "vantage:testapp-order-list",
            "vantage:testapp-author-list",
            "vantage:settings",
            "vantage:topbar",
        ],
    )
    def test_the_page_fits_the_screen(self, name, phone):
        goto(phone, name)
        assert phone.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]

    @pytest.mark.parametrize("route", ["detail", "change", "delete"])
    def test_a_record_page_fits_the_screen(self, route, phone, sample_data):
        goto(phone, f"vantage:testapp-author-{route}", pk=sample_data.author.pk)
        assert phone.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


class TestCards:
    def test_rows_are_stacked_cards(self, phone):
        goto(phone, "vantage:testapp-order-list")
        row = phone.locator("tbody tr").first
        assert row.evaluate("el => getComputedStyle(el).display") == "grid"
        assert phone.locator("tbody tr").count() >= 2
        # Stacked, not side by side.
        first, second = (phone.locator("tbody tr").nth(i).bounding_box() for i in (0, 1))
        assert second["y"] >= first["y"] + first["height"]

    def test_a_card_shows_labelled_values(self, phone):
        goto(phone, "vantage:testapp-order-list")
        label = phone.locator("tbody tr").first.locator("td[data-card]").nth(1)
        assert label.evaluate("el => getComputedStyle(el, '::before').content") != "none"

    def test_columns_beyond_the_card_are_hidden(self, phone):
        goto(phone, "vantage:testapp-order-list")
        hidden = (
            phone.locator("tbody tr")
            .first.locator("td:not([data-card])")
            .evaluate_all(
                "els => els.filter(e => !e.classList.contains('v-select-cell'))"
                ".map(e => getComputedStyle(e).display)"
            )
        )
        assert hidden and set(hidden) == {"none"}

    def test_the_selection_checkbox_is_a_touch_target(self, phone):
        goto(phone, "vantage:testapp-order-list")
        box = phone.locator("tbody td.v-select-cell").first.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44

    def test_the_header_row_still_sorts(self, phone):
        goto(phone, "vantage:testapp-order-list")
        sort = phone.locator("thead a.v-sort").first
        assert sort.is_visible()
        assert sort.bounding_box()["height"] >= 44

    def test_desktop_keeps_the_table(self, sample_data, staff_cookie, pw, live_server):
        context = pw.new_context(viewport={"width": 1280, "height": 800})
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")
        assert pg.locator("tbody tr").first.evaluate("el => getComputedStyle(el).display") == (
            "table-row"
        )
        assert pg.locator(".v-action-close").count() == 1
        assert not pg.locator(".v-action-close").is_visible()
        context.close()


class TestBulkSheet:
    def tick(self, phone, index=0):
        phone.locator("tbody [data-select-row]").nth(index).check()

    def test_it_opens_on_selection_and_sits_on_the_bottom_edge(self, phone):
        goto(phone, "vantage:testapp-order-list")
        bar = phone.locator("[data-action-bar]")
        assert not bar.is_visible()
        self.tick(phone)
        assert bar.is_visible()
        phone.wait_for_function(
            "() => { const r = document.querySelector('[data-action-bar]').getBoundingClientRect();"
            " return Math.abs(r.bottom - innerHeight) < 1; }"
        )
        assert bar.evaluate("el => getComputedStyle(el).position") == "fixed"

    def test_it_is_a_dialog_only_while_it_is_a_sheet(self, phone):
        goto(phone, "vantage:testapp-order-list")
        bar = phone.locator("[data-action-bar]")
        assert bar.get_attribute("role") == "dialog"
        assert bar.get_attribute("aria-label")
        phone.set_viewport_size({"width": 1280, "height": 800})
        phone.wait_for_function(
            "() => !document.querySelector('[data-action-bar]').hasAttribute('role')"
        )

    def test_the_buttons_are_touch_sized(self, phone):
        goto(phone, "vantage:testapp-order-list")
        self.tick(phone)
        for button in phone.locator("[data-action-bar] button:visible").all():
            assert button.bounding_box()["height"] >= 44

    def test_focus_stays_on_the_checkbox_when_it_opens(self, phone):
        goto(phone, "vantage:testapp-order-list")
        self.tick(phone)
        assert phone.evaluate("document.activeElement.matches('[data-select-row]')")

    def test_escape_closes_it_and_returns_focus_to_the_list(self, phone):
        goto(phone, "vantage:testapp-order-list")
        self.tick(phone, 1)
        phone.keyboard.press("Escape")
        assert not phone.locator("[data-action-bar]").is_visible()
        assert phone.locator("[data-select-row]:checked").count() == 0
        assert phone.evaluate("document.activeElement.matches('[data-select-row]')")

    def test_clear_selection_does_the_same(self, phone):
        goto(phone, "vantage:testapp-order-list")
        self.tick(phone)
        phone.locator("[data-action-close]").click()
        assert not phone.locator("[data-action-bar]").is_visible()
        assert phone.locator("[data-select-row]:checked").count() == 0

    def test_the_count_is_still_announced(self, phone):
        goto(phone, "vantage:testapp-order-list")
        self.tick(phone)
        count = phone.locator("[data-selection-count]")
        assert count.get_attribute("role") == "status"
        assert "1" in count.inner_text()

    def test_reduced_motion_removes_the_slide(self, sample_data, staff_cookie, pw, live_server):
        context = pw.new_context(viewport=PHONE, reduced_motion="reduce")
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")
        pg.locator("tbody [data-select-row]").first.check()
        assert (
            pg.locator("[data-action-bar]").evaluate("el => getComputedStyle(el).animationName")
            == "none"
        )
        context.close()


class TestDrawer:
    def test_it_opens_closes_on_escape_and_returns_focus(self, phone):
        goto(phone, "vantage:dashboard")
        toggle = phone.locator("#v-sidebar-toggle")
        sidebar = phone.locator("#v-sidebar")
        assert not sidebar.is_visible()
        toggle.click()
        assert sidebar.is_visible()
        assert toggle.get_attribute("aria-expanded") == "true"
        phone.keyboard.press("Escape")
        assert not sidebar.is_visible()
        assert toggle.get_attribute("aria-expanded") == "false"
        assert phone.evaluate("document.activeElement.id") == "v-sidebar-toggle"

    def test_a_tap_outside_closes_it(self, phone):
        goto(phone, "vantage:dashboard")
        phone.locator("#v-sidebar-toggle").click()
        phone.mouse.click(PHONE["width"] - 10, 400)
        assert not phone.locator("#v-sidebar").is_visible()

    def test_its_links_are_touch_sized(self, phone):
        goto(phone, "vantage:dashboard")
        phone.locator("#v-sidebar-toggle").click()
        assert phone.locator(".v-nav-item").first.bounding_box()["height"] >= 44


class TestFormsAndTouchTargets:
    def test_inputs_are_at_least_16px_so_ios_does_not_zoom(self, phone, sample_data):
        goto(phone, "vantage:testapp-author-change", pk=sample_data.author.pk)
        sizes = phone.locator(
            "main input:not([type=hidden]):not([type=checkbox]):not([type=radio]),"
            " main textarea, main select"
        ).evaluate_all("els => els.map(e => parseFloat(getComputedStyle(e).fontSize))")
        assert sizes and min(sizes) >= 16

    def test_form_controls_and_buttons_are_44px_tall(self, phone, sample_data):
        goto(phone, "vantage:testapp-author-change", pk=sample_data.author.pk)
        heights = phone.locator(
            "main input:not([type=hidden]):not([type=checkbox]), main textarea, main .v-btn"
        ).evaluate_all(
            "els => els.filter(e => e.offsetParent).map(e => e.getBoundingClientRect().height)"
        )
        assert heights and min(heights) >= 44

    def test_the_form_is_a_single_column(self, phone, sample_data):
        goto(phone, "vantage:testapp-author-change", pk=sample_data.author.pk)
        lefts = phone.locator(".v-form-field").evaluate_all(
            "els => els.map(e => Math.round(e.getBoundingClientRect().left))"
        )
        assert len(set(lefts)) == 1

    def test_the_header_search_is_still_a_touch_target(self, phone):
        goto(phone, "vantage:dashboard")
        box = phone.locator("#v-palette-trigger").bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44


class TestPresetsAndDarkMode:
    @pytest.mark.parametrize("scheme", ["light", "dark"])
    @pytest.mark.parametrize("preset", sorted(BUILTIN_PRESETS))
    def test_cards_render_under_every_preset(
        self, preset, scheme, settings, sample_data, staff_cookie, pw, live_server
    ):
        settings.VANTAGE = {**getattr(settings, "VANTAGE", {}), "UI_THEME": preset}
        context = pw.new_context(viewport=PHONE, color_scheme=scheme)
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")
        assert pg.evaluate("document.documentElement.getAttribute('data-ui-theme')") in (
            preset,
            None,
        )
        assert pg.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]
        card = pg.locator("tbody tr").first
        surface = card.evaluate("el => getComputedStyle(el).backgroundColor")
        page_bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
        assert surface != "rgba(0, 0, 0, 0)"
        # The card is a surface of its own, not the page showing through: its text
        # would otherwise be unreadable in whichever scheme it was not tuned for.
        assert card.evaluate("el => getComputedStyle(el).color") != surface
        assert page_bg
        context.close()
