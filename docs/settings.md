# Settings

[← Index](index.md)

Everything a project owner can configure lives in a single `VANTAGE` dict in Django
settings.

```python
# settings.py
VANTAGE = {
    "SITE_TITLE": "Acme",
    "BRAND_COLOR": "#0ea5e9",
    "THEME": "system",
}
```

## Typos are errors

A key absent from the recognised set is a typo, and it is refused at once — both when a
setting is first read and by `manage.py check` (`vantage.E002`):

```pycon
>>> VANTAGE = {"SITE_TTLE": "Acme"}
ImproperlyConfigured: Unrecognised key(s) in the VANTAGE setting:
  'SITE_TTLE' is not a Vantage setting. Did you mean 'SITE_TITLE'?
```

Values with a fixed set of choices are checked too (`vantage.E003`):

```pycon
>>> VANTAGE = {"THEME": "sepia"}
ImproperlyConfigured: VANTAGE['THEME'] must be one of light, dark, system; got 'sepia'.
```

So are the two values rendered into every page's inline stylesheet, where a typo would
otherwise fail every render, the sign-in page included: `BRAND_COLOR` must be a hex
colour (`vantage.E004`) and `BRAND_RADIUS` a CSS length (`vantage.E005`), by the same
validators the settings screen applies to what it stores.

## Where a value comes from

```
SiteConfiguration row  >  settings.VANTAGE  >  built-in default
```

The database layer sits **underneath** attribute access rather than beside it, so every
existing read picks up a change made in the panel without knowing a database exists. It
is lazy (nothing touches the database at import or at `ready()`), failure-tolerant (a
missing table resolves silently to the file settings, which is what makes the migration
that creates the table possible to run), and cached per request.

Not every key can be changed from the UI — see
[what the settings screen deliberately cannot change](theming.md#what-it-deliberately-cannot-change).
The **UI** column below says which ones can.

## Branding

| Key | Default | UI | Effect |
|---|---|---|---|
| `SITE_TITLE` | `"Django Vantage"` | ✓ | Name in the sidebar, the page heading and the browser tab |
| `SITE_ACRONYM` | derived from `SITE_TITLE` | ✓ | Two-letter mark for the collapsed sidebar. `"Acme Corporation"` → `AC` |
| `LOGO` | `None` | ✓ | Static path or resolved URL, shown in the sidebar |
| `LOGO_DARK` | falls back to `LOGO` | ✓ | Used in the dark theme |
| `FAVICON` | `None` | ✓ | Browser tab icon |
| `BRAND_COLOR` | `"#6366f1"` | ✓ | The one colour the whole palette is derived from |
| `BRAND_RADIUS` | `"0.5rem"` | ✓ | `--radius`. Validated as a CSS length |
| `THEME` | `"system"` | ✓ | `"light"`, `"dark"` or `"system"` |
| `THEME_TOGGLE` | `True` | ✓ | Whether users may switch theme. `False` pins everyone to `THEME` |
| `UI_THEME` | `"vantage"` | ✓ | The look: a preset name. See [UI theme presets](ui-themes.md) |
| `UI_THEMES` | `{}` |  | Custom presets; omitted keys inherit from `vantage` |
| `UI_THEME_TOGGLE` | `True` | ✓ | Whether each person may choose their own preset |
| `CUSTOM_CSS` | `None` | — | A stylesheet loaded after Vantage's own |
| `LOGIN_IMAGE` | `None` | ✓ | Image beside the sign-in form, when you mount [Vantage's auth views](getting-started.md#signing-in) |

Details: [Theming](theming.md).

## Discovery

| Key | Default | Effect |
|---|---|---|
| `AUTO_REGISTER` | `True` | Register every eligible model automatically |
| `EXCLUDE` | `[]` | App labels (`"sessions"`) or model labels (`"blog.Draft"`) to skip |
| `INCLUDE_ONLY` | `None` | When set, **only** these are registered and `EXCLUDE` is ignored |

Both accept app labels and model labels, matched case-insensitively.

```python
VANTAGE = {"EXCLUDE": ["django_celery_results", "blog.Draft"]}
```

Auto-registration always skips abstract models and auto-created M2M through-tables, plus
a fixed list of framework plumbing: `sessions.session`, `contenttypes.contenttype`,
`auth.permission`, `admin.logentry`, and Vantage's own tables. `auth.User` and
`auth.Group` are deliberately **not** on that list — they are among the most useful
things to administer.

Vantage's own models are excluded for reasons rather than tidiness: a generic CRUD form
over `SiteConfiguration` would route around the superuser gate on the settings screen,
an editable audit log is not an audit log, and a generic form over a saved view routes
around every permission rule the feature has.

Proxy models are kept: they are a normal way to give a model a second,
differently-configured presence in the panel.

Auto-registration runs **after** `panel.py` discovery and never clobbers an existing
registration.

## Navigation

| Key | Default | Effect |
|---|---|---|
| `NAV` | `None` | Declare the sidebar instead of deriving it |
| `NAV_UNLISTED` | `"append"` | What happens to registered models `NAV` does not mention: `"append"` or `"hide"` |
| `DASHBOARD` | `None` | Dotted path to a `Dashboard` subclass |

With `NAV` unset, navigation is **derived**: registering a model is enough to make it
reachable, grouped by app, with groups sorted by the lowest `order` of any panel in them
and items sorted alphabetically.

Setting `NAV` switches to **declared**:

```python
VANTAGE = {
    "NAV": [
        {"route": "vantage:dashboard", "label": "Overview", "icon": "layout-dashboard"},
        {"group": "Commerce", "items": [
            {"model": "shop.Order", "badge": "live"},
            {"model": "shop.Product", "label": "Catalogue"},
            "shop.Category",
        ]},
        {"group": "System", "items": [
            {"route": "vantage:settings", "label": "Settings", "icon": "sliders"},
            {"page": "schema", "label": "Schema", "icon": "share-2"},
        ]},
        {"group": "Elsewhere", "icon": "shield", "items": [
            {"url": "https://status.example.com", "label": "Status"},
        ]},
    ],
}
```

Each item needs exactly one of:

| Key | Points at |
|---|---|
| `model` | `"app_label.ModelName"`, matched case-insensitively |
| `page` | A registered page's slug |
| `route` | A URL name, with optional `args` |
| `url` | A literal URL; `external` is inferred from the scheme |

Plus optional `label`, `icon` and `badge`. A bare string is shorthand for
`{"model": "..."}`. A top-level item outside any group renders without a heading — the
usual shape for a Dashboard link. A group takes `group` (or `label`), `icon` and
`collapsed`.

Three properties:

**Declaring a nav can reorder, rename and hide — it can never grant access.** Every
entry still passes through the permission layer, so an entry for a model the user cannot
open does not render.

**Unmentioned models still appear** by default, so registering a new model never makes
it silently unreachable. Set `NAV_UNLISTED = "hide"` for a strict sidebar.

**A malformed entry raises; an unresolvable one does not.** A typo is something the
author wants to hear about, and `manage.py check` reports it at startup
(`vantage.E020`, `vantage.E021`, `vantage.W020`) rather than leaving it to a request.
But a sidebar that 500s because someone lost a permission is a poor trade for
strictness, so an item that cannot be shown is omitted.

## Behaviour

| Key | Default | UI | Effect |
|---|---|---|---|
| `PERMISSIONS` | `None` | — | Dotted path to a `PanelPermissions` subclass |
| `SETTINGS_PERMISSION` | `None` | — | Who may open the settings screen. `None` means superusers only |
| `LIST_PREFS_PERMISSION` | `None` | — | Who may save a list's preferences **for everyone**. `None` means superusers only |
| `ROLES_PERMISSION` | `None` | — | Who may open the [roles & permissions screen](roles.md). `None` means superusers only |
| `READ_ONLY` | `False` | ✓ | `True`, or a string that doubles as the explanation shown to users |
| `ENVIRONMENT` | `None` | ✓ | A banner across the top of every page |
| `AUDIT_LOG` | `True` | — | Record what people do in the panel |
| `COMMAND_PALETTE` | `True` | ✓ | The ⌘K palette |
| `DEFAULT_PER_PAGE` | `50` | ✓ | Rows per list page, unless a panel sets `list_per_page` |
| `MAP_TILE_URL` | `None` | — | Opt-in raster tiles for the [map view](list-views.md#map), e.g. `"https://tile.openstreetmap.org/{z}/{x}/{y}.png"`. Must contain `{z}`, `{x}` and `{y}`. `None` draws a dependency-free SVG plot and fetches nothing |
| `RELATION_PICKER` | `"always"` | — | `"always"` or `"threshold"`. Whether a single-valued relation gets the searchable picker regardless of row count, or only above 50 — see [Relations and the search threshold](forms.md#relations-and-the-search-threshold) |
| `IMPORT_MAX_BYTES` | `5242880` (5 MiB) | — | Largest file the CSV importer will read |
| `IMPORT_MAX_ROWS` | `5000` | — | Most rows the CSV importer will read |

The login throttle, rate limits and upload ceiling are behaviour keys too, documented
with their defaults in [Security](security.md#installing-the-middleware) — see
[Two settings layers](security.md#two-settings-layers) for why their defaults live
there.

`SETTINGS_PERMISSION`, `LIST_PREFS_PERMISSION` and `ROLES_PERMISSION` each accept a permission string or a
callable taking the request:

```python
VANTAGE = {
    "SETTINGS_PERMISSION": "vantage.change_siteconfiguration",
    "LIST_PREFS_PERMISSION": lambda request: request.user.groups.filter(name="ops").exists(),
}
```

None is editable from the UI. `SETTINGS_PERMISSION` and `ROLES_PERMISSION` in
particular would be self-grants.

`READ_ONLY` is enforced in the permission layer, not in the views, so it covers every
write path including ones added later — and it is checked **before** the superuser
bypass. See [Read-only mode](permissions.md#read-only-mode).

## Multi-tenancy

Off by default. Setting `TENANT_MODEL` is the only thing that turns any of it on — see
[Multi-tenancy](tenancy.md) for the full design (fail-closed checks, resolvers, the
middleware requirement, and how writes are protected, not only reads).

| Key | Default | Effect |
|---|---|---|
| `TENANT_MODEL` | `None` | The model every other registered model is scoped to, `"app_label.ModelName"`. Every model must either resolve a path to it or be declared exempt once this is set |
| `TENANT_RESOLVER` | `None` | Dotted path to a `TenantResolver` subclass or any callable taking a request. `None` means [`vantage.tenancy.UserAttributeResolver`](tenancy.md#choosing-a-resolver) |
| `TENANT_FIELD` | `"tenant"` | The attribute the default resolver reads off the user, and the conventional column name on tenant-owned models |
| `TENANT_SCOPE` | `{}` | `{"app.model": "lookup__path"}` for anything inference cannot see on its own, and `{"app.model": None}` for a model that is deliberately shared across every tenant |
| `TENANT_EXEMPT` | `[]` | Models every tenant shares — reference data, mostly |
| `TENANT_UNSCOPED` | `"deny"` | What happens to a registered model with no path to the tenant and no exemption. `"deny"` shows no rows and is a `manage.py check` error (`vantage.E060`); `"allow"` shows every tenant's rows and downgrades the check to a warning (`vantage.W060`) |

None of the six are editable from the panel's own UI, deliberately: a tenant boundary is
not a preference, and a signed-in user narrowing or widening it from inside the panel
would not be a boundary.

```python
VANTAGE = {"TENANT_MODEL": "accounts.Organisation"}

MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.tenancy.TenantMiddleware",
]
```

`vantage.tenancy.TenantMiddleware` is not optional once `TENANT_MODEL` is set —
`manage.py check` refuses to pass without it (`vantage.E065`), because a resolver
without the request published to it fails closed rather than resolving nothing.

## Background jobs

A bulk action estimated at more than `JOB_THRESHOLD_SECONDS` of work is written as a
job row and worked off the request instead of run inline — see
[Background jobs](lists.md#background-jobs) for the routing rule, the chunking and
retry behaviour, and the backends it is tried against.

| Key | Default | Effect |
|---|---|---|
| `JOBS` | `True` | Whether large bulk actions may be offloaded at all. `False` always runs in the request, whatever the estimate |
| `JOB_THRESHOLD_SECONDS` | `2.0` | The line between running in the request and writing a job row, in seconds of estimated work |
| `JOB_CHUNK_SIZE` | `500` | Rows per transaction while a job runs |
| `JOB_MAX_STORED_ERRORS` | `1000` | Most per-row errors kept for the downloadable report |
| `JOB_RETENTION_DAYS` | `30` | How long a finished job's row is kept before it is swept |
| `JOB_QUEUE` | `"default"` | Queue name passed to `django.tasks` or RQ. Celery uses its own routing instead |

## Undo

After a delete or an edit, the toast offers **Undo** for a few seconds — see
[Undo](undo.md) for what can be undone, and the cases in which the toast deliberately
has no button.

| Key | Default | Effect |
|---|---|---|
| `UNDO_WINDOW` | `10` | Seconds the Undo button stays redeemable. `0` turns undo off everywhere |
| `UNDO_MAX_ROWS` | `1000` | Most rows one undo may carry, cascade included. Larger changes still happen, without Undo |

`ModelPanel.undo = False` turns it off for one model.

## Scale

What a list page does once the table outgrows the sample data. Everything is fast at
200 rows; these keys are about the other end.

| Key | Default | Effect |
|---|---|---|
| `LIST_COUNT_CAP` | `10000` | Rows counted exactly before the page reports `10,000+`. A page-depth budget, not a data limit |
| `LIST_COUNT_ESTIMATES` | `True` | Above the cap, replace `10,000+` with a planner estimate where one is available |
| `LIST_ESTIMATE_CACHE_SECONDS` | `300` | How long an estimate is cached |
| `LIST_KEYSET_THRESHOLD` | `50000` | Offset depth above which keyset paging is used. `0` disables it |
| `LIST_STATEMENT_TIMEOUT_MS` | `10000` | Per-statement ceiling for list-page queries. `0` disables it |

`COUNT(*)` on Postgres is always a full relation scan under MVCC, and it costs that
whether the page is showing 25 rows or two million. Details, and the reasoning behind
each number: [Deployment](deployment.md#counting-a-large-table).

## Data quality

One key, `DATA_QUALITY`, holding a dict of its own settings (`ENABLED`, `PERMISSION`,
`SAMPLE_SIZE`, `STALE_DAYS`, and so on). Unknown keys inside it are rejected like any
other typo. Every one is documented in [Data quality](data-quality.md#settings).

## Plugins

| Key | Default | Effect |
|---|---|---|
| `PLUGINS` | `None` | An allowlist. When set, only these plugins load |
| `PLUGINS_EXCLUDE` | `[]` | A denylist |

Both are matched against the entry-point name or the plugin's own `name`. Installing a
package must be reversible without uninstalling it, particularly when the reason for
turning it off is that it is broken. See [Extending](extending.md).

## The complete list

Every key in `vantage.conf.DEFAULTS`, and nothing else — `tests/test_release.py` fails
if the two drift apart:

```
SITE_TITLE   SITE_ACRONYM   LOGO   LOGO_DARK   FAVICON
BRAND_COLOR  BRAND_RADIUS   THEME  THEME_TOGGLE
UI_THEME  UI_THEMES  UI_THEME_TOGGLE
CUSTOM_CSS   LOGIN_IMAGE

AUTO_REGISTER  EXCLUDE  INCLUDE_ONLY
NAV  NAV_UNLISTED  DASHBOARD
PLUGINS  PLUGINS_EXCLUDE

PERMISSIONS  SETTINGS_PERMISSION  LIST_PREFS_PERMISSION  ROLES_PERMISSION
READ_ONLY  ENVIRONMENT  AUDIT_LOG  COMMAND_PALETTE
DEFAULT_PER_PAGE  RELATION_PICKER  MAP_TILE_URL
LOGIN_THROTTLE  LOGIN_THROTTLE_LIMIT  LOGIN_THROTTLE_WINDOW
LOGIN_THROTTLE_CACHE  LOGIN_THROTTLE_IP_HEADER
RATE_LIMIT  RATE_LIMIT_CACHE  RATE_LIMITS  UPLOAD_MAX_BYTES
IMPORT_MAX_BYTES  IMPORT_MAX_ROWS

TENANT_MODEL  TENANT_RESOLVER  TENANT_FIELD
TENANT_SCOPE  TENANT_EXEMPT  TENANT_UNSCOPED

JOBS  JOB_THRESHOLD_SECONDS  JOB_CHUNK_SIZE
JOB_MAX_STORED_ERRORS  JOB_RETENTION_DAYS  JOB_QUEUE

UNDO_WINDOW  UNDO_MAX_ROWS

LIST_COUNT_CAP  LIST_COUNT_ESTIMATES  LIST_ESTIMATE_CACHE_SECONDS
LIST_KEYSET_THRESHOLD  LIST_STATEMENT_TIMEOUT_MS

DATA_QUALITY
```

## Reading a setting in your own code

```python
from vantage.conf import vantage_settings

vantage_settings.SITE_TITLE
```

Always through that object, never `settings.VANTAGE["SITE_TITLE"]` directly: this layer
supplies the defaults, derives values, applies the database overrides and rejects typos.
Asking for a key that is not a Vantage setting raises `AttributeError`.

The cache is invalidated by Django's `setting_changed` signal, so `override_settings`
works as expected in tests.

## Django settings that matter

| Setting | Why |
|---|---|
| `LOGIN_URL` | Where anonymous visitors are sent. Point it at [Vantage's auth views](getting-started.md#signing-in) or your own |
| `LOGIN_REDIRECT_URL` | Where they land afterwards |
| `MEDIA_ROOT` / `MEDIA_URL` | Required for logo uploads from the settings screen |
| `CACHES` | A dummy backend disables facet-count caching; the system page warns |
| `STATIC_ROOT` | `collectstatic` needs it in production |
| `LANGUAGE_CODE` | An RTL language flips the layout automatically |
| `AUTHENTICATION_BACKENDS` | Where django-guardian or django-rules is plugged in |

## See also

- [How configuration resolves](configuration.md) — the order this dict is read in
  relative to a `SiteConfiguration` row and a panel's own Python
- [Theming](theming.md) — branding in detail, and the settings screen
- [Permissions](permissions.md) — `PERMISSIONS`, `READ_ONLY`, `AUDIT_LOG`
- [Multi-tenancy](tenancy.md) — the `TENANT_*` family in full
- [Lists](lists.md#background-jobs) — the `JOB_*` family, and what triggers an offload
- [Deployment](deployment.md) — which of these to check before going live
