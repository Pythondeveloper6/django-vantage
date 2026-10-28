# Roadmap

What we plan to build next. Nothing here has shipped yet, and the order may change.
Ideas and use cases are welcome in the
[issue tracker](https://github.com/Pythondeveloper6/django-vantage/issues).

## Records

**History with point-in-time restore.** The [audit log](permissions.md#the-audit-log)
already keeps a diff of every change. A record's history will let you see it exactly as
it was on a given date, and restore some or all of its fields from there.

**Approval workflows.** Mark fields that need a second pair of eyes:

```python
requires_approval = ["price", "status"]
```

A change to one of them waits for someone with permission to approve it before it is
saved, using the same review step the roles matrix and inline editing already have.

## Access and security

**Two-factor sign-in.** Authenticator-app codes (TOTP), which you can require for
superusers or for chosen [roles](roles.md).

**Field-level permissions.** "Support can see `price` but not change it", set in the
roles matrix next to the model permissions, and enforced on every page, export and the
JSON API, the same way [sensitive fields](sensitive-fields.md) are.

## Getting data out

**Scheduled reports.** Subscribe to a [saved view](lists.md#saved-views) and get it as a
CSV by email every day, week or month. Runs as a
[background job](lists.md#background-jobs).

**Webhooks.** Send a POST to a URL you choose when a record is created, changed or
deleted, with a delivery log and automatic retries.

## Everywhere

**A layout for phones.** Lists become cards on small screens, and bulk actions move to a
bottom sheet, so the panel is usable away from a desk.
