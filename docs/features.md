# Features at a glance

Everything django-vantage does, one short paragraph each, with a link to the full page.
Unless it says otherwise, a feature works the moment the package is installed: nothing
here needs configuring before you can use it.

## Getting set up

**Auto-registration.** Add `vantage` to `INSTALLED_APPS`, mount `site.urls`, and every
model in the project gets a list, detail, create, edit and delete page. Framework noise
(sessions, content types, permissions) is left out. [Getting started](getting-started.md)

**Inference.** Columns, filters, search fields, sort order, form widgets and query
joins are worked out from each model. Set one attribute on a panel and only that
attribute changes; the rest stay inferred. [Inference](inference.md)

**Panels.** A `panel.py` next to `models.py` holds the overrides, in ordinary Python with
names a `ModelAdmin` user already knows (`list_display`, `search_fields`,
`readonly_fields`…). [Panels](panels.md)

**`vantage_scaffold`.** Prints a real `panel.py` containing everything that was inferred,
so you can see the decisions and take ownership of any of them.
[See what was inferred](getting-started.md#see-what-was-inferred)

**`manage.py check`.** Every field name in a panel is validated at check time, so a typo
fails loudly instead of quietly dropping a column.
[Check your configuration](getting-started.md#check-your-configuration)

**Configure from the UI too.** Columns, filters, stats, the dashboard, the sidebar and the
branding can also be changed in the panel without a deploy. One fixed order decides who
wins: UI override, then `panel.py` / `VANTAGE`, then inference.
[How configuration resolves](configuration.md)

## Lists

**Data grid.** Sortable columns, a filter rail with counts, relative date filters
("last 30 days"), full-text search and pagination. [Lists](lists.md)

**Board, calendar, gallery and map.** The same list as a Kanban board, a month calendar,
an image gallery or a map, picked from a switcher. Each view is inferred from the model,
and **Group by** / **Dates from** let anyone choose the field from the page.
[List views](list-views.md)

**Saved views.** Save a combination of filters, search, sort and view under a name, keep
it private or share it. [Saved views](lists.md#saved-views)

**Inline editing.** Edit chosen columns in the grid. Changes collect in a pending bar and
are saved together in one transaction, with a conflict check if someone else changed the
row. [Inline cell editing](lists.md#inline-cell-editing)

**Grouping, trees and trash.** Group rows by a field, browse tree-shaped models, and get a
Trash tab with Restore on any model with a `deleted_at` or `is_deleted` column.
[Grouping, trees and trash](lists.md#grouping-trees-and-trash)

**Bulk actions.** Decorate a panel method with `@action` and it appears on the list.
"Select all matching" works on forty thousand rows as cheaply as on three, and big jobs
can run in the background. [Bulk actions](lists.md#bulk-actions)

**Undo.** Deletes, bulk edits and inline edits show a toast with an **Undo** button for a
few seconds. Nothing is ever restored over somebody else's newer change. [Undo](undo.md)

**Export and import.** CSV, TSV and JSON export of exactly what you are looking at, and a
CSV import wizard that maps, previews and validates before writing anything.
[Export](lists.md#export) · [Import](lists.md#import)

**Customise a list from the page.** The Customise rail lets a person reorder and hide
columns, pick filters and add stats for themselves, or for everyone if they may.
[The customisation rail](lists.md#the-customisation-rail)

## Numbers and charts

**Summary bar.** A footer that sums money columns, averages ratings and leaves
identifiers alone, with no configuration. [The summary bar](analytics.md#the-summary-bar)

**Stats and charts.** Declare `Stat` and `Chart` on a panel; they follow the current
filters, compare against the previous period, and render as inline SVG.
[Analytics](analytics.md)

**Dashboard.** Stat, chart, table and list cards declared in Python, or built and
rearranged from the UI. [Dashboards](pages-and-dashboards.md#dashboards)

## Records and forms

**Inferred forms.** Widgets chosen per field type — searchable pickers for big relations,
a colour picker, image thumbnails, a JSON editor — grouped into sections.
[Forms](forms.md)

**Layout DSL.** `Tabs`, `Tab`, `Section` and `Row` arrange the form when the inferred
grouping is not what you want. [The layout DSL](forms.md#the-layout-dsl)

**Editable inlines.** Edit child records on the parent's page, in one transaction.
[Editable inlines](forms.md#editable-inlines)

**Duplicate and bulk edit.** Both reuse exactly the form and validation a single record
uses. [Duplicating a record](forms.md#duplicating-a-record) ·
[Bulk edit](forms.md#bulk-edit)

**Cascade-delete preview.** Before a delete, see everything it takes with it, counted.
[Cascade-delete preview](forms.md#cascade-delete-preview)

**History.** Every change made through the panel is in the record's history, with a diff.
[The audit log](permissions.md#the-audit-log)

## Access and safety

**Permissions.** Django's model permissions out of the box, with object-level and row
filtering hooks, enforced on every page, action and API call. django-guardian and
django-rules are picked up if installed. [Permissions](permissions.md)

**Roles.** Manage Django groups as roles in a permission matrix: tick view / add /
change / delete per model, review the diff, save once. Nobody can grant a permission they
do not hold. [Roles](roles.md)

**Sensitive fields.** Fields named like `password`, `api_token`, `iban` or `card_number`
are masked everywhere — lists, exports, search, the API, the audit log. A permitted user
can reveal one value at a time, and each reveal is logged. [Sensitive fields](sensitive-fields.md)

**Read-only mode.** One setting turns every write off, for a maintenance window or a
replica. [Read-only mode](permissions.md#read-only-mode)

**Impersonation.** See the panel as another user, with a banner you cannot miss.
[Impersonation](permissions.md#impersonation)

**Audit log.** Who changed what, when, from where, with a diff.
[The audit log](permissions.md#the-audit-log)

**Multi-tenancy.** Scope every model to the current tenant with one setting. Anything it
cannot scope is refused, not shown. [Multi-tenancy](tenancy.md)

**Security hardening.** Login throttling, CSP nonces, upload validation and `--deploy`
checks. [Security](security.md)

## Look and feel

**Brand colour.** One colour becomes a full light and dark palette, contrast-checked.
Logo, title and theme can also be set from the settings screen. [Theming](theming.md)

**Theme presets.** Pick a whole look — `vantage`, `shadcn`, `slate`, `rose` and more — in
one line, and let each person choose their own from the account page.
[UI theme presets](ui-themes.md)

**Light, dark and system.** With no flash on load. [Light, dark and system](theming.md#light-dark-and-system)

**Right to left and translations.** RTL layouts and English and Arabic catalogues ship
in the box. [Internationalization](internationalization.md)

**Environment banner.** A coloured banner so nobody mistakes production for staging.
[The environment banner](theming.md#the-environment-banner)

**Accessibility.** Keyboard navigation, labelled controls and visible focus throughout.
[Accessibility](accessibility.md)

## Getting around

**Command palette.** <kbd>⌘</kbd> <kbd>K</kbd> jumps to any model, record, page or action.
[The command palette](pages-and-dashboards.md#the-command-palette)

**Global search.** Search across every model you can see, from the top bar.
[Global search](lists.md#global-search)

**Sidebar.** Group, rename, reorder and hide entries with `VANTAGE["NAV"]`, or from the UI.
A nav entry can hide a model, never grant access to one. [Settings reference](settings.md)

**Schema map.** A live diagram of your models and how they relate, every box a link.
[The built-in pages](pages-and-dashboards.md#the-built-in-pages)

## Operations

**Data quality.** A read-only audit of every model for empty fields, duplicate candidates,
broken references and odd dates. `manage.py vantage_data_quality --fail-on warn` runs the
same audit in CI. [Data quality](data-quality.md)

**System page.** Migrations, cache, database and task backend at a glance.
[The system page](deployment.md#the-system-page)

**Query-count guarantees.** Joins are derived from what each page renders, and tests
assert the number of queries does not grow with the number of rows.
[Query cost](lists.md#query-cost)

## Extending it

**Custom pages.** A class with a title and a `get_context` method becomes a page with a
URL and a nav entry. [Custom pages](pages-and-dashboards.md#custom-pages)

**Slots.** Put your own HTML into named places in the shell.
[Slots](extending.md#slots)

**Plugins.** A package can add panels, pages and widgets through an entry point.
[Plugins](extending.md#plugins)

**JSON API.** Every panel as a JSON API, through the same permission and validation path
as the UI. [The JSON API](api.md)

**`contrib.admin` adapter.** Existing `admin.py` files can be served by Vantage.
[The contrib.admin adapter](extending.md#the-contribadmin-adapter)
