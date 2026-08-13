# django-vantage — Developer API Design

**Date:** 2026-08-13 · **Status:** Approved, implementing · **Milestone:** M0 (kernel)

This document specifies the public Python surface of `django-vantage`. It is the contract every
later milestone builds against. The two rules from `PLAN.md` §1 govern every decision here:
zero config must produce a good admin, and every default must be overridable in one line.

---

## 1. The three layers

Configuration resolves narrowest-scope-first. A value set at a narrower layer always wins.

```
settings.VANTAGE          global defaults, branding, feature flags       (project owner)
        ↓
vantage.site              nav, dashboard, custom pages, registration     (project developer)
        ↓
ModelPanel subclass       per-model columns, filters, stats, forms       (app developer)
```

Nothing in a lower layer needs a higher layer to exist. A project with no `panel.py` files and no
`VANTAGE` setting still gets a complete admin.

---

## 2. Layer 3 — `panel.py`

Autodiscovered from every installed app, exactly as `admin.py` is. `vantage.apps.VantageConfig.ready()`
calls `autodiscover_modules("panel", register_to=site)`.

### 2.1 Zero config

Delete `panel.py` entirely and every model is still registered, with columns, filters, search
fields, widgets, ordering and query optimisation inferred. This is the baseline, not a fallback.

### 2.2 Minimal config

```python
# blog/panel.py
from vantage import ModelPanel, register
from blog.models import Post

@register(Post)
class PostPanel(ModelPanel):
    list_display = ["title", "author", "status", "published_at"]
```

Every attribute is optional. Setting `list_display` overrides inference for columns **and nothing
else** — filters, search and widgets remain inferred. Partial override never cascades into a
full opt-out of intelligence. This is the single most important ergonomic rule in the package.

### 2.3 Full attribute surface

Names match `django.contrib.admin` wherever the semantics match, so existing knowledge transfers.

```python
@register(Post)
class PostPanel(ModelPanel):
    # ─── identity ───────────────────────────────────────────────
    label           = "Articles"          # nav label; default = model verbose_name_plural
    icon            = "file-text"         # lucide icon name
    group           = "Content"           # nav grouping; default = app verbose_name
    order           = 10                  # nav sort weight

    # ─── list page ──────────────────────────────────────────────
    list_display    = ["title", "author", "status"]
    list_link       = "title"             # which column opens the record
    list_filter     = ["status", "author", "published_at"]
    list_search     = ["title", "body"]   # alias: search_fields
    list_editable   = ["status"]          # inline cell editing
    list_per_page   = 50
    ordering        = ["-published_at"]
    list_select_related = None            # None = infer from list_display

    # ─── list analytics (see §5) ────────────────────────────────
    list_stats      = [...]
    list_charts     = [...]
    list_aggregates = {"views": Sum}      # per-column footer row

    # ─── detail page ────────────────────────────────────────────
    fields          = None                # None = infer
    layout          = None                # see §6
    readonly_fields = ["created_at"]
    inlines         = [CommentInline]
    related_tabs    = True                # reverse relations as tabs

    # ─── behaviour ──────────────────────────────────────────────
    actions         = [publish, unpublish]
    row_actions     = ["preview"]
    permissions     = None                # None = Django model permissions

    # ─── hooks ──────────────────────────────────────────────────
    def get_queryset(self, request): ...
    def has_view_permission(self, request, obj=None): ...
    def save_model(self, request, obj, form, change): ...
```

### 2.4 Registration forms

```python
@register(Post)                     # decorator (preferred)
class PostPanel(ModelPanel): ...

site.register(Post, PostPanel)      # imperative
site.register(Post)                 # register with pure inference
site.unregister(Post)               # opt a model out
```

---

## 3. Layer 2 — the `site` object

A module-level singleton, `vantage.site`, holding the registry, the nav, custom pages and the
dashboard. Projects needing more than one panel (e.g. a staff panel and a partner panel) instantiate
`VantageSite` directly and mount it at a second URL.

```python
# project/panel.py
from vantage import site, Page, Dashboard

site.title = "Acme Admin"

@site.register_page
class RevenuePage(Page):
    title      = "Revenue"
    icon       = "trending-up"
    path       = "revenue/"
    group      = "Reports"
    permission = "orders.view_order"

    def get_context(self, request):
        return {"total": Order.objects.total_revenue()}
```

A `Page` needs only `title` and `get_context`. Its template defaults to
`vantage/pages/<slug>.html`, and if that file does not exist the context renders through a generic
card layout — so a custom page with no template at all still produces a presentable screen.

**URLs.** `path("admin/", site.urls)` in the project urlconf. One line, mirroring contrib.

---

## 4. Layer 1 — `settings.VANTAGE`

A single dict. Every key is optional; the shown values are the defaults.

```python
VANTAGE = {
    # ─── branding (website owner) ───────────────────────────────
    "SITE_TITLE":    "Django Vantage",
    "SITE_ACRONYM":  None,            # collapsed-sidebar mark; default = initials of SITE_TITLE
    "LOGO":          None,            # static path, light theme
    "LOGO_DARK":     None,            # defaults to LOGO
    "FAVICON":       None,
    "BRAND_COLOR":   "#6366f1",       # one hex → full derived palette, both themes (§7)
    "BRAND_RADIUS":  "0.5rem",
    "THEME":         "system",        # light | dark | system
    "THEME_TOGGLE":  True,            # let users override
    "CUSTOM_CSS":    None,            # static path, injected last
    "LOGIN_IMAGE":   None,

    # ─── discovery ──────────────────────────────────────────────
    "AUTO_REGISTER": True,
    "EXCLUDE":       [],              # ["sessions", "admin.LogEntry", "myapp.Secret"]
    "INCLUDE_ONLY":  None,            # when set, an allowlist instead

    # ─── navigation ─────────────────────────────────────────────
    "NAV":           None,            # None = grouped by app, alphabetical
    "DASHBOARD":     None,            # dotted path to a Dashboard subclass

    # ─── behaviour ──────────────────────────────────────────────
    "READ_ONLY":     False,
    "ENVIRONMENT":   None,            # ("Production", "red") → banner
    "AUDIT_LOG":     True,
    "COMMAND_PALETTE": True,
    "DEFAULT_PER_PAGE": 50,
}
```

Unknown keys raise a system check error (`vantage.E001`) rather than being silently ignored — a
typo in `BRAND_COLOUR` should fail `manage.py check`, not produce an indigo panel and a confused user.

---

## 5. List-page analytics

The requirement: statistics and charts on the list page **that respect the active filters**.

Both `list_stats` and `list_charts` receive the queryset *after* filters and search are applied but
*before* pagination. They render in a collapsible strip above the grid, and refresh over HTMX
whenever a filter changes — as a separate request from the grid, so a slow aggregate never blocks
the rows.

```python
from vantage import ModelPanel, register, Stat, Chart
from django.db.models import Sum, Count, Avg

@register(Order)
class OrderPanel(ModelPanel):
    list_display = ["number", "customer", "status", "total", "created_at"]
    list_filter  = ["status", "created_at"]

    list_stats = [
        Stat("Revenue",   Sum("total"),  format="currency", trend="created_at"),
        Stat("Orders",    Count("id")),
        Stat("Avg order", Avg("total"),  format="currency"),
        Stat("Refunded",  Count("id", filter=Q(status="refunded")), tone="destructive"),
    ]

    list_charts = [
        Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
    ]
```

`trend="created_at"` makes a `Stat` compute the same aggregate over the preceding equivalent period
and render a delta chip. Charts are declared entirely in Python; no JavaScript is written by the user.

For full control, either accepts a callable:

```python
list_stats = [Stat("Conversion", lambda qs: qs.conversion_rate(), format="percent")]
```

**Performance.** Stats for one queryset are combined into a single `aggregate()` call. Charts each
cost one grouped query. If the total exceeds a configurable budget the strip renders collapsed with
a "compute" button rather than making every list page slow. Defaults must never make a page slower
than it would have been without them.

---

## 6. Form layout DSL

`fields` handles the simple case. `layout` handles the rest, and is plain nested Python:

```python
from vantage import Tabs, Tab, Section, Row

layout = Tabs(
    Tab("Content",
        Section(Row("title", "slug"), "body"),
        Section("Publishing", Row("status", "published_at"), collapsed=True),
    ),
    Tab("SEO", Section("meta_title", "meta_description")),
)
```

Conditional visibility is declared on the container: `Section(..., show_if=Q(status="published"))`.
The condition is evaluated client-side against current form state, and re-validated server-side.

---

## 7. Branding contract

One hex colour in, a complete accessible palette out, for both themes. `BRAND_COLOR` is converted to
OKLCH and used to derive `--primary`, `--primary-foreground`, `--ring`, `--accent` and their hover
states, with lightness targets chosen per theme so contrast ratios hold in light *and* dark. Every
component reads only from the token layer — no component hard-codes a colour — so the derivation is
the single point of control.

Contrast is asserted in tests: the derived `--primary` / `--primary-foreground` pair must clear
WCAG AA at 4.5:1 for any input hue. If a supplied brand colour cannot reach it, the derivation
adjusts lightness until it does and emits a `vantage.W002` check warning rather than shipping an
unreadable button.

---

## 8. Extension points

| Hook | Purpose |
|---|---|
| `@site.register_page` | Whole custom page |
| `@site.register_widget` | Dashboard card |
| `@site.register_action` | Reusable action across panels |
| `ModelPanel.get_queryset` | Scope rows (multi-tenancy) |
| `ModelPanel.slot(name)` | Inject a template fragment at `before_list`, `after_form`, … |
| entry point `vantage.plugins` | Third-party packages register pages, nav and widgets on install |

---

## 9. Dependencies

Runtime dependency: **Django only**. No `django-filter`, `django-tables2`, or `django-import-export`
in the core — a framework-grade package should not export another project's API shape as part of its
own, and each would constrain the config surface specified above.

Optional integrations are detected at runtime and enhance behaviour when present:
`django-simple-history` (revision tab), Celery / RQ / `django-tasks` (bulk-action offload),
`django-guardian` / `django-rules` (object permissions), `psycopg` (faster faceted counts).

**Targets:** Python ≥ 3.11, Django ≥ 5.2 (LTS) including 6.x. Licence: MIT.

---

## 10. Testing strategy

- **Inference** is table-driven: a fixture app of models covering every field type, asserting the
  inferred columns/filters/widgets. This is the package's core value and gets the densest coverage.
- **Query counts** are asserted with `assertNumQueries` on every list and detail view. The N+1
  guarantee is a promise; an untested promise is a lie.
- **Contrast** is asserted numerically over a spread of brand hues (§7).
- **Zero-config integration test**: a project with only `INSTALLED_APPS` renders every page for
  every model in a fixture app without error. This test is the north star, executable.
