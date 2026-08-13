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

### The live region, specifically

`#v-toasts` is rendered **unconditionally**, even with nothing in it. A live region
created in the same DOM mutation as its first message is not announced — assistive
technology has to have been observing the node beforehand. A container wrapped in
`{% if messages %}` looks correct, tests green, and announces nothing. It collapses to
zero height when empty, so it costs nothing to leave in place.

Script that needs to announce something should append into `#v-toasts`, or write into
it directly. Do not create a new live region per feature.

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
   `static/vantage/vantage.js:75` registers a `keydown` listener on `document` and
   guards it with `activeElement.tagName !== "INPUT"`. The design spec (§10) calls this
   out by name: *"a fragile approximation, not a scoping mechanism."* It misses
   `contenteditable` inside a widget that stops propagation, custom elements, and any
   shadow root. Two of the three permitted remedies are missing: there is no on/off
   preference and no remappable keymap. **Fix:** scope the listener to a container, or
   ship the preference. Related: the `e` shortcut in `vantage-inline.js` *is* correctly
   table-scoped but ignores the `vantage-grid-shortcuts` preference that
   `vantage-grid.js` honours, so switching shortcuts off does not switch that one off.
   `⌘K` requires a modifier and is exempt.

2. **Heading order breaks on the list page.** `partials/list_prefs.html` emits `<h3>`
   at lines 51, 103, 145, 204, 272 and 361 with no `<h2>` above them; the nearest
   heading is the page `<h1>` in `list.html:15`. Confirmed by rendering the page and
   parsing it. **Fix:** promote them to `<h2>`, or give the rail a wrapping `<h2>`.

3. **Links with no accessible name in table cells.** `list.html:262` and
   `partials/widget.html:87` wrap the rendered cell value in `<a class="v-cell-link">`.
   When the value is empty, `render.py` emits the em-dash placeholder, so the link's
   entire accessible name is `—`. A screen reader announces "link, dash". **Fix:** fall
   back to the record's `__str__`, or add visually hidden text.

4. **The dark theme's `--border` and `--input` cannot be measured.** They are
   `oklch(1 0 0 / 12%)` and `/ 16%` — translucent, so their real contrast depends on
   what is behind them. Against `--background` in the light theme, `--input` computes
   to **1.27:1**, far under the 3:1 that 1.4.11 requires of a control boundary. Form
   fields are currently distinguished from the page mostly by their fill.
   `test_accessibility.py::test_alpha_tokens_are_knowingly_excluded` pins this hole
   open so it cannot be quietly forgotten.

### Not yet verified

5. **The guard covers only the pages this work owns.** `ALL_PAGES` in
   `tests/test_accessibility.py` is `login`, `logged_out`, `password_change`,
   `dashboard`, `detail`, a custom page, `403`, `404`, `500`. The list, form, delete,
   import, audit, search and settings pages are **not** in it. A one-off probe of those
   pages found them clean on the mechanical checks above, except for the two entries
   already listed — but "clean when probed once" is not "guarded". Widening `ALL_PAGES`
   is the next unit of work.

6. **No screen-reader testing has been done.** Everything above is static analysis and
   parsed HTML. NVDA, JAWS and VoiceOver have not been run against a single page.
   Announcement order, table navigation and the palette's combobox are unverified in
   practice.

7. **Focus behaviour after JS DOM replacement is unverified.** The grid, the inline
   editor and the palette all replace DOM. Whether focus survives has not been tested
   in a browser here.

8. **No automated axe/Lighthouse run in CI.** The Python guard catches structural
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
action bar in `list.html` / `vantage.js`; the contrast readout and warning in
`settings.html`; the peek-drawer load failure and loading state in `vantage-grid.js`;
the reorder result in `vantage-listprefs.js`; the character counter and drop-zone
filename in `vantage-editors.js`.

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

**Headings.** `partials/widget.html` and `partials/analytics.html` use
`<span class="v-card-title">` where a heading belongs, so dashboard cards and charts
contribute nothing to the outline.

### Deliberately out of scope for now

9. **WCAG 2.5.7 Dragging Movements is satisfied, and was checked.** No `draggable`
   attribute or `dragstart` handler exists anywhere in the package; both reorder
   surfaces ship pointer button alternatives (`vantage-grid.js`, `vantage-listprefs.js`)
   and the file drop zone keeps the real file input. This is recorded because "we never
   shipped drag" is the reason it passes, and adding drag later would silently break it.
10. **The 500 page cannot honour an explicit theme choice made in a cookie.** It reads
    `localStorage` only, because reading the cookie needs the request and the request
    is exactly what may be broken. It follows `prefers-color-scheme` otherwise.
11. **`lang` on the 500 page defaults to `en`** when rendered with no context, for the
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
