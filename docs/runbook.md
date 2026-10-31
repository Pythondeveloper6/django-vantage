# Operations runbook

What to do when something is wrong with a deployed panel. Each entry is the shortest
safe path; the linked pages explain why.

## Monitoring

- **Metrics.** Set `VANTAGE["METRICS"] = True` and `VANTAGE["METRICS_TOKEN"]` (from your
  secret store), then scrape `<panel>/_/metrics/` with
  `Authorization: Bearer <token>`. Gauges come from the database, so any worker gives
  the same answer:

    | Metric | Alert when |
    |---|---|
    | `vantage_jobs{status="failed"}` | it increases |
    | `vantage_jobs{status="pending"}` | it stays above zero for longer than a job takes — no worker is running |
    | `vantage_webhook_deliveries{status="failed"}` | it increases — a receiver is down or rejecting signatures |
    | `vantage_webhook_deliveries{status="pending"}` | it grows — `vantage_webhooks` is not being run |
    | `vantage_report_runs{status="failed"}` | it increases — mail is failing |
    | `vantage_change_requests{status="pending"}` | it stays high — approvals are sitting unnoticed |
    | `rate(vantage_process_slow_requests_total[5m])` | sustained; `sum()` across processes |

- **Slow requests.** Add `vantage.metrics.SlowRequestMiddleware` to `MIDDLEWARE`; panel
  requests over `VANTAGE["SLOW_REQUEST_MS"]` (default 1000) are logged to
  `vantage.requests` with `vantage_path`, `vantage_view`, `vantage_ms` and
  `vantage_status` as structured fields.
- **Health page.** Superusers: **System** in the panel — migrations, cache, database,
  task backend.

## Incidents

### Something is changing data that should not be

1. Freeze writes for everyone, superusers included, without a deploy: **Settings →
   Read-only**, or `VANTAGE["READ_ONLY"] = "Frozen while we investigate."`. Reads keep
   working. See [Read-only mode](permissions.md#read-only-mode).
2. Read the [audit log](permissions.md#the-audit-log): filter by actor, model and time.
   Impersonation is recorded under the real person, never the impersonated one.
3. Revoke the account (`is_active = False`) or its roles on the [roles screen](roles.md).
4. Repair individual records from their [History](history.md) page ("restore as of"),
   which saves through the normal form and is itself audited.

### A credential or token was exposed

- Webhook signing secret: rotate it on the webhook's page; receivers must accept the new
  one. See [Webhooks](webhooks.md).
- `METRICS_TOKEN`: change it in the secret store and redeploy; the old one stops working
  immediately.
- A person's second factor: reset it ([Two-factor sign-in](two-factor.md#when-someone-loses-their-phone));
  on the server, `python manage.py vantage_two_factor_reset <username>`.
- Values in [sensitive fields](sensitive-fields.md) are never in the audit log, exports
  or webhook payloads; check who used **Reveal**, which is audited.

### Restoring after a bad deletion

1. Within `UNDO_WINDOW` seconds, the toast's **Undo** restores it.
2. A soft-deleted row is in the model's **Trash**; restore it there.
3. Otherwise restore from a database backup. Undo snapshots are not a backup: they are
   purged once expired (`python manage.py vantage_purge_undo`, safe to run any time).

### Background jobs or webhooks are stuck

- Jobs run on the configured task backend (Django Tasks, Celery or RQ); a growing
  `pending` count means no worker is consuming the queue named by `JOB_QUEUE`.
- Webhook retries are sent by `python manage.py vantage_webhooks`, normally from cron;
  run it by hand to drain the queue.

### After an upgrade

```bash
python manage.py vantage_upgrade_check --from <previous version>
python manage.py migrate vantage
python manage.py check --deploy
```

See [API stability and upgrades](api-stability.md). To roll back, deploy the previous
version and migrate `vantage` back to the migration that version shipped with before
starting it.
