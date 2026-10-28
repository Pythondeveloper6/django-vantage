# Field permissions

[← Index](index.md)

Django's permissions stop at the model: someone who may change an order may change every
column of it. Field permissions add one more level, per [role](roles.md) and per field:

- **Hidden** — the field does not exist for that person. It is not a column, a filter, a
  search path or a sort key; it is not on the detail page, the form, the export or the
  JSON API; it is not in the history or the audit log they read.
- **Read-only** — they see it, everywhere they would see it anyway, and cannot change
  it. Forms render it as text, inline editing skips it, and every write path refuses it
  on the server.

No rule means full access, so nothing changes until you set one. Superusers are never
restricted.

## Setting a rule

### In the roles matrix

Open **System → Roles**, pick a role, and use the **Fields** column next to the model's
permissions. Each model this panel shows has a fold-out list of its fields, each with
*Full access*, *Read-only* or *Hidden*. A changed rule is counted in the toolbar and
marked like a ticked box, and **Review & save** lists it under *Field access* before
anything is written. Permissions and field rules are saved in one transaction, under the
same conflict check: if a colleague changed the role meanwhile, nothing is saved and the
page comes back with their version and your changes re-applied on top.

Rules are stored in the database, one row per role, model and field
(`vantage.FieldPermission`, keyed to `auth.Group`). Deleting a role deletes its rules;
duplicating a role copies them, because they are part of what the role grants.

### In code

One line on the panel:

```python
@register(Order)
class OrderPanel(ModelPanel):
    field_permissions = {"Support": {"total": "readonly", "cost": "hidden"}}
```

Keys are role (group) names. Levels are `"hidden"` and `"readonly"` (`"read-only"` is
accepted too). A role that does not exist is simply never matched. Code rules are added
to whatever the matrix stores; where both name the same field for the same role, the
stricter one applies. The matrix shows a code rule as a *read-only in code* or *hidden in
code* badge — the screen cannot lift it.

`manage.py check` reports a malformed declaration:

| Id | Problem |
|---|---|
| `vantage.E110` | `field_permissions` is not a dict |
| `vantage.E111` | an entry is not `role name → {field: level}` |
| `vantage.E112` | a name is not a field of the model |
| `vantage.E113` | a level is not `"hidden"` or `"readonly"` |
| `vantage.E114` | the rule restricts the primary key, which every link is built from |

## Several roles: most permissive wins

A person in two roles gets the more permissive answer, the same way Django combines
model permissions. If *Support* hides `total` and *Billing* does not, a member of both
sees `total`.

Two refinements stop that from becoming a loophole:

- **Only roles that grant something on the model have a say.** A *Newsletter* role with
  no order permissions says nothing about orders, so it cannot lift Support's rule on
  them. If no role grants the model at all (access came from a direct permission or a
  custom backend), the roles that carry a rule for it are the ones that count.
- **For writing, only roles that grant `add` or `change` on the model have a say.** A
  view-only role that leaves `status` alone shows `status`; it does not combine with a
  change role that made `status` read-only into "may change `status`".

Direct user permissions carry no field rules. The **effective permissions** page for a
person (from a role's *Members* tab) lists what their roles hide and make read-only, per
model, using the same resolution the pages use.

## Where it is enforced

| Surface | Hidden | Read-only |
|---|---|---|
| List columns, phone cards, `card_fields`, related previews | Not a column | Shown |
| Filters, search, `?sort=`, `ordering` | Not offered; refused if named | — |
| Board and calendar "group by", gallery, map | A view arranged by the field is not offered | A board on it cannot be dragged |
| Stat cards, charts, summary bar | A stat or chart that reads it is dropped | Shown |
| Detail page | Not shown | Shown |
| Create, edit and duplicate forms | Not on the page | Text, not an input; a forged value is ignored |
| Child inlines on the edit page | Not a column | Disabled; Django keeps the stored value |
| Inline cell editing | — | Not editable; a forged cell is dropped |
| Bulk edit | Not offered | Not offered; a forged tick writes nothing |
| CSV / TSV / JSON export | Not a column | Exported |
| JSON API | Left out of reads, rows and the schema; `?fields=` naming it is a 400 | Writing it is a 400; the schema marks it `readonly` |
| Import | Not a target | Not a target |
| History and restore | Not in the reconstruction or the timeline | Shown, never restorable, refused if posted |
| Audit log, the detail page's history panel | Left out of each diff | Shown |
| Autocomplete and global search | Not searched; a label that prints it becomes `<Model> #<pk>` | — |
| Reveal (sensitive fields) | Refused | — |
| Data quality | Findings about it are dropped | Shown |
| Webhook delivery log | Left out of the body as shown | Shown |

A create that would need a field the person cannot set — `NOT NULL`, no default — is
refused on the form (and with a 422 from the API) with a sentence saying which field,
rather than failing in the database.

Everything goes through one function, `vantage.fieldperms.access(request, model)`, cached
per request, which asks the permission resolver's `field_access(request, model)`. To
source the rules from somewhere else, override that method on your
[`PanelPermissions`](permissions.md) subclass and return a `FieldAccess(hidden=...,
readonly=...)`. `has_field_perm(request, model, name, "view" | "change")` is there for
your own code.

## Webhooks

Webhook payloads are **not** filtered by field permissions. A webhook acts for nobody: it
fires on everyone's changes, and its receiver is chosen by whoever manages webhooks
(superusers and holders of `vantage.manage_webhooks`). Payloads still leave out
[sensitive fields](sensitive-fields.md). Treat `manage_webhooks` as access to every field
of the models a hook subscribes to. The delivery log *page* is read by a person, so it
leaves out the fields hidden from that person.

## What it does not cover

- **Your own code.** A panel method column, a custom template, a `slot` contribution, a
  callable stat or a `save_model` hook that reads a hidden field shows what it reads.
- **`__str__`.** A record label that embeds a hidden value is replaced in autocomplete,
  global search and API records; elsewhere (page titles, foreign-key chips) the label is
  printed as the model returns it. Keep hidden values out of `__str__`.
- **Webhooks**, as above, and anything that reads the database without going through
  the panel: the shell, management commands, `dumpdata`, backups.
- **Superusers**, by design.
