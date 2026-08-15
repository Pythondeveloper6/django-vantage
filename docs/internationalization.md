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

## Known gap: JavaScript strings are not wired into i18n yet

The runtime under `src/vantage/static/vantage/*.js` (the combobox, the inline grid
editor, the upload widget, …) has user-facing strings — the combobox's "N results
shown", "No matches for…"; the inline editor's "N changes", "Discard N unsaved
changes?" — that are plain string literals today, not run through
[Django's JavaScript i18n](https://docs.djangoproject.com/en/stable/topics/i18n/translation/#internationalization-in-javascript-code)
(`django.jsi18n`, `gettext()`/`ngettext()` in JS) or any other translation
mechanism. Confirmed by running
`django-admin makemessages -d djangojs` against every file in that directory: it
extracts nothing, because there is nothing marked as translatable to extract.

Wiring that up means both a source change (marking those strings for extraction —
typically the `{% get_current_language %}` + `JavaScriptCatalog` view pattern, or
pre-rendering the strings server-side into `data-*` attributes the script reads) and
a template change to serve the catalogue, both outside what this page's scope
covers. Tracked here so it is not mistaken for done.

## Testing

`tests/test_i18n.py` renders real pages under
`django.utils.translation.override("ar")` and asserts on the actual response body —
`dir="rtl"` on `<html>`, a known string coming back in Arabic, an untranslated string
falling back to English rather than going blank — plus every one of Arabic's six
`ngettext` plural categories (zero, one, two, few, many, other), and that a built
wheel really contains the compiled `.mo` files. A test that only checks the
catalogue file exists on disk would pass even if the translation pipeline were
completely broken; none of these do.
