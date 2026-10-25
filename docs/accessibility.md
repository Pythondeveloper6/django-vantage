# Accessibility

The target is **WCAG 2.2 Level AA**. This page says three things, in order of how much
they are worth: what the package guarantees and enforces in CI, what it needs from you
as a panel author, and what is still broken. The third list is the honest one, and it
is deliberately specific — a document that only lists successes is marketing.

---

## 1. What the package guarantees

Each of these is asserted by `tests/test_accessibility.py` against real rendered HTML,
not merely intended. If one regresses, the build fails.

| Guarantee | Criterion | Where |
|---|---|---|
| A skip link, first focusable element on every shell page, pointing at a real, focusable `<main>` | 2.4.1 Bypass Blocks | `base.html`, `vantage-auth.css` |
| Exactly one `<h1>` per page, and no heading level skipped | 1.3.1, 2.4.6 | every owned template |
| Every `<img>` carries an `alt` — `""` when decorative | 1.1.1 Non-text Content | all |
| Every form control has a `<label for>`, an `aria-label`, or a wrapping `<label>` | 1.3.1, 3.3.2 | all |
| Every button and link has an accessible name made of more than a glyph | 4.1.2 Name, Role, Value | all |
| No positive `tabindex` anywhere | 2.4.3 Focus Order | all |
| Exactly one `<main>`; every `<nav>` carries a distinct `aria-label` | 1.3.6, 2.4.1 | `base.html` |
| `<html lang>` set from Django's active language; `dir="rtl"` when the language is bidi | 3.1.1 Language of Page | `base.html` and the auth pages |
| A visible focus ring on every focusable element, at ≥3:1 against its surface for **any** `BRAND_COLOR` hue | 1.4.11, 2.4.7 | `vantage.css`, swept in tests |
| `scroll-padding-block-start` so the sticky header never parks over the focused element | 2.4.11 Focus Not Obscured | `vantage.css:25` |
| All animation suppressed under `prefers-reduced-motion: reduce` | 2.3.3 Animation from Interactions | `vantage.css:89`, guarded by `test_stylesheets.py` |
| A persistent, always-present `aria-live="polite"` region for messages | 4.1.3 Status Messages | `base.html` (`#v-toasts`) |
| Message severity carried as text (`Success:`, `Error:`) and not only as the toast's edge colour | 1.4.1 Use of Colour | `base.html` |
| Cross-token contrast — muted text on cards, foreground on the sidebar, accent pairs — clears 4.5:1 in **both** themes | 1.4.3 Contrast | `theme.py`, asserted in tests |
| A hovered primary button keeps its label at ≥4.5:1 for every brand hue | 1.4.3 | swept in tests |
| Sign-in errors announced via `role="alert"`, with `aria-invalid` and `aria-describedby` on the offending field | 3.3.1 Error Identification | `login.html`, `authviews._style_form` |
| A 403 says "not allowed" and never "not found" | 3.3.1, and plain honesty | `403.html` |
| Resizing a grid column, by drag or by keyboard, announces the column's name and its new width through the grid's own live region, debounced rather than sent on every keypress or drag frame | 4.1.3 Status Messages | `vantage-grid.js`, `tests/test_accessibility.py` |
| The relation picker's real `<select>` is `inert`, not merely `tabindex="-1"` plus `aria-hidden`, so it does not surface as a second, nameless combobox in the accessibility tree | 4.1.2 Name, Role, Value | `combobox.js`, `tests/test_accessibility.py` |

### The live region, specifically

`#v-toasts` is rendered **unconditionally**, even with nothing in it. A live region
created in the same DOM mutation as its first message is not announced — assistive
technology has to have been observing the node beforehand. A container wrapped in
`{% if messages %}` looks correct, tests green, and announces nothing. It collapses to
zero height when empty, so it costs nothing to leave in place.

Script that needs to announce something should append into `#v-toasts`, or write into
it directly. Do not create a new live region per feature.

### The resize announcement, specifically

The resize handle genuinely changes a column's width in real time — the control was
always operable — but until this pass nothing told a screen reader user what it had
become, so the effect was invisible even though the cause was not. Two things matter
about the fix:

- **It is debounced, not sent per keypress or per drag frame.** A held arrow key
  repeats several times a second and a drag is dozens of `pointermove` events; a live
  region is not a log for a screen reader to keep up with. One announcement lands
  half a second after the width stops changing, however many keys or frames it took
  to get there.
- **The column's name rides with the number.** "240 pixels wide" on its own does not
  say which column, and a screen reader user resizing by keyboard is not looking at
  the header to remind themselves. The announcement is always "*Column* column, *N*
  pixels wide."

It reuses the grid's existing `[data-grid-live]` region — the same one column
move, visibility, pin, density and shortcut-preference changes already announce
through — rather than adding a second live region for one more feature, for the
reason `#v-toasts` gives above: one region, watched from boot, is the whole
mechanism this package uses for status messages.

### The relation picker's hidden `<select>`, specifically

The control the browser submits is a real `<select>`, moved out of sight and driven
by `combobox.js`'s type-to-search box in front of it (see that file's own header
comment). Hiding it used to mean `tabindex="-1"` plus `aria-hidden="true"` — kept
focusable on purpose, the old comment said, "so the browser can report a constraint
violation against it." That reasoning is a real one in the abstract and a genuine
platform conflict when it applies: the WAI-ARIA spec says `aria-hidden` must not be
used on a focusable element, and Chromium resolves that conflict in the element's
favour, not the attribute's — a select kept focusable this way stays in the
accessibility tree as a second, nameless combobox next to the one the script draws.
Confirmed live against the demo (an Order's `Customer` field) with Playwright's
accessibility snapshot: two `combobox` nodes where a screen reader user should find
one.

Investigating rather than picking a side of that trade-off first: the reason given
for keeping the select focusable does not hold for this widget as it stands.
`required` is the only HTML5 constraint a `<select>` can carry, and `combobox.js`
already moves it onto the visible `input` before the select is ever hidden
(`input.required = true; select.required = false;`); nothing calls
`select.setCustomValidity` anywhere in the file. By the time the select is hidden it
has no constraint left for a browser to need focus in order to report — the concern
was real when the comment was written and is not live today.

That makes `inert` the honest fix rather than a compromise between the two options:
it drops the element from the accessibility tree without the `aria-hidden` conflict,
because it also makes the element genuinely unfocusable — which costs nothing here,
precisely because nothing above still needs it focusable. Script access
(`.selected`, `.value`, adding an `<option>` for a value that arrived from the
search endpoint) is untouched, since `inert` blocks user interaction and focus, not
code. A browser old enough to lack `inert` (older than Chrome 102 / Firefox 112 /
Safari 15.5 — in line with the `fetch`/`AbortController`/`DOMParser` this file
already assumes without a fallback) falls back to the previous `tabindex="-1"` plus
`aria-hidden` shape, which is the known phantom-combobox behaviour rather than an
interactive select sitting in the tab order.

**The trade-off, spelled out rather than left implicit:** this fix depends on the
select never again being a control a browser needs to focus. If `required` were
ever left on the select instead of moved to the input, or a future change relied on
a native validation bubble against it, `inert` would silently swallow that too.
Nothing in `combobox.js` does either today, and the comment beside the `inert` line
is the tripwire for the day something wants to.

**A tooling note for whoever re-checks this next:** Playwright's own
`locator.ariaSnapshot()` (what the MCP `browser_snapshot` tool is built on) still
lists the select after this fix — it was verified, in this same session, to not
consult `inert` at all, on a page with nothing else wrong (a bare
`<select inert>` in a scratch page still shows up as a `combobox` under that API).
It does correctly drop `hidden`, `display: none` and `aria-hidden`, which is why it
caught the original bug in the first place. The claim above is verified instead
against Chromium's real accessibility tree — CDP's `Accessibility.getFullAXTree`,
reachable from a Playwright session via `page.context().newCDPSession(page)` — which
reports the select with `ignored: true, ignoredReasons: [{name: "inertElement"}]`.
That CDP tree is the one platform screen readers actually query; `ariaSnapshot()` is
not a reliable check for an `inert`-based fix specifically, and reading it naively
here would look like a regression that is not one.

### Colour is never the only signal

`--destructive` and `--warning` are used for **borders and fills, never for body
text**. Measured: `--destructive` against `--card` reaches only **3.56:1** in the dark
theme and `--warning` against `--card` only **2.97:1** in the light theme, both short
of AA. Every error message in the pages this package ships is `--foreground` text
inside a `--destructive` frame, with the word "failed" or the field name spelled out.

---

## 2. What the package asks of you

The panel renders your data and your labels. These are the parts it cannot check.

- **Write real `verbose_name`s.** Column headers, `<dt>` labels and form labels all come
  from them. `verbose_name="dt"` is an accessibility defect, not a style one.
- **Give every model a `__str__` that identifies the record.** It becomes the `<h1>` of
  the detail page and the text of every link to it. `Post object (7)` in a list of
  forty links is forty indistinguishable links.
- **`alt` text for anything you configure.** `LOGO` gets `alt="{{ SITE_TITLE }}"`
  automatically; `LOGIN_IMAGE` is treated as decorative and gets `alt=""`, so do not
  put information in it.
- **`BRAND_COLOR` is safe to choose freely.** `color.accessible_pair` moves the derived
  lightness until the foreground clears 4.5:1, for every hue on the circle. You cannot
  pick a colour that breaks button contrast. You *can* break it by hard-coding a colour
  in `CUSTOM_CSS`, which nothing here can see.
- **Custom pages inherit the shell but not its discipline.** If you ship a template for
  a `Page`, start at `<h2>` — `base.html` does not emit the `<h1>`, your page does, and
  two of them is the most common defect in a custom page.
- **Do not add a positive `tabindex`.** It reorders the whole document, not your widget.
- **Do not rely on a badge tone.** `render.py` colours choice values it recognises as
  states (`paid` green, `failed` red). The label is always rendered too — keep it that
  way in any custom cell renderer.

---

## 3. Keyboard map

| Key | Scope | What it does |
|---|---|---|
| `Tab` | document | Next focusable. The skip link is always first. |
| `Enter` (on the skip link) | document | Moves focus into `<main>`; `tabindex="-1"` is what makes focus actually land. |
| `⌘K` / `Ctrl+K` | document | Opens the command palette. Advertised via `aria-keyshortcuts` on the search box. |
| `/` | document | Focuses the search box. **See the gap below.** |
| `Esc` | palette, drawers, menus | Closes one level. The invariant is "always one guaranteed-safe escape", not the specific key. |
| `↑` `↓` | palette | Moves through results. |
| `↵` | palette | Opens the highlighted result. |
| Arrow keys | data grid | Move between cells. The grid is **one tab stop** (roving `tabindex`): `Tab` enters it, `Tab` leaves it. |

There is currently **no user preference to turn single-key shortcuts off**, which is
what WCAG 2.1.4 asks for. See the gaps.

---

## 4. Known gaps

Ordered by how much they matter. Nothing here is disputed; it is all simply not done.

### Blocking AA

1. **WCAG 2.1.4 Character Key Shortcuts — `/` is bound on `document`.**
   `static/vantage/vantage.js` registers a `keydown` listener on `document`. It skips
   text inputs, textareas and `isContentEditable` targets, and the grid's "Keyboard
   shortcuts" switch turns it off — which satisfies 2.1.4's first remedy ("turn off").
   It did not always: the grid stores the switch as JSON `false` and this listener used
   to compare against `"0"`, so switching shortcuts off silenced everything except `/`.
   The `e` shortcut in `vantage-inline.js` (table-scoped: it fires only when the row
   itself has focus) ignored the switch entirely. Both are fixed and pinned in a real
   browser by `tests/test_browser.py::TestShortcutsPreference`. What remains is
   hardening, not a failure: there is no **remappable keymap**, and the tag/
   `isContentEditable` check approximates scoping — a custom element or shadow root
   with its own keyboard handling is invisible to it. `⌘K` requires a modifier and is
   exempt throughout.

2. **Links with no accessible name in table cells.** `list.html:286` and
   `partials/widget.html:87` wrap the rendered cell value in `<a class="v-cell-link">`.
   When the value is empty, `render.py`'s `EMPTY` constant emits the em-dash
   placeholder, so the link's entire accessible name is `—`. A screen reader announces
   "link, dash". **Fix:** fall back to the record's `__str__`, or add visually hidden
   text.

3. **The dark theme's `--border` and `--input` cannot be measured.** They are
   `oklch(1 0 0 / 12%)` and `/ 16%` — translucent, so their real contrast depends on
   what is behind them. Against `--background` in the light theme, `--input` computes
   to **1.27:1**, far under the 3:1 that 1.4.11 requires of a control boundary. Form
   fields are currently distinguished from the page mostly by their fill.
   `test_accessibility.py::test_alpha_tokens_are_knowingly_excluded` pins this hole
   open so it cannot be quietly forgotten.

Heading order on the list page was on this list in an earlier pass of this audit:
`partials/list_prefs.html`'s `<h3>` section titles had no `<h2>` above them. They now
sit under a wrapping `<h2 class="v-sr-only">` (`list_prefs.html:38`) — visually hidden,
since the rail already has a visible label, but present in the outline — confirmed by
rendering `/admin/<app>/<model>/` and walking the heading sequence live: `h1` → `h2`
→ `h3` × 6 → `h2` → `h3` × 3 → `h2` → `h2` → `h2`, no skip anywhere.

### Not yet verified

4. **The guard covers most, not all, of the pages this work owns.** `ALL_PAGES` in
   `tests/test_accessibility.py` was `login`, `logged_out`, `password_change`,
   `dashboard`, `detail`, a custom page, `403`, `404`, `500` when this gap was first
   written. It has since widened to include `list`, `add`, `delete` and `settings` too
   — confirmed by reading the fixture, not assumed. `import`, `audit` and `search` are
   still **not** in it. A one-off probe of those three found them clean on the
   mechanical checks above — but "clean when probed once" is not "guarded". Widening
   `ALL_PAGES` to cover them is the next unit of work.

5. **No screen-reader testing has been done.** Everything above is static analysis and
   parsed HTML. NVDA, JAWS and VoiceOver have not been run against a single page.
   Announcement order, table navigation and the palette's combobox are unverified in
   practice.

6. **Focus behaviour after JS DOM replacement is unverified.** The grid, the inline
   editor and the palette all replace DOM. Whether focus survives has not been tested
   in a browser here.

7. **No automated axe/Lighthouse run in CI.** The Python guard catches structural
   defects; it cannot catch computed contrast, overlapping focus, or reflow at 320 px.

### Found elsewhere in the package, not fixed here

A full audit of the templates and scripts outside this work's scope turned up roughly
sixty findings. They are grouped by criterion below; line numbers were correct at the
time of the audit and those files are under active change, so treat them as pointers.

**1.4.1 Use of Colour — meaning carried by colour alone.** `partials/stat_card.html`
(`data-tone` on the value, and a trend direction whose hidden text says "versus the
preceding period" but never "up" or "down"); `partials/widget.html` (`<dd data-tone>`);
`partials/inline_cell.html` (the staged/saved/rejected dot is `aria-hidden` and coloured
only); `partials/saved_views.html` (locked and shared are icon + `title` on a span);
`partials/analytics.html` (legend swatch keyed by `data-index` colour);
`form.html` and `confirm_delete.html` (message severity is `data-tone` only);
`pages/schema.html` (`fk` and `o2o` edges differ only in stroke colour).

**4.1.3 Status Messages — changes nothing announces.** The selection count and the bulk
action bar in `list.html` / `vantage.js`; the peek-drawer load failure and loading
state in `vantage-grid.js`; the reorder result in `vantage-listprefs.js`; the
character counter in `vantage-editors.js`. (The contrast readout and warning in
`settings.html` were on this list in an earlier pass; `data-preview-ratio` now
carries `role="status" aria-live="polite"`, confirmed by reading the rendered
template, so both are covered by the guarantee above instead.)

**2.1.1 Keyboard — server HTML that is unusable without JavaScript.** `list.html` ships
`tabindex="-1"` in the template on the select-all box, every row checkbox, every sort
link and every primary cell link. With the grid script absent, the table cannot be
sorted, selected or opened by keyboard at all. The roving tabindex should be applied by
`vantage-grid.js` at boot, not baked into the response.

**4.1.2 Name, Role, Value.** `list.html` puts `role="tab"` on two plain navigation links
with no tabpanel; `audit.html` puts `role="search"` on a form with no search field;
`partials/grid_controls.html` puts `role="dialog"` on a non-modal popover with no focus
trap or restore; `vantage-editors.js` sets `role="toolbar"` without roving tabindex, and
sets `aria-controls` to a possibly-empty id; `pages/schema.html` puts `role="img"` on an
SVG containing focusable links.

**2.4.11 Focus Not Obscured.** Three sticky bottom elements — the summary `<tfoot>`, the
inline changeset bar and the form save bar — with only `scroll-padding-block-start` set
globally. A focused control at the foot of the viewport can be covered.

**Focus management.** `vantage-grid.js` rebuilds the columns list on toggle and drops
focus to `<body>`; light-dismissing the columns popover loses focus the same way; the
error summaries in `form.html`, `import_upload.html` and `import_map.html` carry
`tabindex="-1"` but nothing ever focuses them after a failed POST.

### Deliberately out of scope for now

8. **WCAG 2.5.7 Dragging Movements is satisfied, and was checked.** No `draggable`
   attribute or `dragstart` handler exists anywhere in the package; both reorder
   surfaces ship pointer button alternatives (`vantage-grid.js`, `vantage-listprefs.js`)
   and the file drop zone keeps the real file input. This is recorded because "we never
   shipped drag" is the reason it passes, and adding drag later would silently break it.
9. **The 500 page cannot honour an explicit theme choice made in a cookie.** It reads
   `localStorage` only, because reading the cookie needs the request and the request
   is exactly what may be broken. It follows `prefers-color-scheme` otherwise.
10. **`lang` on the 500 page defaults to `en`** when rendered with no context, for the
    same reason.

---

## 5. Mounting the accessible auth pages

The panel's own sign-in, sign-out and password pages are what the guarantees above
apply to. A project-supplied `registration/login.html` is not covered by any of them.

```python
# urls.py
from django.urls import include, path
from vantage import site

urlpatterns = [
    path("accounts/", include("vantage.authviews")),   # login, logout, password_change
    path("admin/", site.urls),
]

handler403 = "vantage.authviews.permission_denied"
handler404 = "vantage.authviews.page_not_found"
handler500 = "vantage.authviews.server_error"
```

`vantage.authviews` declares no `app_name`, and its route names are Django's own
(`login`, `logout`, `password_change`), so `LOGIN_URL`, `redirect_to_login` and every
`{% url 'login' %}` already in a project keep working. It is a drop-in replacement for
`include("django.contrib.auth.urls")` for those three routes.
