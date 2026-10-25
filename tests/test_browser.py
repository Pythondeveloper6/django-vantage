"""Browser-driven tests for the behaviour Django's test client cannot see.

Every other file in ``tests/`` renders a response through Django's test client, which
parses HTML but never runs a line of JavaScript. That leaves a whole class of bug
invisible to the suite — and three of them have already shipped because of it:

* The bulk-action bar sat on every list page reading "0 records selected" — correct
  markup, a CSS specificity bug (fixed: see the comment on ``[hidden]`` in
  ``vantage.css``). Found by looking at a screenshot, not by any test.
* The top bar's settings screen linked a stylesheet and a script that did not exist —
  two 404s on every visit (fixed in "The top bar's stylesheet and drag script, which
  were referenced but never written"). Found by reading the browser console.
* Focus was not moved to the error summary after a failed save, so a screen-reader
  user was told nothing went wrong. Fixed in ``vantage.js``'s ``focusErrorSummary``,
  and nothing pinned it — see ``TestFocusAfterFailedSave`` below, which is the single
  most important thing this module covers.

This module is that missing layer: a real Chromium, driven by Playwright, against a
``pytest-django`` ``live_server``. It needs no demo project and no server a
contributor has to remember to start — ``live_server`` boots one per test session and
tears it down when the suite finishes, so the whole module is self-contained and
deterministic. Every wait is on a condition (an element, an attribute, a function
result), never a fixed sleep.

Everything here is marked ``@pytest.mark.browser`` (applied once, at module scope) and
the whole module skips cleanly — at collection time if the ``playwright`` package
is not installed, per-test if it is installed but ``playwright install chromium`` was
never run. A contributor who has not set up the ``browser`` dependency group still
gets a green, unmodified suite.

── the ORM-then-Playwright ordering rule ──────────────────────────────────────────

Playwright's sync API drives its browser through a dedicated event loop that lives on
the *calling* thread for as long as ``sync_playwright()``'s context is open. Django's
async-safety guard (``django.utils.asyncio.async_unsafe``) treats any running event
loop on the current thread as reason enough to refuse a database query, even though
nothing here is actually concurrent — so a plain ``Model.objects.create(...)`` raises
``SynchronousOnlyOperation`` if it runs *after* ``playwright.chromium.launch()`` has
already happened on this thread. Verified directly: the identical ORM call succeeds
when it runs first and fails when it runs second, with nothing else different.

The fixtures below are written to make that failure structurally impossible rather
than something every new test has to remember: every fixture that touches the
database (``sample_data``, ``staff_cookie``) is a dependency *of* ``pw``/``page``/
``authed_page``, never the other way around, so pytest's dependency graph — not
parameter order someone could reshuffle — guarantees the database work is finished
before Chromium ever starts. No test in this module performs an ORM query once a
``page`` fixture has been requested.

The same constraint is why ``pw`` (and everything built on it) is function-scoped
rather than session-scoped: ``live_server`` requires transactional tests, which flush
every table between tests, so each test needs its own fresh database rows — which
means each test needs Playwright's event loop to not exist yet when its data fixtures
run. A session-scoped browser would still be sitting on the thread for test two
onward. A fresh Chromium per test costs about a quarter of a second; correctness
is worth it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.urls import include, path, reverse

from vantage import site

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason=(
        "playwright is not installed; run `uv sync --group browser` and "
        "`uv run playwright install chromium` to enable these tests"
    ),
)
sync_playwright = playwright_sync.sync_playwright

pytestmark = pytest.mark.browser


# ── urlconf ──────────────────────────────────────────────────────────────────────
#
# The rest of the suite runs against ``tests.urls``, which mounts only ``site.urls``
# at ``admin/`` — there is no login route on it, on purpose (see
# ``tests/test_auth_pages.py``, which mounts its own urlconf for exactly the same
# reason). These tests need the login page too, so they get their own urlconf, module
# scoped like that one.

urlpatterns = [
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
]


@pytest.fixture(autouse=True)
def _mounted(settings):
    settings.ROOT_URLCONF = "tests.test_browser"


# ── data ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_data(db):
    """Everything every test in this module might need, created once per test.

    One fixture rather than several smaller ones so the ORM-before-Playwright
    ordering rule (see the module docstring) only has to be satisfied in one place.
    """
    from tests.testapp.models import Author, Category, Order, Post, Tag

    staff = User.objects.create_superuser(
        "browser-tests", "browser-tests@example.com", "correct-horse-battery-staple"
    )
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    category = Category.objects.create(name="Engineering", slug="engineering")
    tags = [Tag.objects.create(name=name) for name in ("python", "django", "testing")]
    post = Post.objects.create(
        title="Hello World",
        slug="hello-world",
        body="Body copy.",
        author=author,
        category=category,
    )
    # Five columns (number, status, customer, total, created_at) plus the select and
    # row-actions cells — enough for the roving-tabindex and column-pinning tests to
    # have something to page through and something to overflow.
    orders = [
        Order.objects.create(number=f"ORD-{i:04d}", customer=author, total=10 + i)
        for i in range(6)
    ]
    return SimpleNamespace(
        staff=staff, author=author, category=category, tags=tags, post=post, orders=orders
    )


def _session_cookie(user) -> dict:
    """A pre-authenticated session cookie for ``user``, built without a trip through
    the login form.

    Faster than driving the login UI in every test, and it exercises Django's own
    session machinery rather than reinventing it. Must run before any Playwright
    fixture — see the module docstring — which is why every fixture built on this one
    takes ``sample_data`` (not just ``sample_data.staff``) as its dependency: it keeps
    the one ordering rule visible at every call site rather than trusting each caller
    to remember it.
    """
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    from django.conf import settings as dj_settings

    return {"name": dj_settings.SESSION_COOKIE_NAME, "value": session.session_key}


@pytest.fixture
def staff_cookie(sample_data):
    return _session_cookie(sample_data.staff)


# ── playwright ───────────────────────────────────────────────────────────────────


@pytest.fixture
def pw():
    """A fresh Chromium, function-scoped — see the module docstring for why.

    Skips the test cleanly if Chromium was never downloaded (``playwright install``),
    rather than erroring the whole run.
    """
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(
                "Playwright's Chromium build is not installed "
                f"(`uv run playwright install chromium`): {exc}"
            )
            return
        yield browser
        browser.close()


@pytest.fixture
def page(pw):
    """An anonymous page — for the login screen, which nothing here should be signed
    into."""
    context = pw.new_context()
    pg = context.new_page()
    yield pg
    context.close()


@pytest.fixture
def authed_page(sample_data, staff_cookie, pw, live_server):
    """A page already signed in as ``sample_data.staff``."""
    context = pw.new_context()
    context.add_cookies([{**staff_cookie, "url": live_server.url}])
    pg = context.new_page()
    yield pg
    context.close()


# ── shared helpers ───────────────────────────────────────────────────────────────

#: Every element the CSS says should be invisible, that computed style disagrees
#: with. See vantage.css's comment on ``[hidden]`` for the bug this generalises: the
#: bulk-action bar carried the attribute correctly the whole time and a competing
#: ``display: flex`` still won.
_HIDDEN_BUT_VISIBLE_JS = r"""
() => Array.from(document.querySelectorAll('[hidden]'))
  .filter((el) => getComputedStyle(el).display !== 'none')
  .map((el) => {
    const parts = [el.tagName.toLowerCase()];
    if (el.id) parts.push('#' + el.id);
    if (el.className) parts.push('.' + String(el.className).trim().split(/\s+/).join('.'));
    return parts.join('');
  })
"""


def _assert_page_is_healthy(context, url: str, *, label: str) -> None:
    """No console errors, no failed (incl. 404) requests, nothing ``[hidden]`` that
    is still visible. Opens and closes its own page so each call's listeners start
    clean."""
    console_errors: list[str] = []
    failed_requests: list[str] = []

    pg = context.new_page()
    pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda exc: console_errors.append(f"uncaught exception: {exc}"))
    pg.on(
        "response",
        lambda r: failed_requests.append(f"{r.status} {r.url}") if r.status >= 400 else None,
    )

    pg.goto(url)
    pg.wait_for_load_state("networkidle")

    assert console_errors == [], f"{label} ({url}): console errors: {console_errors}"
    assert failed_requests == [], f"{label} ({url}): failed requests: {failed_requests}"

    visible_hidden = pg.evaluate(_HIDDEN_BUT_VISIBLE_JS)
    assert visible_hidden == [], (
        f"{label} ({url}): element(s) carrying [hidden] that computed style still "
        f"shows: {visible_hidden}"
    )
    pg.close()


# ── the single most important test in this file ─────────────────────────────────


class TestFocusAfterFailedSave:
    """Focus moves to the error summary after a failed save.

    The server already does its half right: the summary is ``role="alert"``, has
    real text, and ``tabindex="-1"`` so it is focusable. What ``vantage.js``'s
    ``focusErrorSummary`` adds is the missing last step — without it, focus is on
    ``<body>`` after the reload, a screen-reader user is told nothing went wrong, and
    a keyboard user starts tabbing from the top of the document. That fix has no test
    pinning it anywhere else in this suite; this is it.
    """

    def test_focus_lands_on_the_error_summary(self, sample_data, staff_cookie, pw, live_server):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()

        edit_url = live_server.url + reverse(
            "vantage:testapp-author-change", kwargs={"pk": sample_data.author.pk}
        )
        pg.goto(edit_url)
        pg.wait_for_load_state("networkidle")

        pg.fill("#id_name", "")
        # The name field is a required HTML5 input, so a native "please fill this
        # out" validation bubble would otherwise stop the browser from ever POSTing
        # and this test would be checking nothing. Disabling constraint validation on
        # the form is the standard way to exercise the *server's* validation path
        # (and vantage.js's response to it) instead of the browser's.
        pg.evaluate("document.querySelector('form.v-form').noValidate = true")

        with pg.expect_response(lambda r: r.url == edit_url and r.request.method == "POST"):
            pg.click("form.v-form button[type=submit]")
        pg.wait_for_load_state("networkidle")

        active = pg.evaluate(
            "() => ({id: document.activeElement.id, tag: document.activeElement.tagName})"
        )
        assert active == {"id": "v-form-summary", "tag": "DIV"}, active
        # And the summary is the thing it claims to be, not a coincidentally-named div.
        assert pg.evaluate(
            "() => document.getElementById('v-form-summary').getAttribute('role')"
        ) == "alert"

        context.close()


# ── console errors, 404s, and visible-but-[hidden] elements everywhere ──────────


class TestEveryMainPageIsHealthy:
    """No console errors, no failed requests (a missing static file fails the
    build), and nothing marked ``hidden`` is actually visible — on every page a
    staff user routinely opens.

    This one check would have caught two of the three bugs in the module docstring:
    the 404ing top-bar stylesheet and script, and the always-visible action bar.
    """

    @pytest.mark.parametrize(
        "name",
        ["dashboard", "list", "detail", "form", "settings", "topbar", "delete"],
    )
    def test_authenticated_page(self, name, sample_data, staff_cookie, pw, live_server):
        pk = sample_data.author.pk
        paths = {
            "dashboard": reverse("vantage:dashboard"),
            "list": reverse("vantage:testapp-author-list"),
            "detail": reverse("vantage:testapp-author-detail", kwargs={"pk": pk}),
            "form": reverse("vantage:testapp-author-change", kwargs={"pk": pk}),
            "settings": reverse("vantage:settings"),
            "topbar": reverse("vantage:topbar"),
            "delete": reverse("vantage:testapp-author-delete", kwargs={"pk": pk}),
        }
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        _assert_page_is_healthy(context, live_server.url + paths[name], label=name)
        context.close()

    def test_login_page(self, pw, live_server):
        context = pw.new_context()
        _assert_page_is_healthy(context, live_server.url + reverse("login"), label="login")
        context.close()


# ── the relation combobox ─────────────────────────────────────────────────────────


class TestRelationCombobox:
    """Type to filter, arrow to highlight, Enter to commit, a chip appears, and the
    real ``<select>`` — never replaced, only driven — ends up holding the value.

    Exercised on ``Post.tags``: a many-to-many always gets the searchable combobox
    (see ``vantage.widgets.infer_widget``'s comment on why a row count is the wrong
    question to ask about a multiple select), so this does not depend on the fixture
    app happening to have enough rows to cross the single-valued relation's search
    threshold.
    """

    def test_type_filter_arrow_select_chip_and_select_value(
        self, sample_data, staff_cookie, pw, live_server
    ):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()

        pg.goto(
            live_server.url
            + reverse("vantage:testapp-post-change", kwargs={"pk": sample_data.post.pk})
        )
        pg.wait_for_load_state("networkidle")

        root = pg.locator('[data-vantage-combobox][data-model="testapp.Tag"]')
        combo_input = root.locator("input.v-combo-input")
        combo_input.click()
        combo_input.type("pyth")

        # Debounced, so wait for the *filtered* response rather than the one the
        # click already fired for an empty term.
        pg.wait_for_function(
            """() => {
                const opts = document.querySelectorAll(
                    '[data-vantage-combobox][data-model="testapp.Tag"] .v-combo-option'
                );
                return opts.length === 1 && opts[0].textContent === 'python';
            }"""
        )

        combo_input.press("ArrowDown")
        assert combo_input.get_attribute(
            "aria-activedescendant"
        ), "arrow key did not highlight a row"
        combo_input.press("Enter")

        chips = root.locator(".v-combo-chip").all_inner_texts()
        assert any("python" in chip for chip in chips), chips

        python_tag = next(t for t in sample_data.tags if t.name == "python")
        select_values = pg.eval_on_selector(
            "#id_tags", "el => Array.from(el.selectedOptions).map((o) => o.value)"
        )
        assert select_values == [str(python_tag.pk)], select_values

        context.close()


# ── the user menu ────────────────────────────────────────────────────────────────


class TestUserMenuKeyboard:
    """Enter opens the menu on the first item, the arrows move between items,
    Escape closes it and returns focus to the trigger button.

    Straight from the WAI-ARIA menu-button pattern the code comment on
    ``initUserMenu`` names: "one tab stop, not eight ... this code moves focus."
    """

    def test_enter_opens_arrows_move_escape_closes_and_refocuses(
        self, sample_data, staff_cookie, pw, live_server
    ):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:dashboard"))
        pg.wait_for_load_state("networkidle")

        trigger = pg.locator("[data-usermenu-trigger]")
        panel = pg.locator("[data-usermenu-panel]")

        trigger.focus()
        assert trigger.get_attribute("aria-expanded") == "false"
        trigger.press("Enter")

        pg.wait_for_function(
            "() => document.querySelector('[data-usermenu-panel]').hidden === false"
        )
        assert trigger.get_attribute("aria-expanded") == "true"
        items = panel.locator('[role="menuitem"], [role="menuitemradio"]')
        assert items.count() >= 2, "not enough menu items to exercise arrow movement"

        first_active = pg.evaluate("() => document.activeElement.textContent.trim()")
        assert first_active == items.nth(0).inner_text().strip()

        pg.keyboard.press("ArrowDown")
        second_active = pg.evaluate("() => document.activeElement.textContent.trim()")
        assert second_active != first_active, "ArrowDown did not move focus to the next item"
        assert second_active == items.nth(1).inner_text().strip()

        pg.keyboard.press("Escape")
        pg.wait_for_function(
            "() => document.querySelector('[data-usermenu-panel]').hidden === true"
        )
        assert trigger.get_attribute("aria-expanded") == "false"
        refocused = pg.evaluate(
            "() => document.activeElement === document.querySelector('[data-usermenu-trigger]')"
        )
        assert refocused, "Escape did not return focus to the trigger"

        context.close()


# ── the grid's roving tabindex ───────────────────────────────────────────────────


class TestGridRovingTabindex:
    """Exactly one element in the grid is tabbable at rest, and stays exactly one
    after arrow-key navigation; the column-resize handle's Left/Right exception to
    that contract is scoped to itself and Escape gets back out of it.
    """

    @staticmethod
    def _tabindexes(pg):
        return pg.eval_on_selector_all(
            "[data-grid-row]", "els => els.map((el) => el.getAttribute('tabindex'))"
        )

    def test_exactly_one_row_is_tabbable_at_rest_and_after_navigation(
        self, sample_data, staff_cookie, pw, live_server
    ):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")

        at_rest = self._tabindexes(pg)
        assert at_rest.count("0") == 1, at_rest

        first_row = pg.locator("[data-grid-row]:not([data-grid-head])").first
        first_row.click()
        after_click = self._tabindexes(pg)
        assert after_click.count("0") == 1, after_click

        pg.keyboard.press("ArrowDown")
        after_arrow = self._tabindexes(pg)
        assert after_arrow.count("0") == 1, after_arrow
        assert after_arrow != after_click, "ArrowDown did not move the roving tab stop"

        context.close()

    def test_resize_handle_left_right_is_scoped_and_escape_returns_focus_to_the_row(
        self, sample_data, staff_cookie, pw, live_server
    ):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")

        handle = pg.locator("[data-col-resize]").first
        column = handle.get_attribute("data-col-resize")
        header_cell = f'th[data-col-name="{column}"]'

        before_width = pg.eval_on_selector(header_cell, "el => el.getBoundingClientRect().width")
        handle.evaluate("el => el.focus({preventScroll: true})")
        pg.keyboard.press("ArrowRight")
        after_width = pg.eval_on_selector(header_cell, "el => el.getBoundingClientRect().width")

        assert after_width != before_width, "ArrowRight on the handle did not resize the column"
        still_on_handle = pg.evaluate(
            f'() => document.activeElement.getAttribute("data-col-resize") === "{column}"'
        )
        assert still_on_handle, "ArrowRight moved focus off the resize handle"

        pg.keyboard.press("Escape")
        back_on_a_row = pg.evaluate(
            "() => document.activeElement.hasAttribute('data-grid-row')"
        )
        assert back_on_a_row, "Escape did not return focus to the row"
        assert self._tabindexes(pg).count("0") == 1

        context.close()


# ── column pinning under RTL, with real overflow ─────────────────────────────────


class TestColumnPinningUnderRTL:
    """A pinned column sticks to the inline-start (right, under ``dir="rtl"``) edge
    of the table under *genuine* horizontal overflow.

    Deliberately not a static CSS assertion: the whole risk here is a rule that reads
    correctly (``inset-inline-start`` rather than ``left``) but a browser, a stacking
    context, or the RTL scroll-position convention (Chromium's own ``scrollLeft``
    runs 0 to negative under ``dir="rtl"``, the opposite of what intuition suggests)
    defeats it in practice. So this forces a table wider than its viewport by
    resizing columns through the real keyboard interaction, scrolls the real
    ``.v-table-wrap`` container to several positions including both ends of its
    actual scrollable range, and measures real ``getBoundingClientRect()`` values.
    """

    def test_pinned_column_stays_flush_with_the_right_edge_while_scrolling(
        self, settings, sample_data, staff_cookie, pw, live_server
    ):
        settings.LANGUAGE_CODE = "ar"  # get_language_bidi() reads this; see authviews.py

        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.set_viewport_size({"width": 480, "height": 720})
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")

        assert pg.eval_on_selector("html", "el => el.getAttribute('dir')") == "rtl"

        columns = pg.eval_on_selector_all(
            "thead th.v-col", "els => els.map((el) => el.getAttribute('data-col-name'))"
        )
        assert len(columns) >= 3, columns
        pinned_column = columns[0]

        pg.click(f'[data-col-pin="{pinned_column}"]', force=True)
        pg.wait_for_selector(f'th[data-col-name="{pinned_column}"][data-col-pinned]')

        # Grow every *other* column through the keyboard resize handle, the same
        # interaction TestGridRovingTabindex exercises. Left column stays at its
        # natural (narrow) width, so it is never the reason the row overflows — only
        # the other columns are, which is what makes "did it really scroll" legible.
        for other in columns[1:]:
            other_handle = pg.locator(f'[data-col-resize="{other}"]')
            other_handle.evaluate("el => el.focus({preventScroll: true})")
            for _ in range(20):
                # Under dir="rtl" the resize handle inverts which arrow grows the
                # column (see the isRtl() check in vantage-grid.js) — Left grows here.
                pg.keyboard.press("Shift+ArrowLeft")

        scroll_width, client_width = pg.eval_on_selector(
            ".v-table-wrap", "el => [el.scrollWidth, el.clientWidth]"
        )
        assert scroll_width > client_width + 200, (
            f"table did not genuinely overflow its container "
            f"(scrollWidth={scroll_width}, clientWidth={client_width})"
        )

        def pinned_and_wrap_rects():
            wrap = pg.eval_on_selector(".v-table-wrap", "el => el.getBoundingClientRect()")
            cell = pg.eval_on_selector(
                "tbody td[data-col-pinned]", "el => el.getBoundingClientRect()"
            )
            return wrap, cell

        def last_column_rect():
            return pg.eval_on_selector(
                f'thead th[data-col-name="{columns[-1]}"]', "el => el.getBoundingClientRect()"
            )

        def outside(rect, wrap_rect) -> bool:
            return rect["right"] <= wrap_rect["left"] or rect["left"] >= wrap_rect["right"]

        # The unscrolled position: with the select-all checkbox column (unpinned,
        # narrow) still fully in view ahead of the pinned column, the pinned cell has
        # not reached the container's edge yet and is not expected to be flush with
        # it — that would be true of any sticky element, pinned or not, and is not
        # the thing under test. Recorded once, before any deliberate scrolling.
        rest_scroll_left = pg.eval_on_selector(".v-table-wrap", "el => el.scrollLeft")
        rest_wrap_rect = pg.eval_on_selector(".v-table-wrap", "el => el.getBoundingClientRect()")
        assert outside(last_column_rect(), rest_wrap_rect), (
            "the last (unpinned, far) column is already visible before any scrolling — "
            "the table is not actually overflowing its container"
        )

        # Find the container's actual scrollable range rather than assuming a sign
        # convention (Chromium runs dir="rtl" scrollLeft 0 to negative; that is not
        # guaranteed elsewhere): clamp far past both ends and read back what the
        # browser accepted, then scroll in whichever direction actually moved.
        pg.eval_on_selector(".v-table-wrap", "el => { el.scrollLeft = 999999; }")
        clamped_high = pg.eval_on_selector(".v-table-wrap", "el => el.scrollLeft")
        pg.eval_on_selector(".v-table-wrap", "el => { el.scrollLeft = -999999; }")
        clamped_low = pg.eval_on_selector(".v-table-wrap", "el => el.scrollLeft")
        scrolled_extreme = (
            clamped_high
            if abs(clamped_high - rest_scroll_left) > abs(clamped_low - rest_scroll_left)
            else clamped_low
        )
        assert scrolled_extreme != rest_scroll_left, "container did not actually scroll"

        # At every position between "just past rest" and "fully scrolled" — never
        # rest itself — the pinned cell's right edge must stay flush with the
        # container's right edge. That is "sticks to the inline-start edge", made
        # concrete: not a CSS property that reads correctly, a cell that visibly does
        # not move while everything scrolling past it does.
        for fraction in (0.25, 0.5, 0.75, 1.0):
            target = rest_scroll_left + (scrolled_extreme - rest_scroll_left) * fraction
            pg.eval_on_selector(".v-table-wrap", f"el => {{ el.scrollLeft = {target}; }}")
            wrap_rect, cell_rect = pinned_and_wrap_rects()
            assert cell_rect["right"] == pytest.approx(wrap_rect["right"], abs=2), (
                f"at scrollLeft fraction {fraction}: pinned cell right edge "
                f"{cell_rect['right']} is not flush with the wrap's right edge "
                f"{wrap_rect['right']}"
            )

        # And the far, unpinned column that was off-screen at rest is now at least
        # partly visible — proof this was genuine overflow-scrolling that actually
        # moved content, not a no-op the flush-edge assertion above would pass
        # vacuously if the table had never really grown past its container.
        assert not outside(last_column_rect(), wrap_rect), (
            "the last column never scrolled into view; nothing actually scrolled"
        )

        context.close()


# ── the shortcuts preference silences every character shortcut ──────────────────


class TestShortcutsPreference:
    """WCAG 2.1.4: an unmodified character shortcut must be switchable off. The
    grid's "Keyboard shortcuts" checkbox is that switch, and it has to reach the
    two shortcuts that live outside vantage-grid.js — ``/`` in vantage.js and ``e``
    in vantage-inline.js. Both once ignored it: ``/`` compared the stored value
    against ``"0"`` while the grid writes JSON ``false``, and ``e`` never looked.

    The switch is flipped by clicking the real checkbox rather than seeding
    localStorage, so the test cannot agree with itself about an encoding the grid
    does not actually use.
    """

    @staticmethod
    def _switch_off(pg):
        pg.evaluate(
            "() => { const box = document.querySelector('[data-shortcuts-toggle]');"
            " if (box.checked) box.click(); }"
        )
        pg.reload()
        pg.wait_for_load_state("networkidle")

    @staticmethod
    def _focus_first_row(pg):
        pg.evaluate(
            "() => document.querySelector('[data-grid-row]:not([data-grid-head])').focus()"
        )

    def test_slash_focuses_search_until_switched_off(
        self, sample_data, staff_cookie, pw, live_server
    ):
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-order-list"))
        pg.wait_for_load_state("networkidle")
        is_search = "() => document.activeElement.type === 'search'"

        self._focus_first_row(pg)
        pg.keyboard.press("/")
        assert pg.evaluate(is_search), "the shortcut should work while switched on"

        self._switch_off(pg)
        self._focus_first_row(pg)
        pg.keyboard.press("/")
        assert not pg.evaluate(is_search), "'/' still fired with shortcuts switched off"
        context.close()

    def test_e_enters_cell_mode_until_switched_off(
        self, sample_data, staff_cookie, pw, live_server, monkeypatch
    ):
        from tests.testapp.models import Post

        monkeypatch.setattr(site.get_panel(Post), "list_editable", ["status"])
        context = pw.new_context()
        context.add_cookies([{**staff_cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:testapp-post-list"))
        pg.wait_for_load_state("networkidle")
        in_a_cell = "() => !!document.activeElement.closest('td')"

        self._focus_first_row(pg)
        pg.keyboard.press("e")
        assert pg.evaluate(in_a_cell), "the shortcut should work while switched on"

        self._switch_off(pg)
        self._focus_first_row(pg)
        pg.keyboard.press("e")
        assert not pg.evaluate(in_a_cell), "'e' still fired with shortcuts switched off"
        context.close()


# ── the dark-theme logo ─────────────────────────────────────────────────────────


#: Two distinguishable 1×1 images, inline so the test needs no static files or network.
_PX = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
_LIGHT, _DARK = _PX + "#light", _PX + "#dark"


class TestDarkLogo:
    """LOGO_DARK was resolved, editable from the settings screen, and rendered
    nowhere. What matters is which image a person actually sees, so this asserts
    computed visibility under each theme condition rather than the markup."""

    _VISIBLE = """() => Object.fromEntries(
        Array.from(document.querySelectorAll('.v-brand img, .v-brand [data-brand-collapsed]'))
          .map((el) => [el.getAttribute('src') ? el.getAttribute('src').split('#')[1] : 'mark',
                        getComputedStyle(el).display !== 'none']))"""

    @pytest.fixture
    def branded(self, settings):
        settings.VANTAGE = {"LOGO": _LIGHT, "LOGO_DARK": _DARK, "SITE_TITLE": "Acme"}

    def _open(self, pw, cookie, live_server, scheme):
        context = pw.new_context(color_scheme=scheme)
        context.add_cookies([{**cookie, "url": live_server.url}])
        pg = context.new_page()
        pg.goto(live_server.url + reverse("vantage:dashboard"))
        pg.wait_for_load_state("networkidle")
        return context, pg

    @pytest.mark.parametrize(
        ("scheme", "expected"),
        [("light", {"light": True, "dark": False}), ("dark", {"light": False, "dark": True})],
    )
    def test_the_device_scheme_picks_the_logo(
        self, branded, sample_data, staff_cookie, pw, live_server, scheme, expected
    ):
        context, pg = self._open(pw, staff_cookie, live_server, scheme)
        assert pg.evaluate(self._VISIBLE) == {**expected, "mark": False}
        context.close()

    def test_a_pinned_light_theme_beats_a_dark_device(
        self, branded, sample_data, staff_cookie, pw, live_server
    ):
        context, pg = self._open(pw, staff_cookie, live_server, "dark")
        pg.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
        assert pg.evaluate(self._VISIBLE) == {"light": True, "dark": False, "mark": False}
        context.close()

    def test_collapsed_the_acronym_stands_in_for_the_logo(
        self, branded, sample_data, staff_cookie, pw, live_server
    ):
        context, pg = self._open(pw, staff_cookie, live_server, "light")
        pg.evaluate(
            "() => document.querySelector('.v-shell').setAttribute('data-collapsed', 'true')"
        )
        assert pg.evaluate(self._VISIBLE) == {"light": False, "dark": False, "mark": True}
        context.close()
