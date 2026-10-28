# History

Every record has a **History** page, linked from the history panel on its detail page
("As it was…"). It lists everything the [audit log](permissions.md#the-audit-log) holds
for the record, and shows the record **as it was at any point** in that list — field by
field, next to how it is now. From there you can **restore** some or all of those
fields.

```
/admin/shop/order/42/history/                       the timeline
/admin/shop/order/42/history/?entry=318             as it was just after entry 318
/admin/shop/order/42/history/?at=2026-03-01T09:30   as it was at that moment
```

There is nothing to configure: every panel gets it while the audit log is on.

```python
@register(Payout)
class PayoutPanel(ModelPanel):
    history = "view"        # show the history, but never offer to restore from it

@register(Invoice)
class InvoicePanel(ModelPanel):
    history = False         # no history page for this model
```

## How a value is worked out

Each change the log records carries the value it replaced and the value it wrote.
Starting from the record as it stands now, the page walks those changes backwards to
the point you chose.

That only tells the truth about changes made **through the panel**. The audit log is
not row history: a script, an import, a data migration or a `shell` session writes
nothing to it. So a value is only shown as known when the log is consistent with
itself — every change's "from" matches the previous change's "to", and the last "to"
matches what the record holds today. If something the panel never saw moved the field
in between, the chain breaks, and the page says the value **cannot be worked out**
instead of showing what the surviving half of the chain suggests.

Every field comes back with one of these verdicts, written out on the page:

| Verdict | Shown | Restorable |
|---|---|---|
| Known, same as now | Yes | Nothing to restore |
| Known, different from now | Yes | Yes |
| Recorded only approximately (a JSON value, a many-to-many, a text clipped at 500 characters) | Yes, with the reason | No |
| Not confirmed, because a bulk change ran since (see below) | Yes, with the reason | No |
| Cannot be worked out (changed outside the panel) | No | No |
| A foreign key or a file | No | No |
| A [sensitive field](sensitive-fields.md) | Masked | No |

## What it cannot reconstruct

Being clear about this is the feature, so here it is in full.

* **Anything before the record's history starts.** If the record existed before the
  audit log was switched on — or before it was first edited through the panel — the
  page says where its history begins and shows nothing earlier. Before a record's
  *create* entry it says the record did not exist yet.
* **Fields that are not inputs on the edit form.** The log only records what the form
  wrote, so an `auto_now` timestamp, a non-editable column, and anything in `exclude` or
  `readonly_fields` has no history at all. They are listed under the table rather than
  shown with today's value as if it were the old one. If you have changed a panel's
  form fields over time, the history follows what each edit's form contained.
* **Foreign keys.** The log records a relation by its label, not by which record it
  pointed to, so there is nothing that could be written back.
* **Files and binary values.** A file is recorded by name only, never its contents; a
  binary column is never recorded.
* **JSON, many-to-many and very long text.** Recorded approximately — list order is not
  kept, a long text is clipped — so they are shown but never restored.
* **Sensitive fields.** Never shown, then or now, and never restored. The diff never
  carried their values in the first place, and an entry written before the field was
  declared sensitive is masked too.
* **Bulk actions.** A bulk action, a one-statement bulk edit and an undo are logged once
  per run, not once per record, so the record's own history cannot say whether one of
  them touched it. When one ran after the point you chose, the page says so, and any
  field it could have touched is shown as *not confirmed* unless an entry at or before
  that point confirms it.
* **Deep history.** The page replays at most the record's 2,000 most recent entries.

## Restoring

Tick the fields you want back and press **Restore selected fields**. A restore is an
ordinary edit:

* It needs the `change` permission on the record, rechecked when you press the button,
  and it is refused in [read-only mode](settings.md).
* It saves through the panel's own edit form — the same validation, the same
  `save_model` hook, the same [tenancy](tenancy.md) guard. If an old value is no longer
  accepted (a choice has been removed, a unique value has since been taken by another
  record), nothing is saved and the page says why.
* It is refused if the record has changed since you opened the page: by the
  [optimistic lock](lists.md) on a model that has one, and otherwise by comparing the
  current values the page was drawn with. A colleague's newer change is never rolled
  back by accident.
* It writes its own [audit entry](permissions.md#the-audit-log) — an update with the
  usual diff, marked "restored as of" the point you chose. That entry is itself part of
  the history, so a restore can be looked back past, and restored from, like any other
  change.

The values themselves never travel through the browser. The page posts back only
*which* fields to restore and the point to restore them from; the server works the
values out again from the log, so a hand-written request cannot restore a field to
anything the history does not say it held.

## Who can see it

Two permissions, because the page answers two questions:

* **What the record held** needs `view` on that record, checked the way every record
  page checks it — a record outside your scope is a 404.
* **Who changed it** is the audit log's own question, so the page also needs what the
  log needs: a superuser, or the `vantage.view_vantagelogentry` permission. Without it
  the detail page shows no history panel and the history page is refused.

Restoring additionally needs `change`, and `history = "view"` turns restoring off for
everyone.

## When you need row history

The log is "what people did in the panel". If you need every write from every source —
workers, imports, the shell — install
[django-simple-history](https://django-simple-history.readthedocs.io/); its timeline
renders next to Vantage's own on the detail page.
