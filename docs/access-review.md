# Access review and elevated access

Two tools for the questions an auditor asks about an admin panel: *who can see and
change what*, and *why does anyone hold admin-wide rights all year*.

## Access review

**System → Access review** (also linked from **Roles**) lists, for every active staff
account or for every role:

- which registered models they may **view**, **add**, **change** and **delete**;
- for each field that is **sensitive** or that some role **restricts**: whether they
  can see it, change it and, for a sensitive field, reveal its value.

Superusers are flagged. Someone holding [elevated access](#elevated-access-break-glass)
right now is flagged with when it ends. A model a subject has no access to is left out.

Every answer comes from the panel's own permission resolver, through the same calls a
page makes, so a panel's `has_<verb>_permission` hooks, read-only mode, a custom
`VANTAGE["PERMISSIONS"]` class, authentication backends and field permissions are all
reflected. The report cannot describe a rule the panel does not apply.

**By role** reviews each role as a person holding only that role. A panel hook that
decides from something else about the user (an email domain, say) answers for that
stand-in, so the per-person report is the one to hand over as evidence.

### Downloading

The screen offers **Download CSV** and **Download JSON** for the current view (by person
or role, all models or one). Each download writes an `access-review.export` entry to the
audit log. CSV cells are protected against formula injection: a role named
`=HYPERLINK(...)` is written as `'=HYPERLINK(...)`.

The same report from a shell, for a scheduled export to an evidence store:

```bash
python manage.py vantage_access_review --format csv --output review.csv
python manage.py vantage_access_review --format json --by role
python manage.py vantage_access_review --model shop.order --model shop.customer
```

| Option | Default | |
|---|---|---|
| `--format` | `csv` | `csv` or `json` |
| `--output` | standard output | file to write |
| `--model` | every registered model | `app_label.model`; repeat for several |
| `--by` | `user` | `user` or `role` |

The JSON carries `generated_at` and `version` (the package version) beside the subjects.
CSV columns: `subject_type, subject, superuser, elevated_until, model, field, view, add,
change, delete, field_visible, field_editable, field_sensitive, field_reveal`. A row
with an empty `field` is the model itself. `field_reveal` is `per-record` when a panel's
`can_reveal` hook needs a record to answer.

### Who may open it

Superusers. `VANTAGE["ACCESS_REVIEW_PERMISSION"]` widens it to a permission string or a
callable taking the request. It is code-only: the report names everyone who holds the
keys to the panel.

### What it does not show

Row-level scoping (tenants, object permissions) decides *which* records someone sees,
not whether they see the model, and is not in the report. Neither are permissions your
own code checks outside the panel.

## Elevated access (break glass)

A role, or named permissions, held for a few minutes with a stated reason, instead of
standing admin rights.

### Asking and granting

**System → Elevated access** is open to every staff member:

- **Request** elevated access for yourself: a role and/or permissions
  (`shop.change_order`), a duration and a reason of at least ten characters. It waits
  until someone allowed to grant approves it.
- **Grant** it to someone directly, or **approve** a waiting request, if you may grant.
- **Revoke** an active grant, or **reject** a request. Anyone can end their own.

Who may grant is `VANTAGE["BREAK_GLASS_PERMISSION"]`: superusers by default, or a
permission string or callable. Code-only, like `ROLES_PERMISSION`. The roles screen's
rule applies: **a granter who is not a superuser can only grant permissions they hold
themselves**, counting their standing permissions only. Elevation never counts towards
granting more of it or towards editing roles.

Nobody approves their own request unless `VANTAGE["BREAK_GLASS_SELF_SERVICE"]` is
`True`. Then anyone allowed to grant may activate their own request at once, beyond
their standing permissions. That is the point of the switch, for an on-call rota whose
members are named by `BREAK_GLASS_PERMISSION`. The reason, the cap and the audit trail
bound it.

Every grant lasts at most `VANTAGE["BREAK_GLASS_MAX_MINUTES"]` (60 by default). Longer
requests are refused, and approval clamps to the cap in force at that moment. Grants
cannot go to superusers (they already hold everything) or disabled accounts.

### What it gives

While a grant is active, its holder has the role's permissions (and its votes in
[field permissions](field-permissions.md)) or the named permissions, everywhere the
panel checks: pages, actions, exports, the [JSON API](api.md), a background job's
re-check before it runs, and the reveal permission for sensitive fields. It never sets
`is_superuser`, and it only ever widens: a role whose field rules are stricter than
yours cannot hide what you could already see. [Tenant](tenancy.md) scoping still
applies in full.

**Nothing is written to group membership, and no job removes anything.** Whether a
grant is active is computed from the clock each time a permission is checked, and
cached for one request only. An expired or revoked grant gives nothing from the next
request on.

While elevated, a banner across every page names what is held, when it ends and how
many minutes are left, with a **Drop elevation** button.

### The audit trail

Each step writes an entry (`action_name`) to the [audit log](security.md):
`break-glass.requested`, `.granted`, `.activated`, `.used` (once per permission a grant
actually decided), `.expired`, `.revoked`, `.rejected`, `.dropped` and `.withdrawn`.
Activation and expiry are recorded by whichever check first observes them, exactly
once. Opening the Elevated access screen or running `vantage_break_glass list` records
expiries nobody has observed yet.

### From a shell

For an emergency in which nobody who could grant is reachable:

```bash
python manage.py vantage_break_glass grant ada --role "Order admins" \
    --minutes 30 --reason "Refunds stuck after the payment provider outage" --by root
python manage.py vantage_break_glass grant ada --perm shop.change_order --minutes 15 \
    --reason "Correct one mistyped order total"
python manage.py vantage_break_glass list          # active and pending; --all for every grant
python manage.py vantage_break_glass revoke 12 --reason "Incident closed"
```

Shell access is already beyond anything the panel restricts, so the command does not
check *who* may grant. The reason, the target and the duration cap are still enforced,
and every grant and revocation is audited (`--by` names the actor).

### Limits

- Code that calls `user.has_perm` directly, outside the permission layer, does not see
  an elevation. That includes your own views and a navigation item's own `permission`.
  The panel's resolver, a page's `permission` string and the reveal checks do.
- Django's own admin and other apps never see it: elevation is the panel's.
