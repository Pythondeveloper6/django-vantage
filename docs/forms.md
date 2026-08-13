# Forms

[← Index](index.md)

Create and edit pages, the widgets on them, how they are laid out, and what happens
when you press Delete.

Vantage does **not** reimplement `ModelForm`. Validation, `_post_clean`,
`construct_instance`, unique checks and `BoundField` are subtle, well tested and
identical to what we would need. Forms are built with `modelform_factory` and the only
thing replaced is the field-to-widget mapping — because Django's defaults date from
2005, and browsers have shipped typed inputs, constraint attributes and native pickers
for a decade.

## Which fields appear

Unset `fields` and inference chooses. Excluded automatically:

- anything Django will not let a form write: auto primary keys, `auto_now` /
  `auto_now_add` timestamps, `editable=False`;
- a primary key with a default — a generated identity is not something to type over by
  accident;
- [sensitive names](inference.md#the-sensitive-name-exclusion);
- many-to-many relations through an **explicit** model. That last one is not a taste
  call: `.set()` raises on a through model with extra fields, so a multi-select there
  would render fine and fail on save.

```pycon
>>> infer_form_fields(Product)
['name', 'sku', 'status', 'price', 'cost', 'stock', 'description',
 'category', 'tags', 'is_featured', 'created_at']
```

Override with `fields`, remove with `exclude`:

```python
@register(Product)
class ProductPanel(ModelPanel):
    fields = ["name", "sku", "category", "price", "status"]
    exclude = ["cost"]
```

`exclude` is applied to whatever `fields` or inference produced, so it works either way.

### Section order

Fields are grouped by **role**, not by type. What an editor scans for is "the thing
itself", then "what it is attached to", then "its switches", then "when it happened".

| Section | Contains |
|---|---|
| Details | the record's own content; the display name leads |
| Relationships | foreign keys and many-to-many |
| Options | booleans |
| Metadata | timestamps, `deleted_at`, JSON |

Declaration order breaks ties inside a section, so the result follows the shape of the
model as written. JSON counts as metadata rather than content: a raw blob is a
technical detail, and putting it between the title and the body pushes the fields
people actually edit below the fold.

Empty sections are dropped. The flat field list and the grouped one are produced by the
same code, so they can never disagree.

## Widget inference

A two-step. `classify()` reduces a field to a **`FieldKind`** — a rendering category,
not a Python type — and `infer_widget()` turns that kind plus the field's own
constraints into a **`WidgetSpec`**.

Splitting them means a custom field class from a third-party package only has to answer
"which kind?" to get the full treatment, and it means the front end has a small closed
vocabulary to write CSS and JavaScript against instead of a hundred Django field
classes. Every kind reaches the template as `data-vantage-kind`.

The kinds: `text`, `longtext`, `richtext`, `number`, `decimal`, `boolean`, `choice`,
`date`, `datetime`, `time`, `duration`, `email`, `url`, `slug`, `json`, `file`, `image`,
`color`, `relation_one`, `relation_many`, `uuid`, `ip`, `binary`, `unknown`.

### Classification order

Order matters throughout, because `ImageField` subclasses `FileField`, `DateTimeField`
subclasses `DateField`, and `EmailField`/`URLField`/`SlugField` all subclass
`CharField`.

- **Relations are settled first, and by flag rather than by class name**, because the
  flags describe traversal direction: a reverse FK (`ManyToOneRel`) has `one_to_many`
  set, not `many_to_one`. A `GenericForeignKey` is checked before everything, since it
  reports `many_to_one` with `related_model is None` and every later branch would
  otherwise be entitled to assume a target model exists.
- **An explicit choice list outranks the storage type.** A `CharField` with `choices`
  is a picker; rendering it as free text loses the whole constraint.
- **Rich text is detected by class name** — `richtext`, `html`, `ckeditor`, `tinymce`,
  `quill`, `prosemirror`, `wysiwyg`, `markdown`. Every rich-text package invents its own
  field class and none share a base we could import without depending on it, so the class
  name is the only portable signal.
- **A colour is a short string named like one.** The length bound matters: a
  `CharField(max_length=500)` called `color_notes` is prose about colour, and a native
  colour picker would truncate it to seven characters.

### What you actually get

Verified against the demo project's models:

| Field | Kind | Widget | Attributes |
|---|---|---|---|
| `CharField(max_length=120)` | `text` | `TextInput` | `maxlength=120` |
| `EmailField` | `email` | `EmailInput` | `maxlength=254`, `type=email` |
| `CharField(choices=…)` | `choice` | `Select` | |
| `BooleanField` | `boolean` | `CheckboxInput` | |
| `BooleanField(null=True)` | `boolean` | `NullBooleanSelect` | no `type` — three states |
| `DateField` | `date` | `DateInput` | `type=date`, ISO format |
| `DateTimeField` | `datetime` | `DateTimeInput` | `type=datetime-local` |
| `DecimalField(max_digits=8, decimal_places=2)` | `decimal` | `NumberInput` | `step=0.01`, `min=-999999.99`, `max=999999.99` |
| `PositiveIntegerField` | `number` | `NumberInput` | `min=0`, `max=9223372036854775807` |
| `TextField` | `longtext` | `Textarea` | `rows=6` |
| `ForeignKey` | `relation_one` | `Select` | `data-vantage-search` |
| `ManyToManyField` | `relation_many` | `SelectMultiple` | `data-vantage-search` |
| `SlugField` | `slug` | `TextInput` | `pattern=[-a-zA-Z0-9_]+` |
| `ImageField` | `image` | `ClearableFileInput` | `accept=image/*` |

A nullable boolean is a `<select>` rather than a checkbox because a checkbox can only
report two states and would silently rewrite every unset value to `False` on first
save.

Without a `step`, a browser rejects `12.50` in a `number` input as invalid, so a
`DecimalField` has to state its scale. The bounds come from `max_digits`, because that
is the constraint the database will actually enforce; explicit `MinValueValidator` /
`MaxValueValidator` are applied afterwards and win.

The `accept` attribute comes from the field's own `FileExtensionValidator` when it has
one — an explicit validator is the developer's statement of intent and is more specific
than `image/*`, so it wins even on an `ImageField`.

Placeholders show the expected *shape* without pretending to be data:
`name@example.com`, `https://example.com`, `HH:MM:SS`, `192.0.2.1`, `{}`.

### Relations and the search threshold

A `<select>` over fifty thousand rows is not a control, it is an outage: the browser
renders every option into the DOM and the page stops responding. Above **50** related
rows the control becomes a typeahead backed by a search endpoint.

The cardinality hint is a *parameter* rather than a lookup, because counting is a query
and the widget module never touches the database. When the caller does not supply one,
the answer is "use search": a search box over five rows is a mild inconvenience, while
a `<select>` over fifty thousand is not.

The endpoint is a single URL, deliberately. A per-model endpoint would multiply the
places that have to get the permission check right, and the permission check is the
entire risk here — an autocomplete is a read API over arbitrary models, and the naive
version happily enumerates a table the caller cannot open. So:

- the target model must be **registered with this site**, not merely importable, which
  is what stops an attacker naming `auth.Permission` and enumerating it;
- `has_model_perm(request, model, "view")` is checked against the **target** model, not
  against the page the request came from;
- the queryset goes through `filter_queryset` before a row is read;
- 20 options per response, with one extra row fetched to answer "is there more" without
  a `COUNT` over a table that may be enormous;
- `Cache-Control: no-store`.

### Overriding a widget

Set the widget on the form field the ordinary Django way, by supplying your own form —
or set `layout` and let the field render with its inferred control. Vantage does not
have a `formfield_overrides` equivalent; if you need one specific control, that is a
gap, and it is listed in the [FAQ](faq.md#what-it-does-not-do).

## The layout DSL

`fields` handles the simple case. This handles the rest, and it is plain nested Python
rather than a configuration dialect.

```python
from vantage import ModelPanel, Row, Section, Tab, Tabs, register


@register(Post)
class PostPanel(ModelPanel):
    layout = Tabs(
        Tab("Content",
            Section(Row("title", "slug"), "body"),
            Section(Row("status", "published_at"), title="Publishing", collapsed=True),
        ),
        Tab("SEO", Section("meta_title", "meta_description")),
    )
```

| Node | Signature | |
|---|---|---|
| `Row` | `Row(*children)` | Fields side by side; falls to one column on a narrow screen |
| `Section` | `Section(*children, title=None, description=None, collapsed=False)` | A group of fields, optionally titled |
| `Tab` | `Tab(label, *children, icon=None)` | One tab; the first argument is its label |
| `Tabs` | `Tabs(*tabs)` | A set of tabs; may contain only `Tab` |

`Section`'s `title` is **keyword-only**, deliberately. Treating a leading string as the
title would make `Section("Publishing", "status")` and `Section("title", "body")`
indistinguishable, and either reading is wrong half the time. One keyword in the titled
case is a small price for a rule with no exceptions.

Nodes nest freely, and a bare string or a list is accepted anywhere a node is.

### Two properties worth the machinery

**A layout is declarative data, so it can be validated before anyone loads the page.**
A mistyped field name is caught by `manage.py check` (`vantage.E017`) rather than
rendering a form that quietly omits a field.

**A layout is inspectable, so forgotten fields are added rather than dropped.** A
layout that silently omits a required field produces a form that cannot be saved and
gives no clue why, which is among the worst failure modes an admin can have.

```pycon
>>> layout = Tabs(Tab("Content", Section(Row("number", "status"), "note")),
...               Tab("Money", Section("total")))
>>> resolve_layout(layout, ["number", "status", "note", "total",
...                         "customer", "shipping_city"])
Tabs(
  Tab('Content', fields=['number', 'status', 'note']),
  Tab('Money',   fields=['total']),
  Tab('Other',   fields=['customer', 'shipping_city']),   # ← added
)
```

The leftovers get their own tab rather than a stray section floating beside the tab
strip, which would have no home. Under a plain `Section` root they are appended as a
trailing "Other" section instead.

The converse also holds: a field the layout names but the **form** does not have is
dropped quietly. That is usually a field the user has no permission to edit, and it
should disappear rather than raise. Empty containers are pruned — a section with
nothing in it is chrome.

## Readonly fields

```python
@register(Article)
class ArticlePanel(ModelPanel):
    readonly_fields = ["created_at", "author"]
```

> **Hiding a control is a UI affordance; removing the field is the security boundary.**

A readonly field is not rendered as a control **and** is not in the form's field list at
all, so a hand-crafted POST that includes it is ignored by `construct_instance` rather
than merely unrendered. There is nothing to trust and nothing to forget.

A readonly name that inference did not already pick up is **appended** to the page.
The canonical case is `readonly_fields = ["created_at"]` on an `auto_now_add` column,
which is not an editable field at all and would otherwise vanish from a page whose whole
purpose was to display it.

To fill a readonly column on save, use the [`save_model` hook](panels.md#save-hook).

## Validation

Django's, unchanged. `clean_<field>`, cross-field `clean()`, model validators, unique
checks and `unique_together` all behave exactly as they do on any `ModelForm`, because
it *is* one.

Three write paths exist — the change form, the CSV importer and inline cell editing —
and all three go through the same generated form class. A second implementation of "is
this value acceptable" is a second set of bugs, and the one that runs on the quick path
is the one nobody tests.

Two consequences worth knowing:

- Inline cell editing refuses any field caught up in a multi-column unique constraint,
  and refuses a model with a custom `clean()` wholesale. Django silently *skips* a
  `unique_together` check when one of its fields is missing from the form, so a
  one-field write would turn a validation error into an `IntegrityError` half a second
  later.
- The importer folds spreadsheet booleans (`true/t/yes/y/1/on`) before the widget sees
  them. `CheckboxInput.value_from_datadict` understands `"false"` but reads `"0"` as
  **True** — it is a non-empty string, and truthiness is the last thing it tries — and a
  CSV that writes 1/0 is normal.

## Saving

```python
def save_model(self, request, obj, form, change):
    ...
```

The hook receives the populated but **unsaved** instance, which is the point: it is
where you set what the form cannot. It must call `obj.save()` itself. Vantage writes
the many-to-many relations afterwards, so that is one thing every hook does not have to
remember, and the whole thing runs inside a transaction on the write database.

The audit entry is written **outside** that transaction, deliberately: catching a
logging error inside an atomic block still marks it for rollback, so a swallowed
exception in the wrong place would destroy the very write it was trying to record.

### Concurrent edits

Where the model carries a version column — an explicit integer `version`, or an
`auto_now` timestamp, which is the column Django is *already* maintaining on almost
every model anyone writes — a write is a compare-and-set rather than a read-then-check:

```sql
UPDATE app_order SET updated_at = %s WHERE id = %s AND updated_at = %s
```

Zero rows affected means somebody moved it under you. Because the statement both tests
and advances the token, the loser of a race cannot win by retrying blindly — it has to
come back with the new token, which means it has to have seen the new state.

The conflict is reported as a **field-level diff**. "This record changed" is not an
answer anybody can act on; "they set status to Refunded, you set it to Paid" is.

A model with neither column cannot be locked, and says so rather than pretending: the
write proceeds unguarded, which is what every ORM does today.

## The record page

The detail page shows the record's fields, and then **what points at it**: a block per
reverse relation, with the first five rows as a small linked table and a link through to
that model's list, pre-filtered to this record. The related queryset is planned with
`select_related` from the columns it renders, so five rows with a relation column is
still one query rather than six.

A row you may not see does not exist: every object lookup goes through `filter_queryset`
before any object-level check runs, so an out-of-scope record answers **404**. Answering
403 would confirm that record 12 exists and is merely off-limits, which is the leak the
scoping was for.

## Cascade-delete preview

Pressing Delete shows the real tree of what the delete takes with it, counted.

```pycon
>>> collect_cascade(order)
CascadeSummary(groups=(CascadeGroup(label='Order items', count=2),),
               cleared=(), total=2, blocked_by=None)
```

Blocked deletes are explained rather than raised at you:

```pycon
>>> collect_cascade(customer).blocked_by
'This customer cannot be deleted while Orders still reference it.
 Remove or reassign those first.'
```

Three details that matter.

**It uses the same `Collector` the delete itself will use**, so the preview cannot drift
from the behaviour it is predicting. A preview computed from a separate walk of the
relation graph is a second implementation of cascade semantics, and the two will
disagree the first time someone adds a `SET_DEFAULT`.

**Both of the collector's row sets are counted.** `data` holds instances Django loaded
into memory; `fast_deletes` holds querysets it intends to delete with a single statement
and therefore never loads. Reporting only the first understates the damage for exactly
the common case — a plain cascading foreign key with no signal receivers.

**`SET_NULL` is reported separately**, as "*Products — category cleared*" rather than as
a deletion. Those are different kinds of bad news, and collapsing them into one number
would make a harmless nulled optional reference look like destruction.

The count goes on the **button**, not only in the list above it. A confirmation whose
button says only "Delete" is a confirmation people click without reading; putting the
number on the control being pressed is the last chance to notice that this order is
taking twelve other rows with it.

A blocked delete re-renders the page with the reason and a `409` status — re-render
rather than raise, because the reason is the useful part and a bare 409 would lose it;
409 rather than 200 so a scripted caller can still tell.

## See also

- [Panels](panels.md) — `fields`, `exclude`, `layout`, `readonly_fields`, `save_model`
- [Permissions](permissions.md) — why a hidden row answers 404
- [Lists](lists.md#inline-cell-editing) — editing without opening the form
