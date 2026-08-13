# Theming

[← Index](index.md)

One colour drives the whole palette. Everything else is derived from it, in both
themes, with the contrast checked numerically rather than by eye.

```python
# settings.py
VANTAGE = {
    "SITE_TITLE":  "Acme",
    "SITE_ACRONYM": "AC",
    "LOGO":        "img/acme.svg",
    "LOGO_DARK":   "img/acme-light.svg",
    "FAVICON":     "img/favicon.png",
    "BRAND_COLOR": "#0ea5e9",
    "BRAND_RADIUS": "0.5rem",
    "THEME":       "system",
}
```

Every one of those can also be changed **inside the panel** by a superuser, without a
deploy. See [the settings screen](#the-settings-screen).

## `BRAND_COLOR` and the derived palette

Supplying one colour and expecting the panel to look like yours means more than tinting
a button. Every interactive surface needs a matching hover, a focus ring, a subtle
accent, and — the part usually got wrong — a foreground colour that is actually
readable on top of it, in both themes, for *any* hue you might pick.

### Why OKLCH

Working in OKLCH is what makes this tractable. It is perceptually uniform, so holding
lightness constant while changing hue produces colours that genuinely look equally
light, which sRGB emphatically does not. That lets the derivation pick lightness
*targets* per role and per theme and trust them across the hue circle.

The whole conversion chain — sRGB transfer function, OKLab matrices, gamut clamping,
WCAG contrast — is implemented in `vantage.color` with no dependencies.

### What is derived

Six tokens per theme:

| Token | |
|---|---|
| `--primary` | the brand surface: buttons, active states, chart fills |
| `--primary-foreground` | text on top of it, guaranteed to clear WCAG AA |
| `--primary-hover` | moved away from the page background: darker on light, lighter on dark |
| `--ring` | the focus ring |
| `--accent` | a heavily desaturated tint, for selected rows and subtle fills |
| `--accent-foreground` | text on the accent |

Only these vary with `BRAND_COLOR`. **The neutral greys are fixed**, and that split
matters: a site owner supplying one colour should get a branded panel, not a panel where
their colour has leaked into the page background and the table borders.

### The contrast guarantee

The derived primary and its foreground clear **WCAG AA at 4.5:1 for every input hue**.
The tests sweep the hue circle to prove it.

The mechanism: aim the primary at a lightness target (0.55 on light, 0.70 on dark),
pick whichever of near-white or near-black reads better on it, and if that pair is
still below 4.5:1, walk the lightness away from the target in both directions and take
whichever reaches AA with the **smaller** move.

Some hues — saturated yellows especially — simply cannot hold text at a mid lightness,
so the primary has to move. Moving it is the correct failure mode; shipping an
unreadable button is not.

Measured:

```pycon
>>> derive_palette("#0ea5e9")            # light theme
{'primary': 'oklch(0.5500 0.1208 237.32)',
 'primary-foreground': 'oklch(0.9850 0.0072 237.32)', ...}
# contrast 4.57 : 1

>>> derive_palette("#facc15")            # a saturated yellow, light theme
# primary oklch(0.5500 0.1126 91.94), foreground oklch(0.9850 …) — contrast 4.66 : 1

>>> derive_palette("#0d9488")            # teal; the target had to move
# primary oklch(0.5400 …) rather than 0.5500 — contrast 4.62 : 1

>>> derive_palette("#0ea5e9", dark=True)
# primary oklch(0.7000 0.1479 237.32), foreground oklch(0.1600 …) — contrast 7.41 : 1
```

Two details in the small print. The foreground is **not** pure white or black: a hint of
the brand hue keeps the pairing from looking like an unstyled default and costs almost
nothing in contrast. And gamut clamping gives up **chroma** while holding lightness and
hue, because a slightly duller colour still reads as the brand whereas a shifted
lightness breaks the very guarantee this exists to provide.

## Tokens

The token contract is shadcn/ui's, because it is well designed and widely understood: a
small set of semantic **role** names rather than a palette of colour names. No component
ever hard-codes a colour, so the whole appearance is controlled from one place.

Twenty-seven tokens per theme — twenty-one fixed neutrals plus the six derived above:

```
background            foreground
card                  card-foreground
popover               popover-foreground
primary               primary-foreground     primary-hover
secondary             secondary-foreground
muted                 muted-foreground
accent                accent-foreground
destructive           destructive-foreground
success               success-foreground
warning               warning-foreground
border   input   ring
sidebar  sidebar-foreground  sidebar-border
```

Plus `--radius`, from `BRAND_RADIUS`.

The neutrals are very slightly cool rather than pure grey, which is what stops a panel
looking like an unstyled document. The dark background is a near-black with a trace of
the same cool hue rather than `#000`, which keeps elevation readable and avoids the
smeary look on OLED.

## Light, dark and system

`VANTAGE["THEME"]` is `"light"`, `"dark"` or `"system"` (the default).

Both themes are **always emitted**, in this order:

```css
:root { --radius: 0.5rem; --background: oklch(1 0 0); ... }        /* light */

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { ... }                          /* system */
}

:root[data-theme="dark"] { ... }                                   /* explicit */
```

Light lives on **bare `:root`**, so a page with no theme information still renders
correctly. Dark is applied under both an explicit `data-theme="dark"` and a
`prefers-color-scheme` media query guarded with `:not([data-theme="light"])`.

> Defining a colour only inside a media query is the bug that makes a theme toggle work
> in one direction and not the other.

The stylesheet is **inlined into `<head>`** rather than served as a file, because the
tokens depend on project settings: a static asset would have to be regenerated on every
settings change, and would be wrong for any project running more than one brand.

### No theme flash

A tiny blocking script in `<head>` stamps the saved theme onto `<html>` before first
paint:

```js
(function () {
  try {
    var saved = localStorage.getItem('vantage-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {}
})();
```

It has to be inline and blocking; a deferred script is too late and the page renders
light, then flips.

Server-side, a `vantage-theme` cookie set by the toggle wins over the project default.
The localStorage read covers the very first paint, before any cookie exists. Set
`THEME_TOGGLE = False` to remove the control and pin everyone to `THEME`.

## Right to left

RTL works from day one and needs no configuration. The layout direction comes from
Django's own `get_language_bidi()`, so `<html dir="rtl">` is set whenever the active
language is right-to-left. The stylesheets use logical properties, so nothing needs a
mirrored build.

Set `LANGUAGE_CODE = "ar"` (or use `LocaleMiddleware`) and the panel flips.

## Logos and marks

| Setting | |
|---|---|
| `LOGO` | Shown in the sidebar |
| `LOGO_DARK` | Used in the dark theme; falls back to `LOGO` when unset |
| `FAVICON` | The browser tab icon |
| `SITE_ACRONYM` | Two letters for the collapsed sidebar; derived from `SITE_TITLE` when unset |

Each accepts a static path (`"img/acme.svg"`), resolved with `{% static %}`, or an
already-resolved URL — `/…`, `//…`, `http://…`, `https://…` or a `data:` URI — which
passes through untouched. That second case is what makes an uploaded logo work:
`{% static %}` would mangle `/media/vantage/logo.svg` into
`/static/media/vantage/logo.svg`.

`SITE_ACRONYM` takes the first letter of each of the first two words —
`"Acme Corporation"` → `AC` — or the first two characters of a single-word title.

## Custom CSS

```python
VANTAGE = {"CUSTOM_CSS": "css/admin-overrides.css"}
```

A stylesheet loaded after Vantage's own, so it can override anything. Because every
component reads tokens, the most useful thing to put in it is usually a token
redefinition rather than a component rule:

```css
:root {
  --radius: 0;
  --sidebar: oklch(0.98 0.01 250);
}

:root[data-theme="dark"],
:root:not([data-theme="light"]) {
  --sidebar: oklch(0.15 0.01 250);
}
```

Define a token in all three places — bare `:root`, the media query and the explicit
`[data-theme="dark"]` selector — or the toggle will work in one direction only.

`CUSTOM_CSS` is deliberately **not** editable from the settings screen. It is code, not
configuration.

## The settings screen

`/admin/_/settings/`. A superuser can change the branding, appearance and behaviour of
the panel without a deploy, and the stored value sits **underneath** the file settings:

```
SiteConfiguration row  >  settings.VANTAGE  >  conf.DEFAULTS
```

Three sections:

| Section | Fields |
|---|---|
| Branding | site title, short mark, logo, dark logo, favicon, login image |
| Appearance | brand colour, corner radius, default theme, theme toggle |
| Behaviour | environment banner, rows per page, command palette, read-only mode |

Each section saves and resets independently, and the reset acts on exactly the fields
the card shows, so a button can never clear something it does not display.

### What it deliberately cannot change

An explicit allowlist, not "every key with a matching column", because the line it
draws is a security boundary and it should be readable in one screenful.

Absent on purpose: `AUTO_REGISTER`, `EXCLUDE`, `INCLUDE_ONLY`, `NAV`, `NAV_UNLISTED`,
`DASHBOARD`, `PERMISSIONS`, `SETTINGS_PERMISSION`, `LIST_PREFS_PERMISSION`,
`AUDIT_LOG`, `CUSTOM_CSS`, `IMPORT_MAX_BYTES`, `IMPORT_MAX_ROWS`. Those are code, not
configuration: they decide what exists and who may reach it.

`SETTINGS_PERMISSION` in particular is never editable from the UI — that would be a
self-grant.

### Who may open it

Superusers, unless `VANTAGE["SETTINGS_PERMISSION"]` widens it. Superuser-only is the
honest default: this screen can rebrand the site, switch the panel to read-only, and
upload a file that every other user's browser will load.

```python
VANTAGE = {
    # a permission string...
    "SETTINGS_PERMISSION": "vantage.change_siteconfiguration",
    # ...or a callable taking the request
    # "SETTINGS_PERMISSION": lambda request: request.user.groups.filter(name="ops").exists(),
}
```

### Three things the screen does that a generic model form cannot

**Show what "empty" means.** Every control displays the value it falls back to when
cleared, because "clear this field" is only a safe instruction if you can see where it
lands. That value is the cascade with the database taken out of it.

**Preview the brand colour honestly.** The preview is rendered by the same colour code
that will render the panel — fetched from the view rather than reimplemented in
JavaScript, because two implementations of a colour space is two answers to the same
question.

**Say when the colour was adjusted.** Silently shipping a different colour from the one
that was typed is the kind of thing that gets blamed on the browser for a week. When the
derivation had to move the lightness, the screen says so:

> `#facc15` cannot carry readable text at the lightness this panel uses for buttons: it
> scores 3.1:1 against the text colour that would sit on it, and WCAG AA needs 4.5:1.
> The derived button colour was darkened until it reached 4.7:1. Your colour is stored
> exactly as you entered it.

### Uploads are treated as hostile

`FileField` rather than `ImageField`, because Pillow is not a dependency and never will
be for this — which means the validation Pillow would have done has to be written out.

- Extensions: `.svg`, `.png`, `.jpg`, `.jpeg`, `.webp`. Nothing else.
- Size: 2 MiB. A logo is a logo; anything larger is a mistake or an attempt to fill a
  disk.
- **SVGs are scanned and refused**, not sanitised. An SVG is served from the panel's own
  origin, so a `<script>` inside one runs with the session cookie of whoever views the
  page — and the logo is on every screen, so that is everyone. Refused constructs:
  `<script>`, inline `on…` handlers, an animation that writes an event handler,
  `javascript:` URLs, and embedded documents (`<foreignObject>`, `<iframe>`, `<embed>`,
  `<object>`).

  A rejection filter rather than a rewriter, because a rewriter that is wrong once is
  worse than a refusal that is occasionally annoying. Serving media from a separate
  origin remains the defence in depth this cannot replace.

- Validation runs **on the field**, not only in the form, so a shell session or a data
  migration cannot route around it.

`BRAND_RADIUS` is validated as a CSS length (`0`, `0.5rem`, `8px`, `50%`) because it is
interpolated straight into a `<style>` block, and is therefore not free text.

## The environment banner

```python
VANTAGE = {"ENVIRONMENT": "Staging — data here is not real"}
```

A strip across the top of every page. Setting it to an empty string from the settings
screen hides a banner that `settings.py` asked for, which is exactly why every stored
column is nullable and `NULL` means "not overridden" — a blank value is a legitimate
override.

## See also

- [Settings](settings.md) — every key, with defaults
- [Analytics](analytics.md#charts) — charts follow `--primary` and `currentColor`
- [Deployment](deployment.md#static-files) — serving the CSS
