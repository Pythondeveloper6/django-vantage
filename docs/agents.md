# Agents

An agent identity lets a script or an automated tool call the [JSON API](api.md) with a
bearer token instead of a person's session. It is off unless `VANTAGE["AGENTS"]` is
`True`.

## What an identity is

An identity is a **service user** plus rules that narrow it. The service user is an
ordinary account with an ordinary [role](roles.md), so every permission, field rule,
sensitive-field mask and tenancy scope applies to the agent exactly as it would to a
person. The identity's own rules can only remove access, never add it.

An agent can never run as a superuser. Creating an identity for a superuser is refused,
and a token whose service user has since become a superuser stops working.

| Rule | Effect |
|---|---|
| `access` | `read` blocks every write. `write` allows writes the service user may make |
| `allowed_models` | The models the agent can see at all. Any other model answers 404, as if it did not exist, and is absent from the schema and OpenAPI document. Empty means every model the service user may see |
| `allow_delete` | Off by default; without it a `DELETE` is refused |
| `rate_limit_per_minute` | Requests per minute. Over it, `429` with `Retry-After`. `0` means no limit |
| `max_writes_per_run` | Write requests allowed in one run. `0` means no limit |
| `expires_at`, `revoked_at` | A token past either stops working at once |
| `owner` | The person the agent acts for; shown in the audit log |

## Creating one

```
python manage.py vantage_agent create --name order-sync --user svc-order-sync \
    --owner alice --access write --models shop.order shop.product \
    --expires-in 90 --rate 30 --max-writes 200
```

If `--user` does not exist it is created with an unusable password, `is_staff` on and
`is_superuser` off. An existing account is accepted only if it also has no usable
password: a person who can sign in with a password cannot be made into a service user (the
token would carry their rights, and the account could still be signed in to). Give that user a role (a group) that grants only what the agent
needs. `list`, `revoke NAME` and `rotate NAME` manage identities afterwards.

The command prints the token once:

```
vta_Ab3dEf9h_<43 characters>
```

Only a SHA-256 hash of the secret part is stored, so the token cannot be shown again.
Lose it and you rotate: `rotate` issues a new token and the old one stops working
immediately. Keep the token in a secret store, never in a repository.

Serve the site over HTTPS. With agents on and no redirect or proxy header configured,
`manage.py check --deploy` warns with `vantage.W116`.

## Calling the API

```
curl -H "Authorization: Bearer $VANTAGE_TOKEN" \
     -H "X-Vantage-Run: nightly-2025-01-31" \
     https://panel.example.com/admin/_/api/shop/order/
```

```python
import requests

session = requests.Session()
session.headers["Authorization"] = f"Bearer {token}"
session.headers["X-Vantage-Run"] = "nightly-2025-01-31"

base = "https://panel.example.com/admin/_/api"
orders = session.get(f"{base}/shop/order/", params={"page_size": 50}).json()
session.patch(
    f"{base}/shop/order/17/",
    json={"fields": {"status": "shipped"}},
)
```

A token request needs no CSRF token. If the request also carries a session cookie, the
token still wins (the cookie is ignored for who you are) but CSRF is enforced, so send
tokens without cookies.

`X-Vantage-Run` groups requests into a run. Without it each request is its own run. The
key is stored as sent only if it is 1 to 64 characters from `A-Z a-z 0-9 . _ : @ + = -`;
any other value is replaced by `h-` and a digest of itself, so the same header still names
the same run.

**The write budget is per run, and the caller chooses the run.** An agent that sends a
new `X-Vantage-Run` (or none) starts a run with a fresh budget, so `max_writes_per_run` is a
tripwire for a well-behaved script, not a hard limit against a hostile one. The ceiling
that holds whatever the run key is the rate limit: keep `rate_limit_per_minute` set (it is
`60` by default; `0` means none) and use `access = read`, `require_approval` and a narrow
role for anything you do not trust. The rate limit is counted in the cache, so with several
processes use a shared cache backend.

A bad, unknown, expired or revoked token is a `401`; it never falls back to a session.

## What an agent can never do

A token works on the JSON API only. Every HTML page refuses it with a `403`: the roles
matrix, user and password management, the account and two-factor pages, impersonation,
break-glass, webhooks, reports and settings. The service user is also never a superuser.

**Tenancy.** The tenant is resolved from the service user (the default resolver) after the
token is accepted; a tenant resolved earlier from a session cookie sent alongside is
discarded. A `HeaderResolver` reads a header the caller controls, so with agents on make
sure the proxy in front sets and strips it, or override `permitted()`.

## The audit trail

Every write an agent makes is logged with the agent, the run, and the service user. The
entry reads "Agent order-sync (for alice)", so the log says who it was done for.
Deleting an identity keeps its entries.

Counting: a write request spends one unit of the run's budget as soon as it passes the
permission checks, whether or not it later validates. Bulk actions count as one write.

See also: [Agent writes: dry run, approval and undo](agent-writes.md).
