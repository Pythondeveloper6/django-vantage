# Webhooks

A webhook sends a signed `POST` to a URL you choose when a record is created, changed or
deleted in the panel, and keeps a log of every delivery. Open **System → Webhooks**.

```python
VANTAGE = {
    "WEBHOOKS_TIMEOUT": 5,        # seconds the receiver gets, per socket operation
    "WEBHOOKS_MAX_ATTEMPTS": 6,   # the first try included
}
```

Nothing needs configuring to start: add a webhook on the page, pick the models and
events it should hear about (none ticked means every model), and save. The **signing
secret** is generated for you and shown once, straight after saving. Copy it then; the
edit form never prints it again, and "generate a new secret" replaces it.

Who can manage webhooks: superusers, and anyone holding the `vantage.manage_webhooks`
permission. A hook can send every change to every model to an address of the manager's
choosing, so it is a permission of its own rather than something implied by editing a
model. `VANTAGE["READ_ONLY"]` blocks changing webhooks (the log stays readable).
`VANTAGE["WEBHOOKS"] = False` removes the feature; stored webhooks are kept.

## What fires a webhook

The same moments the [audit log](permissions.md) records: a create, an edit or a delete
made **through the panel** — the form views, inline edits, the JSON API's delete and
moves on a board. A soft delete (trash) is a `deleted` event with `"soft_delete": true`.
An edit that changed nothing sends nothing.

Out of scope, deliberately: changes made by `manage.py shell`, signals, imports and
other code, and bulk-action runs (which the audit log also records as one entry, not one
per row). A webhook fires whether or not `AUDIT_LOG` is on. The package's own tables
never fire one.

## The request

```http
POST /your/endpoint HTTP/1.1
Content-Type: application/json
User-Agent: django-vantage-webhooks
X-Vantage-Event: updated
X-Vantage-Delivery: 5b0c5c4e-6a0e-4f43-9a52-6c0b2f1f4e7a
X-Vantage-Timestamp: 1767225600
X-Vantage-Signature: sha256=3f1c…
```

```json
{
  "id": "0a1d5f06-8a64-4d4b-9f7b-2d9f0f7c3b11",
  "event": "updated",
  "model": "shop.order",
  "object_id": "1042",
  "object_repr": "Order #1042",
  "actor": {"id": 3, "username": "ada"},
  "timestamp": "2026-01-01T12:00:00+00:00",
  "changes": {"status": {"from": "draft", "to": "paid"}}
}
```

* `event` is `created`, `updated` or `deleted` (and `test`, from the **Send test**
  button). `id` identifies the event and is the same on every retry and redelivery, so a
  receiver can de-duplicate on it. `X-Vantage-Delivery` identifies one delivery.
* `changes` is `{"field": {"from": …, "to": …}}` for an edit, `to` only differing from
  null for a create, and `{"field": {"from": …}}` for a delete — the same shape as the
  audit log, including its rule that values are shortened and files are sent as names.
* `actor` is the person signed in, never the person being impersonated.

### Sensitive fields are never sent

Fields that [`sensitive_fields`](sensitive-fields.md) names or infers (passwords, tokens,
card numbers, ...) are removed from `changes` altogether — not masked, and not listed as
"changed" — and `object_repr` is the same safe label the audit log uses. A model whose
`__str__` is the secret should still not be sent to a third party; keep a webhook off it.

### Verifying the signature

The signature is an HMAC-SHA256, keyed with the secret, over the timestamp, a dot, and
the **raw body**. Including the timestamp lets you reject a replayed capture:

```python
import hashlib, hmac, time

def verify(secret: str, headers, raw_body: bytes, tolerance=300) -> bool:
    stamp = headers["X-Vantage-Timestamp"]
    if abs(time.time() - int(stamp)) > tolerance:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), stamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, headers["X-Vantage-Signature"])
```

Verify against the bytes you received, not a re-serialised copy. Every attempt is signed
afresh, so a retry carries a new timestamp.

## Where the work runs

Delivery never happens inside your database transaction. Each matching webhook gets a
delivery row, and the send is queued from `on_commit` through the same backend detection
as [background jobs](lists.md#background-jobs): `django.tasks` with a real queue, Celery
with a broker, or RQ. A Celery worker needs `vantage.webhooks` in its `imports`
alongside `vantage.jobs`.

**With no queue configured** the delivery runs in-process, right after the commit, with
a timeout capped at 3 seconds. This is a fallback, not the design: the request that
changed the record waits for the receiver. The webhooks page says so while it is true.

## Retries

A delivery succeeds on any `2xx`. Otherwise:

* network errors, timeouts, `5xx`, `408`, `425` and `429` are **retried** after
  `WEBHOOKS_BACKOFF_SECONDS`, doubling each time (30s, 60s, 2m, ... at most a day), up to
  `WEBHOOKS_MAX_ATTEMPTS` attempts in all;
* other `4xx`, any `3xx` (redirects are not followed), and a refused address **fail at
  once**.

Retries are scheduled with the backend where it can delay a task (Celery, RQ with a
scheduler, a `django.tasks` backend that supports it). Where it cannot — and always for
the in-process fallback, which must not sleep inside a request — the delivery waits as
`retrying` with a next-attempt time, and this command sends it when due:

```console
$ python manage.py vantage_webhooks      # from cron, every minute
Tried 3 due deliveries.
Removed 120 old deliveries.
```

It also picks up a delivery that was never queued (the broker was down) and removes
finished deliveries older than `WEBHOOKS_RETENTION_DAYS`; `--no-sweep` skips that.

## The delivery log

Each row records the status, response code, attempt count, how long the last attempt
took, the next attempt time, the error, and the first 2,000 characters of the response.
Open one to see the exact body that was sent. **Send test** sends a `test` event now, in
the page's own request, once, and reports the answer (it works on a paused webhook).
**Redeliver** queues a copy of the original body as a new delivery, with a new
`X-Vantage-Delivery` and the same event `id`.

## Safety

The URL is typed into a form and the *server* connects to it, so:

* **Private addresses are refused by default**: loopback, private ranges, link-local
  (including the cloud metadata address `169.254.169.254`), carrier-grade NAT, multicast
  and reserved ranges, and IPv6 forms that embed them. The check runs **at send time**
  on every address the host name resolves to — one bad record refuses the lot — and the
  connection is made to the address that was checked, not to the name, so DNS cannot
  change its answer in between. `WEBHOOKS_ALLOW_PRIVATE_NETWORKS = True` lifts it, for a
  receiver on your own network. It is a code-only setting for the same reason.
* **Redirects are never followed**; the `3xx` is recorded as the answer.
* **Proxy environment variables are ignored**, since a proxy would be connected to
  instead of the address checked.
* Only `http` and `https`, no credentials in the URL, a timeout on every socket
  operation, and at most a few kilobytes of any response read.
* Prefer `https`. The secret signs the body but does not encrypt it.

## Known limits

* The secret is stored in the database in the clear, because an HMAC cannot be computed
  from a hash. Treat read access to the table like read access to the secret.
* Delivery order is not guaranteed, and a retry can arrive after a later event.
  Use `object_id` plus `timestamp` if order matters.
* Delivery is at least once. De-duplicate on the payload's `id`.
* A response is stored truncated and as text; binary answers show as replacement
  characters.
