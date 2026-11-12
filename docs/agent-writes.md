# Agent writes: dry run, approval and undo

Provisional: the behaviour on this page may change before it is declared stable.

Four tools for letting an [agent identity](agents.md) write with a safety net. Dry run is
open to every caller of the [JSON API](api.md) who may write, not only agents.

## Dry run

Add `?dry_run=1` or the header `X-Vantage-Dry-Run: 1` to a create (`POST`), update
(`PUT`/`PATCH`), delete (`DELETE`) or bulk-action request. The answer is always HTTP 200
and nothing is written:

```
POST /admin/_/api/shop/order/?dry_run=1
{"fields": {"number": "A-100", "customer": 3, "total": "12.50"}}
```

```json
{
  "dry_run": true,
  "would": "create",
  "valid": true,
  "errors": {},
  "diff": [
    {"field": "number", "from": null, "to": "A-100"},
    {"field": "total", "from": null, "to": "12.50"}
  ],
  "cascade": {}
}
```

| Key | Meaning |
|---|---|
| `would` | `create`, `update`, `delete` or `run_action` |
| `valid` | `false` when the real call would fail validation (the real call answers `422`). `errors` then has the same shape as a `422` |
| `diff` | `{"field", "from", "to"}` per field that would change, against the stored row. A sensitive field is `{"field", "changed": true, "masked": true}` with no values |
| `cascade` | For `delete`: `total`, `groups` and `cleared` (counts per model, the preview the delete page shows), `blocked_by`, `unavailable`. A blocked delete is `valid: false` |
| `requires_approval` | For `update`: the changed fields that would wait for approval |

For a bulk action the response also carries `count` (rows it would touch), `scope`,
`requires_confirmation`, `max_rows`, `exceeds_max_rows`. `?dry_run=1` and the older
`"dry_run": true` body key both work.

A dry run runs the same permission, scope, field and form checks as the real call, so it
refuses in the same way (`403`, `404`, `400` for an unknown field, `409` for a stale
`version`). It never reveals a field the real call would hide. It does **not** spend the
run's write budget, but it **is** rate limited.

**What a dry run cannot predict.** It validates; it does not save and does not use a
rolled-back transaction, because that would fire signals, webhooks and emails. So it
cannot see: database constraints the form does not check (a unique index, a trigger), what
a `save_model` hook or a signal receiver does, the effect of a model's `save()`, or a
change someone else makes between the preview and the real call.

**Import is the one exception to "no rolled-back transaction" for people**: the CSV import
preview saves each row and rolls back. For an agent token it does not: an agent's import
preview is validation only (each row is matched and run through the form, nothing is
saved, no save hook or signal runs). It therefore cannot see two rows of the same file
colliding, or a database constraint the form does not know about.

## Approval for agents

When an identity has `require_approval`, its writes never take effect directly.

* `PATCH`/`PUT` of an existing record becomes a change request covering **every** submitted
  field, whatever the panel's `requires_approval` says. The answer is `202`:

```json
{"status": "pending_approval", "change_request": 12, "url": "/admin/approvals/12/",
 "fields": ["status"], "detail": "Your change was sent for approval. ..."}
```

  The record is not saved and no hook runs. The request is audited as the agent and run,
  and notified like any other. A person with the approve permission decides in the
  [approvals](approvals.md) queue; the applied change is audited as theirs, with
  "proposed by agent NAME" in the entry. A PUT cannot bypass this; fields that cannot wait
  (relations, files) are left out of a PUT and make a PATCH naming them a refusal. A change
  that equals the stored value writes nothing and asks for nothing (`200`).
* Create, delete, bulk actions, import with `dry_run=false`, and saved views are refused:

```json
{"detail": "This identity requires approval, and this operation cannot be approved yet. ...",
 "code": "approval_not_supported"}
```

  with status `403`. Fail closed: only an update is routed through approval. Dry runs of all
  of them still work. An agent token can never approve or undo: approval pages are HTML
  routes, which refuse tokens.

## Undo by run

Everything an agent does is tagged with its run. To revert a run:

```
python manage.py vantage_agent undo-run order-sync nightly-2025-01-31 --dry-run
python manage.py vantage_agent undo-run order-sync nightly-2025-01-31
```

or, signed in with a session (an agent token gets `403`):

```
POST /admin/_/api/_agents/runs/nightly-2025-01-31/undo/        (add ?dry_run=1 to preview)
```

(`?agent=NAME` when two agents used the same key.) Only a superuser or the identity's owner
may undo; for anyone else a run does not exist (`404`, the same answer as for a key that was
never used), and the route is `404` for everyone while `AGENTS` is off. Every record is checked against that person's own change or delete
permission. The command acts as the owner unless `--as` names a superuser or the owner.

The report is `{"reverted": [...], "skipped": [{"object", "label", "reason"}],
"conflicts": [{"object", "label", "reason"}]}`:

| The run did | Undo does |
|---|---|
| Created a record | Deletes it, if it still holds the values the run left and nothing else depends on it |
| Updated a record | Restores the changed fields to their earlier values through the panel's form; if any field cannot be restored (relation, file, JSON, sensitive) the record is skipped whole |
| Deleted a record | Skipped: the audit log cannot rebuild it faithfully |
| Ran a bulk action | Skipped: the log keeps no per-row values |

A record changed by someone else after the run, or whose values are not what the run left
(a write the audit log did not see), is a **conflict** and is left alone. Sensitive values
never appear in the report. The undo is audited as the person, with `undo of run KEY` as the
entry name; running it twice is safe. Pending change requests the run made are not withdrawn.

## Destructive actions

A bulk action that deletes needs the identity's `allow_delete`. The built-in
`delete_selected` is recognised, as is any action declared `@action(destructive=True)` or
with `permission="delete"`. `destructive` already confirms before running; it now also means
"an agent needs delete rights" (provisional). Rows deleted this way count in the run's
`deletes`.
