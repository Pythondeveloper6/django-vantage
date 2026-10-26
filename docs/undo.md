# Undo

After a delete or an edit, the confirmation toast carries an **Undo** button:

> Deleted 3 orders. (3 selected records) · **Undo**

It stays for `VANTAGE["UNDO_WINDOW"]` seconds (10 by default), and pressing it — or
<kbd>Ctrl</kbd>/<kbd>⌘</kbd>+<kbd>Z</kbd> while you are not typing in a field — puts the
records back. There is nothing to configure: every panel gets it.

```python
VANTAGE = {
    "UNDO_WINDOW": 10,      # seconds; 0 turns undo off everywhere
    "UNDO_MAX_ROWS": 1000,  # larger changes still happen, without an Undo
}

@register(Payout)
class PayoutPanel(ModelPanel):
    undo = False            # this model's deletes and edits are final
```

The feature exists to stop data being lost, so it is built around one rule: **an undo
that might restore the wrong thing is never offered**. In those cases the change happens
exactly as it always did and the toast simply has no button. Everything below is what
that rule means in practice.

## What can be undone

| Change | How it is reversed |
|---|---|
| Delete (record page) | Soft delete: un-trashed. Hard delete: snapshot and restore |
| Delete (bulk, `actions = ["delete_selected"]`) | The same, for every selected row |
| Bulk edit | Each row's previous values are written back |
| Inline cell edit | The same, and the grid updates in place without a reload |
| Publish / Unpublish (built-in) | The flag goes back to what it was, row by row |
| Your own `@action` | Only if it declares how — see [below](#undo-for-your-own-actions) |

`delete_selected` is the bulk counterpart of the Delete page, and opt-in:

```python
class OrderPanel(ModelPanel):
    actions = ["mark_paid", "delete_selected"]
```

On a model with a trash it trashes, exactly like the per-record button; inside the
trash it deletes for good. It needs the `delete` permission, confirms first, and one
`PROTECT` reference anywhere in the selection leaves every row as it was.

## Edits: only if nobody has touched it since

An edit records each row's values before and after, read back from the database inside
the transaction that wrote them. Undo writes the "before" back **only if every row still
holds exactly the "after"** — checked under a row lock. If a colleague has changed one of
those fields since, nothing is reverted at all, and the toast says so:

> 1 record has been changed by someone else since, so nothing was undone. Their change has
> been kept.

All or nothing, deliberately. Reverting nine rows and skipping the tenth leaves a state
nobody chose; reverting the tenth would destroy someone else's work. The undo also
advances the [optimistic-lock](lists.md) version column, so an edit form somebody has
open on the old values conflicts rather than quietly writing them back.

An edit is not offered for undo when it touches a field whose value cannot be compared
and restored faithfully — a file, a JSON field, a binary field or a many-to-many.

## Soft deletes: already reversible

When the model has a trash (a `deleted_at` timestamp or an `is_deleted` flag — see
[Inference](inference.md)), Delete marks the row and Undo unmarks it, on the condition
that the mark is still the one this delete wrote.

## Hard deletes: snapshot and restore

The delete runs immediately, as it always has, inside one transaction that first records
every row the cascade is about to remove — and the old value of every foreign key that
`SET_NULL` or `SET_DEFAULT` will rewrite. Undo re-inserts those rows with their original
primary keys, many-to-many rows included, and points those foreign keys back.

### Why not "deferred delete"?

The other common design keeps the rows, hides them, and deletes them when the window
closes. It was rejected:

* **Hiding only works inside the admin.** Your site, your API, your workers and your
  reports would keep reading — and writing — records the user was told were gone.
* **Somebody has to run the delete later.** With several worker processes and no
  guaranteed scheduler, a deletion can run late or never. For a record deleted for
  privacy reasons, *never* is the failure that matters.
* **The delete would run against data that has moved.** A new `PROTECT` reference makes
  it fail after the user was told it succeeded; a new `CASCADE` child is destroyed without
  ever having been shown on the confirmation page.

### When a hard delete has no Undo

Snapshot and restore has risks of its own. Each is a case where the delete proceeds
normally and no Undo is offered:

| Condition | Why |
|---|---|
| A `pre_delete`/`post_delete` receiver on any model in the cascade | It may have deleted a file, revoked a key or called an external service. Re-inserting the row cannot reverse that, and would resurrect a record pointing at things that no longer exist |
| A database-level `on_delete` (`DB_CASCADE`, `DB_SET_NULL`…) touching the cascade | The database rewrites rows the snapshot never saw |
| The model overrides `delete()`, or its default queryset overrides `QuerySet.delete()` | Your own delete has to run, and it cannot be snapshotted from outside |
| More than `UNDO_MAX_ROWS` rows, cascade and rewritten references included | The snapshot itself would be the problem |
| A binary column in the cascade | Not snapshotted |

Restoring is just as careful. Rows are inserted with `force_insert`, so a primary key or
a unique value somebody has taken in the meantime is an error, never an overwrite; a
`SET_NULL` reference somebody has since pointed elsewhere is a conflict; foreign-key
integrity is checked before commit. Any failure rolls the whole restore back and says
that nothing was changed.

Restored rows are saved with `raw=True`, exactly as `loaddata` saves fixtures, so
`pre_save`/`post_save` receivers see them — check `raw` if a receiver should ignore them.

## The token

Each Undo carries a random token that is:

* **single-use** — consumed by a conditional `UPDATE` before anything is restored, so a
  double click undoes once and both clicks get the same answer;
* **bound to the user and to the browser session** that made the change;
* **short-lived** — it expires with the window, and the server enforces that whatever the
  page shows;
* **as privileged as the change** — undoing a delete needs `delete`, undoing an edit needs
  `change`, re-checked at the moment of the undo, and refused in read-only mode;
* **redeemed only by a CSRF-protected POST** to `<site>/_/undo/`;
* **audit-logged** — a successful undo writes "undid delete of 3 orders" to the
  [audit log](permissions.md).

Only a hash of the token is stored. Entries live in the `vantage_undoentry` table;
expired ones are purged whenever a new one is written, each user keeps at most twenty,
and a redeemed entry drops its snapshot immediately. A hard delete's snapshot is a copy
of deleted data, so if you need a firm bound on how long it can outlive its window, run
`manage.py vantage_purge_undo` from cron.

## Undo for your own actions

An action is arbitrary code, so its inverse is never guessed. Say which fields it writes
and Vantage records and restores them, with the same conflict check as an edit:

```python
@action("Mark as paid", bulk_strategy="sql", undo_fields=["status"])
def mark_paid(self, request, queryset):
    return f"{queryset.update(status='paid')} orders marked paid."
```

For anything `undo_fields` cannot express, return the state you will need and name a
hook. The hook runs inside a transaction; raise `UndoConflict` to refuse, and everything
it did is rolled back:

```python
from vantage.actions import ActionResult
from vantage.undo import UndoConflict

@action("Refund", undo="undo_refund")
def refund(self, request, queryset):
    ids = list(queryset.values_list("pk", flat=True))
    queryset.update(status="refunded")
    return ActionResult(f"Refunded {len(ids)}.", undo_state={"ids": ids})

def undo_refund(self, request, state):
    rows = Order.objects.filter(pk__in=state["ids"], status="refunded")
    if rows.count() != len(state["ids"]):
        raise UndoConflict("Some of these orders have moved on since the refund.")
    rows.update(status="paid")
```

Actions large enough to run as a [background job](lists.md#background-jobs) never offer
undo: there is no toast to put the button in.

## Accessibility and no-JavaScript

The toast region is a `role="status"` live region, so the message and its button are
announced. The Undo button is an ordinary focusable `<button>`, it declares its shortcut
with `aria-keyshortcuts`, and the shortcut follows the same on/off switch as the grid's
keyboard shortcuts. The toast does not steal focus. Its entrance animation is dropped
under `prefers-reduced-motion`.

Without JavaScript the button is never shown, and every change behaves exactly as it did
before undo existed: a button that could outlive its own window would be a promise the
page cannot keep.
