# Platform decisions — frontend stack and Django internals

**Date:** 2026-08-13 · **Source:** two research passes, one empirical (Tailwind 4.3.3 measured locally,
browser-verified), one source-read (`django/django@main`, `VERSION = (6, 2, 0, "alpha", 0)`).

---

## Part A — Frontend

### A1. The Tailwind-without-Node risk is resolved

This was flagged in `PLAN.md` §6 as the biggest architectural risk: if a user writes a custom page
template with arbitrary Tailwind classes and has no build step, what makes those classes exist?

**Measured answer.** A curated `@source inline()` safelist covering everything a template author
realistically needs compiles to **616 KB raw / 62 KB gzip / 28 KB brotli** — roughly Bootstrap's
shipping weight, covering far more ground. For comparison, *every* nameable base utility (22,168
classes) is 186 KB gzip, and a full variant matrix runs 767 KB–2.4 MB gzip. **Variant multiplication,
not utility count, is the cost driver.**

**Three tiers, defaulting to tier 1:**

| Tier | What | Default |
|---|---|---|
| 1 | Prebuilt `vantage.css` in the wheel: scoped preflight, semantic component classes, `@theme static` for all tokens, curated safelist | **on** |
| 2 | `@tailwindcss/browser` (282 KB raw / 72 KB gzip, MIT, zero deps) vendored locally, gated behind `DEBUG` | opt-in |
| 3 | `manage.py vantage_tailwind` downloading the pinned standalone binary on demand | opt-in |

Tier 2 exists because **no safelist can cover arbitrary values** — `p-[13px]`, `grid-cols-17`,
`bg-[#316ff6]` are generated from the class string and are unbounded. The browser build closes that
gap completely; it was verified working against HTMX `innerHTML` swaps with ~6 ms rebuild latency,
resolving arbitrary values, logical properties and `rtl:` variants on injected content.

Tier 3 never vendors the binary: the standalone builds are 76–107 MB per platform, and PyPI's
per-file limit is 100 MB. Download on demand to a cache dir **outside site-packages**, pin the
version, verify against `sha256sums.txt`.

**The design pressure this creates: invest heavily in semantic component classes.** Every `.btn-primary`
we ship is a utility combination a user does not have to reach for, which is what keeps tier 1 sufficient.

### A2. Build configuration

```css
@import "tailwindcss" source(none);   /* never scan the user's project */
@source "../vantage/templates";
@import "tailwindcss/theme.css" theme(static);   /* emit all tokens, used or not */
```

`source(none)` is the single most important directive — without it the auto-scanner walks the whole
tree and the build depends on whatever else is in the repo. `theme(static)` matters because by
default only *used* variables are emitted, and tier 2/3 users will write utilities we never compiled.

**Determinism was verified empirically:** three consecutive builds produced identical SHA-256, and
randomly shuffling class order in the scanned source produced byte-identical output. Tailwind sorts
canonically. Remaining work is ours: pin `tailwindcss` exactly (patch releases add utilities), build
in CI, assert a checksum.

**Scope preflight.** It is an aggressive global reset (`margin: 0`, `border: 0 solid` on `*`). Import
`tailwindcss/preflight.css` inside a scoping layer rather than letting it hit the host project.

### A3. RTL

`rtl` and `ltr` are **built-in variants in v4** — no plugin, no `@custom-variant`. They match
`:where(:dir(rtl), [dir="rtl"], [dir="rtl"] *)`, so `<html dir="rtl">` is sufficient even on browsers
without `:dir()`.

**`space-x-*` and `divide-x-*` now emit logical properties — the long-standing v3 complaints are
fixed.** Do not carry v3-era workarounds forward.

**Banned in our own templates** (physical, will not flip): `ml-*`/`mr-*`/`pl-*`/`pr-*`,
`left-*`/`right-*`, `rounded-l-*`/`rounded-r-*`, `border-l-*`/`border-r-*`, `text-left`/`text-right`,
`float-left`/`float-right`, `origin-left`/`origin-right`, `bg-left`/`bg-right`, `scroll-ml-*`.
Use the logical equivalents. **Ship a CI lint that greps the template directory for these.**

**Irreducible cases needing hand-audit:** `translate-x-*` (no logical equivalent exists in CSS —
breaks sliding drawers and toggle knobs), `skew-x-*`/`rotate-*`, gradients (`bg-linear-to-r`), masks,
and icons. Chevrons and back arrows need deliberate `rtl:-scale-x-100`; icons with intrinsic meaning
(checkmarks, clocks, media play) must **not** mirror.

Adopt the v4.2 names now: `inset-s-*`/`inset-e-*` (`start-*`/`end-*` are deprecated).

---

## Part B — Django internals

### B1. Version floor corrected to Python ≥ 3.12

Django 5.2 is the **only** active LTS (security until 2028-04); 4.2 is EOL. Django 6.0's mainstream
support ended 2026-08-04; 6.1 is current. Python 3.12 is the intersection of 5.2 and 6.x support —
5.2 is the last series supporting 3.10/3.11.

`requires-python = ">=3.12"`, `Django>=5.2`. Test matrix `{5.2, 6.1, main} × {3.12, 3.13, 3.14}`.

**DEP 20** (accepted 2026-08-10): from January 2028 Django ships one feature release a year, named by
year, with three years of support. Django 6.2 is the last under the old policy. Keep the CI matrix
cheap to collapse.

### B2. `_meta` is mostly private API — build one facade

Only `get_field()`, `get_fields()` and `pk_fields` are public. `Options.fields`, `.concrete_fields`,
`.many_to_many` and `.related_objects` all carry the docstring *"Private API intended only to be used
by Django itself."* `contrib.admin` uses them freely because it ships in the same tarball; we cannot.

**All introspection goes through `vantage/introspection.py`.** Nothing else touches `_meta`.

Key facts the facade must encode:

- `isinstance(f, Field)` is the discriminator between real fields and `ForeignObjectRel`.
  `ForeignObjectRel` is not a `Field` subclass; `GenericForeignKey` is, but has `concrete = False`.
- **`ManyToOneRel.many_to_one == False` and `.one_to_many == True`.** The flags describe the relation
  *as traversed from the current model*, not the class name. Django's own source calls this
  "unfortunate". Getting it backwards silently produces `select_related` on a reverse FK.
- **`through._meta.auto_created` is the owning model class, not `True`.** Truthiness works; identity
  comparison does not.
- **`GenericForeignKey` has `many_to_one = True` but `related_model is None`.** Never assume
  `many_to_one → related_model is not None` — that single assumption is the most common crash in
  third-party admin replacements.
- `rel.accessor_name` is for `getattr`; `rel.name` is for `filter()`. They differ when
  `related_query_name` is set. `accessor_name` returns **`None`** for symmetrical self-M2M.
- **Never introspect reverse relations at import time or early in `ready()`** — `get_field()` raises
  if `apps.models_ready` is false. Do it lazily or in a system check.

### B3. Composite primary keys — a real differentiator

`contrib.admin` flatly refuses them (`sites.py`: `raise ImproperlyConfigured` on
`model._meta.is_composite_pk`). Supporting them is a citable reason for Vantage to exist. Requires
encoded tuple PKs in URLs, `Q`-object lookup rather than `pk=`, and an audit of every `pk__in=`.

### B4. Fetch modes (Django 6.1) — the most consequential recent ORM addition for us

An admin's core problem is that *user-supplied display callables* trigger relation access we cannot
statically predict. `fetch_mode()` addresses exactly that, and propagates to a whole tree of relations.

| Context | Mode |
|---|---|
| Production list view | `FETCH_PEERS` — caps an un-hinted callable at **one extra query instead of N** |
| `DEBUG` / tests | `FETCH_RAISE` — surfaces every un-hinted access loudly, so users fix their hints |
| Detail view | default `FETCH_ONE` — N=1 |

Version-gate on `django.VERSION >= (6, 1)`; do not raise the floor for it.

### B5. Formsets: two-tier inline strategy

`BaseModelFormSet.get_queryset()` **deliberately refuses to slice** — the source comment cites
ticket #13023: "max_num should not prevent existing related objects/inlines from being displayed."
A parent with 50,000 children renders 50,000 forms, and `_existing_object` materialises all of them
into a dict on POST. Prefixes are **index-positional**, not PK-keyed, so a lazy-loaded page 2 whose
ordering shifted writes to the wrong rows.

- **Tier 1 — inlines ≤ ~20 rows (the 90% case):** `inlineformset_factory` unchanged. Battle-tested for
  FK assignment, cross-set `unique_together`, deletion, ordering. Set `absolute_max` explicitly.
- **Tier 2 — paginated / lazy inlines:** drop `BaseFormSet` entirely. Row-oriented endpoints binding a
  **single `ModelForm`** per row (`ChildForm(data, instance=child)`). We still reuse all of
  `django.forms`; we only discard the management layer, which is the one part that doesn't fit.
  Carry `updated_at` in the row payload for optimistic concurrency — something formsets structurally
  cannot provide.

Reuse from `django.forms` without hesitation: `ModelForm`, `fields_for_model`, `modelform_factory`,
`construct_instance`, `BoundField`, `formfield_callback`. Reimplement: form *rendering*, and the
field→widget mapping (Django's defaults are 2005-era — `TextInput` for `EmailField`).

### B6. Permissions — three separated concerns

`ModelBackend._get_permissions` returns `set()` whenever `obj is not None`, so with the stock backend
`has_perm(perm, obj)` is **always False** and never falls back to the model-level check. Worse, the
permission cache is only populated when `obj is None` — so calling `has_perm(perm, obj)` per row on a
100-row list executes 100 queries under guardian. This is the #1 performance failure in
object-permission admin UIs.

Three methods, never conflated:

1. `has_model_perm(request, model, action)` — may they see this section?
2. `has_object_perm(request, model, action, obj)` — may they act on this row? **N=1 contexts only.**
3. `filter_queryset(request, qs, action)` — which rows may they see? **Must be bulk.**

Ship a guardian adapter using `get_objects_for_user(..., accept_global_perms=True)` — the only
scalable answer for list views. `rules` has no queryset story at all. Soft-detect with
`apps.is_installed()`, never `try: import`.

**Never use `ahas_perm`.** `_auser_has_perm` skips any backend lacking it, and neither guardian nor
rules implements it — async permission checks would silently deny.

### B7. Faceted counts — reconciling the two research passes

The list-page research called the disjunctive (self-excluding) rule non-negotiable; the internals
research showed AND-facets are 6× cheaper because all filters can share one base queryset and merge
into a single `aggregate()`. Both are right about different UI shapes:

- **Single-select filters → AND-facets.** Counts computed within the current selection, all filters
  folded into **one** `aggregate()` call. This is the default.
- **Multi-select checkbox facets → disjunctive, self-excluding**, one query per facet, because
  otherwise every unselected option reads `(0)` and the user cannot navigate. Opt-in per filter.

Django itself ships facets **off by default** (`ShowFacets.ALLOW`, opt-in via `?_facets`) and warns
that queries scale with filter count. Take the hint: gate on a cached `reltuples` estimate, above
which counts are skipped entirely.

Avoid Django's `SimpleListFilter.get_facet_counts` shape — it embeds `Q(pk__in=lookup_qs)` per choice,
which the planner sees as N subqueries. Pathological at 1M rows.

`Count(filter=Q(...))` is **portable**: Postgres/SQLite get native `FILTER (WHERE …)`, and Django
transparently rewrites to `CASE WHEN` on MySQL/Oracle/MSSQL. No backend-specific code needed.

### B8. The multi-annotation cartesian-product bug

Directly relevant to `list_aggregates`. Two `annotate(Count(...))` over different multi-valued
relations produce a cartesian product and **both counts are wrong** (Django ticket #10060, documented
in Django's own docs). `distinct=True` fixes `Count` only — `Sum`/`Avg`/`Max` over two multi-valued
joins cannot be fixed that way and will silently ship wrong numbers.

**Always generate a correlated subquery per aggregate.** Four mandatory steps, each failing
differently if omitted:

```python
Subquery(
    Child.objects.filter(parent=OuterRef("pk"))
        .order_by()          # strip Meta.ordering or it leaks into GROUP BY
        .values("parent")    # group by the outer ref
        .annotate(c=Count("*"))
        .values("c"),        # project exactly one column
    output_field=IntegerField(),
)
```

Wrap in `Coalesce(..., 0)` or rows with no children sort as NULL.

### B9. Counting at scale

`COUNT(*)` on Postgres is always a full relation scan (~50–200 ms at 1M rows, 0.5–2 s at 10M) and
dominates every other cost on a large list view. `contrib.admin` makes this worse: with
`show_full_result_count = True` (the default) it runs **two** counts, the second over the entire
unfiltered table, purely to render "1 result (12,345 total)". **Vantage ships no equivalent enabled
by default.**

Mitigations in implementation order: `reltuples` for the unfiltered estimate (O(1)); `EXPLAIN (FORMAT
JSON)` `Plan Rows` for a filtered estimate; a count-capping paginator (~30 lines, highest value);
keyset pagination above a row threshold; `SET LOCAL statement_timeout` so a pathological filter errors
instead of pinning a worker.

Do not adopt `Count(Window())` as a default — it saves a round trip but forces full materialisation.

### B10. Dependencies — confirmed: Django only

| Package | Verdict |
|---|---|
| **django-filter** 26.1 | Soft-detect and adapt. Healthy, but `FilterSet` is form-centric and has no facet-count hook — we'd rebuild §B7 on top of it anyway. |
| **django-tables2** 3.0.0 | Avoid. It's a server-rendered-table *framework*; every part of it is something we must own. 2.9.0 was yanked and re-released as 3.0.0 a week later. Study its `Accessor` design. |
| **django-import-export** 4.4.1 | Optional extra, at arm's length. Pins `diff-match-patch==20241021` — an exact `==` in a library's install_requires is a genuine packaging defect. No Django 6.x support yet. |
| **django-simple-history** 3.13 | Soft-detect only. It creates a model **in the user's app**, requiring a migration there and roughly doubling write volume. Unacceptable to require. Ship our own single generic-FK `VantageLogEntry` instead — "what someone did in the admin", not full row history. |
| **django-htmx** 1.29 | Reimplement (~50 lines of header reading). Not the 93 lines, the *architectural commitment*: depending on it says "this is an HTMX panel". Coexist gracefully if present. |
| **django-guardian / rules** | Soft-detect adapters. |

`dependencies = ["Django>=5.2"]` and nothing else. Every integration is
`vantage/contrib/<name>.py` guarded by `apps.is_installed()`.

### B11. Async: sync core, narrow async surface

**Transactions do not work in async mode** — `SynchronousOnlyOperation`. That alone disqualifies async
for every write path. **Relation access on a non-prefetched field raises** under async evaluation,
which is fatal for a list view rendering arbitrary user callables. `CONN_MAX_AGE` must be disabled
project-wide.

Core CRUD is sync. A narrow async surface is worth it for autocomplete, dashboard fan-out to external
APIs, and SSE job progress.

**Use `django.tasks` (Django 6.0+) for long-running work**, not async views and not a Celery
dependency — it gives a backend-agnostic enqueue API and ships `ImmediateBackend` so Vantage works
with no worker configured. Version-gate; fall back to synchronous with a warning on 5.2.

### B12. Patterns to copy verbatim from `contrib.admin`

- **`checks_class` attribute** delegating to a checks object — lets users subclass and extend
  validation for their own panel base class.
- **`default = True` on the autodiscovering AppConfig**, plus a `SimpleVantageConfig` without
  autodiscovery for users who register explicitly.
- **Function-local imports in the `register` decorator** to avoid the cycle with `sites.py`.
- **`WeakSet` of all sites** so test-created sites don't leak into the checks framework.
- Registry-walking check that skips un-customised panels and filters by `app_configs`.
