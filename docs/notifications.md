# Notifications

Slack, Microsoft Teams and email for the events people otherwise miss: an approval
request nobody opened the panel to see, a webhook that has run out of retries, a
background job that failed.

```python
VANTAGE = {
    "NOTIFICATIONS": {
        "ops": {
            "type": "slack",                       # or "teams"
            "url": os.environ["SLACK_WEBHOOK_URL"],
            "events": ["webhook.failed", "job.failed"],
        },
        "approvers": {
            "type": "email",
            "to": "approvers",                     # or a list of addresses
            "events": ["approval.requested"],
        },
    },
}
```

| Event | Sent when |
|---|---|
| `approval.requested` | Someone's edit was held for [approval](approvals.md) |
| `approval.approved`, `approval.rejected`, `approval.withdrawn` | The request was decided |
| `webhook.failed` | A [webhook](webhooks.md) delivery ran out of retries (test sends excluded) |
| `job.failed` | A [background job](deployment.md#large-bulk-actions-become-background-jobs) failed |

Channel keys: `type` (`slack`, `teams`, `email`), `events` (a non-empty list), `url`
(chat; an `https://` incoming-webhook URL — Teams accepts a Workflows or connector URL
that takes `{"text": ...}`), `to` (email; a list of addresses, or `"approvers"` for every
active user holding the model's `approve_<model>` permission, directly or through a role,
plus superusers). Anything else is rejected when the settings load.

## What a message contains

The event, the model, the record's label, the *number* of fields involved, and a link to
the record. Never a field value or field name: a chat channel's members may not be
allowed to see either. A webhook failure names the hook and the error; a job failure its
summary.

## Safety

- **Code-only.** Webhook URLs are credentials; keep them in your secret store.
- **Same network guard as webhooks.** Chat posts go through the webhook client, so
  private, loopback and link-local addresses are refused unless
  `WEBHOOKS_ALLOW_PRIVATE_NETWORKS` is on, redirects are not followed and a post is cut
  off after 3 seconds.
- **After the commit, best effort.** Messages are sent once the database transaction
  commits, and a failing channel is logged to `vantage.notifications` instead of
  failing the save that caused it.
- **Global, like webhooks.** Channels receive events for every tenant. With
  `TENANT_MODEL` set, `"to": "approvers"` is refused (it would mail one tenant's record
  label to another tenant's approvers); list addresses explicitly instead.
