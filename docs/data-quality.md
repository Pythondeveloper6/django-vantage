# Data quality

A read-only health check of the *rows* in every registered model, at
`/_/data-quality/` under the System group, and as `manage.py vantage_data_quality`.

`System` answers "is the deployment sound?". This answers the question that comes a
month later: is the data any good?

## What it checks

| Check | Finds | Links to the list page? |
|---|---|---|
| `nulls` | Share of rows where a nullable or blank field is empty | Yes, for nullable non-text fields the list filters on |
| `duplicates` | Fields named like identifiers (`email`, `slug`, `sku`, `code`, `username`...) that are not unique-constrained but hold repeated values (case-insensitive) | No |
| `orphans` | Foreign keys with `db_constraint=False` and generic foreign keys that point at a missing row or a missing content type | No |
| `choices` | Values outside the field's declared `choices` | No |
| `dates` | Created-like timestamps in the future (more than a day of clock skew) and any date before 1900 | Yes, as a date window |
| `empty` | A model with no rows | n/a |
| `stale` | A last-modified field (`auto_now`, `updated_at`, `modified`...) older than `STALE_DAYS` | Yes, as a date window |

A link is only offered when the list page can express exactly the offending rows: the
panel must filter on that field (`list_filter`, or inferred) and the real filter must
accept the value. Otherwise the finding has no link rather than a wrong one.

The overview lists every model with its row count, a 0-100 score and its worst issue;
click a model for field-level findings and how each check went. The score is 100 minus
25 per failure (an orphaned reference) and 8 per warning, per finding rather than per
row. Notes (a nullable field that is mostly filled, an empty table) cost nothing.

## It never shows a value

A finding is a count, a field name and a link. No cell from any row is ever read into
the report, so the page cannot become a way around field masking. That holds for the
page, the cache, the JSON output and the command.

## It is bounded

* Above `SAMPLE_SIZE` rows, checks read the **newest rows by primary key** (one index
  lookup finds the cutoff). The report says `sampled`, and findings say "in sample".
* Above `SKIP_ABOVE` *estimated* rows (PostgreSQL planner estimate; reuses the list
  page's `~` counts) the data checks are skipped and the row says `skipped`.
* Each check has `CHECK_TIME_BUDGET` seconds, enforced between queries and, on
  PostgreSQL, as a `statement_timeout`. A check that runs out keeps what it found and
  is marked `timeout`.
* A render spends at most `PAGE_TIME_BUDGET` seconds auditing. Models not reached show
  `not yet run`; the **Re-run** button continues where it stopped.
* Results are cached (`CACHE_SECONDS`). The key hashes the scoped SQL and the
  thresholds, so a different user, tenant or setting never sees someone else's result.

Background jobs were considered and not used: `vantage.jobs` is built around a stored
action and selection, and an audit has neither. For a very large database run the
management command from cron or CI; it has no page budget and warms the same cache the
page reads.

## Access

Superusers by default. Widen it with a permission string or a callable:

```python
VANTAGE = {
    "DATA_QUALITY": {
        "PERMISSION": "myapp.view_data_quality",   # or lambda request: ...
    },
}
```

Whoever gets in still only sees models the permission resolver lets them *view*, through
the same queryset as the list page: the panel's `get_queryset`, the resolver's
`filter_queryset` and tenant scoping. A tenant-scoped user audits their tenant's rows.

## Settings

All under `VANTAGE["DATA_QUALITY"]`; an unknown key is an error with a suggestion.

| Key | Default | Effect |
|---|---|---|
| `ENABLED` | `True` | `False` hides the page and disables the command |
| `PERMISSION` | `None` | `None` is superusers; a permission string or `callable(request)` |
| `CHECKS_EXCLUDE` | `[]` | Check names skipped everywhere |
| `SAMPLE_SIZE` | `50000` | Rows read per model before sampling |
| `SKIP_ABOVE` | `5000000` | Estimated rows above which data checks are skipped |
| `CHECK_TIME_BUDGET` | `3.0` | Seconds per check per model |
| `PAGE_TIME_BUDGET` | `12.0` | Seconds a page render may spend auditing |
| `CACHE_SECONDS` | `3600` | How long a result is cached |
| `NULL_WARN_RATE` | `0.5` | Empty share above which a field is a warning |
| `STALE_DAYS` | `365` | Days without a change before a table is stale |
| `DUPLICATE_FIELDS` | `[]` | Extra field names (`"external_id"` or `"app.model.field"`) to treat as should-be-unique |

## Opting a model out

One line on the panel:

```python
class AuditLogPanel(ModelPanel):
    data_quality = False                 # leave it out entirely
    # data_quality = ["nulls", "stale"]  # or skip just these checks
```

An unknown check name is `vantage.E063` in `manage.py check`.

## Adding a check

```python
from vantage.dataquality import Check, register_check

@register_check
class InvoicesHaveLines(Check):
    name = "invoice-lines"
    label = "Invoices without lines"

    def applies(self, panel):
        return panel.model._meta.label == "billing.Invoice"

    def run(self, ctx):
        # ctx.scan is the bounded queryset; ctx.tick() raises when the budget is spent.
        ctx.tick()
        count = ctx.scan.filter(lines__isnull=True).count()
        if count:
            yield ctx.finding(self.name, "warn", f"{count} invoices have no lines",
                              count=count, scanned=ctx.scanned)
```

Yield counts and field names only, never row values. `ctx.filter_url(path, value)`
returns a pre-filtered list link when the filter system can express it, else `None`.

## The command

```
manage.py vantage_data_quality [app.Model ...] [--format text|json]
                               [--fail-on warn|fail] [--no-cache] [--user NAME]
```

Runs as the first active superuser unless `--user` is given. `--fail-on` exits 1 when
any finding is at least that severe, for CI. Results are cached like the page's.
