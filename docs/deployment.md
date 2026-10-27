# Deployment

[← Index](index.md)

What to check before pointing this at production. Vantage is
[alpha](faq.md#how-finished-is-this); read this page as a checklist rather than as
reassurance.

## The short list

```bash
python manage.py check --deploy      # Django's own, first
python manage.py check               # includes Vantage's three checks
python manage.py migrate
python manage.py collectstatic
```

Then open `/admin/_/health/` as a superuser and read it.

## The system page

`/admin/_/health/`, superusers only — the detail is deployment information.

Deliberately a read-only diagnostic rather than a monitoring dashboard. The value is
having the five or six facts you actually check on one page inside the panel, instead of
shelling into a box to find out.

| Probe | Reports |
|---|---|
| Migrations | Every migration applied, or the first five that are not, with a hint |
| Database | Vendor, database name, and the server version where the backend exposes one |
| Cache | The backend class, or a warning if a value written did not read back |
| Background tasks | Celery, RQ, Django Tasks, or none |
| Media storage | `MEDIA_ROOT`, or a warning that logo uploads will fail |
| Debug mode | A warning when `DEBUG` is on |
| Versions | Vantage, Django and Python |

Every probe is defensive: a check that raises reports a failure rather than taking the
page down. A health page that 500s when something is unhealthy is worse than none.

A real run against the demo:

```
[ok  ] Migrations         All applied.
[ok  ] Database           sqlite · /path/to/db.sqlite3
[ok  ] Cache              LocMemCache
[ok  ] Background tasks   Django Tasks configured.
[ok  ] Media storage      /path/to/media
[warn] Debug mode         DEBUG is on.
[info] Versions           Vantage 0.2.0a1 · Django 6.1 · Python 3.13.12
```

## Read-only mode

The single most useful operational switch. Point the panel at production, let people
look, and refuse every write:

```python
VANTAGE = {"READ_ONLY": "Frozen during the data migration. Back at 14:00."}
```

A string doubles as the explanation shown to the user. It refuses exactly `add`,
`change` and `delete`; `view` is unaffected.

Three things to know:

- It is enforced **in the permission layer**, so it covers buttons, bulk actions, the
  create and edit forms, inline cell editing and the CSV importer at once — and any
  write path added later inherits the refusal.
- It is checked **before the superuser bypass**. An exemption for the account most
  likely to be signed in during an incident would make the switch decorative.
- It is editable from the [settings screen](theming.md#the-settings-screen), so an
  operator can freeze the panel without a deploy — and unfreeze it the same way.

```pycon
>>> client.get("/admin/shop/order/").status_code       # a superuser
200
>>> client.get("/admin/shop/order/add/").status_code
403
```

## Static files

Vantage ships its CSS and JavaScript prebuilt under `vantage/static/vantage/`. There is
no Node.js build step, no bundler, and nothing to compile.

```bash
python manage.py collectstatic
```

Serve `STATIC_ROOT` however you normally do. WhiteNoise, a CDN and a plain nginx
`location` block all work; nothing in the panel needs a specific one.

Two things are **not** static files, on purpose:

- **The theme stylesheet is inlined into `<head>`.** The tokens depend on
  `BRAND_COLOR` and `BRAND_RADIUS`, which can change from the settings screen at
  runtime, so a static asset would be stale the moment someone edited them — and wrong
  for any project running more than one brand.
- **The no-flash script is inline and blocking**, in `<head>`, before the stylesheet. A
  deferred script paints light first and then flips.

Both blocks carry a nonce, so a strict `Content-Security-Policy` without
`'unsafe-inline'` works. Add `vantage.security.csp_context` to your template context
processors and generate the policy with Django's `SECURE_CSP` or django-csp;
`vantage.W056` warns if a policy is configured without the context processor. The
exact policy, directive by directive, is in
[Security → Content-Security-Policy](security.md#content-security-policy).

Charts are inline SVG rendered on the server. There is no charting library to load and
nothing fetches data after page load.

## Caching

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
```

A working cache is not required, and the panel does not fall over without one. The
system page will warn:

> A value written to the cache did not read back. A dummy or misconfigured backend.
> Faceted counts will not be cached.

The settings row is read once per request and held in the process between requests.
Per-request refresh is what makes a save on the settings screen visible to every worker
rather than only to the one that handled the form, and it keeps the query count of a
page constant rather than "one extra on whichever request happened to be first".

Per-list preferences are cached **per request**: one query answers both the personal
and site-wide rows for one list, however many getters ask. Without that, a page with
six calls to `get_list_display` would pay six queries.

## The facet-count cost model

The thing most likely to make a large list page slow, and the thing most worth
understanding before you switch it on.

**Facet counts are off by default.** They appear only when the URL carries `?_facets`,
or when someone has turned them on for that list from the
[customisation rail](lists.md#the-customisation-rail).

When they are on:

| | Cost |
|---|---|
| All cheap filters, nothing multi-select ticked | **1** aggregate for the whole rail |
| Each *active* multi-select filter | **1** additional aggregate |
| Ceiling | `MAX_FACET_QUERIES = 8`; beyond it, counts are dropped |

Each count is a `Count("pk", filter=Q(...))` folded into a single `.aggregate()` — not
Django's `Q(pk__in=<subquery>)` per choice, which the planner sees as N correlated
subqueries and which is pathological on a large table.

Filters classify themselves and only the cheap ones are counted:

| Never counted | Because |
|---|---|
| Any multi-valued path | a count over a fan-out double-counts rows |
| Numeric range | a continuous range has no options |
| An unindexed date column | each bucket costs a scan of the filtered set |
| A relation with more than 50 options | the aggregate would carry one `CASE WHEN` per row of the related table |

**Practical advice:** index the date columns you filter on. An indexed
`DateField`/`DateTimeField` is counted; an unindexed one renders without counts. That is
the difference between a filter rail that answers instantly and one that scans.

A page that is slow is worse than a page without counts, which is why the ceiling drops
counts entirely rather than trimming them silently.

### Related costs to know about

| Feature | Cost |
|---|---|
| Summary bar | 1 query, whatever the column count. On by default |
| `list_stats` | 1 aggregate per join group, plus 1 shared query for all trends |
| `list_charts` | 1 grouped query each, plus 1 shared span probe |
| Both together | Capped by [the budget](analytics.md#the-budget) — 8 queries by default, and the whole strip defers rather than partially computing |
| Relation filter options | 1 query per relation filter, whether or not counts are on |
| Global search | 1 sliced query per model the user may see |
| List page rows | `select_related` / `prefetch_related` planned from the rendered columns; the query count does not grow with the row count |

## Counting a large table

`COUNT(*)` on Postgres is always a full relation scan under MVCC — roughly 50–200 ms at
one million rows and 0.5–2 s at ten million — and it costs that whether the page is
showing 25 rows or two million. GitLab measured 14,972 ms for an exact count against
1.198 ms for the planner's estimate of the same set, at 1.2% error.

`contrib.admin` goes the other way: `show_full_result_count` defaults on and runs a
*second* count over the whole unfiltered table purely to render "1 result (12,345
total)". Vantage ships no equivalent enabled by default.

Instead, a ladder:

1. **Capped exact count.** At most `LIST_COUNT_CAP` rows (10,000) are counted. Below the
   cap the number is exact and unqualified; at the cap the page says `10,000+`. Correct
   on every backend, correct for every user, and the cost is constant regardless of how
   large the table is.
2. **Planner estimate above the cap**, so a big number reads as a number rather than as
   a ceiling — `EXPLAIN (FORMAT JSON)` for the filtered set, `pg_class.reltuples` for the
   whole table. **Postgres only**, cached for `LIST_ESTIMATE_CACHE_SECONDS`, and rendered
   with a `~` prefix so an approximation can never be mistaken for a count.
3. **Statement timeout** (`LIST_STATEMENT_TIMEOUT_MS`, 10 s), so the filter nobody
   anticipated fails loudly on one request instead of pinning a worker for the length of
   a scan.

Deep pagination switches to keyset paging above `LIST_KEYSET_THRESHOLD` rows of offset.

Two rules run through all of it, and both are worth knowing before you tune the
settings.

**An estimate is a nicety and must never be the thing that 500s the page.** Every
Postgres-only path is guarded on the connection vendor first and wrapped in
`DatabaseError` second, and returns `None` rather than raising. SQLite and MySQL have
neither `reltuples` nor a machine-readable plan, so they take the capped path and
nothing else changes.

**An estimate is whole-table, and a whole-table number told to a row-restricted user is
an information leak.** A support agent scoped to their own twelve tickets must never be
shown "~2,000,000 records". Estimates are gated on whether the user is unrestricted,
and that is answered by comparing the SQL before and after `filter_queryset` rather
than by trusting a flag — so a project that scopes rows gets the capped count and no
estimate, automatically.

Set `LIST_COUNT_ESTIMATES = False` to switch step 2 off entirely and always show
`10,000+`.

Vantage's analytics layer never counts rows at all: its row budget is compared against
an estimate the caller supplies, precisely to avoid this.

## Large bulk actions become background jobs

A bulk action whose estimated cost is above `JOB_THRESHOLD_SECONDS` (2 seconds by
default) is turned into a job: it runs in chunks, reports progress at `/admin/_/jobs/`,
and lists exactly which rows failed. Smaller actions still run in the request.

No queue is required. Vantage uses the first of these it finds: Django Tasks (`TASKS`
naming a real backend), Celery with a broker, or RQ via `django_rq`. With none of them,
the job runs **in the web request** — you still get the job row and the failure report,
but a very large action can hit your gateway timeout. The system page shows which
backend is in use, and says "None configured." when it is the in-process fallback.

For production, configure one of the three, and cap actions you know are heavy:

```python
@action("Reprice", max_rows=5000, bulk_strategy="instances")
def reprice(self, request, queryset):
    ...
```

Details: [Background jobs](lists.md#background-jobs) ·
[settings](settings.md#background-jobs).

## Exports stream; imports are capped

Exports use `StreamingHttpResponse` over `QuerySet.iterator()` in chunks of 2,000, so
they start immediately and do not put the result set in memory. Note that a streaming
response holds a database connection for the duration of the download — worth knowing
if you run with a small connection pool.

Responses carry `Cache-Control: no-store`; nothing downstream should cache a per-user,
permission-scoped extract.

Imports are capped by [`IMPORT_MAX_BYTES` and
`IMPORT_MAX_ROWS`](settings.md#behaviour), 5 MiB and 5,000 rows. The uploaded file is
stashed in a private temporary directory under a random token bound to its uploader,
not in the session and not in storage. Make sure that directory survives between the
preview and the commit request — which means **sticky sessions or a shared filesystem
if you run more than one process**, or the commit will not find the file the preview
validated.

## Media and uploads

The settings screen can upload a logo and a favicon, so `MEDIA_ROOT` and `MEDIA_URL`
must be configured and writable, or those fields fail. The system page warns when
`MEDIA_ROOT` is unset.

Uploads are validated as hostile: five allowed extensions, a 2 MiB ceiling, and SVGs
containing script, inline event handlers, `javascript:` URLs or embedded documents are
**refused** rather than sanitised. Details in
[Theming](theming.md#uploads-are-treated-as-hostile).

An SVG logo is served from the panel's own origin and appears on every screen, so a
`<script>` inside one would run with the session cookie of everyone who uses the panel.
The rejection filter is the first line; **serving media from a separate origin is the
defence in depth it cannot replace**, and is worth doing if your users can upload
branding.

## Permissions to review before launch

- Who is a superuser? Superusers bypass every model permission by default.
- Is `SETTINGS_PERMISSION` left at `None`? That means superusers only, which is the
  right default for a screen that can rebrand the site and switch it to read-only.
- Is `LIST_PREFS_PERMISSION` left at `None`? That means only superusers can change what
  every colleague sees when they open a list.
- Do you need row scoping? If so, implement
  [`filter_queryset`](permissions.md#why-filter_queryset-must-be-bulk), not a loop over
  `has_object_perm`.
- Is impersonation acceptable in your environment? It is available to every superuser,
  cannot target another superuser, is banner-loud, and logs both ends at `warning` level
  to the `vantage.security` logger. Ship those logs somewhere.

## Logging

| Logger | Carries |
|---|---|
| `vantage.security` | Impersonation start and end, at `warning` |
| `vantage.dashboards` | A widget that raised, with a traceback |
| `vantage.filters` | Filter-layer diagnostics |

The in-panel [audit log](permissions.md#the-audit-log) is a separate thing: it records
what a person did through the admin, with the actor, and is on by default.

## Multiple panels

`VantageSite` can be instantiated more than once and mounted at more than one URL. Two
things to remember: the `VANTAGE` settings dict is **global**, so both sites share the
branding and the permission resolver; and a panel's `has_<action>_permission` hook is
honoured wherever that model appears, so a second mounted site cannot quietly reopen
what the first one closed.

## See also

- [Settings](settings.md) — every key
- [Accessibility](accessibility.md) — what is guaranteed and what is still broken
- [Permissions](permissions.md) — read-only, impersonation, the audit log
- [Lists](lists.md#the-cost-of-counts) — the facet cost model in full
- [FAQ](faq.md) — what is not built yet
