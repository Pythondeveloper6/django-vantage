# Internationalization

The package is structured for i18n throughout — around 570 `gettext_lazy` and
`{% translate %}` call sites, and RTL that works from day one because the
stylesheets use logical CSS properties rather than a mirrored build (see
[Theming → Right to left](theming.md#right-to-left)). This page is about the other
half: the actual translation catalogues that make that scaffolding real, what ships
today, and what a project has to do to use it.

## What ships

`src/vantage/locale/` carries two locales, compiled and ready to use:

| Locale | Catalogue | Status |
|---|---|---|
| `en` | `locale/en/LC_MESSAGES/django.po` | Complete — an identity catalogue (every `msgstr` equals its `msgid`), since the source strings are already English. Exists mainly to prove the extract → compile → ship pipeline round-trips, and as a base for a project that wants to fork it into British or another English variant. |
| `ar` | `locale/ar/LC_MESSAGES/django.po` | **Partial, on purpose.** High-traffic strings are translated: page chrome (skip link, nav landmarks, breadcrumb), buttons (save, cancel, discard, export), the list page's empty states and search/filter controls, the form page's unsaved-changes and validation banners, delete confirmations, the top bar and account menu, and one fully-translated plural (`ngettext`) entry. Everything else is left with an empty `msgstr`, which `gettext` falls back to the English source for — cleanly, and without a project ever seeing a blank label. A wrong guessed translation is a bug someone has to find later; an empty one is not. |

Both `.mo` files are compiled and shipped in the wheel (`pyproject.toml`'s
`[tool.hatch.build.targets.wheel] artifacts = ["*.mo"]` forces this even though
`.mo` is also committed to git — see the comment in `.gitignore` for why both).

Arabic specifically, not a placeholder locale, because RTL support that only Django's
`get_language_bidi()` plumbing exercises but no shipped `.po` ever activates is a claim
without a demonstration. Loading the panel with Arabic active is the proof.

## Using the shipped locales in a project

Vantage needs nothing beyond what any Django project needs for i18n:

```python
# settings.py
USE_I18N = True                 # on by default; listed for clarity
LANGUAGE_CODE = "ar"            # or "en", or leave unset for English

# Only needed if the language should be negotiated per request (Accept-Language
# header, a session value, a URL prefix, …) rather than fixed for the whole project:
MIDDLEWARE = [
    "django.middleware.locale.LocaleMiddleware",
    # ... the rest of the project's middleware
]
```

Setting `LANGUAGE_CODE = "ar"` (or otherwise activating Arabic — `LocaleMiddleware`,
`django.utils.translation.activate("ar")`, `translation.override("ar")` around a
request) is also what flips `<html dir="rtl">` and the whole layout, with no separate
RTL switch to remember. See [Theming → Right to left](theming.md#right-to-left).

`vantage`'s own locale directory is discovered automatically — Django looks for a
`locale/` folder inside every installed app, so nothing needs adding to
`LOCALE_PATHS` for the shipped catalogues specifically. Add `LOCALE_PATHS` only for
the project's *own* translations (its own app labels, its own custom pages).

## Filling in the rest of the Arabic catalogue

`locale/ar/LC_MESSAGES/django.po` is a normal gettext `.po` file — edit it with any
PO editor (Poedit, Lokalize, or a text editor) or by hand, then recompile:

```bash
cd src/vantage
django-admin compilemessages -l ar
```

Run from `src/vantage/` specifically, so the `#:` source references inside the `.po`
stay package-relative (`actions.py:85`, not
`/home/you/django-vantage/src/vantage/actions.py:85`) — portable across every
machine and CI runner that touches the file.

## Adding a new locale

```bash
cd src/vantage
django-admin makemessages -l fr              # Python + templates
django-admin makemessages -d djangojs -l fr  # JavaScript — see the caveat below
django-admin compilemessages -l fr
```

`makemessages` needs `DJANGO_SETTINGS_MODULE` set and the repository root on
`PYTHONPATH` to run outside a configured project, e.g.:

```bash
DJANGO_SETTINGS_MODULE=tests.settings PYTHONPATH=/path/to/django-vantage \
  django-admin makemessages -l fr
```

Translate the strings that matter for a working panel — you do not have to fill in
all ~890 entries before a locale is useful. Leave the rest with an empty `msgstr`;
that falls back to English rather than shipping a guess. Re-run `compilemessages`
after every edit; the `.po` is not read at runtime, only the compiled `.mo` is.

## JavaScript strings

The runtime under `src/vantage/static/vantage/*.js` — the combobox, the inline grid
editor, the upload widget, the palette — has plenty of user-facing text: the
combobox's "N results shown" and "No matches for…", the inline editor's "N changes"
and "Discard N unsaved changes?". All of it goes through
[Django's JavaScript catalogue](https://docs.djangoproject.com/en/stable/topics/i18n/translation/#internationalization-in-javascript-code),
so the panel does not fall back to English the moment it starts speaking to you
interactively.

The catalogue is mounted at `_/jsi18n/` with `packages=["vantage"]`, which scopes it
to this package's own strings — a project's translations are its own view to add, and
serving them from here would make one catalogue responsible for two things. It is
cached for a day: the response is identical for everyone in a given language, and
generating it walks every `.mo` file's plural forms. Django keys that cache by the
active language automatically when `USE_I18N` is on, so there is no Vary header to
get wrong.

`base.html` loads the catalogue and then `vantage-i18n-fallback.js`, in that order and
neither deferred. The fallback defines `gettext`, `ngettext`, `interpolate`,
`pgettext`, `npgettext` and `gettext_noop` as pass-throughs **only if the catalogue
did not already define them** — so a project that has not run `collectstatic`, or
whose proxy has eaten the route, gets English rather than a `gettext is not defined`
that stops every script on the page.

Plurals go through `ngettext`, and interpolation through `interpolate` with whole
sentences: `gettext("Showing %s of %s")`, never a translated fragment concatenated to
a number, because word order is not universal. Arabic is the useful test here — it has
six plural forms, and the inline editor's change counter is translated across all of
them.

Deliberately left untranslated: single-glyph button labels (B, I, •, ↑, ↓), keyboard
key names ("Ctrl+B"), and byte-unit abbreviations (B/KB/MB), matching how the Python
side treats the same cases.

To extract them after a change:

```bash
cd src/vantage
django-admin makemessages -d djangojs -l ar
django-admin compilemessages
```

## Testing

`tests/test_i18n.py` renders real pages under
`django.utils.translation.override("ar")` and asserts on the actual response body —
`dir="rtl"` on `<html>`, a known string coming back in Arabic, an untranslated string
falling back to English rather than going blank — plus every one of Arabic's six
`ngettext` plural categories (zero, one, two, few, many, other), and that a built
wheel really contains the compiled `.mo` files. A test that only checks the
catalogue file exists on disk would pass even if the translation pipeline were
completely broken; none of these do.
