# Browser tests

`tests/test_browser.py` drives a real Chromium, through
[Playwright](https://playwright.dev/python/), against every other test file's blind
spot: JavaScript. Django's test client — what the other ~3,400 tests use — parses HTML
but never runs a line of script, so client-side-only bugs are invisible to it. Three of
them already shipped this way: the bulk-action bar staying visible at zero selection
(a CSS specificity bug, correct markup the whole time), the top bar's settings screen
linking a stylesheet and a script that did not exist (two 404s a test client cannot
see, because it never fetches anything a `<script>` or `<link>` tag points at), and
focus not moving to the error summary after a failed save (a behaviour, not a
document, so nothing short of running the script would notice).

The module needs no demo project and no server you have to remember to start:
`pytest-django`'s `live_server` fixture boots a real HTTP server against the same
`tests.testapp` fixture models the rest of the suite uses, for the duration of each
test, and Playwright drives a browser against it. Self-contained and deterministic —
same as everything else in `tests/`.

## Running them

They are **not** part of a plain `uv run pytest` and do not need to be — install
Chromium and they run in place, wherever `tests/test_browser.py` sits in the suite:

```bash
uv sync --group dev --group browser
uv run playwright install chromium
uv run pytest -m browser
```

Without the `browser` group, or with it but no downloaded Chromium, the module skips
cleanly — at collection time if `playwright` itself is not installed, per test if it is
installed but `playwright install chromium` was never run. Either way, `uv run pytest`
(no `-m browser`) still collects the file and reports the skip; it never errors and
never silently vanishes from the run. A contributor who has not opted into the browser
group gets an unmodified, green suite.

CI runs them in their own job (`.github/workflows/ci.yml`, `browser`), separate from
the main test matrix so that job's runners never need Chromium at all.

## What it covers

- Focus lands on `#v-form-summary` after a failed save — the fix in `vantage.js`'s
  `focusErrorSummary` that nothing else in the suite pins.
- No console errors and no failed requests (a missing static file is a failed request,
  and fails the build) on the dashboard, a list, a detail page, a form, the site
  settings screen, the top-bar arrangement screen, a delete confirmation, and the login
  page.
- No element carrying `hidden` computes to anything other than `display: none` on any
  of those same pages — the generalised form of the action-bar bug.
- The relation combobox: typing filters the list, arrows highlight a row, Enter
  commits it, a chip appears, and the real `<select>` — never replaced, only driven —
  ends up holding the value.
- The account menu: Enter opens it on the first item, the arrows move between items,
  Escape closes it and returns focus to the trigger.
- The grid's roving tabindex: exactly one element is tabbable at rest and after arrow
  navigation, and the column-resize handle's Left/Right exception to that contract is
  scoped to itself, with Escape handing focus back to the row.
- Column pinning under `dir="rtl"`, against a table forced to genuinely overflow its
  container (not asserted from CSS source, from real `getBoundingClientRect()` values
  at several real scroll positions): the pinned column stays flush with the
  inline-start (right, under RTL) edge throughout.

## Why they are written the way they are

- **No arbitrary sleeps.** Every wait is on a condition — an element appearing, an
  attribute changing, a `page.wait_for_function` predicate, `networkidle` — never a
  fixed delay.
- **A fresh Chromium per test, not once for the module.** `live_server` requires
  transactional tests, so the database is flushed between tests; each one creates its
  own user and fixture rows. Playwright's sync API leaves a running event loop on
  whichever thread called `chromium.launch()`, which trips Django's async-safety guard
  for any ORM query issued afterwards on that thread — verified directly, not assumed.
  A session-scoped browser would poison every database-touching fixture from the
  second test onward. See the comment at the top of `tests/test_browser.py` for the
  full reasoning; the short version is that data fixtures are always a dependency *of*
  the Playwright fixtures, never requested alongside them in an order someone could
  reshuffle.
- **Cookie-based login, not the login form, for every test except the ones actually
  about the login page.** A session created directly through
  `django.contrib.sessions.backends.db.SessionStore` and handed to the browser as a
  cookie is faster than driving the form each time and does not make sixteen other
  tests brittle against the login template's markup.
