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
| `TextField` named `body_html`, or a field class matching a rich-text hint | `richtext` | `Textarea` | `rows=12`, `data-markdown` |
| `ForeignKey` | `relation_one` | `Select` | `data-vantage-search` |
| `ManyToManyField` | `relation_many` | `SelectMultiple` | `data-vantage-search` |
| `SlugField` | `slug` | `TextInput` | `pattern=[-a-zA-Z0-9_]+` |
| `FileField` | `file` | `VantageFileInput` (a `ClearableFileInput` subclass, with a filename/size preview) | `data-vantage-kind=file` |
| `ImageField` | `image` | `VantageImageInput` (subclasses `VantageFileInput`, adds a thumbnail preview) | `accept=image/*` |

A nullable boolean is a `<select>` rather than a checkbox because a checkbox can only
report two states and would silently rewrite every unset value to `False` on first
save.

A `richtext` field carries `data-markdown`, which `static/vantage/vantage-editors.js`
enhances into a small formatting toolbar (bold, italic, code, link, heading, lists,
quote — `Ctrl+B`/`Ctrl+I`/`Ctrl+K` work too) plus a character count. Vantage integrates
no CKEditor, TinyMCE or Quill of its own — whichever package's class name is what
triggered the `richtext` classification, what actually renders is the same plain
`Textarea` every `longtext` field gets, just with markdown buttons above it. With
JavaScript off, or before the script has run, it is that `Textarea`, unenhanced and
fully usable.

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
renders every option into the DOM and the page stops responding. What replaces it is
`SearchSelect` (single-valued relations) or `SearchSelectMultiple`
(many-to-many) — a typeahead backed by [the autocomplete endpoint](#the-endpoint-is-a-single-url)
below — and **how often** it replaces the plain `<select>` is
[`VANTAGE["RELATION_PICKER"]`](settings.md#behaviour):

| `RELATION_PICKER` | Single-valued relation (FK, O2O) | Many-to-many |
|---|---|---|
| `"always"` (the default) | Always the picker, whatever the row count — a 3-row `ForeignKey` gets it too | Always the picker |
| `"threshold"` | A plain `<select>` at or below **50** related rows; the picker above it | Always the picker |

`"always"` is the default because one control for every relationship is easier to learn
than two, and because a native `<select multiple>` is a poor control at any size — the
selection hides behind ctrl-click, and one ordinary click discards it, which is exactly
the failure `SearchSelectMultiple` exists to avoid. That reasoning does not depend on
row count, so a many-to-many gets the picker in **both** modes; `RELATION_PICKER` only
ever changes the *single*-valued case.

Set `RELATION_PICKER = "threshold"` on a panel used mostly from phones: a native
`<select>` opens the platform's own picker, which beats anything a page can draw, and
is worth keeping for the common case of a short list on a small screen.

```pycon
>>> # RELATION_PICKER = "threshold" (not the default)
>>> infer_widget(field_of(Post, "author"), related_count=3).build_widget()
<django.forms.widgets.Select object at ...>       # a 3-row FK stays a plain select
>>> infer_widget(field_of(Post, "author"), related_count=55).build_widget()
<vantage.widgets.SearchSelect object at ...>       # above 50, the picker regardless
```

The cardinality hint (`related_count`) is a *parameter* rather than a lookup, because
counting is a query and the widget module never touches the database. When the caller
does not supply one, the module assumes "use search" — a search box over five rows is
a mild inconvenience, while a `<select>` over fifty thousand is not.

#### The endpoint is a single URL

It is a single URL, deliberately. A per-model endpoint would multiply the
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

## Media and uploads

Pillow is optional, and never imported at module scope — `vantage.media` soft-detects
it the same way the rest of the package soft-detects an optional dependency, so a
project with no Pillow installed still gets working file uploads; it just gets a chip
with a filename instead of a thumbnail.

### Thumbnails

A list cell and a detail-page preview for an `ImageField` both go through
`vantage.media.thumbnail()`, at two different sizes — `render_cell(..., size="sm")`
(40px) on the list, `size="lg"` (224px) on the detail page, and the upload widget's own
preview beside the control uses `"md"` (96px). All three are the same function; only
the requested box changes.

A thumbnail is generated, not stored beside the source: the derived file's name is a
hash of the source name, the box size, the pixel ratio and the source's own
modification time or size, so replacing the source image produces a new derived file
automatically — nothing has to be told to invalidate the old one. Resolved URLs are
cached for five minutes, negative results included, so a broken source is not
re-attempted on every page view.

The image is rendered at **twice the CSS box size** (`PIXEL_RATIO = 2`) so it looks
sharp on a high-density screen, while the `<img>` tag still declares the CSS-box
`width`/`height` — the browser does the downscaling, the same trade every retina-aware
image pipeline makes. **EXIF orientation is corrected before resizing**, because a
phone camera stores a portrait photo's pixels sideways and trusts the orientation tag
to straighten it; skipping that step turns every portrait upload into a landscape
thumbnail lying on its side. Output prefers **WebP** when the installed Pillow build
supports encoding it, falling back to PNG for an image with real transparency or
progressive JPEG for one without — never a format decided by the source file's own
extension.

`thumbnail()` returns `None` — which renders as a plain chip, not a broken image — for
any of: an empty field, Pillow not installed, an `.svg` source (never thumbnailed,
deliberately: see below), a source file over 25 MiB, a declared pixel count over
50,000,000 (the decompression-bomb guard — refused before the file is even decoded, from
its declared dimensions alone), or content that does not sniff as one of the supported
raster types. Nothing here raises; a thumbnail that cannot be made degrades to a name.

**There is no lightbox.** Clicking a thumbnail opens the original file in a new tab
(`target="_blank"`), and that is the whole interaction. The template's own comment
states the reasoning: "a chip is a worse picture and a better answer" than a broken
image state, but a click-to-enlarge modal is simply not built yet — if you need one,
this is a genuine gap to fill rather than a setting to flip.

### Upload validation

Every `FileField`/`ImageField` form field carries a validator attached at the field
level — `validate_file_upload` for a plain `FileField`, `validate_image_upload`
(stricter: requires raster bytes, rejects SVG outright) for an `ImageField` — so the
check runs whether the upload came through the change form, the importer or a shell
session, not only when a particular view happens to call it.

Content is **sniffed**, never trusted from the extension or the browser-supplied
content type: a file claiming to be a JPEG that is actually HTML is refused, and the
mismatch is named in the error rather than silently accepted. `MEDIA_ROOT` and
`MEDIA_URL` must be configured for any of this to have somewhere to write to — see
[Settings](settings.md#django-settings-that-matter).

If [`PanelSecurityMiddleware`](security.md#panelsecuritymiddleware) is installed, every
upload is screened a second time at the request level, before it reaches a view at
all — the same content-sniffing logic, applied to *every* file in `request.FILES` on
any panel write, not only the ones a form's own field validators happen to cover.

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

## Editable inlines

An `Order` whose `OrderItem`s can only be reached through a second page is not an order
form, it is a receipt. `ModelPanel.inlines` puts the children on the parent's own form:
validated with it, saved with it, and rolled back with it.

```python
from vantage import ModelPanel, register
from vantage.inlines import Inline

from shop.models import Order, OrderItem


class OrderItemInline(Inline):
    model = OrderItem
    fields = ["product", "quantity", "unit_price"]
    extra = 1
    max_num = 50          # see the note below — do not leave this unset


@register(Order)
class OrderPanel(ModelPanel):
    inlines = [OrderItemInline]
```

> **Set `max_num` explicitly.** Leaving it unset currently raises `ValueError` from
> Django's own formset machinery the moment the parent's form renders — Django
> substitutes an unset `max_num` with its internal default of 1,000, which is larger
> than the 500-row ceiling this module declares for every inline
> (`vantage.inlines.ABSOLUTE_MAX`), and the two disagreeing is what raises. This is a
> real gap between the module's own example and its own behaviour, not a deliberate
> restriction — until it is fixed, give every `Inline` a `max_num` you are comfortable
> with (500 or below).

### What an `Inline` accepts

| Attribute | Default | |
|---|---|---|
| `model` | required | The child model. Nothing to infer this from |
| `fk_name` | inferred | Which foreign key on the child points at the parent — only needed when the child has more than one, which is exactly when guessing would be a data-loss bug |
| `fields` | inferred | Same inference `fields` uses elsewhere, minus the parent link |
| `exclude` | `None` | Removed from whatever `fields` or inference produced |
| `extra` | `1` | Blank rows offered on load |
| `min_num` / `max_num` | `0` / `None` | Bounds on how many child rows the form accepts |
| `can_delete` | `True` | Whether a saved row may be removed from this form |

`TabularInline` and `StackedInline` pin the layout instead of letting it infer from
field count and kind: a grid for children that fit on a line, a stack of full forms for
children that do not — a `Textarea`, a file input or a many-to-many picker forces the
stacked layout wherever it appears, because a paragraph or a filename does not read
inside a table cell.

### What one transaction buys you

Every inline's formset is validated **before anything is written**, and the write
itself — parent and every child formset — runs inside the same
`transaction.atomic()` block the parent's own save already opens. A parent that saved
while a child failed validation would be data loss with a success message on top: the
user is told the edit landed, and the half that did not land is the half they were
actually editing.

### Permissions are answered against the child model

`change_order` is not `delete_orderitem`. Every inline resolves its own four verbs
against **its own model**, through the same
[`PanelPermissions`](permissions.md) the rest of the panel uses — so read-only mode, a
`has_<action>_permission` hook and row scoping all apply to the child independently of
the parent. Missing `add` removes the extra blank rows and locks the formset to editing
existing rows only; missing `delete` removes the checkbox **and** skips deletions on
the way in, so a hand-crafted POST cannot do what the rendered page never offered.

### The query count does not follow the row count

Two things would otherwise make it grow: the child rows' own `__str__` (which
conventionally touches a foreign key of its own), and — the subtler one — every
`ModelChoiceField` in every row, because Django deliberately re-clones a choice field's
queryset per form instance to force a fresh iterator. Twenty rows with a `product`
picker would otherwise be twenty identical `SELECT` statements; Vantage evaluates each
distinct field's options once and shares the list across every row.

### Errors belong to a row

Django's formsets collect errors per form; a template that pours them all into one list
at the top hands the user a heap of "This field is required" with no way to tell which
of eleven lines is short. Inline errors render in the offending row, and a row carrying
any is marked so it can be found without reading every one.

### What inlines are not

- **Not a replacement for the read-only [related blocks](#the-record-page).** Those
  still render for every reverse relation a panel does *not* declare as an inline — a
  quick look-through with a link to the full list, not something to edit here.
- **Not the same control as `related_tabs`.** [`ModelPanel.related_tabs`](#the-record-page)
  groups the *read-only* related blocks below into a tab strip; declaring `inlines` puts
  a different set of records — the ones a panel opted into editing — on the form itself
  and has no effect on the tab strip either way.
- **Not the same thing as [inline cell editing](lists.md#inline-cell-editing)** on the
  list page, despite the name. That feature edits one field of many *rows of the model
  you are already looking at*; this one edits many rows of a *different, related*
  model while you look at their parent. They share a word in the module name and
  nothing else — `vantage.inlines` (plural, this page) versus `vantage.inline`
  (singular, the list-page grid).

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

## Duplicating a record

`row_actions = [..., "duplicate", ...]` (or the built-in row actions Vantage offers by
default) puts a form in front of the person, pre-filled from the source record, rather
than writing a copy straight to the database. Nothing is saved until they press the
button a second time, on a page that already shows every collision the copy would have
hit.

**Every unique field gets a value nothing else holds, chosen at form-build time.** A
slug becomes `my-post-copy`, then `my-post-copy-2` and onward if that is taken too; a
plain unique `CharField` becomes `"Copy of My Post"`, then `"Copy of My Post (2)"`. An
email, URL, UUID or IP address has no honest "copy of" — those are cleared instead, so
the form asks for one rather than pre-filling something that would fail its own field
validator. Trying the candidates against the database now, rather than waiting for the
save, is what turns "your duplicate failed" into "the form already shows the value it
picked" — the database is asked again on submit regardless, so a row that appears in
the gap is still caught as a field error, not an `IntegrityError`.

```pycon
>>> duplicate_plan(panel, category).initial["slug"]
'baking-copy'
>>> duplicate_plan(panel, category).notes
('Slug was changed to “baking-copy” to keep it unique.',)
```

A `unique_together` (or `UniqueConstraint`) collision is not resolved automatically —
which of several columns to vary is a judgement about the data, and guessing would
change what the copy means without saying so. It is named in a note instead, so the
person makes that call once rather than discovering it as a failed save.

**A file or image is not duplicated — the copy points at the original file.** Storage
duplication is not attempted, and a note says so plainly, because the alternative
failure mode is silent: someone deletes what they think is their own copy's image and
the original record loses its picture too. Replacing the file on the copy affects only
the copy.

**Many-to-many relations copy only through an auto-created through table.** A custom
`through` model is skipped outright — Vantage cannot know what a row on that table is
supposed to mean for a different parent, and guessing would write data nobody asked
for.

**A soft-deleted record's tombstone is never carried over.** The pattern's field is
reset to live (`None` or `False`) on the copy, because a duplicate that lands directly
in the trash is not what "Duplicate" has ever meant to anyone who pressed it.

**Related children (reverse foreign keys) are opt-in, every time.** The duplicate form
lists each reverse relation with a live count — *"3 order items"* — and copies none of
them unless ticked. Each ticked child is cloned with the same unique-field resolution
described above, and each clone is audited as its own `create`, not folded into the
parent's.

Duplicating requires the **`add`** permission, not `change` and not a `duplicate`
permission of its own — someone trusted to edit an order but not to create one must not
be able to create one by copying, so the check is the same one that gates the ordinary
"Add" button.

## Bulk edit

Distinct from [bulk actions](lists.md#bulk-actions): a bulk action is a named piece of
behaviour a developer wrote (`@action`); bulk edit is a generic "change one field on
many rows at once" screen that needs no `panel.py` code at all, reached from the list
page's selection bar.

The picker offers a field only if setting it in bulk is actually safe to do without
looking at each row individually: not `readonly_fields` or `exclude`d, not unique or
the primary key (a unique value cannot be the same across many rows by definition),
and never a file or image field (there is no one file to set many rows to). Tick a
field to change it — the tick is itself checked against that same allowlist on submit,
so a hand-crafted POST cannot switch on a field the picker never offered.

Every row is written through the same `ModelForm` the single-record edit page uses, so
validation, `save_model` and audit logging behave identically to editing one record by
hand — bulk edit is not a second, faster, laxer write path. Vantage picks the
implementation automatically: a straight `UPDATE ... WHERE` when nothing about the
model needs a row loaded into memory, or one `save()` per row — capped, refusing
outright above 1,000 rows rather than partially applying — when the panel defines
`save_model`, the model overrides `save()`, a ticked field is many-to-many, or a
`pre_save`/`post_save` signal is connected. Either way the count of rows actually
changed is what comes back, the same "the count is the count that happened" rule
[bulk actions](lists.md#bulk-actions) follow.

**Rows edited by someone else since you selected them are skipped, not overwritten.**
The moment the selection was made is captured and compared against every row's own
version on write, so a colleague's edit in between wins and is reported as skipped
rather than silently clobbered by whatever the bulk form said.

Bulk edit is gated on the model-level `change` permission only — there is no
per-row object-level check, because checking every one of a few hundred rows one at a
time is the N+1 this feature exists to avoid in the first place; scope what a bulk edit
can reach with `filter_queryset`, the same layer every other bulk operation already
goes through.

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

**None of this runs for a soft delete.** A model with a detected
[`soft_delete` pattern](panels.md#detected-conventions) archives the row instead of
destroying it, so nothing downstream can be orphaned or protected and the cascade
check is skipped outright — computing it would report the page "blocked" by a
`PROTECT` reference that archiving does not actually violate, hiding the only button
on the page. See [Deleting, and undoing it](lists.md#deleting-and-undoing-it) for the
full trash/restore mechanics, including what a *second* delete on an already-trashed
row does.

## See also

- [Panels](panels.md) — `fields`, `exclude`, `layout`, `readonly_fields`, `inlines`, `save_model`
- [Permissions](permissions.md) — why a hidden row answers 404
- [Lists](lists.md#inline-cell-editing) — editing without opening the form, and why it
  is a different feature from [editable inlines](#editable-inlines) above despite the name
- [Lists](lists.md#bulk-actions) — bulk *actions*, a developer-written `@action`; a
  different feature from [bulk edit](#bulk-edit) above despite the neighbouring name
- [Security](security.md) — upload screening at the request level, and media on a
  separate origin
