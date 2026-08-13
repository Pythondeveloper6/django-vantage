# Inference

[← Index](index.md)

Inference is the core value of the package. A project that writes no `panel.py` at all
must still get columns worth looking at, filters that matter, search that works and a
sensible order.

It also has to be **predictable**. A user who cannot guess what the engine will do
will configure everything by hand and lose the benefit, so this page states the rules
exactly rather than describing them in the abstract.

Two structural facts, before the rules:

- **Every function is pure.** Model in, configuration out. No database access, no
  request, no settings. That is what makes the whole engine table-testable, and it is
  why the inference tests are the densest in the suite.
- **Inference is resolved per attribute.** Setting `list_display` on a panel overrides
  the columns and nothing else. See [the rule](panels.md#the-rule-that-governs-every-attribute).

You can run any of this yourself:

```pycon
>>> from vantage import inference
>>> inference.infer_all(Order)
{'list_display': ['number', 'status', 'customer', 'shipping_city', 'total', 'created_at'],
 'list_link': 'number',
 'list_filter': ['status', 'created_at', 'customer'],
 'list_search': ['number', 'shipping_city', 'note', 'customer__name'],
 'ordering': ['-created_at']}
```

## The sensitive-name exclusion

Before anything else: some field names are never shown in a list, never searched and
never offered as a filter, whatever their type.

The name is matched **as a whole word**, case-insensitively, against this pattern:

```
password  passwd  secret  token  api_key  apikey  private_key
salt      hash    signature  otp   pin     cvv     ssn
```

A word boundary is a start of string, an end of string, or an underscore. So
`api_token`, `reset_token` and `password_hash` are all excluded; `tokenise_queue` and
`hashtag` are not.

Rendering a credential in a table is a security bug — a shoulder-surfer, a screenshot
in a support ticket or a shared export is all it takes. This exclusion is applied in
`infer_list_display`, `infer_list_link`, `infer_list_filter` and `infer_list_search`,
and the same pattern is reused by the audit log so a diff can never carry a secret
either.

It has visible effects on real models:

```pycon
>>> inference.infer_list_display(User)
['username', 'is_superuser', 'is_staff', 'is_active', 'email', 'last_login']
```

`password` is absent. On the demo project's `Customer`, which has an `api_token`
column:

```pycon
>>> inference.infer_list_display(Customer)
['name', 'country', 'tier', 'is_active', 'email', 'phone']
>>> inference.infer_list_search(Customer)
['email', 'name', 'phone', 'city']
```

The exclusion is not a permission system. It stops a credential appearing by accident;
it does not stop you putting one in `list_display` deliberately.

## `list_link` — the column that opens the record

Chosen first, because everything else is arranged around it.

1. The first field named `name`, `title`, `label`, `headline`, `subject` or
   `display_name` — in that order of preference, not declaration order.
2. Failing that, the first `CharField` that is not a `SlugField`, has no `choices`, and
   is not sensitive.
3. Failing that, the primary key. Every row is always openable.

```pycon
>>> inference.infer_list_link(Order)      # no `name`; first plain CharField
'number'
>>> inference.infer_list_link(OrderItem)  # no CharField at all
'id'
```

## `list_display` — the columns

Every concrete field is scored; the highest six win, the linked column is forced to the
front, and timestamps are forced to the back.

### The scores

| Score | Field |
|---:|---|
| — | **Never shown:** sensitive name, `TextField`, `JSONField`, `BinaryField`, `FileField`/`ImageField`, the primary key, or a `CharField` with `max_length > 200` |
| 100 | A conventional display name (`name`, `title`, `label`, `headline`, `subject`, `display_name`) |
| 80 | Any field with `choices` |
| 70 | `BooleanField` |
| 60 | A forward `ForeignKey` or `OneToOneField` |
| 50 | `EmailField`, `URLField` |
| 45 | `CharField` with `max_length <= 100` |
| 40 | `IntegerField`, `DecimalField`, `FloatField` |
| 35 | Anything else |
| 30 | `DateField`, `DateTimeField` |
| 20 | `SlugField` |
| 15 | `CharField` with `100 < max_length <= 200` |
| 10 | `created_at`, `created`, `updated_at`, `updated`, `modified`, `modified_at` |

Then:

- **The linked column always comes first**, regardless of its score. The eye needs a
  stable anchor at the start of every row.
- **Ties break on declaration order**, so the result follows the shape of the model you
  wrote rather than an arbitrary sort.
- **At most six columns.** Beyond that they are noise, and the rest are one click away
  on the detail page.

### Why those scores

The scale is coarse and gapped on purpose, so inserting a new category later does not
reshuffle existing results.

A **choice field scores just below the name** because a status column is the
highest-signal thing in a row after the record's own label: it is what people scan for
and what they filter on next.

The **primary key is never a column** when something else can be. The display column
already links to the record, so the id is redundant width — unless the model has no
text field at all, in which case the pk becomes the link and therefore the first
column.

A **long `CharField` is prose in disguise**. It wrecks row height well before it earns
its place, so it is demoted past 100 characters and dropped past 200. `TextField` is
never a column for the same reason, only more so.

**Timestamps are useful but last.** Almost every model has `created_at`, almost nobody
scans by it, and putting it third pushes something that matters off the end.

Worked example, `shop.Product`:

```pycon
>>> inference.infer_list_display(Product)
['name', 'status', 'is_featured', 'category', 'sku', 'price']
```

`name` is the link and leads. `status` (80) then `is_featured` (70) then `category`
(60) then `sku` (45, a short `CharField`) then `price` (40). `cost` and `stock` also
score 40 but fall outside the cap; `description` is a `TextField` and never eligible;
`created_at` scores 10.

## `list_filter` — the filter rail

Built in a fixed order rather than by score, because the aim is a rail that is both
cheap and useful:

1. Every field with `choices`, and every `BooleanField`, in declaration order.
2. **One** date field. If the model has both a domain date and an audit timestamp, the
   domain date wins — a record's own date is what people filter on, and a second date
   filter rarely earns its width.
3. Every forward `ForeignKey` / `OneToOneField`, in declaration order.

Capped at five. Sensitive names and the primary key are skipped.

```pycon
>>> inference.infer_list_filter(Product)
['status', 'is_featured', 'created_at', 'category']
>>> inference.infer_list_filter(Customer)
['country', 'tier', 'is_active', 'joined_on']
```

Choice fields and booleans lead because they are cheap to facet and partition the data
usefully. Foreign keys are included but capped, because their cardinality is unknown
until query time — a relation with fifty thousand rows degrades to a typeahead and
loses its counts. See [the facet cost model](lists.md#the-cost-of-counts).

## `list_search` — the search box

Only these field types are searchable at all: `CharField`, `TextField`, `EmailField`,
`SlugField`, `URLField`. Then each candidate gets a rank, lowest first:

| Adjustment | Reason |
|---|---|
| start at `0` if `unique` or `db_index`, else `1` | indexed fields are both more useful to search and cheaper to |
| `+1` for a `SlugField` | a slug duplicates the title it came from |
| `+1` for a `TextField` | long bodies are the most expensive thing to scan |
| declaration order breaks ties | alphabetical looks arbitrary to a reader |

**Choice fields are excluded entirely.** Substring-matching one is close to useless: the
column stores `draft` while the user types "Draft" or "in review". Choice fields belong
in `list_filter`, and inference puts them there.

Then **one hop into each foreign key**, targeting that related model's own display
field — but only when the related model actually has one, so no search path ever ends
at a bare primary key.

Capped at five.

```pycon
>>> inference.infer_list_search(Order)
['number', 'shipping_city', 'note', 'customer__name']
```

`number` is unique, so rank 0. `shipping_city` is a plain `CharField`, rank 1. `note`
is a `TextField`, rank 2. Then the hop into `customer`, whose own display field is
`name`.

Search is applied as `icontains` OR-ed across every path. Vantage does not implement
`contrib.admin`'s `^`, `=` and `@` prefixes; if a path carries one it is stripped and
the field is searched with `icontains` like any other.

## `ordering` — the default sort

1. `Meta.ordering`, if the model declares it. That is the developer's own statement of
   intent and always wins.
2. Newest first on the first of `created_at`, `created`, `date_joined`, `timestamp`,
   `date` that exists.
3. Descending primary key.

The fallback matters more than it looks. Pagination requires a total order — without
one, page two can repeat a row from page one — and descending pk is both stable and a
decent proxy for recency.

```pycon
>>> inference.infer_ordering(Order)     # Meta.ordering = ["-created_at"]
['-created_at']
>>> inference.infer_ordering(User)      # no Meta.ordering; has date_joined
['-date_joined']
```

## Form fields and widgets

Which fields a form contains, and what control each one gets, is inferred separately —
see [Forms](forms.md#widget-inference). The short version: Vantage keeps Django's form
*machinery* (validation, `_post_clean`, `construct_instance`, `BoundField`) and
replaces only the field-to-widget mapping, because Django's defaults date from 2005 and
browsers have shipped typed inputs for a decade.

## Query optimisation

`select_related` and `prefetch_related` are not inferred from the model; they are
**derived from what the page actually renders**. Every column, filter and search field
names a path, and each path is either joinable or it is not.

The rule: walk the path, staying in `select_related` while every hop is single-valued;
the moment a hop is multi-valued — a reverse FK, an M2M, a generic relation — the whole
path becomes one `prefetch_related` lookup.

Because the plan comes from the rendered columns, adding a relation column to
`list_display` cannot reintroduce an N+1. The test suite asserts that the query count
does not grow with the row count.

## Conventions

Beyond field-level inference, seven **pattern detectors** recognise the modelling
conventions a project already follows — trees, soft deletes, publication flags, money
columns and so on — and unlock the matching affordance with no configuration. They are
documented with the panel attributes that override them:
[Detected conventions](panels.md#detected-conventions).

## Seeing and freezing what was inferred

```bash
python manage.py vantage_scaffold blog            # print it
python manage.py vantage_scaffold blog --write    # write blog/panel.py
```

Inference is only trustworthy if you can look at it. Once written out, the file is
ordinary code: readable in a diff, editable, and no longer subject to inference
changing under it on upgrade. That last point is the practical reason to scaffold —
if you need the columns to be stable across a Vantage upgrade, write them down.

## See also

- [Panels](panels.md) — how to override any of this
- [Lists](lists.md) — what the list page does with the result
- [Forms](forms.md) — widget inference
