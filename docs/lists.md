# Lists

[← Index](index.md)

The list page is where people spend their time, so most of the package's effort is
here. This page covers the grid itself: columns, filtering, search, sorting, saved
views, the per-user customisation rail, bulk actions and moving data in and out.

Everything on this page is bounded by the permission layer first. The list queryset is
built as `panel.get_list_queryset(request)`, then narrowed by
[`filter_queryset`](permissions.md#why-filter_queryset-must-be-bulk), before a single
filter or search term is applied.

## Columns

`list_display` names them; when it is unset they are
[inferred](inference.md#list_display--the-columns). Entries may be:

- a field on the model — `"title"`
- a path across a relation — `"customer__name"`
- an attribute or property on the model — `"margin"`
- a method on the panel taking `(self, obj)` — see below

```python
@register(Order)
class OrderPanel(ModelPanel):
    list_display = ["number", "customer", "status", "total", "line_count"]

    def line_count(self, obj):
        return obj.items.count()
```

A method column cannot be sorted or filtered, because there is nothing for SQL to do
with it. A column that resolves to a real single-valued path is sortable; a
multi-valued one is not, because sorting across a fan-out silently duplicates rows.

### How a cell is rendered

Rendering is deliberately separate from inference: inference decides *which* columns
appear, rendering decides what each looks like, and a project can change one without
disturbing the other.

| Value | Rendered as |
|---|---|
| A field with `choices` | a badge showing the label, toned by the value |
| A detected money column | the value formatted to the field's decimal places |
| `URLField` | a link, `rel="noopener" target="_blank"` |
| `EmailField` | a `mailto:` link |
| `DateTimeField` | `07 Aug 2026, 12:00`, with the ISO value in the `title` attribute |
| `DateField` | `07 Aug 2026` |
| A relation | the related object's `__str__` |

Badge tone is matched against a small vocabulary of status words, so a column reads
correctly without configuration:

| Tone | Values |
|---|---|
| `success` | published, active, approved, paid, complete, completed, done, sent |
| `warning` | pending, review, in_review, draft, processing, waiting, hold |
| `destructive` | failed, error, cancelled, canceled, refunded, rejected, banned |

Anything else gets a neutral badge.

### Query cost

`get_list_queryset` plans `select_related` and `prefetch_related` from the columns and
search paths the page actually uses, so adding a relation column cannot reintroduce an
N+1. The rule: stay in `select_related` while every hop is single-valued; the moment a
hop is multi-valued the whole path becomes one `prefetch_related`.

## Filtering

`list_filter` names the fields; when it is unset they are
[inferred](inference.md#list_filter--the-filter-rail). Entries must be real field
paths, and may traverse relations. The filter class is chosen from the field:

| Field | Filter | Multi-select | Parameter |
|---|---|---|---|
| Any field with `choices` | `ChoiceFilter` | yes | `?status=paid&status=shipped` |
| `BooleanField` | `BooleanFilter` | no | `?is_active=1` / `?is_active=0` |
| `DateField` / `DateTimeField` | `DateFilter` | no | `?created_at=P30D` |
| `ForeignKey` / `OneToOne` / `ManyToMany` | `RelatedFilter` | yes | `?customer=3&customer=7` |
| `IntegerField` / `DecimalField` / `FloatField` | `NumericRangeFilter` | — | `?total__gte=500&total__lte=1000` |
| `TextField`, `JSONField`, `BinaryField`, `FileField` | none | | free text has no bounded set of values; it belongs in search |

Relations are checked before `choices` so a foreign key never degrades into a plain
choice list, and `choices` before the concrete type so a boolean or an integer with
`choices` keeps its labels.

### The query-string grammar

Repeated parameters. **OR within one filter, AND across filters.**

```
?status=paid&status=shipped&customer=3
```
means *(paid or shipped) and customer 3*.

```pycon
>>> Order.objects.count()
200
>>> # ?status=paid
37
>>> # ?status=paid&status=shipped
168
>>> # ?customer=3
1
```

Two special values:

- `?field=null` matches rows where the field is null — offered only where the column
  actually is nullable, and skipped on a choice field that already declares `null` as a
  choice value.
- A date range: `?created_at=2026-01-01..2026-03-31`. Either side may be omitted for an
  open-ended window.

### The query string is hostile input

A value that does not clean is **dropped**, never raised on and never passed
uncoerced to a lookup. A filter rail is not a form, and a stale bookmark should degrade
to a broader result set rather than to a 500.

```pycon
>>> # ?status=nonsense  — the value is not one of the declared choices
200            # every row; the filter did not apply
```

A path that resolves only partially — `created_at__year`, say — is rejected outright
rather than guessed at. Half an understood path is how a filter silently widens a
result set.

### Relative dates

Date windows are stored as **ISO-8601 durations**, not as resolved dates, so a saved
view still means "the last seven days" a month later.

| Value | Meaning |
|---|---|
| `P1D` | Today |
| `P7D` | Last 7 days |
| `P30D` | Last 30 days |
| `P90D` | Last 90 days |
| `current-year` | The calendar year to date |
| `2026-01-01..2026-03-31` | A custom window, either side optional |

Windows are anchored on whole local days and **include today**, so "last 7 days" covers
seven calendar days rather than the eight a naive `today - 7 days` produces. For a
`DateTimeField` the window becomes a half-open `[start of day, start of the next day)`
range — which an index on the column can serve — rather than a `__date` lookup, which
one cannot.

### Counts on filter options

Facet counts are **off unless asked for**: add `?_facets` to the URL, mirroring
Django's own opt-in, or switch them on per list from the
[customisation rail](#the-customisation-rail).

The cost model is the whole design here.

**One aggregate per filter, never one per choice.** Django's own
`SimpleListFilter.get_facet_counts` builds a `Q(pk__in=<subquery>)` for every choice,
which the planner sees as N correlated subqueries and which is pathological on a large
table. Every count here is a `Count("pk", filter=Q(...))` folded into a single
`.aggregate()`, so the database walks the filtered set once and returns all the
buckets. The SQL Vantage actually emits:

```sql
SELECT COUNT("shop_order"."id") FILTER (WHERE "shop_order"."status" = 'pending') AS "f0_0",
       COUNT("shop_order"."id") FILTER (WHERE "shop_order"."status" = 'paid')    AS "f0_1",
       ...
```

That form is portable: Postgres and SQLite get a native `FILTER (WHERE ...)` clause and
Django rewrites it to `CASE WHEN` elsewhere.

**Single-select filters share one query; an active multi-select pays for its own.**
Counts for a single-select filter are computed inside the current selection, so every
such filter folds into the same aggregate. A multi-select filter that has a value
ticked must have its *own* predicate removed and everyone else's applied — otherwise
every option the user did not tick reads `(0)` and the filter becomes a dead end — so
it needs a different base queryset and therefore a query of its own. An untouched
multi-select imposes no predicate to exclude, so it rejoins the shared aggregate.

Measured on the demo project's order list, which has three filters:

| State | Facet queries |
|---|---|
| Nothing selected | 1 |
| One multi-select ticked | 2 |

(Loading a relation filter's option list is a separate query, and happens whether or
not counts are on.)

### The cost of counts

Each filter is classified when it is built, and only the cheap ones are counted:

| Cost | Meaning | Which filters |
|---|---|---|
| `CHEAP` | counted | choice, boolean, related below the option cap, **indexed** date |
| `COSTLY` | rendered without counts | unindexed date, a relation with more than 50 options |
| `NEVER` | counting is wrong or meaningless | any multi-valued path, numeric range |

Three ceilings:

- **`MAX_FACET_QUERIES = 8`.** Beyond it counts are dropped rather than trimmed
  silently, because a page that is slow is worse than a page without counts.
- **`MAX_FACET_CHOICES = 50`.** A relation with more options renders as a typeahead and
  is never counted; the aggregate would otherwise carry one `CASE WHEN` per row of the
  related table. Options already selected are still loaded so the pills render.
- A count over a **multi-valued join double-counts rows**, so it is refused outright
  rather than reported wrongly.

A `BooleanFilter` is single-select on purpose, and the consequence is deliberate: with
"Yes" selected, "No" reads zero. For two mutually exclusive options that is honest —
clearing the filter is one click and needs no count to explain itself — and it keeps
booleans off the per-filter query budget.

## Search

One box, `icontains` OR-ed across every path in `list_search`, applied **before** the
filters so a filter's facet counts are computed inside the search.

```
?q=nakamura
```

Inference reaches one hop into each foreign key, so `?q=` on an order list also matches
the customer's name without anyone configuring it.

Vantage does not implement `contrib.admin`'s `^`, `=` and `@` search prefixes. A path
carrying one is stripped and searched with `icontains` like any other.

### Global search

The command palette (<kbd>⌘K</kbd>) searches every model at once, and the whole
difference from the list box is restraint.

- **Access first.** Every model passes `has_model_perm` before it is searched at all,
  and every queryset passes through `filter_queryset` before a row is read. A result
  the user cannot open is not a near miss, it is a leak — a row's `__str__` alone can
  disclose a name, an email address or an order number.
- **Bounded cost.** Every model contributes exactly one sliced query, relevance is
  decided afterwards in Python over rows the database has already agreed a limit on,
  and the overall cap is applied last, so a wide search costs the same as a narrow one.

```pycon
>>> GlobalSearch(site, request).search("nakamura")
[SearchGroup('Customers', 3 hits), SearchGroup('Feedback', 3), SearchGroup('Orders', 5)]
```

## Sorting

Click a column header, or set `?sort=total` / `?sort=-total`.

The parameter goes straight into `order_by`, so it is never trusted. It is dropped
unless it is **one of the columns currently displayed** and resolves to a concrete
single-valued field path. `?sort=nonsense` renders the page in its default order rather
than raising.

A tiebreaker on the primary key is always appended, so pagination stays stable when the
sort column has duplicate values — the usual case for a status column.

The default order comes from `ordering`, then `Meta.ordering`, then
[inference](inference.md#ordering--the-default-sort).

## Grouping, trees and trash

Three affordances the list page offers when a
[detected convention](panels.md#detected-conventions) makes them meaningful.

**Time grouping.** A model with a timeline date can group its rows into buckets:
`?group=day`, `week`, `month` or `year`. The boundaries are computed from the rows
already fetched, not by a second grouped query — the page is at most a couple of
hundred rows, and a query would have to re-apply every filter to stay consistent with
what is on screen.

**Trees.** A model detected as a hierarchy renders parent-before-child with indentation.
It switches off while searching, filtering or sorting, because those produce a subset
and a tree drawn from a subset shows children whose parents are missing — which reads
as corruption rather than as a filtered view. `?flat=1` forces the flat grid.

mptt and treebeard store an orderable path, so the database returns the rows already in
tree order. A plain self-FK has no such column, so the page is arranged in Python
instead: correct within a page, and honest across them — a child whose parent sits on
another page renders as a root rather than being hidden.

**Trash.** A model detected as soft-deleting gets `?trash=1`, which lists the hidden
rows. When the model's default manager already excludes them the trash queryset starts
from `_base_manager`, because a trash view built on `get_queryset()` would filter a
queryset that can never contain what it is looking for and silently show nothing.

### Deleting, and undoing it

Pressing Delete on a model with a [`soft_delete` pattern](panels.md#detected-conventions)
does not destroy the row — it stamps the detected column (`deleted_at` set to now, or
`is_deleted` set to `True`) with an `UPDATE`, not `instance.save()`, so a model whose
`save()` stamps `updated_at` or fires a signal meant for edits does not have that fire
because someone pressed Delete. No cascade check runs first, on purpose: nothing is
being destroyed, so a `PROTECT` reference downstream does not block it — the same
button that would refuse to hard-delete a customer with orders archives that customer
without complaint, because archiving does not touch the orders.

**A trashed row's Restore button undoes exactly that stamp** — it sets the column back
to `None`/`False`, again as an `UPDATE`, and is reachable only as a row action on the
trash tab (`?trash=1`); there is no restore URL to type. It is deliberately **POST-only**
(`http_method_names = ["post"]`, so a GET answers `405`) because restoring changes what
every other list on the site shows, and a GET that mutates is a link a crawler or a
chat client's link-preview fetch will trigger on its own. It is gated on the **delete**
permission, not a separate one — whoever is trusted to move a record into the trash is
trusted to take it back out, and requiring a wider permission to reverse a narrower
action would leave rows only an editor could trash and only an administrator could
recover.

**Pressing Delete on a row that is already in the trash destroys it, permanently, with
no further confirmation beyond the one the button already asked for.** This is what
makes the Trash tab's own Delete button actually empty the bin: once a row is trashed,
`get_soft_delete()` returns `None` for it (the pattern is honoured once, not
recursively), so the same view falls through to the ordinary cascade-delete path —
counted, blocked by `PROTECT` references the same as any other hard delete, and
irreversible. There is no separate "empty trash" action; deleting each row from the
trash tab is emptying it one row at a time, and the confirmation button's label always
says which one of the two it is about to do — "Move *order* to trash" versus "Delete
*order*" — never a bare "Delete" that leaves you to guess.

## Inline cell editing

`list_editable` turns chosen columns into controls in the grid.

```python
@register(Product)
class ProductPanel(ModelPanel):
    list_display = ["name", "sku", "category", "status", "price", "stock"]
    list_editable = ["status", "stock"]
```

Nothing writes when a cell changes. Edits accumulate in the browser, a persistent bar
says how many are pending, and one request commits them: one transaction, one
permission pass, one review step before production moves. Airtable autosaves every
keystroke because the grid *is* the product; here the grid is a view onto **someone
else's production database**, which is a different thing.

Four rules govern it.

**The same form, not a second validation path.** Every write goes through the same
`ModelForm` the change page builds, so coercion, choice validation, `max_length`,
unique checks and `_post_clean` behave identically.

**The same permission answers.** Model-level `change` gates the endpoint; every row is
fetched through `filter_queryset` first, so a row outside your scope answers 404 rather
than confirming it exists, and only then does the object-level check decide the verb.
The allowlist is computed twice — once to decide which cells to draw, and again, from
scratch, on the way in. Filament's own documentation admits its `TextInputColumn` does
not check model policies before saving; that is the failure this is written against.

**Not every field.** Only kinds a single cell can express honestly: boolean, choice,
short text, number, decimal, date. Never a file, a long text, JSON, a relation, the
linked column, or — the subtle one — any field caught up in a multi-column unique
constraint. Django silently *skips* a `unique_together` check when one of its fields is
missing from the form, so a one-field inline write would turn a validation error into
an `IntegrityError` half a second later. A model with a custom `clean()` is excluded
wholesale for the same reason: cross-field validation cannot be answered a cell at a
time.

**A rejected cell is never silently reverted.** The response says which row and which
field failed; nothing else changes and the browser keeps what the user typed.

Concurrent edits are caught rather than lost. Where the model carries a version column —
an explicit integer `version`, or an `auto_now` timestamp — the write is a
compare-and-set (`UPDATE … WHERE pk = %s AND updated_at = %s`) rather than a
read-then-check, and a conflict is reported as a field-by-field diff: "they set status
to Refunded, you set it to Paid". A model with neither column cannot be locked and says
so rather than pretending.

## Saved views

A saved view is a named filter, sort and search state for one list page — and it is **a
query re-evaluated per viewer, never a stored result set**. Two people opening the same
shared view legitimately see different rows and different counts, because the rows are
filtered by `filter_queryset` on every request.

That is the whole security model in one sentence, and it is why the stored state is
only ever query parameters: URL filter state has zero security value. A view cannot
widen access, because nothing in it is ever trusted to decide which rows exist.

```pycon
>>> savedviews.capture(panel, request.GET)
{'version': 1, 'filters': {'status': ['paid']}, 'q': 'NW', 'sort': '-total'}
```

Only parameters the panel's own filters own, plus the search term and the sort. The
page number, the export flag, the facet opt-in and the view identifier itself are about
this *visit* rather than about this view, and storing them would make "reload my saved
view" reopen page seven of a CSV download.

### Sharing and editing

Two axes, kept apart on purpose. **Visibility** is who can see the view exists; **edit
scope** is who can change it. Airtable conflates them and then has to warn its users
that a personal view is not a security feature.

| Visibility | |
|---|---|
| `private` | Only me |
| `shared` | Everyone who can see this model |

| Edit scope | |
|---|---|
| `owner` | Only the owner |
| `editors` | The owner and named editors |
| `everyone` | Anyone who can see it |

Plus a `locked` flag that freezes the configuration and **binds the creator too** — a
lock the owner can ignore protects nobody from the person most likely to change the
view by habit. There is no superuser bypass on editing: being able to rebrand the site
is not a reason to be able to rewrite a colleague's private view, and Save-as is one
click away.

**Save degrades to Save-as when the view is immutable to you.** Not the owner, not a
named editor, the view is locked, the view is somebody else's — all four produce the
same affordance, so nobody has to be told which applies before they can get their
filter written down.

**A shared view never autosaves.** Not even for its owner, who is precisely the person
whose habitual edits would land on everyone else's screen. Only a private, owner-only,
unlocked view follows the filter rail automatically.

### Broken references

Field references are validated **on load, never on save**. Fields get renamed and
`list_filter` gets edited long after a view was written.

```pycon
>>> savedviews.hydrate(panel, {"filters": {"nonexistent": ["x"]}}).broken
[BrokenReference(parameter='nonexistent', values=['x'],
                 reason='“nonexistent” no longer exists on orders.', widens=True)]
```

A stored predicate whose field has gone renders as a broken pill with a banner saying
the results may be broader than intended. Never auto-repaired, and above all never
silently dropped: a filter that quietly stops applying shows people more rows than they
asked to see and tells them nothing.

### The URL

`?view=42&f=<override>`. The `f` parameter carries an override query string, which is
expanded into ordinary filter parameters and redirected. Expanding eagerly is what lets
the list page, the exporter, the paginator and every link keep working without any of
them knowing saved views exist. `f` surviving into the URL is the marker that the page
has diverged from what was saved; its absence means pristine.

## The customisation rail

Below saved views sits a third layer: per-list preferences, stored in the database and
editable from the UI without a deploy.

```
per-user row  >  site-wide row  >  panel.py  >  inference
```

It is deliberately the same shape as the [settings cascade](settings.md#where-a-value-comes-from),
one level down. That module puts a row underneath `vantage_settings.X`; this one puts a
row underneath `panel.get_list_display(request)`, so a call site that already asks the
panel picks up a UI override without knowing a database exists.

Six sections, each saved and reset independently:

| Section | Controls |
|---|---|
| `columns` | which columns, in what order, and rows per page |
| `search` | the search box on or off, and which fields it covers |
| `filters` | the rail on or off, which filters, and whether counts are computed |
| `stats` | the stat strip on or off, which stats, plus stats built in the UI |
| `charts` | the chart strip on or off, which charts, plus charts built in the UI |
| `summary` | the footer bar on or off, and the aggregate per column |

Four properties are load-bearing.

**Lazy.** Nothing reads the database until a list page is actually rendered, and never
during app loading, `check`, `makemigrations` or `scaffold`.

**Failure-tolerant.** A missing table resolves to the code declaration, silently.

**Cached per request.** One query answers both rows for one list, however many getters
ask. Without that, a page with six calls to `get_list_display` would pay six queries.

**The panel is the ceiling.** Everything arriving from the form — every field name,
every aggregate, every chart target — is checked against what the panel *currently*
exposes, both when it is stored and again when it is read. Twice, because the two
checks answer different questions: the first rejects a hostile POST, the second stops a
preference stored last month from resurrecting a column since removed from
`list_display`. **A stored preference can hide, reorder and re-aggregate what the panel
already offers. It can never widen it.**

The menu of choices comes from the panel with this layer switched off, which is what
makes hiding a column reversible: if the choices were drawn from the *effective*
configuration, hiding a column would remove it from the menu that could bring it back.

Saving for yourself is the default. Saving as the site-wide default — what every
colleague opens tomorrow morning — takes a separate permission, superuser by default
and widened with [`LIST_PREFS_PERMISSION`](settings.md#behaviour). A POST asking for it
without that permission is refused rather than quietly downgraded to a personal save; a
silent downgrade would look exactly like success.

## Bulk actions

An action is an ordinary method with a decorator. This is not the same feature as
[bulk edit](forms.md#bulk-edit) — a generic, code-free "change one field on many rows"
screen reached from the same selection bar — despite the neighbouring name; a bulk
action is behaviour a developer wrote, and bulk edit needs no `panel.py` code at all.

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

`@action` works bare or called — `@action` and `@action("Publish")` both work, because
forgetting the parentheses is the mistake everyone makes once.

| Argument | Default | Effect |
|---|---|---|
| `label` | the method name, prettified | Button text |
| `icon` | `None` | Lucide icon name |
| `permission` | `"change"` | The verb the user must hold. `"delete"` for destructive ones, `None` to require only the view permission the page already checked |
| `destructive` | `False` | Confirms before running, and styles the button as dangerous |
| `confirm` | `None` | A confirmation sentence. `{count}` is substituted |
| `max_rows` | `None` | Refuses above this many rows, guarding against a mis-click wiping a table |
| `keywords` | `[]` | Extra terms that match in the command palette |
| `undo_fields` | `None` | Fields the action writes; the toast offers [Undo](undo.md#undo-for-your-own-actions), which restores them where nobody has changed them since |
| `undo` | `None` | A panel method name (or callable) that reverses the run from `ActionResult.undo_state` |

The method returns a string, `None`, or an `ActionResult(message, level, redirect)`
where `level` is one of `success`, `info`, `warning`, `error`.

A ready-made bulk delete ships as `"delete_selected"` — add it to `actions` to get the
same trash-or-destroy behaviour as the record page's Delete button, with
[Undo](undo.md).

### How the selection travels

**A predicate plus an exclusion set, never a list of primary keys.** "Select all 40,000
matching" is the case that decides this: sending 40,000 ids in a POST body is slow and
hits server limits, and Filament's alternative — hydrating every selected model into
memory — led it to ship a `selectCurrentPageOnly()` switch, which is disabling the
feature rather than solving it. A predicate is a few hundred bytes whatever the row
count, and it stays a queryset all the way down.

Selecting all and then unticking a handful is the common case, not an edge one, so the
exclusion set is part of the design rather than bolted on.

### Two safety properties

**The resolved scope is always reported back.** `django.contrib.admin` shipped a defect
where `select_across` silently stayed off with `actions_on_bottom = True`, so a
destructive action applied to page one only. Silent scope reduction on a destructive
action is the worst failure available here.

```pycon
>>> Selection(select_all=True, ids=[], excluded=["3", "9"]).describe(200)
'all 200 matching records, 2 excluded'
>>> Selection(select_all=False, ids=["1"], excluded=[]).describe(1)
'1 selected record'
```

That sentence is appended to whatever the action itself reports, so the message reads
`Published 37 reviews. (all 37 matching records)`.

**The count is the count that happened.** Rows change between selection and execution,
so an action reports what it actually affected rather than echoing the number shown at
selection time. If nothing still matches, the page says so and nothing runs.

Actions run **in the request** below a certain size, and are automatically moved off it
above that size — see [Background jobs](#background-jobs). `max_rows` is a hard refusal
regardless of that threshold; the two are independent guards for two different risks.

## Background jobs

A bulk action over nine rows and a bulk action over ninety thousand are the same
feature and cannot honestly be the same code path. Nine rows is 40ms and belongs in the
request, where the user gets an answer and a redirect. Ninety thousand is minutes, and
running it in the request means a gateway timeout half way through with no record of
how far it got.

**The threshold is estimated cost, not row count.** [`JOB_THRESHOLD_SECONDS`](settings.md#background-jobs)
(default `2.0`) is compared against an estimate: a `bulk_strategy = "sql"` action (one
`UPDATE ... WHERE`) barely grows with row count, while `"instances"` (loading and
saving every row, running signals and `full_clean`) costs roughly per row. Declare
`bulk_strategy` on an `@action` that does per-instance work so the estimate is honest —
see [Bulk actions](#bulk-actions) for the decorator. Below the threshold nothing changes
about how an action runs; above it, a job row is written and the request redirects to
its status page instead of waiting.

**No queue is required.** Four backends are tried in order, and the package depends on
nothing beyond Django to make the feature work:

1. `django.tasks` (Django 6.0+), if `TASKS` names a real queue backend.
2. Celery, when it is importable *and* has a broker configured — being merely
   installed is not enough, since Celery is a transitive dependency of several
   unrelated packages.
3. RQ, via `django_rq` in `INSTALLED_APPS`.
4. **In-process, synchronously** — the fallback on the overwhelmingly common Django
   project with no worker at all. The request pays for the work exactly as it would
   have without this feature, but the job row, the honest per-row counts and the
   downloadable error report all exist afterwards, which is the part that was missing.
   [The system page](deployment.md#the-system-page) reports which backend is in play.

**Chunking, and what a partial failure means.** The selection is sliced into chunks of
[`JOB_CHUNK_SIZE`](settings.md#background-jobs) rows, each its own transaction — a
failure at row 39,000 rolls back the few hundred rows around it, not the 38,500 already
committed. A failed chunk is then retried row by row, so the finished job can report
*which* rows failed rather than condemning the whole chunk. The job itself is
deliberately not one transaction: that is the trade that turns "the whole thing blew
up" into "38,000 succeeded, 12 failed."

**Permission is re-checked as the job runs, not only when it was enqueued.** A job can
sit in a queue for an hour, which is long enough for someone to be moved out of a
group; the check runs again before every chunk against a freshly loaded user.

**Watching it run.** Submitting an action that crosses the threshold redirects to a
jobs inbox at `/admin/_/jobs/`, showing every job the current user started (and, for a
superuser, everyone's). A running job's progress bar polls its own status endpoint
every 1.5 seconds and stops once the job reaches a terminal state — nothing streams. A
finished job with any failures offers a CSV of exactly which rows failed and why,
values passed through the same armouring the CSV exporter uses against formula
injection. A job is visible to the person who started it and to superusers, and to
nobody else — a job for a different user answers `404`, not `403`, so its existence is
not confirmed to a guesser.

Settings: [`JOBS`, `JOB_THRESHOLD_SECONDS`, `JOB_CHUNK_SIZE`, `JOB_MAX_STORED_ERRORS`,
`JOB_RETENTION_DAYS`, `JOB_QUEUE`](settings.md#background-jobs).

## Export

Every list page exports what it is currently showing. To get a saved view by email on a
schedule instead, see [Scheduled reports](reports.md).

```
?export=csv          orders.csv          text/csv
?export=tsv          orders.tsv          text/tab-separated-values
?export=json         orders.ndjson       application/x-ndjson
```

Two properties matter more than format support.

**It honours the active filters, search and sort.** An export that silently ignores
your filters and hands back the whole table is worse than no export — you only notice
after sending it to someone. The exporter shares `build_queryset` with the page itself,
so the two cannot disagree.

```pycon
>>> # GET /admin/shop/order/?export=csv&status=paid
Order number,Customer,Status,Total,Shipping city,Placed at
ORD-5023,Milos Lindqvist,paid,14.41,Valencia,2026-08-07 12:00:51.817566+00:00
...
>>> len(rows)
37                      # exactly the rows the filtered page shows
```

**It streams.** `StreamingHttpResponse` over `QuerySet.iterator()` in chunks of 2,000,
so exporting 400,000 rows does not put 400,000 rows in a worker's memory and the
download starts immediately rather than appearing to hang.

Columns are the ones on screen, headers are the column labels, and related objects
export as their `__str__` — what a human reading the file expects, rather than a
primary key they would have to join back themselves.

JSON is **newline-delimited**, one object per line, rather than one big array: a
streamed array cannot be closed if the queryset raises halfway, leaving a file that is
silently truncated and invalid. Every NDJSON line is independently parseable, so a
partial download is obviously partial rather than quietly wrong.

Responses carry `Cache-Control: no-store` — nothing downstream should cache a per-user,
permission-scoped extract.

## Import

A four-screen wizard at `<list>/import/`: upload, map, preview, commit. The order is
the whole design — nothing is written until you have seen, in the same units you will
be asked to approve, exactly what would change.

Import is the **mirror of export**: the columns an export produces are the columns an
import accepts, so a file can be pulled out, edited in a spreadsheet and pushed back
without anyone hand-editing a header. That round trip is asserted in the test suite,
because it is the property that breaks silently the first time either side changes its
column labels.

### The four steps

**1. Read.** Everything that can be wrong with a file is decided here, once, with a
sentence a human can act on:

```pycon
>>> importer.read_csv(b"")
ImportRejected: That file is empty.
>>> importer.read_csv(b"\x00\x01\x02binary")
ImportRejected: That does not look like a text file. Export it as CSV — a .xlsx
                workbook or a PDF cannot be read here.
>>> importer.read_csv(b"a,a\n1,2\n")
ImportRejected: Two columns are both called “a”. Column names must be unique, or
                there is no way to say which one feeds a field.
```

Size and row ceilings come from [`IMPORT_MAX_BYTES` and
`IMPORT_MAX_ROWS`](settings.md#behaviour), 5 MB and 5,000 rows by default.

**2. Map.** Which model fields may be written at all, and which column probably feeds
each one:

```pycon
>>> fields = importer.importable_fields(panel, request)
[('id', 'Id', writable=False, unique=True),
 ('name', 'Name', writable=True, unique=True),
 ('slug', 'Slug', writable=True, unique=True)]
>>> importer.infer_mapping(("Name", "Slug"), fields)
{'Name': 'name', 'Slug': 'slug'}
```

**3. Resolve — the security boundary.** The mapping arrives from a form and is treated
as hostile. Every target is looked up in options recomputed from the model *on this
request*, never in a list carried between steps, so a hand-crafted POST naming
`password_hash`, a readonly column, or a field the user may not write cannot reach
`construct_instance`. Such a column is dropped and *reported*, not obeyed and not made
into an error page: the honest UI outcome of "you may not write this" is "this column
was ignored".

**4. Dry run, then commit.** The same row loop, differing only in what happens to the
transaction at the end.

```pycon
>>> result = importer.dry_run(panel, parsed, mapping, request=request)
>>> result.summary()
'2 created'
>>> Tag.objects.filter(slug="docs-demo-tag").count()
0                       # nothing was written
```

### Two commitments

**Validation is Django's, not ours.** Every row goes through a real `ModelForm` built
by the same code the edit page uses, so coercion, `clean_<field>`, cross-field
`clean()`, unique checks and `full_clean` behave exactly as they do by hand. A CSV
importer that writes its own `int(value)` coercion is a second, worse validation layer
that will disagree with the form the same data would have gone through.

**Partial failure is reported, never smoothed over.** Each row saves inside its own
savepoint, so one bad row cannot poison the chunk around it and cannot half-write. The
result carries the exact count of each outcome plus every error keyed to its row and
column, which is what makes `1,204 created, 3 failed` an honest sentence rather than a
guess. Failed rows can be downloaded back as a CSV to fix and re-upload.

Permissions are layered rather than checked once. `add` gates every screen, because the
ability to bulk-create is the floor for being here at all; then the chosen mode is
re-checked against the verbs it needs, so a user with `add` but not `change` can create
records and cannot upsert. Both checks run again on the commit request.

## Every query parameter

| Parameter | Meaning |
|---|---|
| `?q=` | Search term |
| `?sort=` / `?sort=-` | Sort by a displayed column |
| `?page=` | Page number |
| `?_facets` | Compute filter counts for this render |
| `?export=csv\|tsv\|json` | Stream the current result set |
| `?group=day\|week\|month\|year` | Bucket rows by the timeline date |
| `?trash=1` | Show soft-deleted rows |
| `?flat=1` | Show a hierarchy as a flat grid |
| `?view=` / `?f=` | Apply a saved view, with an override |
| *anything else* | A filter parameter, if a filter owns it; ignored otherwise |

## See also

- [Analytics](analytics.md) — the stat strip, charts and the summary bar
- [Permissions](permissions.md) — row scoping and read-only mode
- [Panels](panels.md) — the attributes that configure all of this
