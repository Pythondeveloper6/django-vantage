# Panels

[← Index](index.md)

A `ModelPanel` is one model's configuration: what it is called, which columns its list
page shows, how it can be filtered, what its form looks like, and what may be done to
it. It is ordinary Python, and it lives in a `panel.py` next to your `models.py`.

```python
# blog/panel.py
from vantage import ModelPanel, register

from blog.models import Post


@register(Post)
class PostPanel(ModelPanel):
    list_display = ["title", "author", "status", "published_at"]
```

## The rule that governs every attribute

> **Every attribute defaults to `None`, meaning *infer it*. Setting one attribute
> overrides that attribute and nothing else.**

Configuring `list_display` must never switch off inference for filters, search or
widgets. That is the package's most important ergonomic guarantee, and it is why
inference is resolved per attribute rather than as a single all-or-nothing fallback.

You can see it in the panel class itself: every getter falls back independently.

```python
def get_list_filter(self, request=None) -> list[str]:
    stored = listprefs.override(self, request, "list_filter")   # a UI preference
    if stored is not None:
        return list(stored)
    if self.list_filter is not None:                            # your panel.py
        return list(self.list_filter)
    return inference.infer_list_filter(self.model)              # inference
```

The consequence in practice, from the demo project:

```python
@register(Product)
class ProductPanel(ModelPanel):
    label_plural = "Catalogue"
    icon = "box"
    group = "Commerce"
    list_display = ["name", "sku", "category", "status", "price", "stock"]
```

`ProductPanel` declares columns and nothing else. Asking the live panel what it
resolves confirms the rest is still inferred:

```pycon
>>> panel = site.get_panel(Product)
>>> panel.get_list_display()
['name', 'sku', 'category', 'status', 'price', 'stock']     # yours
>>> panel.get_list_filter()
['status', 'is_featured', 'created_at', 'category']         # inferred
>>> panel.get_list_search()
['sku', 'name', 'description', 'category__name']            # inferred
```

## Registering

Three ways, all equivalent.

```python
from vantage import ModelPanel, register, site

# 1. The decorator — the usual one.
@register(Post)
class PostPanel(ModelPanel):
    ...

# 2. Explicitly, against the default site.
site.register(Post, PostPanel)

# 3. With no panel at all, taking every default.
site.register(Post)
```

Auto-registration runs *after* `panel.py` discovery and never clobbers an existing
registration, so an explicit panel always wins over inference whichever ran first.
Registering the same model twice raises `AlreadyRegistered`.

`site.unregister(Model)` removes one. It raises `NotRegistered` if the model was not
there.

## Identity

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `label` | `str` | `Meta.verbose_name`, capitalised | Singular name in headings and buttons |
| `label_plural` | `str` | `Meta.verbose_name_plural`, capitalised | Plural name in the sidebar, the page title and the export filename |
| `icon` | `str` | guessed from the model name | Sidebar and palette icon |
| `group` | `str` | the app's `verbose_name` | Which sidebar group the model sits in |
| `order` | `int` | `0` | Sorts sidebar groups; lower comes first, and may be negative |
| `description` | `str` | `None` | A sentence under the list-page heading |

```python
@register(Order)
class OrderPanel(ModelPanel):
    label = "Order"
    label_plural = "Orders"
    icon = "shopping-cart"
    group = "Commerce"
    order = -10
    description = "Every order placed in the last six months."
```

Icons are [Lucide](https://lucide.dev) names. When `icon` is unset, a small table of
common model names supplies one for free — `user` → `users`, `order` → `shopping-cart`,
`post` → `file-text`, `category` → `folder` — and anything unlisted falls back to `box`.

`order` sorts *groups*, not items: a group takes the lowest `order` of any panel in it,
and items inside a group are sorted alphabetically. To control item order too, declare
[`NAV`](settings.md#navigation).

## The list page

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `list_display` | `list[str]` | inferred | Columns, in order. Field paths, model attributes or panel methods |
| `list_link` | `str` | inferred | Which column opens the record |
| `list_filter` | `list[str]` | inferred | Filter rail. Real field paths only — they are resolved in SQL |
| `list_search` | `list[str]` | inferred | Fields the search box covers. Real field paths only |
| `search_fields` | `list[str]` | `None` | Alias for `list_search`, for `contrib.admin` muscle memory |
| `list_editable` | `list[str]` | `None` | Columns editable in place on the list page |
| `list_per_page` | `int` | `DEFAULT_PER_PAGE` (50) | Rows per page |
| `ordering` | `list[str]` | inferred | Default sort. `Meta.ordering` wins if you set neither |

`list_display` entries may traverse relations with `__`, and may also name a method on
the panel or an attribute on the model:

```python
@register(Order)
class OrderPanel(ModelPanel):
    list_display = ["number", "customer__name", "status", "total", "line_count"]

    def line_count(self, obj):
        return obj.items.count()
```

`list_filter` and `list_search` are stricter: their entries are fed straight to the ORM,
so a panel method is not acceptable there and `manage.py check` says so
(`vantage.E011` / `vantage.E012`).

`list_search` reaches one hop into a foreign key. Searching an order by its customer's
name is what a human means by "search", and it is the single most common thing people
add by hand to a Django admin — so inference does it for you.

Details: [Lists](lists.md).

## List analytics

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `list_stats` | `list[Stat]` | `None` | Number cards above the table, computed over the filtered queryset |
| `list_charts` | `list[Chart]` | `None` | Charts above the table, drawn as server-side SVG |
| `list_aggregates` | `dict[str, str \| type]` | `None` | Overrides the per-column footer aggregate |

```python
from django.db.models import Avg, Count, Q, Sum
from vantage import Chart, ModelPanel, Stat, register


@register(Order)
class OrderPanel(ModelPanel):
    list_stats = [
        Stat("Revenue", Sum("total"), format="currency", trend="created_at"),
        Stat("Orders", Count("id")),
        Stat("Average order", Avg("total"), format="currency"),
        Stat("Refunded", Count("id", filter=Q(status="refunded")), tone="destructive"),
    ]
    list_charts = [
        Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
        Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
    ]
    list_aggregates = {"total": "sum", "stock": None}
```

`list_aggregates` accepts a function name (`"sum"`, `"avg"`, `"min"`, `"max"`,
`"count"`), an aggregate class (`Sum`) so it reads naturally next to the `Stat`
declarations above it, or `None` to suppress a column the footer would otherwise sum.

The summary bar needs no configuration at all: a money column is summed, a rating
column is averaged, and an identifier is left alone. Details:
[Analytics](analytics.md).

## The detail and form pages

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `fields` | `list[str]` | inferred | Which fields the form contains, in order |
| `exclude` | `list[str]` | `None` | Names removed from whatever `fields` or inference produced |
| `layout` | `Node` | `None` | Tabs, sections and rows — see the [layout DSL](forms.md#the-layout-dsl) |
| `readonly_fields` | `list[str]` | `None` | Shown but not editable, and *removed from the form entirely* |

`readonly_fields` is a security boundary rather than a UI affordance. A readonly field
is not rendered as a control **and** is not in the form's field list, so a hand-crafted
POST that includes it is dropped by `construct_instance` rather than merely unrendered.

A name in `readonly_fields` that inference did not pick up is appended rather than
dropped — the canonical case being `readonly_fields = ["created_at"]` on an
`auto_now_add` column, which is not an editable field at all and would otherwise vanish
from a page whose whole purpose was to display it.

Details: [Forms](forms.md).

## Behaviour

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `actions` | `list[str \| Action]` | `None` | Bulk actions offered on the list page |
| `checks_class` | `type` | `ModelPanelChecks` | Subclass it to extend validation for your own panel base class |

When `actions` is left unset, every method on the panel decorated with `@action` is
discovered. Decorating a method is already an explicit statement of intent, so
requiring it to be listed a second time would be ceremony:

```python
from vantage import ModelPanel, action, register


@register(Review)
class ReviewPanel(ModelPanel):
    @action("Publish", icon="check", permission="change")
    def publish(self, request, queryset):
        return f"Published {queryset.update(is_published=True)} reviews."

    @action("Delete", icon="trash", permission="delete", destructive=True,
            max_rows=500, confirm="Permanently delete {count} reviews?")
    def delete_selected(self, request, queryset):
        deleted, _ = queryset.delete()
        return f"Deleted {deleted} records."
```

Set `actions = []` to offer none, or list names to choose a subset and their order.
Details: [Bulk actions](lists.md#bulk-actions).

## Overridable methods

Where an attribute cannot express what you need, override the method behind it. Each
one takes `request`, so the answer can depend on who is asking.

| Method | Returns | Notes |
|---|---|---|
| `get_queryset(request)` | `QuerySet` | The base queryset for **every** view. Override to scope rows per tenant |
| `get_list_queryset(request)` | `QuerySet` | The list page's queryset, with `select_related`/`prefetch_related` already planned |
| `get_list_display(request)` | `list[str]` | |
| `get_list_link(request)` | `str` | |
| `get_list_filter(request)` | `list[str]` | |
| `get_list_search(request)` | `list[str]` | |
| `get_ordering(request)` | `list[str]` | |
| `get_per_page(request)` | `int` | |
| `get_label()` / `get_label_plural()` | `str` | |
| `get_group()` | `str` | |
| `get_icon()` | `str` | |
| `get_patterns()` | `ModelPatterns` | The [conventions](#detected-conventions) detected on this model |
| `get_trash_queryset(request)` | `QuerySet` | Soft-deleted rows, or an empty queryset |

`get_list_queryset` derives its optimisation from what the page actually renders — every
column and every search path — so a column added to `list_display` cannot reintroduce
an N+1. The test suite asserts that with `assertNumQueries`.

`get_trash_queryset` starts from `_base_manager` when the model's default manager
already hides soft-deleted rows. A trash view built on `get_queryset()` would filter a
queryset that can never contain what it is looking for, and silently show nothing.

### Read-only properties

| Property | Value |
|---|---|
| `model` | The model class |
| `opts` | `model._meta` |
| `slug` | URL segment: `shop.Order` → `shop-order` |
| `list_url_name` | `vantage:shop-order-list` |
| `count` | `model._default_manager.count()` |

## Permission hooks

Define any of these on the panel and Vantage consults it before its own answer. The
signature is `(request, obj=None)` for all four verbs — including `add`, where
`contrib.admin` omits `obj`. One uniform signature is worth more than
bug-compatibility with an inconsistency, and the
[`contrib.admin` adapter](extending.md) bridges the difference for adopted admins.

```python
@register(Invoice)
class InvoicePanel(ModelPanel):
    def has_delete_permission(self, request, obj=None):
        return False        # nobody deletes an invoice, ever
```

`has_view_permission`, `has_add_permission`, `has_change_permission` and
`has_delete_permission` are consulted **before the superuser bypass**, deliberately. A
panel that says nobody may delete these rows means nobody.

Returning `None` means "no opinion" and hands the decision back to the normal chain.
Row-level scoping does not belong here — see
[why `filter_queryset` must be bulk](permissions.md#why-filter_queryset-must-be-bulk).

## Save hook

Define `save_model(request, obj, form, change)` and Vantage hands you the populated but
unsaved instance, which is the point: it is where you set what the form cannot.

```python
@register(Article)
class ArticlePanel(ModelPanel):
    readonly_fields = ["author"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.author = request.user
        obj.save()
```

The hook must call `obj.save()` itself. Vantage writes the many-to-many relations
afterwards, so that is one thing every hook does not have to remember. The same hook is
used by the CSV importer and by inline cell editing, so a rule written once applies to
all three write paths.

## Detected conventions

Vantage recognises the modelling conventions the Django community has settled on, and
offers the matching affordance with no configuration at all: a tree view for a
hierarchy, a trash bin with restore for a soft delete, time grouping for a date column,
publish/unpublish buttons for a publication flag, currency formatting for a money
column.

Seven detectors run, and each is pure: model in, description out. No queries, no
request, no clock. The bar for a detection is deliberately higher than for column
inference, because a false positive costs more than a miss — a model wrongly treated as
soft-deleted *hides rows*. Every detector therefore wants both a name the convention
dictates **and** a field type that makes the convention work.

| Pattern | Recognises | Switch off | Name the field |
|---|---|---|---|
| `tree` | a self-FK called `parent`, django-mptt, django-treebeard | `tree = False` | `tree_parent_field = "..."` |
| `soft_delete` | `deleted_at`, `date_deleted`, `deleted_on`, `is_deleted`, `deleted` | `soft_delete = False` | `soft_delete_field = "archived_at"` |
| `timeline` | a date to group rows by, preferring a domain date over an audit stamp | `timeline = False` | `timeline_field = "..."` |
| `sortable` | `position`, `sort_order`, `display_order`, `sequence`, `order`, `sort` | `sortable = False` | `sortable_field = "..."` |
| `publishable` | a `status` choice field, `is_published`, or `published_at` | `publishable = False` | `publishable_field = "..."` |
| `ownership` | `owner`, `user`, `created_by`, `author`, `creator`, `added_by` | `ownership = False` | `owner_field = "..."` |
| `money` | a field name ending in `price`, `total`, `amount`, `cost`, `fee`, `balance`, `revenue` | `money = False` | `money_fields = [...]` |

Setting the boolean to `True` asserts the convention and waives the naming rule — with
one exception: `tree = True` on a model with two self-referential foreign keys still
needs `tree_parent_field`, because which one is the parent would otherwise be a coin
flip.

Inspect what fired:

```pycon
>>> site.get_panel(Customer).get_patterns().detected
('soft_delete', 'timeline')
>>> site.get_panel(Category).get_patterns().detected
('tree',)
>>> site.get_panel(Review).get_patterns().detected
('timeline', 'publishable')
>>> site.get_panel(Order).get_patterns().money
(MoneyField(name='total', decimal_places=2),)
```

A model with a publication flag and no declared actions gets publish and unpublish
actions for free. That happens **only** when the panel offers no actions of its own: an
author who wrote actions has thought about which ones belong, and silently adding two
more to their list is the kind of helpfulness that reads as a bug.

## Validation

`manage.py check` validates every user-written panel. Panels left entirely at their
defaults are skipped.

| ID | Reported when |
|---|---|
| `vantage.E010` | `list_display` names something that is not a field, a model attribute or a panel method |
| `vantage.E011` | `list_filter` names something that is not a real field path |
| `vantage.E012` | `list_search` names something that is not a real field path |
| `vantage.E013` | `ordering` names something that is not a real field path |
| `vantage.E014` | `list_link` is not one of the columns in `list_display` |
| `vantage.E015` | `list_editable` names something that is not a field |
| `vantage.E016` | A column is in both `list_editable` and `list_link` |
| `vantage.E017` | `layout` names a field the model does not have |

To extend this for your own panel base class, subclass `ModelPanelChecks` and point
`checks_class` at it.

## Declared but not yet wired up

These attributes exist on `ModelPanel` and are accepted without error, but nothing
currently reads them. They are listed here so you do not spend an afternoon wondering
why setting one changed nothing.

| Attribute | Status |
|---|---|
| `inlines` | Not implemented. Editing related rows from a parent's form is not built |
| `related_tabs` | Not implemented. The detail page always shows related-record blocks |
| `row_actions` | Not implemented. Use [`slots`](extending.md) and the `row_actions` slot |
| `permissions` | Not implemented. Configure the resolver with [`VANTAGE["PERMISSIONS"]`](permissions.md#swapping-the-resolver) or use the per-panel `has_*_permission` hooks |
| `list_select_related` | Not read by the core. Relation planning is derived from the rendered columns instead, which is strictly better; the attribute exists only so the `contrib.admin` adapter has somewhere to put what it found |

## Second panels and second sites

A proxy model is the normal way to give one table two differently-configured presences
in the panel. Proxy models are deliberately kept by auto-registration for that reason.

```python
class UnpaidOrder(Order):
    class Meta:
        proxy = True
        verbose_name = "unpaid order"


@register(UnpaidOrder)
class UnpaidOrderPanel(ModelPanel):
    group = "Commerce"

    def get_queryset(self, request):
        return super().get_queryset(request).filter(status="pending")
```

For a genuinely separate panel — a staff panel and a partner panel, say — instantiate
`VantageSite` again and mount it at another URL:

```python
from vantage import VantageSite

partner_site = VantageSite(name="partner")
partner_site.register(Order, PartnerOrderPanel)

urlpatterns = [
    path("admin/", site.urls),
    path("partner/", partner_site.urls),
]
```

Note that the `VANTAGE` settings dict is global: both sites share the branding, the
navigation declaration and the permission resolver.

## See also

- [Inference](inference.md) — what the defaults will be
- [Lists](lists.md) — the list page in detail
- [Forms](forms.md) — widgets, layout, readonly, delete preview
- [Permissions](permissions.md) — the three-layer model
