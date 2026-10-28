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

That first branch is not incidental: it is the same **UI override → `panel.py` →
inference** order every configurable surface in the package follows. See
[How configuration resolves](configuration.md) for the one page that states it and
explains why the order runs that way round.

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

## List views

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `list_views` | `list[str]` | inferred | Views the switcher offers, first is the default. `["table"]` switches the others off |
| `kanban_field` | `str` | inferred | Choices or boolean field the board has one column per value of |
| `calendar_field` | `str` | inferred | Date or datetime field the calendar places records by |
| `kanban_fields` | `list[str]` | inferred | What the board's "Group by" picker offers; `[]` hides it |
| `calendar_fields` | `list[str]` | inferred | What the calendar's "Dates from" picker offers; `[]` hides it |
| `gallery_field` | `str` | inferred | Image (or picture-named file) field a gallery card leads with |
| `map_fields` | `str` or `(str, str)` | inferred | A `PointField`, or a `("latitude", "longitude")` pair |
| `card_fields` | `list[str]` | inferred | Fields under a card's title on the board and in the gallery |

Details: [List views](list-views.md).

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
| `inlines` | `list[type[Inline]]` | `None` | Child records editable on the parent's own form — see [Editable inlines](forms.md#editable-inlines) |
| `related_tabs` | `bool \| None` | `None` | Group the detail page's related-record blocks into a tab strip. `None` decides by count (two or more); `True`/`False` assert it either way — see [The record page](forms.md#the-record-page) |
| `sensitive_fields` | `list[str]` | inferred | Fields masked on every surface, revealable one at a time. A list replaces inference; `[]` switches masking off — see [Sensitive fields](sensitive-fields.md) |

`readonly_fields` is a security boundary rather than a UI affordance. A readonly field
is not rendered as a control **and** is not in the form's field list, so a hand-crafted
POST that includes it is dropped by `construct_instance` rather than merely unrendered.

A name in `readonly_fields` that inference did not pick up is appended rather than
dropped — the canonical case being `readonly_fields = ["created_at"]` on an
`auto_now_add` column, which is not an editable field at all and would otherwise vanish
from a page whose whole purpose was to display it.

`inlines` is unset by default — nothing is inferred here, because guessing which of a
model's reverse relations belong on the parent's own form (rather than as the
read-only [related blocks](forms.md#the-record-page) the detail page already shows)
is a judgement call the package should not make for you. Full treatment, including a
gotcha worth knowing before you rely on the default: [Forms](forms.md#editable-inlines).

Details: [Forms](forms.md).

## Behaviour

| Attribute | Type | Default | Effect |
|---|---|---|---|
| `actions` | `list[str \| Action]` | `None` | Bulk actions offered on the list page |
| `row_actions` | `list[str]` | `None` | Which actions appear in the menu on each list row — see below |
| `undo` | `bool \| None` | `None` | Whether deletes and edits offer **Undo** in their toast. `None` follows `UNDO_WINDOW`; `False` turns it off for this model — see [Undo](undo.md) |
| `history` | `bool \| "view" \| None` | `None` | The record's [History](history.md) page and restoring from it. `None` follows `AUDIT_LOG`; `"view"` keeps the page without restore; `False` removes it |
| `requires_approval` | `list[str]` | `None` | Fields whose changes wait for someone with `<app>.approve_<model>` before they are saved; the rest of an edit saves as usual — see [Approvals](approvals.md) |
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

### Row actions

Bulk actions apply to a selection; `row_actions` puts the same kind of thing — plus a
handful of built-ins — into a menu on **one row**, so a reviewer never has to select a
single record just to publish it.

```python
@register(Review)
class ReviewPanel(ModelPanel):
    row_actions = ["view", "edit", "duplicate", "publish", "delete"]

    @action("Publish", icon="check")
    def publish(self, request, queryset):
        return f"Published {queryset.update(is_published=True)} reviews."
```

`row_actions` is not a second decorator or a second way of declaring behaviour — it is
a list of names, and each name resolves to one of two things:

- **A built-in**: `"view"`/`"detail"`, `"edit"`/`"change"`, `"duplicate"`/`"copy"`, or
  `"delete"`. Each renders as an ordinary link to the page that already does the job —
  see [Duplicating a record](forms.md#duplicating-a-record) for exactly what the
  duplicate page does with unique fields, files and related children.
- **The name of an `@action`-decorated method** — the exact same decorator [bulk
  actions](lists.md#bulk-actions) use, read for its label, icon, permission and
  `confirm` text rather than restated. It renders as a `POST` button, never a link, so
  a crawler cannot fire it.

Leaving `row_actions` unset offers nothing beyond what the row already links to. A
soft-deleted row gets a Restore button automatically wherever the [`soft_delete`
pattern](#detected-conventions) applies — that one is not declarable in `row_actions`
at all, because it only makes sense on a trashed row and never on a live one. See
[Deleting, and undoing it](lists.md#deleting-and-undoing-it) for what Delete and
Restore actually do to the row, and why a second Delete on an already-trashed row
destroys it for good.

For content that is not an action at all — a badge, a status dot, a link to somewhere
outside the panel — use the `row_actions` [slot](extending.md#slots) instead, which is
a different mechanism with the same name: it contributes arbitrary HTML to the same
trailing cell rather than declaring a button with a permission and a confirmation. A
panel that overrides `slot()` without calling `super()` loses its declared
`row_actions` silently, because rendering them is itself implemented as a slot
contribution — worth knowing if you use both on the same panel.

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
| `get_sensitive_fields()` | `frozenset[str]` | The masked field names — `sensitive_fields`, or inference |
| `can_reveal(request, obj, field_name)` | `bool` | Whether this request may reveal one value. Default: superusers and `vantage.reveal_sensitive` — see [Revealing a value](sensitive-fields.md#revealing-a-value) |
| `get_requires_approval()` | `tuple[str, ...]` | `requires_approval`, minus any entry a check reports (E106–E108) |

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
| `vantage.E018` | `list_select_related` names something that is not a field path, or crosses a multi-valued relation `select_related` cannot follow |
| `vantage.E019` | `related_tabs` is set to something other than `True`, `False` or `None` |
| `vantage.E020` | A [removed attribute](#removed-attributes) — currently only `permissions` — is still set |
| `vantage.E070` | `list_views` is not a non-empty list, or names a view that does not exist |
| `vantage.E071` | `list_views` names a view the model has no field for |
| `vantage.E072` | `kanban_field` is not a local field with 2–12 choices, or a `BooleanField` |
| `vantage.E073` | `calendar_field` is not a `DateField` or `DateTimeField` |
| `vantage.E074` | `gallery_field` is not an `ImageField` or `FileField` |
| `vantage.E075` | `map_fields` is neither a `PointField` nor a pair of `FloatField`/`DecimalField`s |
| `vantage.E076` | `card_fields` names something that is not a field, a model attribute or a panel method |
| `vantage.E078` | `kanban_fields` is not a list, or names a field a board cannot be grouped by |
| `vantage.E079` | `calendar_fields` is not a list, or names something that is not a date |
| `vantage.E077` | `undo` is set to something other than `True`, `False` or `None` |
| `vantage.E091` | `history` is set to something other than `True`, `False`, `"view"` or `None` |
| `vantage.E092` | `history = True` while `VANTAGE["AUDIT_LOG"]` is off |
| `vantage.E090` | `sensitive_fields` is not a list, or names something that is not a field or attribute |
| `vantage.W090` | `list_search`/`search_fields`, `list_filter` or `ordering` explicitly names a [sensitive field](sensitive-fields.md); it is dropped at request time |
| `vantage.E105` | `requires_approval` is not a list of field names |
| `vantage.E106` | `requires_approval` names something that is not a field of the model |
| `vantage.E107` | `requires_approval` names a field the panel's form does not let anyone edit (not editable, readonly, excluded or left out of `fields`) |
| `vantage.E108` | `requires_approval` names a many-to-many, a reverse relation or a file field, which cannot be held back |

`vantage.E020` is also issued from an unrelated place, `nav.py`, for a malformed
[`NAV`](settings.md#navigation) entry — the same numeric id covers two different
mistakes, which is a quirk of the current numbering rather than a hint that the two are
connected; read the message, not the id, if you hit it.

To extend this for your own panel base class, subclass `ModelPanelChecks` and point
`checks_class` at it.

## Declared but not yet wired up

Nothing, currently. `inlines`, `row_actions`, `list_select_related`, `related_tabs` and
`layout` all used to belong on this list and no longer do:

- [Editable inlines](forms.md#editable-inlines) and [Row actions](#row-actions) above.
- `list_select_related` **is** read: it is unioned into the query plan the optimiser
  already derives from the rendered columns (`get_list_queryset`), and it is validated
  (`vantage.E018`) — set it only for a join the optimiser has no other reason to make,
  such as one your own overridden `get_list_queryset` needs but no column or search
  path names.
- `related_tabs` groups the detail page's related-record blocks into a tab strip once
  there is a choice to make — two or more blocks by default, or always/never with an
  explicit `True`/`False`. See [The record page](forms.md#the-record-page).
- `layout` — `Tabs`, `Tab`, `Section` and `Row` — now replaces the fixed-section
  grouping on the create/edit form outright. See [The layout DSL](forms.md#the-layout-dsl).

## Removed attributes

`ModelPanel.permissions` does not exist. It is not silently ignored — setting it fails
`manage.py check` outright, as `vantage.E020`:

```
ERRORS:
<class 'InvoicePanel'>: (vantage.E020) 'permissions' is no longer a Vantage panel
attribute.
    HINT: Access is resolved in one place. Define has_view_permission /
    has_add_permission / has_change_permission / has_delete_permission on the panel,
    or point VANTAGE['PERMISSIONS'] at a PanelPermissions subclass.
```

A second declarative mechanism for the same decision is how two answers to "may this
user delete?" end up disagreeing, so it was removed rather than left dormant. Use the
[per-panel hooks](#permission-hooks) or [`VANTAGE["PERMISSIONS"]`](permissions.md#swapping-the-resolver)
instead.

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

- [How configuration resolves](configuration.md) — the UI-override layer that sits in
  front of every getter this page describes
- [Inference](inference.md) — what the defaults will be
- [Lists](lists.md) — the list page in detail
- [Forms](forms.md) — widgets, layout, readonly, delete preview, editable inlines
- [Permissions](permissions.md) — the three-layer model
