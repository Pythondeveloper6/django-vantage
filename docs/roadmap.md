# Roadmap

Everything that was on this page is built and waiting for the next release. Each item
links to its documentation, and the [changelog](https://github.com/Pythondeveloper6/django-vantage/blob/main/CHANGELOG.md)
lists the details and the known limitations. Ideas and use cases for what comes after
are welcome in the
[issue tracker](https://github.com/Pythondeveloper6/django-vantage/issues).

## In the next release

### Records

**[History with point-in-time restore](history.md).** See a record exactly as it was on
a given date, rebuilt from the [audit log](permissions.md#the-audit-log), and restore
some or all of its fields from there.

**[Approval workflows](approvals.md).** Mark fields that need a second pair of eyes:

```python
requires_approval = ["price", "status"]
```

A change to one of them waits for someone with permission to approve it before it is
saved.

### Access and security

**[Two-factor sign-in](two-factor.md).** Authenticator-app codes (TOTP), which you can
require for superusers or for chosen [roles](roles.md).

**[Field-level permissions](field-permissions.md).** "Support can see `price` but not
change it", set in the roles matrix next to the model permissions, and enforced on every
page, export and the JSON API, the same way [sensitive fields](sensitive-fields.md) are.

### Getting data out

**[Scheduled reports](reports.md).** Subscribe to a [saved view](lists.md#saved-views)
and get it as a CSV by email every day, week or month.

**[Webhooks](webhooks.md).** Send a signed POST to a URL you choose when a record is
created, changed or deleted, with a delivery log and automatic retries.

### Everywhere

**[A layout for phones](phones.md).** Lists become cards on small screens, and bulk
actions move to a bottom sheet, so the panel is usable away from a desk.
