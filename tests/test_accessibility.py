"""Accessibility as an executable guard, not a checklist in a document.

A written audit rots the day after it is written. Everything here is a property of
real rendered HTML — the pages are fetched through the test client and parsed — so a
regression fails the build instead of surviving until someone reruns axe by hand.

Scope is deliberate and stated: the pages asserted over are the shell and the pages
this module can currently keep clean. ``docs/accessibility.md`` lists what is *not*
covered and why, and widening ``PAGES`` is the whole job of closing those gaps.

The colour half of the file works on the token contract rather than on pixels, using
``vantage.color.contrast_ratio``. ``test_theme.py`` already checks each token against
its own paired ``*-foreground``; what is added here is the *cross* pairs a component
actually produces — muted text on a card, a focus ring against the surface it sits on
— and a sweep of the hue circle for the ones derived from ``BRAND_COLOR``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.template import loader
from django.urls import reverse

from tests.testapp.models import Author, Post
from vantage.color import OKLCH, WCAG_AA, contrast_ratio, oklch_to_hex
from vantage.theme import build_tokens

#: Contrast required of a non-text UI boundary — a focus ring, a control edge.
#: WCAG 1.4.11, and lower than the 4.5:1 text bar on purpose.
WCAG_AA_NON_TEXT = 3.0

#: The two live-demo findings below are client-side behaviour a Django test client has
#: no JavaScript to reproduce (see TestLiveDemoFindings' own docstring), so — like
#: TestTheControlIsShipped in test_widgets.py — they are asserted against the shipped
#: source rather than against rendered HTML.
STATIC = Path(__file__).resolve().parent.parent / "src/vantage/static/vantage"


# ── a tiny accessibility-oriented DOM ───────────────────────────────────────


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    #: Everything a screen reader would read from inside this element, including the
    #: alt text of images and the contents of visually hidden spans.
    text: str = ""
    ancestors: tuple[str, ...] = ()
    #: True when this element, or anything above it, is hidden from assistive tech.
    hidden: bool = False

    @property
    def name(self) -> str:
        """The accessible name, near enough for the properties asserted below.

        aria-label wins over content, which is the real algorithm's order too. Full
        accname resolution needs a browser; this covers the failure that actually
        happens, which is a control with no name of any kind.
        """
        for key in ("aria-label", "title"):
            if self.attrs.get(key, "").strip():
                return self.attrs[key].strip()
        return self.text.strip()

    @property
    def has_name(self) -> bool:
        """A name made only of glyphs -- a multiplication sign, an arrow -- is no name.

        This is the check that catches icon-only buttons, and it has to reject
        punctuation or every ``&times;`` close button passes while announcing nothing.
        """
        return any(char.isalnum() for char in self.name)


class Collector(HTMLParser):
    """Parses just enough structure for the assertions in this file."""

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._open: list[Element] = []

    # -- parsing ------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        element = self._make(tag, attrs)
        if tag in self.VOID:
            # A void element cannot hold text, but its alt or label is still part
            # of the accessible name of whatever contains it.
            self._contribute(
                element.attrs.get("alt", "") + " " + element.attrs.get("aria-label", "")
            )
        else:
            self._open.append(element)

    def handle_startendtag(self, tag, attrs):
        self._make(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index].tag == tag:
                del self._open[index:]
                return

    def handle_data(self, data):
        self._contribute(data)

    # -- internals ----------------------------------------------------------

    def _make(self, tag, attrs) -> Element:
        parent_hidden = any(node.hidden for node in self._open)
        resolved = {key: (value or "") for key, value in attrs}
        element = Element(
            tag=tag,
            attrs=resolved,
            ancestors=tuple(node.tag for node in self._open),
            hidden=parent_hidden
            or resolved.get("aria-hidden") == "true"
            or "hidden" in resolved,
        )
        self.elements.append(element)
        return element

    def _contribute(self, text: str) -> None:
        for node in self._open:
            node.text += text


def _tabindex(value: str | None) -> int:
    """``tabindex`` as a number; anything absent or unparseable counts as zero."""
    try:
        return int((value or "0").strip())
    except ValueError:
        return 0


@dataclass
class Document:
    """A parsed page, plus the lookups the assertions need."""

    elements: list[Element]
    label_targets: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, html: str) -> Document:
        collector = Collector()
        collector.feed(html)
        elements = collector.elements
        return cls(
            elements=elements,
            label_targets={
                el.attrs["for"] for el in elements if el.tag == "label" and el.attrs.get("for")
            },
            ids={el.attrs["id"] for el in elements if el.attrs.get("id")},
        )

    def of(self, *tags: str) -> list[Element]:
        return [el for el in self.elements if el.tag in tags]


# ── the pages under guard ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mounted(settings):
    """Reuse the auth suite's urlconf: it mounts the panel *and* the auth routes.

    Declaring a second identical one would only create a second thing to keep in
    step with the first.
    """
    settings.ROOT_URLCONF = "tests.test_auth_pages"


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("staffer", "staff@example.com", "correct-horse")


@pytest.fixture
def post(db):
    author = Author.objects.create(name="Ada Lovelace", email="ada@example.com")
    return Post.objects.create(
        title="A post", slug="a-post", body="Body text", author=author,
        status=Post.Status.PUBLISHED,
    )


@pytest.fixture
def pages(client, staff, post) -> dict[str, str]:
    """Every page this module owns, as rendered HTML.

    Fetched through the test client rather than rendered from a context dict, because
    half of what is asserted here (landmarks, heading order, the skip link) only
    exists once the shell and the page template have been composed.
    """
    rendered = {"login": client.get("/accounts/login/").content.decode()}
    client.force_login(staff)
    rendered.update(
        {
            "logged_out": client.post("/accounts/logout/").content.decode(),
            "500": loader.get_template("vantage/500.html").render(),
        }
    )
    client.force_login(staff)
    rendered.update(
        {
            "password_change": client.get("/accounts/password_change/").content.decode(),
            "dashboard": client.get(reverse("vantage:dashboard")).content.decode(),
            "detail": client.get(
                reverse("vantage:testapp-post-detail", args=[post.pk])
            ).content.decode(),
            "custom_page": client.get(reverse("vantage:page-revenue")).content.decode(),
            "404": client.get("/no/such/page/").content.decode(),
            "403": client.get("/denied/").content.decode(),
            # The pages docs/accessibility.md names as "not yet verified" (gap 5):
            # never rendered through this guard before. Widening coverage to them is
            # the whole point of this pass — see the live-demo findings that motivated
            # each one, referenced on the tests below.
            "list": client.get(reverse("vantage:testapp-post-list")).content.decode(),
            "add": client.get(reverse("vantage:testapp-post-add")).content.decode(),
            "delete": client.get(
                reverse("vantage:testapp-post-delete", args=[post.pk])
            ).content.decode(),
            "settings": client.get(reverse("vantage:settings")).content.decode(),
        }
    )
    return rendered


#: Everything rendered inside vantage/base.html. These get the landmark and skip-link
#: guarantees; the standalone pages below deliberately do not.
SHELL_PAGES = (
    "password_change", "dashboard", "detail", "custom_page", "404", "403",
    "list", "add", "delete", "settings",
)
ALL_PAGES = (*SHELL_PAGES, "login", "logged_out", "500")


@pytest.fixture
def docs(pages) -> dict[str, Document]:
    return {name: Document.parse(html) for name, html in pages.items()}


class TestEveryPage:
    """Properties that hold for every page this module renders."""

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_exactly_one_h1(self, docs, name):
        """Zero leaves a screen reader with no title for the page; two mean the
        outline is a guess."""
        assert len(docs[name].of("h1")) == 1

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_headings_never_skip_a_level(self, docs, name):
        levels = [int(el.tag[1]) for el in docs[name].of("h1", "h2", "h3", "h4", "h5", "h6")]
        previous = 0
        for level in levels:
            assert level <= previous + 1, f"{name}: h{previous} followed by h{level}"
            previous = level

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_every_image_has_alt(self, docs, name):
        """Including the decorative ones, which need alt="" and not a missing
        attribute — that is the difference between "skip me" and "read the filename"."""
        missing = [el.attrs for el in docs[name].of("img") if "alt" not in el.attrs]
        assert not missing, f"{name}: {missing}"

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_every_control_is_labelled(self, docs, name):
        document = docs[name]
        offenders = []
        for el in document.of("input", "select", "textarea"):
            if el.attrs.get("type", "text") in {"hidden", "submit", "reset", "button", "image"}:
                continue
            labelled = (
                el.attrs.get("aria-label", "").strip()
                or el.attrs.get("aria-labelledby", "").strip()
                or el.attrs.get("title", "").strip()
                or el.attrs.get("id", "\0") in document.label_targets
                or "label" in el.ancestors
            )
            if not labelled:
                offenders.append(el.attrs)
        assert not offenders, f"{name}: unlabelled control(s) {offenders}"

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_no_positive_tabindex(self, docs, name):
        """A positive tabindex reorders the whole document's tab sequence, not just
        its own element, and the damage is invisible to whoever added it."""
        offenders = [
            el.attrs for el in docs[name].elements if _tabindex(el.attrs.get("tabindex")) > 0
        ]
        assert not offenders, f"{name}: {offenders}"

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_every_control_has_an_accessible_name(self, docs, name):
        """The icon-only-button check. A button whose whole content is an SVG or a
        ``&times;`` announces itself as "button" and nothing else."""
        offenders = []
        for el in docs[name].of("button", "a"):
            if el.hidden:
                continue
            if el.tag == "a" and not el.attrs.get("href"):
                continue  # an anchor target, not a link
            if not el.has_name:
                offenders.append(el.attrs)
        assert not offenders, f"{name}: unnamed control(s) {offenders}"

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_the_document_declares_a_language(self, docs, name):
        """Without it a screen reader reads English content in the user's own
        accent, or in the wrong voice entirely."""
        html = docs[name].of("html")
        assert html and html[0].attrs.get("lang", "").strip()

    @pytest.mark.parametrize("name", ALL_PAGES)
    def test_exactly_one_main_landmark(self, docs, name):
        assert len(docs[name].of("main")) == 1


class TestTheShell:
    """Properties of vantage/base.html specifically."""

    @pytest.mark.parametrize("name", SHELL_PAGES)
    def test_there_is_a_skip_link_and_it_points_somewhere_real(self, docs, name):
        """WCAG 2.4.1. A skip link whose target does not exist is worse than none:
        it consumes the first tab stop and does nothing."""
        document = docs[name]
        links = [
            el for el in document.of("a")
            if el.attrs.get("href", "").startswith("#") and "skip" in el.name.lower()
        ]
        assert links, f"{name}: no skip link"
        assert links[0].attrs["href"][1:] in document.ids

    @pytest.mark.parametrize("name", SHELL_PAGES)
    def test_the_skip_link_is_the_first_focusable_element(self, docs, name):
        """After the banners it is decoration — the user has already tabbed past the
        content it claims to skip."""
        focusable = [
            el for el in docs[name].of("a", "button", "input", "select", "textarea")
            if not el.hidden and (el.tag != "a" or el.attrs.get("href"))
            and el.attrs.get("type") != "hidden"
        ]
        assert focusable and "skip" in focusable[0].name.lower()

    @pytest.mark.parametrize("name", SHELL_PAGES)
    def test_the_main_region_can_receive_focus(self, docs, name):
        """Browsers move the scrollbar to a skip target but leave focus on <body>
        unless the target is focusable."""
        main = docs[name].of("main")[0]
        assert main.attrs.get("tabindex") == "-1"

    @pytest.mark.parametrize("name", SHELL_PAGES)
    def test_every_navigation_landmark_is_named(self, docs, name):
        """Two unnamed navs are announced identically, which is the same as having
        no landmarks at all."""
        for nav in docs[name].of("nav"):
            assert nav.attrs.get("aria-label") or nav.attrs.get("aria-labelledby"), name

    @pytest.mark.parametrize("name", SHELL_PAGES)
    def test_the_message_region_exists_even_with_nothing_in_it(self, docs, name):
        """A live region created at the same instant as its first message is not
        announced; assistive technology has to have been watching the node already."""
        regions = [el for el in docs[name].elements if el.attrs.get("aria-live")]
        assert any(el.attrs.get("id") == "v-toasts" for el in regions), name

    def test_a_message_carries_its_level_as_text_not_only_as_colour(self, client, staff):
        """WCAG 1.4.1. The tint on the toast's edge is the only visual signal of
        severity, so the word has to be in the accessible name too."""
        client.force_login(staff)
        client.post(
            "/accounts/password_change/",
            {
                "old_password": "correct-horse",
                "new_password1": "battery-staple-99",
                "new_password2": "battery-staple-99",
            },
        )
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert 'class="v-sr-only">Success:' in body, "the toast level is colour only"


class TestLiveDemoFindings:
    """Regressions confirmed by driving the running demo (localhost:8765) with
    Playwright -- keyboard only, the accessibility tree, and both themes -- rather
    than by reading templates. Kept here as failing tests per the audit brief: a
    failing test is the clearest statement of a finding, and each one turns green
    the day the gap it names is actually closed. Where a finding cannot be expressed
    against server-rendered HTML at all (it is a client-side behaviour a Django test
    client has no JavaScript to reproduce), it is not here -- see the audit's ranked
    list instead.
    """

    def test_a_failed_sign_in_is_not_associated_with_either_field(self, client, staff):
        """WCAG 3.3.1 Error Identification, 4.1.2 Name, Role, Value.

        docs/accessibility.md's guarantees table claims: 'Sign-in errors announced
        via role="alert", with aria-invalid and aria-describedby on the offending
        field.' True for a *field-level* error (a blank password, with novalidate
        bypassed) -- false for the failure every user actually hits first: a wrong
        username/password pair. Django's AuthenticationForm attaches that as a
        non-field error, so authviews._style_form's ``if bound.errors`` never fires
        for either field. Confirmed live: the alert renders with real text ("Please
        enter a correct username and password...") and role="alert", but neither
        input carries aria-invalid or aria-describedby, and the alert <div> itself
        has no id for anything to point at. A screen reader user who tabs back into
        the username field hears "edit text, username" -- nothing marks it invalid,
        nothing describes what to fix.
        """
        response = client.post(
            "/accounts/login/", {"username": staff.username, "password": "definitely-wrong"}
        )
        document = Document.parse(response.content.decode())
        alerts = [el for el in document.of("div") if el.attrs.get("role") == "alert"]
        assert alerts, "fixture drifted: no failed-login alert rendered"
        fields = [
            el for el in document.of("input") if el.attrs.get("name") in ("username", "password")
        ]
        assert fields, "fixture drifted: no username/password field rendered"
        assert any(
            field.attrs.get("aria-invalid") == "true" or field.attrs.get("aria-describedby")
            for field in fields
        ), "neither field is associated with the failed-login alert"

    def test_dashboard_cards_are_headings_not_styled_spans(self, docs):
        """WCAG 1.3.1 Info and Relationships, 2.4.6 Headings and Labels.

        The dashboard's outline used to be exactly one heading long — the page's own
        ``<h1>`` — however many stat cards, charts and list panels rendered under it,
        because every card was titled with ``<span class="v-card-title">``. Someone
        navigating by heading found one stop on the whole page and no way to reach
        "Revenue" or "Latest orders" directly.

        They are ``<h2>`` rather than ``<h3>``: the page ``<h1>`` is the only thing
        above them, and jumping a level is its own outline bug.
        """
        document = docs["dashboard"]
        titles = [el for el in document.elements if el.attrs.get("class") == "v-card-title"]
        assert titles, "fixture drifted: no card titles rendered at all"

        spans = [el.text.strip() for el in titles if el.tag == "span"]
        assert not spans, f"card title(s) still contribute nothing to the outline: {spans}"

        headings = {el.text.strip() for el in document.of("h2")}
        missing = [el.text.strip() for el in titles if el.text.strip() not in headings]
        assert not missing, f"card title(s) are not h2: {missing}"

    def test_the_settings_contrast_readout_is_a_live_region(self, pages):
        """WCAG 4.1.3 Status Messages.

        Confirmed live: typing a new BRAND_COLOR on this page updates the
        "N.N:1 on button text" badge instantly and correctly as you type -- but
        neither the badge nor anything wrapping it carries aria-live, so nothing is
        announced while the value changes under the user's fingers. A screen reader
        user has to stop, tab away from the field they are editing, and go find the
        badge by hand to learn whether their new colour still passes. docs/
        accessibility.md already names this gap ("the contrast readout and warning
        in settings.html"); this pins it to the actual markup so a fix has something
        to turn green.
        """
        html = pages["settings"]
        assert "data-preview-ratio" in html, "fixture drifted: no contrast badge rendered"

        # The whole opening tag, not the markup preceding the marker. Attribute order
        # is arbitrary, and a window that only looks backwards from `data-preview-ratio`
        # reports a correctly-wired element as broken whenever aria-live happens to be
        # written after it — which is what this assertion did.
        start = html.rindex("<", 0, html.index("data-preview-ratio"))
        tag = html[start : html.index(">", start) + 1]
        assert "aria-live" in tag, f"the contrast readout is not a live region: {tag}"
        assert "role=" in tag, f"the contrast readout has no status role: {tag}"

    def test_column_resize_announces_the_new_width(self):
        """WCAG 4.1.3 Status Messages.

        Confirmed live: focusing a resize handle and pressing Left/Right genuinely
        changes the column's width in real time (`vantage-grid.js`'s own
        `setWidth`), but nothing told a screen reader user what it had become — the
        control was operable and its effect invisible. Fixed by routing every width
        change through a debounced announcement on the grid's existing live region.

        A Django test client has no JavaScript, so this cannot be driven through
        rendered HTML the way the rest of this file works; it is pinned against the
        shipped source instead, the same way TestTheControlIsShipped in
        test_widgets.py pins combobox.js's shape.
        """
        js = (STATIC / "vantage-grid.js").read_text(encoding="utf-8")

        # Debounced, not fired from every keypress or pointermove frame: a held
        # arrow key repeats several times a second and a drag is dozens of frames.
        assert "RESIZE_ANNOUNCE_DEBOUNCE_MS" in js
        assert "window.clearTimeout(resizeAnnounceTimer)" in js, (
            "the previous pending announcement is not being cancelled -- "
            "this is what makes it a debounce rather than a flood"
        )

        # setWidth is the single path both the drag handler and the keyboard
        # handler go through (see the pointermove and keydown listeners on
        # [data-col-resize] below it in the file), so hooking the announcement
        # there is what makes it fire for both interaction styles.
        set_width = js[js.index("function setWidth(") : js.index("function setPinned(")]
        assert "scheduleResizeAnnounce(name)" in set_width

        # A pixel count alone does not say which column changed. The column's own
        # label has to be in the message, not just the number.
        schedule = js[
            js.index("function scheduleResizeAnnounce(") : js.index("function setWidth(")
        ]
        assert "columnByName[name].label" in schedule
        assert "widths[name]" in schedule

    def test_the_relation_pickers_hidden_select_does_not_surface_as_a_phantom_combobox(
        self,
    ):
        """WCAG 4.1.2 Name, Role, Value.

        Confirmed live (an Order's Customer field, `/admin/shop/order/<pk>/edit/`)
        with Chromium's own accessibility tree (CDP ``Accessibility.getFullAXTree``,
        not Playwright's ``ariaSnapshot()`` -- that helper does not consult `inert`
        and reports this element regardless of the fix): ``combobox.js`` used to hide
        the real ``<select>`` behind the type-to-search box with
        ``tabindex="-1"`` plus ``aria-hidden="true"``, kept focusable so the browser
        could report a constraint violation against it. ``aria-hidden`` on a
        focusable element is a spec conflict; Chromium resolves it in the element's
        favour, so the select stayed in the tree as a second, nameless combobox.

        The constraint-validation concern does not hold for this widget as it
        stands: `required` is the only HTML5 constraint a ``<select>`` can carry,
        and it is already moved onto the visible ``input`` a few lines above this
        (`input.required = true; select.required = false;`) before the select is
        ever hidden. `inert` is the fix that actually removes the element from the
        accessibility tree, at the honestly-documented cost that it also makes the
        element unfocusable -- see docs/accessibility.md's "hidden `<select>`"
        section for the trade-off this depends on.
        """
        js = (STATIC / "combobox.js").read_text(encoding="utf-8")

        # The invariant the whole fix depends on: required has to be off the select
        # by the time it is hidden, or `inert` would be swallowing a real
        # constraint rather than a moot one. Order matters here, not just presence.
        required_moved = js.index("select.required = false;")
        hidden = js.index('select.setAttribute("tabindex", "-1");')
        assert required_moved < hidden, (
            "select.required is no longer being cleared before the select is hidden -- "
            "the `inert` fix stops being honest the day this changes"
        )

        # `inert` is the primary path; the old shape survives only as the fallback
        # for a browser too old to support it, not as the normal behaviour.
        assert '"inert" in select' in js
        assert "select.inert = true;" in js

        # Never aria-hidden on a still-focusable select: whichever branch runs, the
        # two must not both apply to the same element at once.
        fallback = js[js.index('"inert" in select') :]
        assert 'select.setAttribute("aria-hidden", "true")' in fallback
        # And it is genuinely a fallback -- gated behind the feature check above,
        # not a second, unconditional application of the old attributes.
        unconditional = js[: js.index('"inert" in select')]
        assert 'select.setAttribute("aria-hidden", "true")' not in unconditional


class TestTheErrorPages:
    def test_the_500_page_needs_nothing_from_the_context(self):
        """Rendered with no request and no context at all — the property the rest of
        this file cannot assert for it, because there is nothing to render *from*."""
        document = Document.parse(loader.get_template("vantage/500.html").render())
        assert len(document.of("h1")) == 1
        assert document.of("html")[0].attrs.get("lang")

    def test_the_403_page_does_not_pretend_to_be_a_404(self, pages):
        """Telling someone a page does not exist when they simply cannot see it is a
        lie, and an accessibility problem as much as a support one: the user has no
        way to learn the real state of the system."""
        assert "403" in pages["403"]
        assert "404" not in pages["403"]


# ── colour ──────────────────────────────────────────────────────────────────

OKLCH_CALL = re.compile(r"oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)")


def _oklch(value: str) -> OKLCH | None:
    """``None`` for a token written with an alpha channel.

    ``--border`` in the dark theme is ``oklch(1 0 0 / 12%)`` — white at 12% over an
    unknown surface. Its real contrast depends on what is behind it, which this layer
    cannot know, so it is excluded rather than measured wrongly. That exclusion is
    recorded as a known gap in docs/accessibility.md.
    """
    match = OKLCH_CALL.match(value)
    if not match:
        return None
    return OKLCH(float(match.group(1)), float(match.group(2)), float(match.group(3)))


#: Foreground/background pairs a component genuinely puts together, beyond the
#: same-named pairs test_theme.py already covers. Text, so 4.5:1.
TEXT_PAIRS = [
    ("foreground", "card"),
    ("foreground", "sidebar"),
    ("card-foreground", "background"),
    ("secondary-foreground", "background"),
    ("muted-foreground", "background"),
    ("muted-foreground", "card"),
    ("muted-foreground", "sidebar"),
    ("accent-foreground", "accent"),
    ("primary-foreground", "primary-hover"),
]

#: Boundaries and indicators rather than text: WCAG 1.4.11's 3:1 applies.
NON_TEXT_PAIRS = [
    ("ring", "background"),
    ("ring", "card"),
    ("ring", "sidebar"),
    ("primary", "background"),
    ("primary", "card"),
]

#: Brand colours spread around the hue circle at a chroma high enough to be awkward.
#: A guarantee that only holds for indigo is not a guarantee.
SWEEP = [oklch_to_hex(OKLCH(0.6, 0.18, hue)) for hue in range(0, 360, 30)]


class TestTokenContrast:
    @pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
    @pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
    def test_text_pairs_clear_aa(self, fg, bg, dark):
        tokens = build_tokens(dark=dark)
        foreground, background = _oklch(tokens[fg]), _oklch(tokens[bg])
        assert foreground and background, f"{fg}/{bg} is not a plain oklch() colour"
        assert contrast_ratio(foreground, background) >= WCAG_AA

    @pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
    @pytest.mark.parametrize(("fg", "bg"), NON_TEXT_PAIRS)
    def test_non_text_pairs_clear_the_three_to_one_bar(self, fg, bg, dark):
        tokens = build_tokens(dark=dark)
        assert (
            contrast_ratio(_oklch(tokens[fg]), _oklch(tokens[bg])) >= WCAG_AA_NON_TEXT
        )

    @pytest.mark.parametrize("brand", SWEEP)
    @pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
    def test_the_focus_ring_stays_visible_for_every_brand_hue(self, brand, dark):
        """The focus ring is the whole of keyboard visibility. If it disappears
        against the page for some hue, that project's panel is unusable by keyboard
        and nothing else in the suite would notice."""
        tokens = build_tokens(brand, dark=dark)
        ring = _oklch(tokens["ring"])
        for surface in ("background", "card", "sidebar"):
            ratio = contrast_ratio(ring, _oklch(tokens[surface]))
            assert ratio >= WCAG_AA_NON_TEXT, f"{brand} {surface}: {ratio:.2f}"

    @pytest.mark.parametrize("brand", SWEEP)
    @pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
    def test_a_hovered_primary_button_keeps_its_label_readable(self, brand, dark):
        """``--primary-hover`` moves lightness without recomputing the foreground, so
        the readable pairing derive_palette guarantees is only guaranteed at rest."""
        tokens = build_tokens(brand, dark=dark)
        ratio = contrast_ratio(
            _oklch(tokens["primary-foreground"]), _oklch(tokens["primary-hover"])
        )
        assert ratio >= WCAG_AA, f"{brand}: {ratio:.2f}"

    def test_alpha_tokens_are_knowingly_excluded(self):
        """Documents the hole rather than hiding it: the dark theme's border tokens
        are translucent, so no static pair can be measured for them."""
        dark = build_tokens(dark=True)
        assert _oklch(dark["border"]) is None
        assert _oklch(dark["input"]) is None
