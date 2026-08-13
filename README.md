<div align="center">

# django-vantage

**A smart, beautiful admin panel for Django.**
Zero config to start. One line to override anything.

</div>

---

> **Status: early development.** The API described here is the design target. Not yet released.

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
INSTALLED_APPS = [
    "vantage",
    ...
]

# urls.py
from vantage import site

urlpatterns = [
    path("admin/", site.urls),
]
```

That is the entire setup. Every model is now administrable.

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

## Statistics and charts on any list page

Declared in Python, computed against the currently filtered queryset, no JavaScript:

```python
from vantage import ModelPanel, register, Stat, Chart
from django.db.models import Sum, Count, Avg

@register(Order)
class OrderPanel(ModelPanel):
    list_filter = ["status", "created_at"]

    list_stats = [
        Stat("Revenue",   Sum("total"), format="currency", trend="created_at"),
        Stat("Orders",    Count("id")),
        Stat("Avg order", Avg("total"), format="currency"),
    ]
    list_charts = [
        Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
    ]
```

Change a filter and the numbers follow.

## Brand it

```python
VANTAGE = {
    "SITE_TITLE":  "Acme",
    "LOGO":        "img/acme.svg",
    "LOGO_DARK":   "img/acme-dark.svg",
    "BRAND_COLOR": "#0ea5e9",
    "THEME":       "system",
}
```

One colour produces a complete, contrast-checked palette in both light and dark themes.

## Add your own page

```python
from vantage import site, Page

@site.register_page
class RevenuePage(Page):
    title = "Revenue"
    icon  = "trending-up"
    path  = "revenue/"

    def get_context(self, request):
        return {"total": Order.objects.total_revenue()}
```

## Requirements

Python 3.11+ · Django 5.2+ (including 6.x)

No Node.js. Assets ship prebuilt.

## Licence

MIT
