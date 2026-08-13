<div align="center">

# django-vantage

**A smart, beautiful admin panel for Django.**
Zero config to start. One line to override anything.

</div>

---

> **Status: early development.** Working and heavily tested, but not yet released to PyPI
> and the API may still move.

## Why

Django's built-in admin is remarkable for what it is, and it hasn't fundamentally changed in
twenty years. `django-vantage` keeps what made it great — configuration in ordinary Python, next
to your models — and rebuilds everything else: the rendering, the data grid, the relationship
handling, and the look.

Two rules govern the whole package:

1. **Smart.** Install it, and every model in your project already has a good admin — columns,
   filters, search, widgets, and query optimisation inferred automatically.
2. **Easy.** Every inferred default is overridable in one line. If it takes more, the API is wrong.

## Install

```bash
pip install django-vantage
```

```python
# settings.py
INSTALLED_APPS = ["vantage", ...]

# urls.py
from vantage import site
urlpatterns = [path("admin/", site.urls)]
```

That is the entire setup. Every model is now administrable, with a dashboard, a searchable
data grid, create/edit/delete, a command palette, light and dark themes, and a schema map.

## Customise a model

Create `panel.py` next to your `models.py`:

```python
from vantage import ModelPanel, register
from blog.models import Post

@register(Post)
class PostPanel(ModelPanel):
    list_display = ["title", "author", "status", "published_at"]
```

Overriding `list_display` overrides *only* the columns. Filters, search fields and widgets stay
inferred. Partial configuration never opts you out of the intelligence.

Not sure what it inferred? Ask it to write it down:

```bash
python manage.py vantage_scaffold blog
```

That prints a real `panel.py` containing exactly what Vantage decided, which you can then own
and edit. Delete an attribute and it goes back to being inferred.

## Statistics and charts on any list page

Declared in Python, computed against the currently filtered queryset, no JavaScript:

```python
from vantage import Chart, ModelPanel, Stat, register
from django.db.models import Avg, Count, Q, Sum

@register(Order)
class OrderPanel(ModelPanel):
    list_filter = ["status", "created_at"]

    list_stats = [
        Stat("Revenue",   Sum("total"), format="currency", trend="created_at"),
        Stat("Orders",    Count("id")),
        Stat("Avg order", Avg("total"), format="currency"),
        Stat("Refunded",  Count("id", filter=Q(status="refunded")), tone="destructive"),
    ]
    list_charts = [
        Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
    ]
```

Change a filter and every number follows. All four stats cost a single query; the charts render
as inline SVG that follows your brand colour and the current theme.

There is also a per-column summary bar at the foot of every table, which needs no configuration
at all — a money column is summed, a rating column is averaged, and an identifier is left alone.

## Bulk actions

Ordinary methods with a decorator:

```python
from vantage import ModelPanel, action, register

@register(Review)
class ReviewPanel(ModelPanel):
    @action("Publish", icon="check")
    def publish(self, request, queryset):
        return f"Published {queryset.update(is_published=True)} reviews."

    @action("Delete", destructive=True, permission="delete", max_rows=500,
            confirm="Permanently delete {count} reviews?")
    def delete_selected(self, request, queryset):
        deleted, _ = queryset.delete()
        return f"Deleted {deleted} records."
```

"Select all matching" travels as a predicate plus an exclusion set, never as a list of ids, so
selecting forty thousand rows costs the same as selecting three. The scope actually resolved is
always reported back — a bulk action must never quietly apply to less than you asked for.

## Lay out a form

```python
from vantage import Row, Section, Tab, Tabs

layout = Tabs(
    Tab("Content", Section(Row("title", "slug"), "body")),
    Tab("SEO", Section("meta_title", "meta_description")),
)
```

A field the layout forgets is appended rather than dropped, and a field it names that does not
exist fails `manage.py check` rather than silently vanishing from the form.

## Brand it

```python
VANTAGE = {
    "SITE_TITLE":  "Acme",
    "LOGO":        "img/acme.svg",
    "BRAND_COLOR": "#0ea5e9",
    "THEME":       "system",
}
```

One colour produces a complete palette for both themes, with contrast checked: the derived
primary and its foreground are guaranteed to clear WCAG AA for any hue you supply.

Anything here can also be edited **in the panel** by a superuser — logo upload, brand colour,
title, theme — and the database value overrides the file.

## Organise the sidebar

```python
VANTAGE = {
    "NAV": [
        {"route": "vantage:dashboard", "label": "Overview", "icon": "layout-dashboard"},
        {"group": "Commerce", "items": [
            {"model": "shop.Order", "badge": "live"},
            {"model": "shop.Product", "label": "Catalogue"},
        ]},
        {"group": "Elsewhere", "items": [
            {"url": "https://status.example.com", "label": "Status"},
        ]},
    ],
}
```

Declaring a nav can reorder, rename and hide — it can never grant access. Every entry still
passes through the permission layer.

## Add your own page

```python
from vantage import Page, site

@site.register_page
class RevenuePage(Page):
    title = "Revenue"
    icon  = "trending-up"

    def get_context(self, request):
        return {"total": Order.objects.total_revenue()}
```

A class, a title, and a method. The URL, the nav entry and the rendering all have defaults, so a
page with no template of its own still produces a presentable screen.

## What else is in the box

- **Command palette** (⌘K) with global search across every model you can see
- **Dashboard** of widgets declared in Python — stat, chart, table, list, or your own template
- **Schema map** derived from the registry, so it is never out of date, with clickable models
- **Cascade-delete preview** — the real tree of what a delete takes with it, counted
- **Read-only mode** and **user impersonation**, both enforced in the permission layer
- **System health page** — migrations, cache, database, task backend
- **Streaming CSV export** that honours the filters and sort you are looking at
- **Light / dark / system themes** and **RTL**, both from day one
- **Query-count guarantees** — `select_related` and `prefetch_related` are derived from what
  the page actually renders, and tests assert the cost does not grow with the row count

## Requirements

Python 3.12+ · Django 5.2+ (including 6.x)

Django is the only runtime dependency. No Node.js — assets ship prebuilt.
django-guardian, django-rules and django-simple-history are used if present, never required.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check src tests
```

A full demo project lives in `example/`:

```bash
./example/run.sh          # migrate, seed, and serve on :8765
```

Sign in as `admin` / `admin12345`.

## Licence

MIT
