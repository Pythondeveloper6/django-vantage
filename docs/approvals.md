# Approvals

Some fields need a second pair of eyes. Name them on the panel:

```python
@register(Product)
class ProductPanel(ModelPanel):
    requires_approval = ["price", "status"]
```

That is the whole configuration. Anyone who may change a product still can. When
someone who **cannot approve** changes `price`, every other field they changed is saved
as usual, and the new price becomes a **change request**: who asked, when, the value
they saw, the value they want, and an optional note. The product keeps its old price
until someone who **can approve** says yes.

```
/admin/_/approvals/          the queue: waiting for you, your requests, recently decided
/admin/_/approvals/12/       one request: what it is now, what was asked, approve or reject
```

## Who may approve

Superusers, and anyone holding the model's approve permission:

```
shop.approve_product
```

It is named the way Django names the other four (`get_permission_codename("approve",
opts)`), and it is created for every panel that sets `requires_approval` when you run
`migrate` — no `Meta.permissions` and no migration in your app. It appears in that
model's row of the [roles matrix](roles.md), next to view, add, change and delete.

Approving writes the record, so it also needs **change** permission on it. A panel can
answer for itself, the same way it does for the other verbs:

```python
def has_approve_permission(self, request, obj=None):
    return request.user.groups.filter(name="Finance").exists()
```

Someone who may approve changes these fields directly: their edits are not held, the
same as before. Read-only mode (`VANTAGE["READ_ONLY"]`) refuses approving, rejecting and
withdrawing along with every other write.

**Nobody approves their own request — superusers included.** A superuser's own edits
are never held in the first place, so in practice this only stops someone who was
given the approve permission after asking. "I asked, and then I said yes" is exactly
what the feature exists to rule out.

## What the person editing sees

The edit page says which fields will wait before anything is typed, and offers a box
for a note to the approver. After saving, the message says what was saved and what was
sent for approval; if the only change was to a held field, it does not say "saved".

The record's detail and edit pages show a banner while a change to it is waiting: the
field, the value now, the value asked for, who asked and when. Anyone who can see the
record sees it. The **Review** link only appears for someone who could act on it.

## Approving, and when it is stale

The review screen shows each field's value when the request was made, the value asked
for, and — if it has moved since — what it is now.

**Approve** saves the change through the same path as an edit: the panel's form
validates it again, `save_model` runs, [tenancy](tenancy.md) is enforced, and the audit
entry and the `updated` [webhook](webhooks.md) happen then — at the moment the record
actually changes, with the approver as the actor.

Each request carries the value its author saw. If the field has changed since — someone
else edited it, or another request was approved first — the request is **stale**: it no
longer means what it said, so it is not applied. The page marks the field and shows its
current value. Reject it, and the author asks again from the record as it now is. A
request is never applied over a newer value.

If the requested value is no longer acceptable — a choice that has been removed, a
unique value taken since — nothing is saved and the request stays pending, to be
rejected.

**Reject** needs a reason, which the requester sees on the queue. **Withdraw** is the
requester taking their own request back. Each of the three is one conditional update,
so two people pressing at once produce one decision and one "already decided".

## Every way to change a record

| Path | What happens to a held field |
|---|---|
| Edit page | Held; the rest of the form saves |
| [History](history.md) restore | Held, like an edit; the message counts only what was restored |
| Inline cell editing | Held; the rest of the row saves, the cell shows the stored value and the bar says why |
| Kanban move | Held; the card goes back to its column with a note on it |
| [JSON API](api.md) `PUT`/`PATCH` | Held; answers **202** with the stored record and a `pending` object |
| [Import](lists.md) (update rows) | Held **per record**; counted as "sent for approval" in the preview and the result |
| Bulk edit | **Refused** when a held field is ticked, before anything is written |
| Built-in Publish/Unpublish, and any action whose `undo_fields` name a held field | **Not offered** to someone whose change would be held |
| Creating a record | Not held — there is no approved value to protect yet |

The bulk paths refuse rather than queue because a bulk edit can be one `UPDATE` over
forty thousand rows. That cannot be held back row by row, and forty thousand requests
would bury the queue the feature depends on. The message names the field and says to
change it one record at a time.

An import is different: every row already goes through its own form and its own
save, so each one queues like an edit would. A dry run shows how many would be sent
for approval and leaves nothing behind.

Over the API, a client can tell in advance: the [schema](api.md) marks each field with
`needs_approval` for the caller asking. A held write answers:

```json
HTTP/1.1 202 Accepted

{
  "pk": 42, "fields": {"price": "10.00", "...": "..."},
  "pending": {
    "id": 12,
    "status": "pending",
    "fields": ["price"],
    "requested": {"price": "12.50"},
    "detail": "Your change to Price was sent for approval. ..."
  }
}
```

Send `"note"` beside `"fields"` to leave one for the approver.

## The queue

The **Approvals** page is in the System group of the navigation for anyone who can view
a model that sets `requires_approval`. It has three parts:

- **Waiting for you** — pending requests you may decide, oldest first. The number
  beside the page in the navigation is how many there are.
- **Your requests** — what you asked for, and what became of it.
- **Recently decided** — for approvers, the last twenty decisions.

A request is exactly as visible as its record. Every lookup goes through the panel's
scoped queryset — `PanelPermissions.filter_queryset`, which is also where
[tenancy](tenancy.md) is applied — so a request about a record you cannot see is not in
your queue, not in your count, and answers 404 by its id. The one exception is your
own request whose record you can no longer reach: you can still read it and withdraw
it.

## Sensitive fields

A [sensitive field](sensitive-fields.md) can be held like any other, and it stays
masked on the queue, on the review screen, in the banner and in the API's `pending`
object — on both sides. An approver who may reveal values does so on the record itself,
where the reveal is logged.

The value asked for has to be stored to be applied, so it is kept on the request row,
as the record itself keeps the current one. The request table is not offered as a
model in the panel.

## What is audited

| When | Entry |
|---|---|
| A change is held | `action` — `requested approval of price (#12)` |
| It is approved | `update` — the real diff, `approved change to price (#12)`, by the approver |
| It is rejected | `action` — `rejected change to price (#12)` |
| It is withdrawn | `action` — `withdrew request to change price (#12)` |

Only the approval is an `update` with a diff, so the record's [history](history.md) and
its webhooks see the change once, when it happens. The other entries carry no values;
those live on the request, where the reason and the note are too. When an edit both
changes ordinary fields and holds one, its own `update` entry lists only the fields
that were saved.

## Checks

`manage.py check` names every entry that cannot be held, because at runtime such an
entry is skipped — and a field you meant to protect would then save directly:

| Id | Problem |
|---|---|
| `vantage.E105` | `requires_approval` is not a list of field names |
| `vantage.E106` | It names something that is not a field of the model |
| `vantage.E107` | It names a field the panel's form does not let anyone edit |
| `vantage.E108` | It names a many-to-many, a reverse relation or a file field |

Many-to-many values are written after the row and files are stored as they are saved,
so neither can wait for approval.

## Limits

Said plainly, because a control that looks stronger than it is does harm:

- **Only what goes through the panel.** A management command, a data migration, a
  `shell` session or your own view writing the model directly is not held. This is a
  review step in the admin, not a database constraint.
- **Your own actions.** A custom `@action` that writes a held field with
  `queryset.update()` is only kept from non-approvers when it lists that field in
  `undo_fields`. Otherwise, check `vantage.approvals.guarded_for(request, panel)` in it.
- **Creating** a record is not held; the add permission is the gate for that.
- **A deleted record** takes its pending requests out of everyone's queue but their
  author's, who can still withdraw them.
