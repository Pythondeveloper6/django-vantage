# List-page design decisions

**Date:** 2026-08-13 · **Source:** competitive research across Filament, Linear, Airtable, Notion,
Metabase, Retool, Jira, GitHub, Shopify Polaris, AG Grid, TanStack Table, Algolia, and Django's own
`contrib.admin`. **Milestone:** M3.

Where products disagree, the disagreement is recorded with the reasoning, because that is where the
real decision lives.

---

## 1. Statistics placement — corrects the API spec

**The API design spec said stats render "in a collapsible strip above the grid". That was wrong**,
and it was wrong in the direction most admin panels get wrong. Filament puts stat cards above the
table; that costs 100–140px of vertical space on every page load, permanently, above the thing the
user actually came for. Linear made the opposite call (collapsible right sidebar behind a keyboard
shortcut) and Airtable a third (a 32px summary bar at the bottom, per-column aggregates). Directus
refuses to put analytics on a list page at all.

**Decision — three affordances, defaulting to the cheapest:**

| Affordance | Default | Model |
|---|---|---|
| Bottom summary bar, per-column aggregate | **on**, zero config | Airtable |
| Collapsible stat/chart panel, right sidebar, keyboard toggle, remembered per user per model | off until toggled | Linear |
| Pinned stat cards above the table | **opt-in** via `list_stats`, capped at one row | Filament |

`list_stats` therefore means "pin these above the table", and is explicitly the opinionated choice a
panel author makes for a model that warrants it — not the default experience.

## 2. Aggregation must be one query, never N

Filament's canonical example runs one `COUNT(*)` per stat card, each re-running the full filtered
query, and defaults to re-polling every 5 seconds. Do neither.

```python
qs.aggregate(
    total   = Count("id"),
    paid    = Count("id", filter=Q(status="paid")),
    revenue = Sum("total", filter=Q(status="paid")),
)
```

One round trip for the whole stat row. Polling defaults to **off**. One HTTP request for the entire
stat panel, never one per card.

**Never `COUNT(*) OVER ()`** to get page and total together — it defeats `LIMIT` early-exit; a
documented case went 0.251 ms → 1915 ms.

**Stats are computed from the filter predicate, not the rendered rows.** Retool's built-in filters
are client-side over loaded rows and its summary row does not recalculate — the moment the dataset
exceeds one page that approach is simply wrong.

## 3. Counting policy

`COUNT(*)` is O(n) under MVCC. GitLab measured 14,972 ms vs 1.198 ms for a planner estimate at 1.2%
error, and answers it in the UI with a bounded `1000+`.

- Filtered set under ~10k estimated rows → exact `COUNT(*)`.
- Above → `EXPLAIN (FORMAT JSON)` estimate rendered with a `~` prefix, or a `LIMIT`-bounded `10,000+`.
- Whole-table size for gating decisions → cached `reltuples`.
- **Estimates are whole-table and over-report to permission-restricted users** — gate that path to
  unrestricted users only.

## 4. Faceted filter counts

**The disjunctive rule is non-negotiable.** Counts for a facet's other options must be computed with
that facet's own filter removed, all others applied. Otherwise every other option reads `(0)`.
Django 5.0's `FacetsMixin.get_facet_queryset(exclude_parameters=...)` already implements this
correctly — copy the architecture. One `.aggregate()` folds all values of one filter, so the cost is
one query **per filter**, not per value.

Most products ship no facet counts at all — Linear, Notion, GitHub, Airtable and Jira all decline,
putting counts on **group headers** instead, where they are nearly free. So:

- Classify each field at registration as `CHEAP` (bool, choices, indexed date bucket, FK ≤50 distinct),
  `COSTLY`, or `NEVER` (free text, JSON, Python properties).
- Counts on for CHEAP only; COSTLY renders a typeahead.
- Hard budget ~6–8 facet queries per render; beyond that drop counts and log.
- Group-header counts always — highest value per unit of cost in the whole feature.
- Cache on `(model, normalized_filters, permission_scope)`. **Omitting the permission scope is a data leak.**

## 5. Filter grammar

Store a recursive tree, versioned from day one; expose CNF (within-facet OR, across-facet AND) in the
simple UI. Relative dates store as **ISO-8601 durations** (`"P2W"`), not resolved dates, so a saved
view stays correct forever.

## 6. Saved views

**Never autosave a shared view.** Airtable's entire personal/locked view feature line exists because
its collaborative views autosave and one person's filter silently changed everyone's screen.

Shopify Polaris supplies the cleanest control set: **Save / Save as / Cancel**, where *Save degrades
to Save-as when the view is immutable to you* — one rule covering every permission case.

- URL: `?view=42&f=<override>&sort=-created&page=2`. `f` present ⇒ diverged; absent ⇒ pristine.
- Keep `edit-scope` and `visibility` as **separate axes** — Airtable conflates them and has to warn
  users that personal views are not a security feature.
- Locking freezes configuration, not data, and binds the creator too.
- Optimistic locking (`version` int) on shared views.
- **Validate field references on load, not on save.** Fields get renamed. Render a broken pill in an
  error state with a "results may be broader than intended" banner; never auto-repair, never silently drop.
- A view is a *query re-evaluated per viewer* (Jira): two users legitimately see different rows and
  different counts. Never cache a count without the permission scope.
- URL filter state has **zero** security value; always re-apply row permissions server-side.

## 7. Column state

Layered resolution, per-user wins:

```
effective = merge(ModelPanel defaults, saved-view state, per-user override)
```

Server-authoritative, **rendered into the initial HTML** so there is no flash — the documented AG Grid
failure is the grid painting default widths then jumping. localStorage is a same-device write-through
crash cache only. Debounced PATCH (400–800ms trailing for widths, immediate for toggles), flushed on
`visibilitychange → hidden` via `sendBeacon`.

State shape follows TanStack's independent slices (`columnSizing`, `columnOrder`,
`columnPinning:{left,right}`, `columnVisibility`) rather than AG Grid's flat array, because **pinning
is a partition, not a per-column property** — modelling it flat forces you to answer "what happens
when a user drags an unpinned column into the pinned region."

Wire semantics borrow AG Grid's `undefined` ≠ `null` (do-not-apply vs clear), enabling partial updates.

**Security:** always whitelist restored column ids against the code-declared `list_display`. A
user-PATCHable blob driving column selection is an information-disclosure bug.

**Identity:** key on the field name, never index. On restore, ignore unknown ids and append
newly-added columns — otherwise adding a field to `list_display` is invisible to existing users.

**The sharpest line in the whole design: the URL owns "what rows", preferences own "how they look."**

## 8. Paginate, don't virtualize

50–200 rows per page. This buys `position: sticky` pinning, browser find-in-page, Ctrl+A, and native
print for free, and avoids an entire bug class — virtualizers position rows with `transform`, which
creates a containing block and breaks sticky descendants.

CSS consequences: `table-layout: fixed` is mandatory once columns resize; widths drive from CSS
custom properties so a resize is one `setProperty`; `border-collapse: collapse` **breaks borders on
sticky cells**; sticky cells need explicit backgrounds or content shows through.

## 9. Keyboard: row-level focus, and it's forced

Cell-level focus consumes the single-letter namespace — in a spreadsheet, typing `p` types "p".
So a cell-level grid *cannot* have Linear's mnemonic action keys. This is the deepest reason Linear
and Airtable feel different, and it is a forced choice, not taste.

**Row-level primary; cell mode an explicit opt-in entered with `e`, exited with `Esc`**, with the
transition announced in `aria-live`.

Linear's highest-leverage idea, adapted: single mnemonic keys map to **properties of the focused row**
(`s`=status, `a`=assign, `p`=priority), each opening a *picker* rather than mutating blindly, and each
generalising to the whole selection for free. In Vantage these generate from the panel:
`shortcut_fields = {"s": "status", "a": "assigned_to"}`.

APG grid pattern: **one tab stop** (Tab enters, Tab leaves, arrows navigate inside), roving tabindex
rather than `aria-activedescendant`, and with pagination set `aria-rowcount` to the total with
absolute `aria-rowindex` — otherwise screen readers announce "row 3 of 50" on page 7.

`Esc` always goes back one level. The value is the invariant, not the keystroke: users experimenting
with unknown single-key commands need one guaranteed-safe escape or they stop experimenting.

## 10. Three WCAG criteria apply simultaneously

- **2.1.4 Character Key Shortcuts (Level A).** A Linear-style keymap trips this. Comply by scoping
  `keydown` to the **grid container** (exemption 3), shipping an on/off preference (Gmail's model —
  which is why Gmail defaults shortcuts off), and making the keymap a declarative remappable table.
  A `document` listener that checks `activeElement.tagName !== 'INPUT'` is a fragile approximation,
  not a scoping mechanism.
- **2.5.7 Dragging Movements.** Drag-only column reorder is a **failure**. A keyboard path does not
  satisfy it — this is about pointer users without drag dexterity. Ship move up/down buttons in the
  columns panel.
- **2.4.11 Focus Not Obscured.** A sticky header hiding the focused row is a failure. Set
  `scroll-padding-block-start`.

## 11. Bulk actions

Transmit a **predicate plus an exclusion set**, never 40,000 ids:

```json
{"select_all": true, "filters": {...}, "excluded_ids": [12, 88, 341]}
```

Filament hydrates every selected model into memory and "solves" the scale problem by offering
`selectCurrentPageOnly()` — disabling the feature is not a solution.

Django's own `select_across` has shipped a bug (#33083) where it silently stayed 0 with
`actions_on_bottom=True`, applying a destructive action to page 1 only. **Silent scope reduction on a
destructive bulk action is the worst available failure mode** — test that path explicitly.

- Capture `selected_at`; skip rows modified since and report them as skipped. Never echo the
  selection-time count as the affected count.
- Past ~2s of work, enqueue and report `{processed, total, failed}`; chunk with per-chunk
  transactions so a failure at row 39,000 has a resume point.
- Partial failure is normal at scale: "38,000 succeeded, 12 failed" plus a downloadable error CSV.
- `queryset.update()` bypasses `save()`, signals and `auto_now`. Make this an explicit per-action
  `bulk_strategy = "sql" | "instances"` declaration, never a silent choice.
- Destructive bulk actions confirm with the count in the button label ("Delete 50 orders") even when
  the single-row version does not — fifty times the damage.
- Undo: execute-then-reverse, a **10s** toast (5s is too short to read and react), plus a durable
  trash view for the regret that surfaces ten minutes later. If undo is genuinely impossible
  (emails sent, payments captured), say so in the confirmation and show no undo toast.

## 12. Inline editing: stage, don't autosave

Airtable autosaves because the grid *is* the product. Retool and Django admin stage a changeset
because the grid is a view onto **someone else's production database**. Vantage is the second case.

A persistent "Save 12 changes / Discard" bar plus `beforeunload` protection. One transaction, one
permission check, one audit entry, atomic rollback, and a review step before touching production.

**Security warning worth heeding:** Filament's own docs admit `TextInputColumn` does not check model
policies before saving, and recommend full edit pages "where policy enforcement is essential". Any
inline write must run the **same** per-field permission check as the change form, server-side, every time.

Never inline-edit: fields with cross-field validation, fields needing context (rich text, uploads,
relations), fields with expensive side effects, or anything the audit trail treats as significant.

Concurrency: optimistic, `UPDATE ... WHERE pk=:pk AND updated_at=:expected`, 409 with server state,
and a field-level diff offering Keep mine / Use theirs / Merge.

## 13. Never blank the table on refetch

**The single highest-value item in the loading-state research.** A filter change is a refinement, not
a navigation. Keep the previous rows, dim them, swap when the new data arrives. Blanking destroys
spatial context and makes a 150ms operation feel like a page load.

Skeletons belong to the **cold load only** — and the most-cited study (Viget, n=136) found skeletons
performed *worse* than a plain spinner on every metric, so treat the question as low-stakes. Match
the skeleton to the page size (cap ~10 rows), use the real persisted column widths so CLS is zero,
don't skeleton the header, and gate any animation behind `prefers-reduced-motion`.

Timings: show no loader under ~200ms; once shown, hold ~300–500ms to avoid flicker; past 10s switch
to percent-done with a cancel.

## 14. Detail drawer

Soft navigation (row click) intercepts to a drawer and `pushState`s the record URL; hard navigation
(paste, refresh) renders the full page. In Django this is one server-side branch on `HX-Request`.

- Prev/next inside the drawer uses `replaceState`, not push — otherwise arrowing through 40 records
  leaves 40 history entries.
- **Filter/sort/page state must survive alongside the record id** (`/orders/123/?status=paid&page=3`).
  Losing it is the most common bug in this pattern.
- Non-modal drawers must **not** trap focus; modal ones must.
- Don't stack drawers. Replace content and push history; show a breadcrumb chain.
- Open mode is a panel-level default with a per-user override and a per-click modifier (Notion's model).

## 15. Empty states are five states, not one

Products conflate these constantly. Each needs different copy and a different action:

1. **No records exist** → "Create order". Never show "clear filters".
2. **Filters return zero** → echo the actual filter values back, offer to remove *the one* responsible.
   Never show a Create CTA — it implies the list is empty when it isn't.
3. **Failed to load** → error class and Retry, visually distinct from state 1.
4. **Permission denied** → say so. Showing zero results is a lie that generates support tickets.
5. **Loading** → never rendered as empty.

Plus a sixth: **partial failure** — the table rendered but the stat panel 500'd. Render the error in
that region only; don't destroy a working table because a widget failed.
