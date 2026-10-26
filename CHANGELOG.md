# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is below `1.0` the public API may change between minor versions.
Anything that would be a breaking change after 1.0 is listed under **Changed** with the
word *breaking*, so the log is useful either way.

## [Unreleased]

## [0.2.0a1] — 2026-10-26

Six features, each built on its own branch and merged into `release/0.2.0a1`. Five
migrations were added (`0012`–`0015` in `vantage`, plus `shop/0002` in the demo), so run
`migrate` after upgrading.

### Known limitations

- **The per-person UI theme is a cookie**, not a column on the user, so it does not
  follow someone to another browser.
- **Undo is not offered everywhere.** A hard delete gets no Undo button when the cascade
  has delete signal receivers, a database-level `on_delete`, an overridden `delete()`, a
  binary column, or more than `UNDO_MAX_ROWS` rows. The delete itself still runs.
- **Masking is inferred from field names.** A secret kept in a field called `notes`
  needs `sensitive_fields`. Panel method columns, custom templates, slots and
  `list_stats` that read a sensitive field are not masked; see
  [Sensitive fields](docs/sensitive-fields.md#threat-model).
- **Roles are global.** With `ROLES_PERMISSION` widened, whoever is let in can search
  every user and see anyone's effective permissions; groups and users are not
  tenant-scoped.
- **Data quality row estimates and the skip threshold need PostgreSQL**; other databases
  sample the newest rows.
- New strings are not yet in the `.po` catalogues.

### Added

#### List views

- Any list can also be shown as a **Kanban board**, a **month calendar**, a **gallery**
  or a **map**, picked from a switcher in the list toolbar. Each view is inferred from
  the model (a status-like choices field, an event-like date, an image field, a
  `PointField` or latitude/longitude pair) and overridable in one line: `list_views`,
  `kanban_field`, `calendar_field`, `gallery_field`, `map_fields`, `card_fields`.
- Every view honours the current search, filters, trash, tenancy and permissions, and
  the stats strip still applies. The view is part of the URL (`?view=kanban`),
  remembered per person per model (migration `0012`), and stored in saved views.
- Kanban cards move by drag and drop or a keyboard-accessible "Move to…" menu that
  also works without JavaScript. A move goes through the same permission checks,
  `full_clean` validation, optimistic locking and audit log as inline editing. Column
  counts come from one aggregate and cards from one windowed query.
- **Group by** on the board and **Dates from** on the calendar pick the field the view
  is arranged by, from every eligible field (or `kanban_fields` / `calendar_fields`;
  `[]` hides the picker; sensitive fields are never offered). The choice is in the URL
  (`?by=`), kept in saved views and remembered per person (migration `0015`), and a
  card always moves within the field it was drawn under. `vantage.E078` and
  `vantage.E079` validate the lists.
- The map is a dependency-free, server-rendered SVG that fetches nothing by default;
  `VANTAGE["MAP_TILE_URL"]` opts into raster tiles.
- `vantage.E070`–`vantage.E076` validate the new attributes; `vantage_scaffold` writes
  the inferred views out.

#### Undo

- Deletes (record page and bulk), bulk edits, inline cell edits and the built-in
  Publish/Unpublish actions offer **Undo** in their toast for `VANTAGE["UNDO_WINDOW"]`
  seconds (default 10; `0` disables; `ModelPanel.undo = False` per model, checked by
  `vantage.E077`). Ctrl/⌘+Z redeems it outside text fields.
- Edits are restored only where nobody has changed the row since; soft deletes are
  un-trashed; hard deletes are snapshotted and restored with their original keys,
  refusing rather than overwriting if a key or unique value has since been taken.
- Tokens are single-use, stored hashed (`UndoEntry`, migration `0014`), bound to user
  and session, permission-checked again at redeem time, CSRF-protected and audit-logged.
- `delete_selected`, an opt-in built-in bulk delete that trashes where the model has a
  trash. `@action(undo_fields=[...])` and `@action(undo=...)` let custom actions offer
  Undo. `manage.py vantage_purge_undo` clears expired entries.

#### Data quality

- A **Data quality** page (System) and `manage.py vantage_data_quality`: a read-only
  audit of every registered model for empty-value rates, duplicate candidates, orphaned
  references (generic foreign keys included), values outside `choices`, suspicious
  dates, and empty or stale tables. Reports hold counts only, never row values; findings
  link to the pre-filtered list wherever the filter system can express them.
- Bounded and cached: sampling above `SAMPLE_SIZE` rows, per-check and per-page time
  budgets, a re-run button, and `--fail-on` for CI. Checks are pluggable
  (`vantage.dataquality.Check`, `@register_check`).
- `VANTAGE["DATA_QUALITY"]` (enable, permission, thresholds) and
  `ModelPanel.data_quality = False | [check names]` (`vantage.E063`).
- `Page.post(request)`: a custom page can handle a form POST.

#### Sensitive fields

- Sensitive fields are **inferred and masked** with no configuration: whole-word names
  (password, token, api_key, secret, ssn, iban, card_number, cvv, pin, otp, hash,
  signature, …) on value-bearing field types, any `Encrypted*Field`, and Django's
  `User.password`, which can never be revealed. `ModelPanel.sensitive_fields` overrides
  inference (`[]` switches it off); `VANTAGE["SENSITIVE_PATTERNS"]` extends or replaces
  the word list.
- Values render as a fixed-width `••••••••` (or `•••• 4242` for card and account
  numbers) on every surface: list cells and cards, the detail page, exports, the JSON
  API, search, the palette, autocomplete, inline and bulk editing, duplicate, import
  preview, audit history, and titles where a `__str__` contains a secret.
- Sensitive fields can never be searched, filtered, sorted or aggregated
  (`vantage.W090` warns when a panel declares one; `vantage.E090` rejects a bad
  `sensitive_fields`).
- **Reveal on click**: a rate-limited, audited `POST` answering with
  `Cache-Control: no-store`, gated by the new `vantage.reveal_sensitive` permission or
  `ModelPanel.can_reveal()`. The value re-masks after 30 seconds or on navigation.
- A **Set password** page next to a masked password hash. Sensitive inputs on edit
  forms are write-only: an empty submission keeps the stored value.

#### Roles & permissions

- A **Roles** screen (`_/roles/`, in the sidebar and palette) manages `auth.Group`s as
  roles: create, rename, duplicate, delete with an impact preview; a permission matrix
  (models by app, view/add/change/delete plus custom permissions, tri-state toggles,
  filter, keyboard navigation) saved atomically after a "+N / −M" review; members with
  a searchable picker; and any user's effective permissions with the source of each.
- Superusers only unless `VANTAGE["ROLES_PERMISSION"]` widens it, and a non-superuser
  can only grant what they hold. Concurrent edits conflict (409) instead of
  overwriting; every change is audit-logged; read-only mode is respected; the matrix
  costs a constant number of queries.

#### UI themes

- `VANTAGE["UI_THEME"]` selects a whole look. Ten ship: `vantage` (the original,
  byte for byte), `shadcn`, `slate`, `stone`, `zinc`, `rose`, `blue`, `green`, `violet`
  and `orange`. An explicit `BRAND_COLOR` or `BRAND_RADIUS` still wins.
- `VANTAGE["UI_THEMES"]` registers custom presets, which inherit from `vantage`.
- `VANTAGE["UI_THEME_TOGGLE"]` (on by default) lets each person pick a look from the
  account page, with a live preview and no flash on load. The site default is editable
  on the settings screen (migration `0013`).
- Every built-in preset is tested light and dark for 4.5:1 text contrast and 3:1
  form-field borders.

### Changed

- Card and panel borders read `--border-width`, and the bulk-action bar uses the shadow
  token, so presets can restyle both.
- The detail page shows sensitive fields masked instead of leaving them out.

### Fixed

- A record URL whose pk the field cannot parse (`/1/change/`) is a 404, not a 500.
- Custom pages no longer run `get_context()` twice per render.

## [0.1.0a1] — 2026-10-25

The first published pre-release. Everything below is new in it: the entries are
reconstructed from the commit history, which is the design record for this package and
worth reading directly — each commit explains why the shape is what it is and what was
rejected.

### Known limitations

Read these before deploying an alpha anywhere it matters.

- **The API is not frozen.** Attribute names, method signatures and settings keys may
  change in any `0.x` release; breaking changes will be marked *breaking* below.
- **No WCAG 2.2 AA claim.** The three blockers the first audit named are fixed (see
  *Fixed*), but the remaining findings and the lack of any screen-reader pass are
  listed in [Accessibility](docs/accessibility.md#4-known-gaps).
- **The CSV importer stashes uploads on local disk.** Preview and commit must reach
  the same filesystem: sticky sessions or a shared temp directory when running more
  than one process. See [Deployment](docs/deployment.md#exports-stream-imports-are-capped).
- **Exports have no row ceiling.** They stream, so memory is flat, but a very large
  table is exported in full.
- **Multiple sites share one settings dict.** Branding, `NAV` and the permission
  resolver are global.

### Added

#### The kernel

- `vantage.conf` — the `VANTAGE` settings dictionary, with defaults, derived values
  (`SITE_ACRONYM` from `SITE_TITLE`, `LOGO_DARK` from `LOGO`) and loud rejection of
  unknown keys with a did-you-mean suggestion. A typo must not silently produce a
  default-coloured panel.
- `vantage.registry` — registration, auto-registration with app and model exclusion
  patterns, and an `INCLUDE_ONLY` allowlist. Auto-registration never clobbers an
  explicit panel.
- `vantage.panels.ModelPanel` — the full attribute surface, every value defaulting to
  `None` meaning *infer*. Resolution is per attribute, which is the mechanism behind the
  guarantee that setting `list_display` overrides the columns and nothing else.
- `vantage.checks` / `vantage.panel_checks` — field paths validated at `manage.py check`
  time. Filters and search reject panel methods because they resolve in SQL;
  `list_display` accepts them because it renders in Python.
- `vantage.introspection` — the single facade over `Model._meta`, encoding the three
  documented traps: `ManyToOneRel`'s inverted flags, `GenericForeignKey` claiming
  `many_to_one` while `related_model` is `None`, and auto-created through models storing
  the owning class in `Meta.auto_created`.
- Autodiscovery via Django's `autodiscover_modules` contract, so a `panel.py` that raises
  cannot leave the site half-registered.

#### Inference

- `vantage.inference` — pure functions, model in and configuration out, with no database,
  request or settings access, which is what makes the engine table-testable.
  `infer_list_link`, `infer_list_display`, `infer_list_filter`, `infer_list_search` and
  `infer_ordering`. Sensitive field names (password, token, secret, api_key, hash…) are
  never shown, searched or filtered.
- `vantage.widgets` — 24 `FieldKind`s mapping every Django field to a control, replacing
  Django's 2005-era `formfield()` defaults: `type=email`, `type=url`, `type=number` with
  step derived from `decimal_places`, `maxlength` from `max_length`, `accept` from
  `FileExtensionValidator`, `pattern` on slugs. Django's `ModelForm` machinery is reused
  wholesale; only the widget mapping is ours.
- `vantage.optimizer.plan_related` — `select_related`/`prefetch_related` derived from the
  paths a page actually renders, so adding a column cannot reintroduce an N+1.
- `manage.py vantage_scaffold` — writes what inference decided out as an ordinary
  `panel.py` you then own. `--write` refuses to clobber an existing file without
  `--force`. The strongest test asserts the generated configuration passes the package's
  own system checks.
- `vantage.patterns` — detectors for soft delete, hierarchy, timeline, publishing and
  money, feeding trash views, tree rendering, date bucketing, inferred publish/unpublish
  actions and currency formatting.

#### Theme and rendering

- `vantage.color` — full sRGB ⇄ OKLab ⇄ OKLCH conversion with binary-search gamut
  clamping that gives up chroma rather than lightness. `accessible_pair` walks lightness
  only as far as it must to reach 4.5:1. Contrast is asserted numerically across the
  whole hue circle at 10-degree steps in both themes.
- `vantage.theme` — the token layer, adopting shadcn/ui's semantic-role token contract.
  Neutrals are fixed; only brand-derived tokens vary with `BRAND_COLOR`.
- The panel shell: `base.html`, nav derived from the registry, the list page, the theme
  toggle persisted to both `localStorage` and a cookie, distinguished empty states, and
  icons inlined as SVG so a strict CSP is workable.
- Light, dark and system themes, and RTL, both from day one.

#### List page

- Validated column sorting with a primary-key tiebreaker appended to every sort, because
  pagination over a column with duplicate values is otherwise unstable.
- `vantage.filters` — `ChoiceFilter`, `BooleanFilter`, `DateFilter`, `RelatedFilter` and
  `NumericRangeFilter`, with counts off by default, CHEAP/COSTLY/NEVER classification, a
  hard query budget, and the disjunctive self-exclusion rule so an unselected option
  never falsely reads zero.
- `vantage.stats` — `Stat` and `Chart` declared in Python and computed against the
  filtered queryset. Four stats cost one query; charts render as inline SVG whose
  segments separate by lightness rather than hue, so a `BRAND_COLOR` change and the dark
  theme both survive.
- `vantage.summary` — a per-column summary bar at the foot of every table, on by default
  and needing no configuration. The whole bar is one `aggregate()` call. It declines to
  compute what it would get wrong: identifiers stored as numbers, and anything reached
  through a join.
- Streaming CSV, JSON (newline-delimited) and TSV export, all honouring the active
  search, filters and sort, all `Cache-Control: no-store`.
- Time bucketing — day, week, month or year — computed from the rows already on the page
  rather than a second grouped query.
- Hierarchy rendering for self-referential models, with mptt and treebeard ordering
  handled natively and a plain self-FK arranged depth-first in Python.
- `vantage.listprefs` — per-list, per-user customisation from the UI: filters, facet
  counts, the stat strip, charts, the summary bar, visible columns and their order, page
  size and search fields. Resolution is UI override → `panel.py` → inference, with a
  per-user row always beating the site-wide default. Switching a section off removes its
  queries, not merely its markup.

#### Detail, forms and relations

- A read-only detail page showing fields the list omits by design, honouring
  `get_queryset` so multi-tenancy hooks actually scope access.
- Bounded related-record previews — the first few rows with the child panel's own
  columns, plus a count and a link through to the pre-filtered list. Five rows and a link
  cost the same two queries whatever the count.
- Create, edit and delete, with a cascade-delete preview showing the real tree of what a
  delete takes with it, counted.
- `vantage.layout` — the form layout DSL: `Tabs`, `Tab`, `Section`, `Row` as plain nested
  Python. A field the layout forgets is appended rather than dropped; a field it names
  that does not exist fails `manage.py check` (vantage.E017).
- Relation autocomplete: one endpoint for every model, with three guards — the target
  must be registered, the view permission is checked against the target model, and
  row-level `filter_queryset` applies. "Is there more" is answered by fetching one extra
  row rather than a `COUNT`.
- `vantage.inline` and `vantage.concurrency` — inline cell editing with optimistic
  concurrency.

#### Actions and bulk operations

- `@action` — ordinary panel methods with a decorator. `destructive`, `permission`,
  `max_rows` and `confirm` are declared alongside.
- Selection travels as a predicate plus an exclusion set, never as a list of primary
  keys, so selecting forty thousand rows costs the same as selecting three. The scope
  actually resolved is always reported back in words.
- `vantage.models.VantageJob` — one row per bulk action too large to run inline.

#### Site-wide features

- Dashboard widgets: `StatCard`, `ChartCard`, `TableCard`, `ListCard` and `TemplateCard`,
  on a four-column grid. `DefaultDashboard` means zero config still produces something
  worth looking at. Widgets fail independently and are permission-gated *before* render,
  so a widget over a forbidden model costs zero queries.
- Command palette (⌘K), global cross-model search, and a no-JS results page.
- Schema map derived from the registry, laid out server-side in Python, every node a
  link.
- Custom pages: a class, a title and a `get_context` method, with the URL, nav entry and
  rendering all defaulted, and a generic renderer for a page with no template.
- Declared navigation via `VANTAGE["NAV"]` — groups, order, labels, icons, badges, and
  arbitrary links. `NAV_UNLISTED` defaults to `append` so a forgotten model still
  appears.
- The audit log (`VantageLogEntry`) and its viewer.
- Saved views, with grants and per-user state.
- The CSV import wizard: upload, map columns, dry-run diff, commit.
- System health page — migrations, cache, database, task backend — superusers only.
- The in-panel settings screen: logo upload, brand colour, title and theme, editable by
  a superuser with the database value overriding the file.
- Read-only mode and user impersonation, both enforced in the permission layer.
- `vantage.slots` and `vantage.plugins` — extension points.
- `vantage.contrib.django_admin` — an adapter for projects with existing
  `contrib.admin` classes.
- `vantage-editors.js` — a dependency-free markdown toolbar for `richtext` fields,
  degrading to the plain textarea it enhances. File upload drag-and-drop is a separate
  feature, `upload.js`, wired through `vantage/widgets/file.html`.

#### Permissions

- Three concerns kept apart deliberately: model-level (may they see this section),
  object-level (may they act on this row, N=1 only) and queryset-level (which rows may
  they see, answered in bulk). `filter_queryset` exists so that a hundred-row page never
  runs a hundred uncached `has_perm(perm, obj)` calls.
- `GuardianPermissions` for row scoping, using `get_objects_for_user` with
  `accept_global_perms`. Opt-in, because whether object permissions should narrow the
  admin is a policy decision.
- django-guardian, django-rules and django-simple-history are used if present, never
  required.

#### Project infrastructure

- `example/` — a runnable "Northwind Supply" demo: 9 models covering the full spread of
  field types and relation shapes, a seeded RNG, and 200 orders backdated over 180 days.
- Guards on the stylesheets themselves (`tests/test_stylesheets.py`): no physical
  directions, so RTL keeps working from `dir="rtl"` alone; every colour from a token,
  with a single deliberate exception for neutral shadows; and anything animating must
  respect `prefers-reduced-motion`.
- CI over Python 3.12/3.13/3.14 × Django 5.2 LTS and 6.1, with Django `main` as an
  allowed failure; a lint job; an advisory type-check job; and a build job that installs
  the wheel into a clean virtualenv and renders a template out of it.
- `scripts/verify_wheel.py`, `tox.ini`, `.pre-commit-config.yaml`, `CONTRIBUTING.md`,
  `SECURITY.md` and `CODE_OF_CONDUCT.md`.
- A documentation site at <https://pythondeveloper6.github.io/django-vantage/>, built
  from `docs/` with MkDocs Material, checked with `--strict` on every pull request and
  published from `main` by `.github/workflows/docs.yml`.
- A logo, and screenshots of the demo in both themes, regenerated by
  `scripts/screenshots.py` from a pristine copy of the demo database.

### Changed

- The Django requirement is `>=5.2,<6.2`, and 6.0 joined the CI matrix. The bound
  moves only when a new Django joins the matrix — see CONTRIBUTING's *Supported
  versions*.

- Python floor raised to 3.12 — the intersection of Django 5.2 LTS and 6.x.
- *Breaking, pre-release:* `Section(title=...)` is keyword-only. The
  leading-string-is-the-title convenience was ambiguous: a section titled "Publishing"
  containing `status` was indistinguishable from a section containing the two fields
  `title` and `body`.
- Inferred actions apply only to panels that declare none. Merging them into a declared
  list meant a panel offering one action silently grew two more.
- `build_queryset` returns the unfiltered base alongside the filtered queryset, because
  the facet engine needs the base to compute a disjunctive count.
- `dashboards.creation_field` no longer delegates to `detect_timeline`; they answer
  different questions. The detector picks the best field to *group by* and prefers a
  domain date, while "recent records" needs the date the row came into existence.
- Query-count tests assert that rendering five rows and two hundred and five rows cost
  the same, with a ceiling, rather than asserting an exact number that legitimate
  features move.
- Package metadata: the `htmx` and `tailwind` keywords are gone — neither is used
  anywhere in the package — and the deprecated `License ::` classifier is dropped in
  favour of the PEP 639 `license` expression. The sdist now carries `tests/` so
  downstream packagers can run the suite.

### Fixed

#### In the release preparation

- The grid's "Keyboard shortcuts" switch now also silences `/` (it compared against a
  value the grid never writes) and the inline editor's `e` (it never checked).
- An empty linked cell names its record to assistive technology instead of announcing
  "link, dash", on the list grid, dashboard widgets and related blocks.
- `--input`, the form-field boundary, clears 3:1 against every surface in both themes;
  it was 1.27:1 in light and translucent in dark.
- The FAQ, security and settings pages describe the CSP nonce and the security
  settings as the code implements them.
- Every install, clone and advisory link names the real repository.
- `LOGO_DARK` is rendered: swapped in for the dark theme on the sidebar and both
  sign-in screens. It was resolved and editable, and shown nowhere.
- With a `LOGO` set, the collapsed sidebar shows the acronym tile instead of nothing.
- `THEME_TOGGLE = False` now also ignores a theme a user saved before it was turned off.
- An unreadable `BRAND_COLOR` or `BRAND_RADIUS` is a `manage.py check` error
  (`vantage.E004`, `vantage.E005`) instead of a failure on every page render.
- A trending stat on a list page compares with the period before the filtered one. The
  list never passed the stats engine its unfiltered rows, so filtering on the trend's
  own date field left nothing to compare with and every trend read as the whole total.
- The sidebar's dashboard item is highlighted only on the dashboard, not on every page
  of a panel mounted below the site root.
- The grid's row checkboxes no longer render as "☐…".
- On Django 5.2, inlines no longer grow an extra column for the parent link. Before
  Django 6.0 the inline formset appends that field to the list it is given, and the
  list it was given was the one that also named the columns.
- A self-referencing model on the schema map's top row keeps its loop on the canvas.

#### Earlier

- The dashboard card cap was acting as an access rule: `DefaultDashboard` capped at
  `MAX_DEFAULT_STATS` *before* filtering by permission, so a user who could see exactly
  one model whose model sorted outside the first twelve was served an empty dashboard.
- `vantage.SiteConfiguration`, `VantageLogEntry`, `SavedView`, `SavedViewGrant`,
  `SavedViewUserState` and `ListPreference` were auto-registering with Add/Edit/Delete
  forms. An editable audit log is not an audit log, and a generic CRUD form for a saved
  view routes around every permission rule in `savedviews.py`.
- Derived navigation listed custom pages without consulting `Page.has_permission`, so a
  gated page appeared in the sidebar for users who could not open it. For a page gated
  on something sensitive, the label itself is a disclosure.
- Audit hooks are placed relative to the transaction, not inside it: create and edit log
  *outside* (catching a logging error inside an atomic block still marks it for
  rollback), delete logs *before* (`Model.delete()` blanks the pk).
- `{% static %}` corrupted a URL the database layer had already resolved
  (`/media/vantage/logo.svg` became `/static/media/vantage/logo.svg`). A new
  `{% asset %}` tag passes resolved URLs through.
- The bulk-action `<form>` was never closed and ran to the end of the document,
  swallowing the pagination. There is now a test asserting open/close parity.
- The bulk-selection column vanished when a parallel change re-indented the table; the
  row checkbox now also carries the record's own label rather than a generic one.
- CSV import: encoding order is `utf-8-sig`, `utf-8`, `latin-1` — plain `utf-8` succeeds
  on a BOM file and glues the BOM to the first header. Binary is rejected before
  decoding, because `latin-1` decodes a PNG happily.
- CSV import: `CheckboxInput.value_from_datadict` reads the string `"0"` as `True`, and a
  CSV that writes 1/0 is entirely normal; the blind spot is closed before the widget sees
  the value.
- CSV import: foreign keys accept a label as well as a primary key, because the exporter
  writes `str(obj)`. Ambiguous matches are an error rather than a coin flip.
- Combining `Sum('total')` with `Count('posts')` in a single `aggregate()` inflates
  *both* through the join fan-out, not only the relational one. Stats are partitioned by
  which multi-valued relation they touch, and a single stat spanning two raises rather
  than lying.
- Messages were rendered per template, so a success message set before a redirect was
  never displayed and would later surface on an unrelated screen. They now render in
  `base.html`.
- URL ordering hazards: `add/` must precede the pk route, and because the pk converter is
  `path:`, `edit/` and `delete/` must be declared before the bare detail route. Both are
  asserted by a `resolve()` round trip.
- `@dataclass` installs its own `__init__` unconditionally, silently discarding the
  custom constructors the layout containers need; they are `init=False` now.
- Light-dismiss on the column popover is capture-phase, because a bubbled click arriving
  after the popover re-renders has a detached target.
- A missing `contenttypes` import in `models.py` had left the whole package unimportable.
- Three real contrast failures in hand-picked neutrals (light secondary text at 4.42:1,
  the light success surface at 4.34:1, white on the dark destructive surface at 3.42:1),
  all corrected by computing the required lightness.
- Inference: choice fields were offered as search targets (the column stores `draft`
  while the user types `Draft`), search ties broke alphabetically rather than on
  declaration order, and a `CharField(300)` was used as a column, which wrecks row
  height.

### Security

- Sort parameters reach `order_by`, so they are never trusted: a value is dropped unless
  it names a column currently in `list_display` **and** resolves to a concrete
  single-valued field path. Without the `list_display` restriction the sort parameter is
  an oracle for columns the panel deliberately does not expose.
- The autocomplete endpoint is a read API over arbitrary models; the target must be
  registered, the view permission is checked against the *target* model rather than the
  page the request came from, and row-level filtering applies.
- Declared navigation is presentation only. It reorders, renames and hides; it can never
  grant access. Naming a model in `NAV` does not make it reachable.
- A relation whose model the user cannot view is omitted from the detail page entirely
  rather than shown as a bare count — "this customer has 47 orders" is itself a leak.
- Read-only mode is enforced in the permission layer, before the superuser bypass,
  because it is a statement about the deployment rather than about who is asking.
- Impersonation is superusers only, never onto another superuser, refused for inactive
  accounts, and logged at warning level on both entry and exit.
- Permission-scoped exports are `Cache-Control: no-store`, so an extract does not sit in
  a shared cache.
- Aggregate aliases are positional, so a user-supplied choice key can never become an
  identifier.
- Sensitive field names are excluded from columns, filters, search, exports and audit
  diffs.

[Unreleased]: https://github.com/Pythondeveloper6/django-vantage/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/Pythondeveloper6/django-vantage/compare/v0.1.0a1...v0.2.0a1
[0.1.0a1]: https://github.com/Pythondeveloper6/django-vantage/releases/tag/v0.1.0a1
