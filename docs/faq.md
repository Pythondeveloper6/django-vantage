# FAQ

[← Index](index.md)

## How finished is this?

**Alpha.** The version is `0.2.0a2`, the second published pre-release, and the PyPI
classifier is `Development Status :: 3 - Alpha`.

What that means in practice:

- **The behaviour is heavily tested.** More than 5,000 collected test cases across more
  than 80 modules, including
  query-count assertions, a hue sweep proving the contrast guarantee, and a round-trip
  test asserting that what the exporter writes the importer can read.
- **The API has a contract.** Every setting and panel attribute is classified as
  *stable* (frozen, changed only through a deprecation cycle) or *provisional* (new
  since the last release). See [API stability](api-stability.md). Pin an exact version
  (`django-vantage==0.2.0a2`) while it is pre-1.0, and run
  `manage.py vantage_upgrade_check` before upgrading.
- **Several features are declared but not wired up.** They are listed below rather than
  glossed over.

Use it on internal tools and back-offices. It has not yet been through an independent
security audit or a published large-deployment validation; read the
[security model](security.md) before exposing it to people you do not trust with the
data behind it.

## How is this different from `django.contrib.admin`?

Django's admin is remarkable for what it is, and it has not fundamentally changed in
twenty years. Vantage keeps what made it good — configuration in ordinary Python, next
to your models — and rebuilds the rest.

### What is genuinely different

**Inference, and per-attribute inference at that.** `contrib.admin` shows one column
(`__str__`) until you write `list_display`, and no filters or search until you write
those too. Vantage infers columns, filters, search, ordering, widgets and query
optimisation from the model, and overriding one of them leaves the others inferred.
That second half is the part that matters: in `contrib.admin` there is nothing to lose
by configuring, because nothing was inferred.

**Query cost is a designed property, not an afterthought.** `select_related` and
`prefetch_related` are derived from the columns the page renders, so a new column
cannot reintroduce an N+1. Facet counts fold into a single aggregate rather than one
correlated subquery per choice. Stats share an `aggregate()` call, partitioned by the
join they force so Django ticket #10060 cannot produce two wrong numbers. Analytics run
under a query budget and defer rather than degrade.

**Bulk selection is a predicate, not a list of ids.** "Select all 40,000 matching" costs
the same as selecting three, and the scope that was actually resolved is always
reported back — `contrib.admin` shipped a defect where `select_across` silently stayed
off and a destructive action applied to page one only.

**Analytics in the box.** Stat cards, charts and a per-column summary bar, all computed
over the filtered queryset in Python you declare rather than in a template override.

**Design.** Light, dark and system themes, RTL, a full palette derived from one brand
colour with WCAG AA guaranteed, a command palette, a schema map, a cascade-delete
preview that counts, an audit log, saved views, and a customisation rail that lets a
non-developer tune a list page without a deploy.

**Read-only mode and impersonation**, both enforced in the permission layer rather than
bolted onto views.

### What is deliberately the same

- Configuration is a Python class next to your models.
- `panel.py` is discovered exactly the way `admin.py` is.
- Permission codenames are Django's, resolved with `get_permission_codename`.
- Forms are real `ModelForm`s. Validation, `_post_clean`, `construct_instance` and
  unique checks are Django's, untouched — only the widget mapping is replaced.
- `search_fields` is accepted as an alias for `list_search`, and
  `has_<action>_permission` hooks work as you expect.
- Misconfiguration fails `manage.py check`.

### Can I run both at once?

Yes, and that is the honest way to evaluate a replacement. Mount `contrib.admin` where
it already is, mount Vantage somewhere else, and adopt your existing configuration:

```python
# project/panel.py
from vantage import site
from vantage.contrib.django_admin import adopt_admin_site

adopt_admin_site(site)
```

Every model registered with `contrib.admin` gets a Vantage panel carrying its columns,
filters, search fields, ordering, page size, form fields, actions and permission
methods. Nothing in `admin.py` changes.

Only what the admin actually *declared* is translated — an attribute left at its
`ModelAdmin` default stays unset on the panel, so Vantage's inference still runs.
Adopting an admin that only set `search_fields` should not cost you inferred columns,
filters and widgets everywhere else.

What did not come across is **reported per model** through `manage.py check`, rather
than silently dropped. A silent partial translation is worse than no translation: it
looks finished, and the missing half is discovered by a user who needed it. Details:
[Extending](extending.md).

## What it does not do

An honest list. Some of these are decisions; some are gaps.

### Not implemented

| | Notes |
|---|---|
| **`panel.permissions`** | Removed outright, not merely ignored. Setting it fails `manage.py check` (`vantage.E020`). Use `VANTAGE["PERMISSIONS"]` or the per-panel `has_*_permission` hooks — see [Panels](panels.md#removed-attributes) |
| **Full translation coverage** | `locale/` ships `en` (complete) and `ar` (complete Modern Standard Arabic, including all six plural forms; strings added later fall back to English until translated), for templates, Python and the JavaScript alike — see [Internationalization](internationalization.md) |

`inlines`, `row_actions`, `list_select_related`, `related_tabs`, `layout`, background
job offload for bulk actions and per-field widget overrides (`formfield_overrides`,
`widgets`) used to belong on this table and no longer do — all seven are real, tested
features now. See [Editable inlines](forms.md#editable-inlines),
[Row actions](panels.md#row-actions), [The record page](forms.md#the-record-page),
[The layout DSL](forms.md#the-layout-dsl), [Bulk actions](lists.md#bulk-actions) and
[Overriding a widget](forms.md#overriding-a-widget).

**The JSON API is built.** [`vantage.api`](api.md) is a full read/write API over every
registered panel — cursor pagination, optimistic concurrency, the same permission and
validation path the HTML pages use. It has no file-upload endpoint (a JSON body cannot
carry binary data) and no bulk-action, saved-view or import/export endpoints yet; see
[what it does not do](api.md#what-this-api-does-not-do) for the current edge.

### Deliberately not done

| | Why |
|---|---|
| **Authentication itself** | Vantage ships *styled* sign-in, sign-out and change-password views over `django.contrib.auth`'s own forms, and no more. Password reset, MFA and SSO are the auth stack's job |
| **Row history** | Vantage's audit log records what a *person did in the panel*. Row history means a model and a migration in **your** app and roughly double the write volume; django-simple-history is the right tool, and it is soft-detected and rendered alongside when you have chosen it |
| **`raw_id_fields`, `autocomplete_fields`, `filter_horizontal` / `filter_vertical`** | The relation widget already searches above 50 rows. These are three names for one behaviour |
| **`prepopulated_fields`** | Slug prepopulation |
| **`save_as`, `save_on_top`, `actions_on_top` / `actions_on_bottom`** | Chrome placement is fixed |
| **Search prefixes `^`, `=`, `@`** | Every path is `icontains`. A prefix in a path is stripped |
| **Auto-enabling django-guardian** | Whether object permissions narrow the admin is a policy decision, not a consequence of having a package installed. Set `PERMISSIONS` explicitly |
| **A CSS or JS build step** | Assets ship prebuilt. No Node.js, no bundler, no htmx, no Tailwind — the CSS is hand-written OKLCH tokens and the JavaScript is dependency-free |

### Known limitations

- **Row counts above 10,000 are capped or estimated.** That is deliberate — see
  [counting a large table](deployment.md#counting-a-large-table) — but it means the
  number at the foot of a large list is `10,000+` or `~2,300,000`, not an exact figure.
  Planner estimates are Postgres-only and are withheld from row-restricted users.
- **A strict CSP needs one context processor.** The token stylesheet and the no-flash
  script are inline in `<head>`; `base.html` nonces both through `csp_nonce_attr` once
  `vantage.security.csp_context` is in your context processors, and `vantage.W056`
  warns if a policy is configured without it. A handful of inline `style` attributes
  remain, which no nonce can cover — see
  [Content-Security-Policy](security.md#content-security-policy) for the exact policy.
- **The import wizard stashes the uploaded file on local disk.** Running more than one
  process needs sticky sessions or a shared filesystem between the preview and the
  commit.
- **Multiple sites share one settings dict.** Branding, `NAV` and the permission
  resolver are global.
- **A tree spanning pages** shows a child whose parent is on another page as a root. That
  is the honest answer rather than a recursive lookup per row, but it can look odd.

## Does it need Node.js?

No. There is no build step, no bundler and no JavaScript dependency. The CSS is
hand-written using OKLCH tokens, the JavaScript is plain and dependency-free, and both
ship in the wheel. `collectstatic` is the whole deployment story for assets.

## Does it need a database table?

It creates four (plus one for background jobs), for site configuration, the audit log,
saved views and per-list preferences. Each is optional in practice: if the table is
missing, the layer that uses it silently falls back to what the code says. That is not
laziness — a settings layer that raises when its own table is absent makes the migration
that would create the table impossible to run.

## Which optional packages does it use?

All soft-detected, none required:

| Package | Used for |
|---|---|
| django-guardian | Bulk row scoping, via `GuardianPermissions` |
| django-rules | Object-level predicates, through `AUTHENTICATION_BACKENDS` |
| django-mptt | Tree detection |
| django-treebeard | Tree detection (MP, NS and AL node types) |
| django-simple-history | A row-history timeline beside the audit log |
| django-ordered-model | Sortable detection |
| Celery / RQ / Django Tasks | Reported on the system page |

Detection uses `apps.is_installed`, not `try: import`. A package being importable says
nothing about whether *this project* uses it, and importing an optional dependency is a
side effect that pure detection has no business having.

## Why is my column not showing?

In order of likelihood:

1. It is a `TextField`, `JSONField`, `BinaryField` or `FileField` — never a column.
2. Its name matches the [sensitive pattern](inference.md#the-sensitive-name-exclusion) —
   anything containing `password`, `token`, `secret`, `hash` and so on as a whole word.
3. It is a `CharField` longer than 200 characters.
4. It scored below the top six. Run `manage.py vantage_scaffold <app>` to see the
   result, then write `list_display` yourself.
5. Somebody hid it from the [customisation rail](lists.md#the-customisation-rail). A
   personal preference beats your `panel.py`; check whether the list has a site-wide
   preference row.

## Why is my filter doing nothing?

- The field is not in `list_filter`. Only declared or inferred filters read the query
  string.
- The value did not clean, so it was dropped. A filter rail is not a form, and a stale
  bookmark should degrade to a broader result set rather than to a 500.
- The path is partial — `created_at__year` is rejected rather than guessed at.
- The field type has no filter: text, JSON, binary and file fields belong in search.

## Why has my stat got no trend?

`trend` names a date field, and the previous period is computed from the span of the
data on screen. If the user has filtered *on that same date field*, the preceding period
has been filtered away. The list page passes a baseline queryset with that one filter
removed to cover exactly this, so if you are seeing it, you are probably calling
`compute_stats` yourself without a `baseline`.

## Why are two of my numbers wrong?

Almost certainly two aggregates over different multi-valued relations in one
`aggregate()` call — the joins multiply and **both** numbers are inflated, including
aggregates over local columns, which is the part that surprises people (Django ticket
#10060).

Vantage partitions stats by the join they force so this cannot happen across separate
stats, and refuses a *single* stat that reaches across two:

```
Stat 'Total' aggregates across the multi-valued relations ['items', 'reviews'] in one
expression. … Express one of them as a correlated Subquery instead.
```

If you are seeing wrong numbers anyway, check the summary bar: it only ever aggregates
local concrete columns, so a custom `list_aggregates` naming a traversed path is the
other place to look.

## Can a saved view or a stored preference show someone more than they should see?

No, and both are built so that the answer cannot become yes.

A saved view stores **query parameters**, and the rows are filtered by
`filter_queryset` on every request regardless of who saved it. URL filter state has zero
security value; a view decides only which of the rows you may already see are worth
showing you.

A stored list preference is validated against what the panel *currently* exposes, both
when it is written and again when it is read. It can hide, reorder and re-aggregate what
the panel already offers; it can never widen it.

## How do I contribute or report a bug?

```bash
uv sync --all-groups
uv run pytest
uv run ruff check src tests
```

The demo project in `example/` is the fastest way to reproduce something:

```bash
./example/run.sh          # migrate, seed, serve on :8765
```

## See also

- [Index](index.md) — the two rules and the map
- [Panels](panels.md#declared-but-not-yet-wired-up) — the inert attributes, in context
- [Deployment](deployment.md) — the production checklist
- [Extending](extending.md) — slots, plugins, and the `contrib.admin` adapter
