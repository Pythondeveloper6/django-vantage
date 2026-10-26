# Pages and dashboards

[← Index](index.md)

Two escape hatches from the model-shaped world: a screen that is not a model, and the
landing page that greets everyone.

## Custom pages

A reporting screen, an operations console, a settings form. The bar this API has to
clear is that adding one should feel no heavier than writing a `ModelPanel` — a class, a
title, and a method that returns some data.

```python
# ops/panel.py
from vantage import Page, site

from shop.models import Order


@site.register_page
class RevenuePage(Page):
    title = "Revenue"
    icon = "trending-up"

    def get_context(self, request):
        return {"total": Order.objects.total_revenue()}
```

Everything else has a default. The URL comes from the class name, the nav entry appears
on its own, and the template falls back to a generic renderer that displays whatever
`get_context` returned — so a page with no template at all still produces a presentable
screen, which is what keeps the first one cheap to write.

### Attributes

| Attribute | Default | |
|---|---|---|
| `title` | the slug, prettified | Nav label and page heading |
| `description` | `""` | A line under the heading |
| `icon` | `None` | Lucide icon name |
| `group` | `"Pages"` | Which sidebar group it sits in |
| `order` | `0` | Sorts the group |
| `path` | `"<slug>/"` | URL segment, relative to where the site is mounted |
| `slug_override` | `None` | An explicit slug, if the derived one is not what you want |
| `permission` | `None` | A permission string, or a callable taking the request |
| `template_name` | `None` | Falls back to `vantage/pages/<slug>.html`, then `vantage/page.html` |

### Slugs and URLs

The trailing `Page` is dropped, because every class has it and repeating it in every URL
would be noise, and CamelCase becomes hyphens.

```pycon
>>> class RevenueReportPage(Page): ...
>>> RevenueReportPage.get_slug()
'revenue-report'
>>> RevenueReportPage.get_path()
'revenue-report/'
```

Reverse it as `vantage:page-<slug>` — `reverse("vantage:page-revenue-report")`.

A page with nothing declared at all still works:

```pycon
>>> class BarePage(Page): ...
>>> BarePage.get_slug(), BarePage.get_title()
('bare', 'Bare')
>>> BarePage().get_template_names()
['vantage/pages/bare.html', 'vantage/page.html']
```

Custom pages are routed **before** model routes, so a page may deliberately claim a path
that would otherwise look like an app prefix.

### Permissions

```python
class PayoutsPage(Page):
    permission = "finance.view_payout"           # a Django permission string


class BetaPage(Page):
    permission = staticmethod(lambda request: request.user.email.endswith("@acme.com"))
```

A callable receives the request, so a page can be gated on anything — a feature flag,
group membership, the time of day — without inventing a permission just to express it.

The nav consults the same gate, so a page you cannot open is not listed. Listing a page
that 403s on click is a worse experience than omitting it, and for a page gated on
something sensitive the label itself can be a disclosure.

### Templates

Extend Vantage's base to inherit the chrome:

```html
{% extends "vantage/base.html" %}

{% block content %}
  <h1>{{ page.get_title }}</h1>
  <p>{{ total }}</p>
{% endblock %}
```

Or supply nothing at all and let `vantage/page.html` render `get_context`'s return
value.

### The built-in pages

Four pages are registered by the app config rather than left for projects to add,
because they are only useful if they are always there — one you have to switch on is one
you never remember exists.

| Page | Path | Access |
|---|---|---|
| Schema | `/_/schema/` | anyone who can reach the panel |
| System | `/_/health/` | superusers only |
| Data quality | `/_/data-quality/` | superusers, unless `DATA_QUALITY["PERMISSION"]` widens it — see [Data quality](data-quality.md) |
| Roles | `/_/roles/` | superusers, unless `ROLES_PERMISSION` widens it; only with `django.contrib.auth` — see [Roles](roles.md) |

All four register **before** autodiscovery, so a project can unregister or replace one from
its own `panel.py`.

The **schema map** is derived from the registry at request time, so it is always
current, and every node is a link — seeing that Orders point at Customers and clicking
straight through is a different act from reading a picture. The layout is computed in
Python rather than by a JavaScript graph library: a schema graph is tens of nodes, not
thousands, and a server-side layered layout means the page needs no JavaScript, works
with the print stylesheet, and cannot flash. Models the user cannot view are omitted.

```pycon
>>> graph = build_graph(site, request)
>>> len(graph.nodes), len(graph.edges)
(9, 9)
```

The **system page** is covered under [Deployment](deployment.md#the-system-page).

## Dashboards

A dashboard is a class with a method, exactly like a page.

```python
# shop/dashboard.py
from django.db.models import Avg, Count, Sum

from shop.models import Order, Product, Review
from vantage import Chart
from vantage.dashboards import (
    ChartCard, Dashboard, ListCard, StatCard, TableCard, TemplateCard,
)


class ShopDashboard(Dashboard):
    title = "Overview"
    description = "Six months of trading, computed on request."

    def get_widgets(self, request):
        return [
            StatCard("Revenue", Order.objects.exclude(status="refunded"),
                     aggregate=Sum("total"), format="currency", trend="created_at"),
            StatCard("Orders", Order.objects.all(), trend="created_at"),
            StatCard("Average order", Order.objects.all(),
                     aggregate=Avg("total"), format="currency"),
            StatCard("Refunded", Order.objects.filter(status="refunded"),
                     tone="destructive"),
            ChartCard("Revenue over time", Order.objects.all(),
                      chart=Chart.timeseries("Revenue", date="created_at",
                                             value=Sum("total"))),
            ChartCard("Orders by status", Order.objects.all(),
                      chart=Chart.breakdown("Orders", group_by="status",
                                            value=Count("id"), kind="donut")),
            TableCard("Latest orders", Order.objects.all(), limit=5),
            TableCard("Newest reviews", Review.objects.select_related("product"),
                      columns=["headline", "product", "rating"], limit=5),
            ListCard("Catalogue", lambda request: [
                ("Products", Product.objects.count()),
                {"label": "Out of stock",
                 "value": Product.objects.filter(stock=0).count(),
                 "tone": "warning"},
            ]),
            TemplateCard("Five-star reviews", "shop/_five_star.html"),
        ]
```

Point at it:

```python
VANTAGE = {"DASHBOARD": "shop.dashboard.ShopDashboard"}
```

That is the demo's dashboard, and it renders ten cards.

### Widgets

| Widget | Default width | |
|---|---|---|
| `StatCard(label, source, aggregate=None, format="number", tone=None, trend=None, currency="$")` | 1 | One number over a queryset |
| `ChartCard(label, source, chart=Chart…)` | 2 | One chart, drawn as server-side SVG |
| `TableCard(label, source, columns=None, limit=5, order_by=None)` | 2 | The most recent handful of records, linked |
| `ListCard(label, items)` | 1 | Arbitrary label/value pairs |
| `TemplateCard(label, template_name, context=None)` | 2 | Your own template inside a card |

Every widget also takes `width` (1 to 4 — the grid is four columns), `permission`,
`description`, `url` and `icon`.

```pycon
>>> StatCard("Too wide", Order.objects.all(), width=9)
ValueError: Widget 'Too wide' declares width 9; the dashboard grid is 4 columns wide,
            so width must be 1 to 4.
```

`StatCard`'s aggregate defaults to a row count, because that is what most of the first
cards on any dashboard are and it should not have to be spelled out.

`TableCard` is deliberately short and unpaginated: a glance at what just happened with a
way through to the list page, not a second list page. Its columns default to the
model's inferred ones, capped at four, and it renders cells through the model's
registered panel, so your own cell formatting applies. `select_related` is planned from
those columns — five rows with a relation column is still five extra queries otherwise,
and a dashboard is the page most likely to hold several of these at once.

`ListCard` items are `(label, value)` pairs or dicts with `label` / `value` and optional
`url` and `tone`.

The `source` of a queryset widget may be a **queryset**, a **manager**, or a
**callable** returning one. A callable is the escape hatch for anything that depends on
the request, and it may take `request` or no arguments at all — `Order.objects.all` and
`lambda request: …` are equally natural things to write.

### Four load-bearing properties

**Nothing is computed at import time.** Widgets are declarations; the queryset a widget
holds is never evaluated until the dashboard renders inside a request. A module-level
queryset is re-cloned per request with `.all()`, so it cannot cache its rows the first
time it is iterated and serve that snapshot to every later request — the classic
module-level-queryset bug.

**A widget the user may not see never runs its query.** The permission gate is checked
*before* the card is rendered, not after. A card that computes a number and then hides it
has already leaked it into the query log and paid for it. A queryset widget infers its
model from its source, so it is gated on the same `view` permission the model's own
pages are, and can never become a side channel around the sidebar. A callable source
cannot be inspected without running it — and running user code to decide whether the
user may run it is backwards — so those widgets declare `model=` or `permission=`.

Every queryset also passes through `filter_queryset`, so a number computed over rows you
cannot list is not a leak.

**Widgets fail independently.**

```pycon
>>> D(site).render(request)          # one card has a broken aggregate
[('Broken', error=True), ('Fine', error=False)]
```

An exception inside one card renders that card as an error and leaves its neighbours
intact. A dashboard that 500s because one team's custom aggregate broke is a dashboard
nobody trusts.

**Aggregation is not reimplemented here.** Every number and every chart goes through the
same code the [list page's analytics](analytics.md) use, so the dashboard inherits its
formatting, its trend windows and its join-fanout correctness for free.

Dashboard cards are **never budgeted**. The [budget gate](analytics.md#the-budget)
belongs to the list page, where analytics are chrome above the thing the user came for.
A dashboard *is* the thing the user came for.

### The default dashboard

Zero configuration is the package's thesis, so the default cannot be a placeholder.
Declare no `DASHBOARD` and you get:

- a **count card** for every model you may see, in registry order so the cards read in
  the same order as the sidebar (capped at 12);
- a **timeseries of new records** for the most interesting model that stamps a creation
  date;
- a **recent-records table** for up to three such models.

```pycon
>>> DefaultDashboard(site).render(request)
13 cards: Groups, Users, Categories, Customers, Orders, Order items, Products,
          Reviews, Tags, New orders (chart), Recent orders, Recent products,
          Recent reviews
```

Two details.

**Permission filtering happens before the cap.** Capping first would mean a user who can
see only one model, and whose model sorts outside the first twelve, gets an entirely
empty dashboard — the cap would silently have become an access rule.

**Framework apps sort last for the space-hungry cards.** `admin`, `auth`,
`contenttypes`, `sessions` and `vantage` still get a count card, but a chart of user
signups on a two-account project is a flat line where the project's own orders would
have been a shape.

The "when was this created" field is found separately from the timeline pattern
detector, because they answer different questions: the detector picks the best field to
*group by* and therefore prefers a domain date such as `published_at`, while "recent
records" and "new per week" need the date the row came into existence. `auto_now_add` is
proof of that and wins outright.

```pycon
>>> creation_field(Order), creation_field(Product), creation_field(Customer)
('created_at', 'created_at', 'joined_on')
```

A nullable date is no good here — half the rows would fall outside every bucket — so it
is refused.

## Arranging the dashboard from the UI

Everything above is the *code* layer. `?arrange=1` puts the dashboard into an editing
mode with a row underneath that code, following the same shape
[the per-list customisation rail](lists.md#the-customisation-rail) and
[`vantage.listprefs`](configuration.md#the-same-shape-wherever-it-appears)
established first:

```
per-user row  >  site-wide row  >  VANTAGE["DASHBOARD"]  >  DefaultDashboard
```

A stored arrangement is a list of entries, and each entry is one of two things: a
pointer at a card the code already declares — by key, so the declaration stays the
source of what the card *means* while the row only says where it sits, how wide it is
and whether it currently shows — or a card someone built entirely in the UI, over any
model they may view.

### Building a card without writing Python

The "Add a card" picker offers exactly two shapes — a stat or a chart, not the table
and list cards, which need columns and hand-written pairs no picker can meaningfully
ask for. Choosing a model, an aggregate (count, sum, average, min, max), a column to
aggregate over, and — for a chart — a date field to bucket by or a low-cardinality
column to break down by, produces a card that runs through exactly the same
`vantage.stats.compute_panel` every Python-declared `Stat` and `Chart` does. There is
one aggregation engine in this package; a card built in the UI is not a second,
lesser one.

A card may also carry a **filter**, re-expressed as the query string the list page's
own filter rail would produce — the same parsing, the same rejection of a parameter
the panel does not own. A filter path that would fan rows out through a to-many join is
refused outright rather than silently double-counted: a list page can hide that fan-out
behind `DISTINCT`, but a sum cannot, and a card reporting a number nobody can reproduce
is worse than a card that refuses to build.

**The model picker only ever offers what the viewer may already open** —
`permissions.visible_panels(request, ...)`, computed before any cap is applied, because
capping a registry slice *before* filtering it by permission is exactly the bug that
once gave a restricted user a blank default dashboard (see
[the default dashboard](#the-default-dashboard)). Every field offered for aggregation,
grouping or dating is drawn from that model's own panel, so the picker can never offer
a column the panel does not expose, and the same allowlist validates the POST that
follows — the editor cannot offer a control whose value the write end would then
reject.

### Rearranging, hiding, and the two save scopes

Cards drag to reorder within the grid and resize across it (1 to 4 columns wide); a
declared card can be switched off (`show: False`) without touching the Python that
declares it, and a UI-built card can be deleted outright.

Saving for yourself is the default. Saving "for everyone" — what every colleague sees
tomorrow morning — needs the same permission [the customisation rail's site-wide
save](lists.md#the-customisation-rail) does,
[`LIST_PREFS_PERMISSION`](settings.md#behaviour) (superuser by default): a request for
the site-wide row without it is refused outright rather than quietly downgraded to a
personal save, because a silent downgrade looks exactly like success and the person
would never learn their colleagues saw nothing change. "Reset" writes `NULL`, not
today's resolved layout, for the same reason a Python `panel.py` has to stay the
answer after a reset — see [How configuration resolves](configuration.md).

### `known_keys`: telling "removed" from "not yet seen"

A declared card the stored arrangement never mentions is appended at the end rather
than dropped — the same choice `NAV_UNLISTED = "append"` makes for the sidebar, and for
the same reason: a developer adding a card to `get_widgets()` after a colleague last
saved an arrangement must not have it silently swallowed by that older row.

But "never mentions" is ambiguous by itself — a key absent from a stored arrangement is
either a card that did not exist when the arrangement was last saved, or one that did
and was deliberately left out. `known_keys` is the snapshot that tells the two apart:
every declared key that existed in the code the moment the layout was last written is
recorded on the row alongside it. A key recorded there and now absent from the
arrangement was a decision, and stays gone; a key that is neither mentioned nor
recorded is new since that save, and is appended automatically.

```pycon
>>> layout.known_keys      # captured at the moment this row was last saved
['stat:revenue', 'chart:orders-over-time', 'table:latest-orders']
>>> # a fourth card added to get_widgets() afterwards is not in known_keys,
>>> # so it appears on every dashboard using this layout without anyone re-saving
```

### What this costs, and what it does not widen

The two properties that carry the code layer carry this one too: **the permission
check happens before the query**, so a card over a model the viewer cannot see is
neither drawn nor computed and never reaches `aggregate()`; and **the cost is one
aggregate per card**, because every card — declared or UI-built — goes through
`compute_panel` and inherits its join-bucket partitioning rather than growing a second
aggregation path here. A stored arrangement is capped (60 entries total, 24 of them
UI-built — the smaller number bounds the query cost, since a pointer at a declared card
costs nothing the code was not already going to spend) and a card's filter is capped in
length, but neither cap is a security boundary: every card is a validated aggregate
over a column the panel already exposes, never an arbitrary expression.

## The command palette

<kbd>⌘K</kbd> / <kbd>Ctrl+K</kbd>. A palette is a promise that anything reachable by
clicking is reachable by typing, and the only honest way to keep that promise is to
build the list from the same facts the sidebar is built from rather than from a
hand-maintained copy.

So navigation and create commands are derived from the registry and from the registered
pages, every one filtered through the permission layer on the way past. That filtering
is not cosmetic: a palette is *read* as a menu of what you are allowed to do, and
listing "Delete customers" to someone who cannot delete customers is worse than
useless, because they will try.

Commands are shipped to the browser as JSON and filtered there, because a palette that
round-trips on every keystroke of "das" to find "Dashboard" is not a palette. Record
search is the opposite case and goes to the server.

Add your own:

```python
site.register_command(
    id="reindex-search",
    label="Reindex search",
    url="/ops/reindex/",
    icon="refresh-cw",
    section="Actions",
    permission="ops.run_reindex",
)
```

`id` is required — it is what lets a plugin replace a command a project registered first
(or vice versa) instead of the palette silently carrying two rows for the same action.

Switch the whole thing off with `VANTAGE["COMMAND_PALETTE"] = False`. The global search
results page stays available and styled either way.

## See also

- [Analytics](analytics.md) — `Stat` and `Chart` in full
- [Permissions](permissions.md) — how a widget is gated
- [Extending](extending.md) — slots, for putting your own HTML on an existing page
- [Settings](settings.md) — `DASHBOARD`, `COMMAND_PALETTE`
- [How configuration resolves](configuration.md) — the resolution order the dashboard's
  UI arrangement layer follows, stated once for every surface that uses it
- [Lists](lists.md#the-customisation-rail) — the same UI-override pattern, one page
  over, including `LIST_PREFS_PERMISSION` and what "reset" actually does to the row
