# Analytics

[← Index](index.md)

Numbers on a list page, declared in Python and computed in SQL against the queryset the
user is actually looking at. Three things:

| | Where | Configuration | Default |
|---|---|---|---|
| [The summary bar](#the-summary-bar) | a strip at the foot of the table | none needed | **on** |
| [`list_stats`](#stats) | cards above the table | `list_stats = [Stat(...)]` | off |
| [`list_charts`](#charts) | charts above the table | `list_charts = [Chart(...)]` | off |

Change a filter and every number follows, because all three are computed from the
filter predicate rather than from the rows that happened to be rendered.

## The summary bar

Of the three places a product can put list-page analytics, this is the cheapest and the
highest value per pixel. Filament puts stat cards above the table and spends 100–140px
of vertical height on every page load, before the user reaches the thing they came for.
Airtable puts a single 32px strip at the bottom with a per-column aggregate, and it
costs almost nothing. This is the Airtable one — and unlike `list_stats` it is **on by
default**, because it needs no configuration to be useful: a column of money wants
summing whether or not anyone said so.

The whole bar folds into one `aggregate()` call over the filtered queryset, however many
columns it covers.

```pycon
>>> infer_summaries(order_panel, order_panel.get_list_display())
{'total': 'sum'}
>>> compute_summary(Order.objects.all(), {'total': 'sum'})     # 1 query
[SummaryCell(column='total', function='sum', value=Decimal('227298.26'),
             display='227,298.26')]
```

### What each column gets

Conservative on purpose. A wrong number in a footer is worse than a blank one, because
a reader has no way to tell it is wrong.

1. **Nothing** if the column name is, or ends in, an identifier word: `id`, `pk`,
   `number`, `code`, `year`, `zip`, `postcode`, `phone`. Summing an order number
   produces a large meaningless integer, which erodes trust in the whole row.
2. **Average** if the name contains a rate or score word: `rating`, `score`, `percent`,
   `percentage`, `rate`, `ratio`, `average`, `avg`.
3. **Sum** if the field is an `IntegerField`, `DecimalField` or `FloatField`.
4. **Nothing** otherwise.

Only a **local concrete column** is ever aggregated. Aggregating across a join silently
multiplies values through the fan-out, and a footer is the last place to discover that.

Real results from the demo project:

```pycon
>>> # Order:   ['number', 'customer', 'status', 'total', 'shipping_city', 'created_at']
{'total': 'sum'}                    # 'number' is an identifier word
>>> # Product: ['name', 'sku', 'category', 'status', 'price', 'stock']
{'price': 'sum', 'stock': 'sum'}
>>> # Review:  ['headline', 'product', 'customer', 'rating', 'is_published', 'created_at']
{'rating': 'avg'}
```

### Overriding it

```python
@register(Product)
class ProductPanel(ModelPanel):
    list_aggregates = {
        "price": "avg",     # a name, or the class: Avg
        "stock": None,      # suppress a column the footer would otherwise sum
    }
```

Function names are `sum`, `avg`, `min`, `max`, `count`. Passing the aggregate class
itself works too, so `{"total": Sum}` reads naturally next to the `Stat` declarations
above it.

Users can also change the function per column from the
[customisation rail](lists.md#the-customisation-rail), within the same allowlist.

## Stats

```python
from django.db.models import Avg, Count, Q, Sum
from vantage import ModelPanel, Stat, register


@register(Order)
class OrderPanel(ModelPanel):
    list_filter = ["status", "created_at", "customer"]

    list_stats = [
        Stat("Revenue", Sum("total"), format="currency", trend="created_at"),
        Stat("Orders", Count("id")),
        Stat("Average order", Avg("total"), format="currency"),
        Stat("Refunded", Count("id", filter=Q(status="refunded")), tone="destructive"),
    ]
```

Rendered over the demo's 200 orders:

```
Revenue $227,298.26  ↑ +$227,298.26     Orders 200
Average order $1,136.49                 Refunded 9
```

Narrow the page to `?status=paid` and every card follows:

```
Revenue $30,470.04    Orders 37    Average order $823.51    Refunded 0
```

### `Stat`

| Argument | Default | |
|---|---|---|
| `label` | required | The card's title |
| `value` | required | An aggregate expression, or a callable taking the filtered queryset |
| `format` | `"number"` | `number`, `currency`, `percent`, `duration` |
| `tone` | `None` | Passed through to the template as `data-tone`; `success`, `warning`, `destructive`, `primary` are styled |
| `trend` | `None` | Names a date field; adds a delta against the preceding period |
| `description` | `None` | A line of explanatory text under the number |
| `currency` | `"$"` | Prefix for `format="currency"` |
| `key` | slug of the label | Stable identifier, used by the customisation rail |

Formatting, verified:

```pycon
>>> format_value(1234.5, "currency")
'$1,234.50'
>>> format_value(0.4237, "percent")
'42.4%'
>>> format_value(3725, "duration")
'1h 2m'
>>> format_value(12000)
'12,000'
```

`percent` reads its input as a **fraction**, because the aggregates that produce
percentages — an `Avg` over a boolean, a ratio of two counts — naturally yield 0..1, and
guessing from magnitude would make `0.5` ambiguous.

An empty filtered set is a normal state, not an error: `Sum` returns NULL and the card
reads `0`, not `None` and not a traceback.

### Trends

`trend="created_at"` computes the same aggregate over the period **immediately
preceding the data's own span**, and renders the pair as a delta chip with a direction
of `up`, `down` or `flat`.

The span comes from the data rather than from a fixed window, so a list showing three
days compares against the previous three days and a list showing two years compares
against the previous two.

The comparison period is folded into each aggregate's `FILTER (WHERE …)` clause rather
than being a separate query per stat, and it composes with any `filter=` the author
already wrote instead of replacing it. That is what lets every trending stat in a group
share **one** extra query.

One subtlety worth knowing: when the user filters on the trend field itself, the
preceding period has been filtered away and the trend reads flat. The list page passes
a baseline queryset with that one filter removed — the same trick faceted counts use —
so the comparison stays real.

### The query-cost guarantees

**One round trip for the whole stat row.** Filament's canonical example runs one
`COUNT(*)` per card, each re-running the full filtered query. Every stat that can share
an `aggregate()` call does.

```pycon
>>> plan_queries(Order, stats)      # four stats, one of them trending
2
>>> with CaptureQueriesContext(connection):
...     compute_stats(Order.objects.all(), stats)
2 queries
```

Two, not one: **one** aggregate for the four stats, and **one** more for the trend
window shared by every trending stat in the group. Four non-trending stats cost exactly
one query.

**Sharing an `aggregate()` call is not always safe.** Two aggregates over *different*
multi-valued relations produce a cartesian product and **both** numbers come out wrong
(Django ticket #10060). `distinct=True` rescues `Count` and nothing else. So stats are
partitioned by the join they force, and only joins that agree share a query. This is
the one place where correctness costs a query, and it takes it.

A single stat that reaches across two multi-valued relations in one expression cannot
be rescued at all, and is refused with an explanation:

```
Stat 'Total' aggregates across the multi-valued relations ['items', 'reviews'] in one
expression. Two joins in one aggregate produce a cartesian product and both numbers
come out wrong; distinct=True fixes Count only. Express one of them as a correlated
Subquery instead.
```

**Stats come from the filter predicate, never from the rendered page.** Retool's summary
row aggregates the rows it happens to have loaded, which stops being true the moment the
result exceeds one page. A sliced queryset is rejected outright:

```pycon
>>> compute_stats(Order.objects.all()[:10], stats)
ValueError: Stats must be computed from the filtered queryset before pagination.
            A sliced queryset would aggregate the current page only.
```

### The escape hatch

`value` may be a callable taking the filtered queryset, for anything the ORM cannot say
in one expression:

```python
Stat("Churn rate", lambda qs: churn_for(qs), format="percent")
```

A callable costs whatever it costs and is never combined with anything. If it raises,
the card renders as `—` with the error attached rather than taking the page down.

## Charts

Server-rendered inline SVG. No JavaScript charting library, no client-side data
fetch — the colours are `currentColor` and `var(--primary)`, so a chart follows the
theme and a `BRAND_COLOR` change without a second stylesheet, and `viewBox` plus a width
of 100% makes it responsive without a resize listener. Each carries `role="img"` and an
`aria-label` stating the shape and the extremes.

```python
from vantage import Chart

list_charts = [
    Chart.timeseries("Revenue over time", date="created_at", value=Sum("total")),
    Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut"),
]
```

`kind` is `"line"` (the default and the fallback), `"bar"` or `"donut"`.

### `Chart.timeseries`

| Argument | Default | |
|---|---|---|
| `label` | required | |
| `date` | required | The date or datetime field to bucket by |
| `value` | required | The aggregate per bucket |
| `kind` | `"line"` | |
| `bucket` | chosen from the data | `"day"`, `"week"` or `"month"` |
| `key` | slug of the label | |

The bucket is chosen from the span of the data unless pinned, so a chart of one week and
a chart of five years are both readable without the author choosing:

| Span | Bucket |
|---|---|
| up to 62 days | day |
| up to 730 days | week |
| beyond that | month |

Chosen so a chart never renders more than roughly 100 points from its default bucket.
There is a hard ceiling of 500 points whatever the bucket.

**Gaps are filled with zeroes.** A line chart that skips an empty week draws a slope
where the truth is a floor, and the fill is free.

```pycon
>>> compute_charts(Order.objects.all(), [Chart.timeseries(
...     "Revenue over time", date="created_at", value=Sum("total"))])
ChartResult(label='Revenue over time', kind='line', bucket='week',
            labels=['2026-02-09', '2026-02-16', '2026-02-23', ...],   # 27 points
            series=[{'name': ..., 'data': [682.2, 3560.64, 3986.87, ...]}])
```

### `Chart.breakdown`

| Argument | Default | |
|---|---|---|
| `label` | required | |
| `group_by` | required | The field to group by |
| `value` | required | The aggregate per group |
| `kind` | `"bar"` | |
| `segments` | `20` | Segments drawn before the remainder folds into "Other" |
| `allow_high_cardinality` | `False` | Waive the refusals below |
| `key` | slug of the label | |

Choice labels are resolved, booleans render as Yes/No, and NULL renders as `—`.
Segments are ordered by value with the group value breaking ties, so a redraw of the
same data never reshuffles the segments under the reader.

```pycon
>>> Chart.breakdown("By status", group_by="status", value=Count("id"), kind="donut")
ChartResult(labels=['Shipped', 'Paid', 'Cancelled', 'Refunded', 'Pending'],
            series=[{'data': [131, 37, 19, 9, 4]}])
```

Two refusals, both reported inside the chart's own card rather than as a page error:

- **A unique or primary-key `group_by`** — one segment per row is not a chart.
- **More than 200 distinct groups** — a breakdown of it would be unreadable. The query
  is sliced one past the limit, so the refusal costs nothing extra.

The remainder beyond `segments` folds into an "Other" bucket **only when the aggregate
is additive** (`Count` or `Sum`). An average cannot be summed, so a truncated breakdown
of averages reports no remainder rather than a wrong one.

### Chart query cost

One grouped query per chart, plus **one shared span probe** — a single `Min`/`Max`
aggregate covering every timeseries whose bucket is not pinned. That probe is what lets
the same declaration render a week of data by day and five years by month.

```pycon
>>> plan_queries(Order, charts=charts)      # two charts, one auto-bucketed
3
>>> with CaptureQueriesContext(connection):
...     compute_charts(Order.objects.all(), charts)
3 queries
```

Pin every bucket and the probe disappears.

A chart that raises a `FieldError` renders its error in that region only. A widget
failing must not take the table down with it.

## The budget

The default list page must never be slower because stats exist. Stats and charts are
therefore planned before they are run, and the plan is measured against a budget.

| `Budget` field | Default | |
|---|---|---|
| `enabled` | `True` | |
| `max_queries` | `8` | Covers stats and charts **together**, so a page cannot slip past by splitting the cost |
| `max_rows` | `None` | Compared against an estimate the caller supplies |

When the plan exceeds the budget the whole strip is returned **deferred**: zero queries,
a reason, and a flag the template renders as a "compute" button. It is not made cheaper
by silently computing something less correct.

`max_rows` is compared against an estimate the *caller* supplies. This layer never
counts rows itself — `COUNT(*)` under MVCC is exactly the cost being avoided.

Switching the strip off from the [customisation rail](lists.md#the-customisation-rail)
removes its **queries**, not merely its markup. A version that computed the panel and
then hid it would leave the page exactly as slow as the thing the user turned off to
make it faster.

Dashboard cards are **never** budgeted. The budget gate belongs to the list page, where
analytics are chrome above the thing the user came for; a dashboard *is* the thing the
user came for. See [Pages and dashboards](pages-and-dashboards.md#widgets).

## What this module does not decide

Nothing here decides how anything looks. Results are plain, JSON-serialisable values
plus a preformatted display string; tone names are passed through untouched. Colour,
icons and layout belong to the template layer — which is also why a widget result could
be serialised for an asynchronous refresh later without redesigning anything.

## See also

- [Lists](lists.md) — the page these sit on
- [Pages and dashboards](pages-and-dashboards.md) — the same machinery on a dashboard
- [Theming](theming.md) — where a chart's colour comes from
