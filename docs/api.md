# The JSON API

[← Index](index.md)

Anything the panel's UI can do to a record, a script can do too — through the exact
same permission and validation path, never a parallel one. `vantage.api` is a thin HTTP
skin over the code that already answers "which rows, which columns, is this allowed,
does this validate": [`vantage.permissions`](permissions.md), `vantage.listquery`,
`vantage.filters`, `vantage.pagination` and `vantage.serialize`. It adds no new
dependency — plain Django views returning `JsonResponse`, same as everything else in
this package.

Only models registered with the panel are reachable, and never a framework table
Vantage itself excludes — `auth.Permission`, sessions, and so on answer `404`, the same
guard [the autocomplete endpoint](panels.md) uses.

## Mounting

Already there. `path("admin/", site.urls)` mounts the whole panel, API included; there
is nothing extra to add to `urls.py`.

## Authentication

Session, exactly like every other page in the panel — the same cookie, the same
`request.user`, no separate login and no API keys. A script authenticates the way a
browser does:

```python
import requests

session = requests.Session()

# 1. Get a CSRF cookie and submit the login form, same as a browser would.
session.get("https://example.com/admin/accounts/login/")
session.post(
    "https://example.com/admin/accounts/login/",
    data={
        "username": "alice",
        "password": "hunter2",
        "csrfmiddlewaretoken": session.cookies["csrftoken"],
    },
    headers={"Referer": "https://example.com/admin/accounts/login/"},
)

# 2. Every further request rides the session cookie `requests` is already keeping.
session.headers["X-CSRFToken"] = session.cookies["csrftoken"]
response = session.get("https://example.com/admin/_/api/shop/order/")
```

A caller must be signed in **and** `is_staff` — the same bar every HTML page in the
panel sets. An anonymous request gets `401`; a signed-in non-staff account gets `403`.
Beyond that, every endpoint checks the model-level permission for the verb
(`view`/`add`/`change`/`delete`) through
[`get_permissions()`](permissions.md), object-level permissions on a single record, and
row scoping — including [multi-tenancy](#multi-tenancy), if it is on — on every list
and retrieve. Nothing here can reach a row the HTML pages could not.

## CSRF

**Never exempted.** `GET`/`HEAD` need no token; `POST`/`PUT`/`PATCH`/`DELETE` are
checked by `CsrfViewMiddleware` exactly like an HTML form post, and the token goes in
an `X-CSRFToken` header because a JSON body has no hidden input to carry it in. Every
authenticated response — reads included — refreshes the `csrftoken` cookie, so a script
that only ever talks to this API and never renders the login page's own form still has
something to send back on its first write.

Django's default CSRF failure is an HTML page — the one thing this whole module exists
to avoid handing a JSON client. Point the *whole project's* failure view at
`vantage.api.csrf_failure` to make that one response JSON too:

```python
# settings.py
CSRF_FAILURE_VIEW = "vantage.api.csrf_failure"
```

This is project-wide, not API-only — there is one `CSRF_FAILURE_VIEW` per Django
project — so only set it if you are comfortable with every CSRF rejection in the
project answering JSON, including one from a stray HTML form outside the panel.

## Errors

Every refusal is a `JsonResponse`, never an HTML page — a JSON client cannot read one.
The shapes:

| Status | When | Body |
|---|---|---|
| `400` | Malformed JSON, a body without `"fields"`, an unknown or read-only field name, a cursor that will not decode, a sort that cannot be cursor-paged | `{"detail": "..."}` |
| `401` | Not signed in | `{"detail": "Authentication required."}` |
| `403` | Signed in but not staff, or the model/object permission refuses this verb, or a CSRF check fails | `{"detail": "..."}` |
| `404` | The model is not registered, or the row does not exist *for this caller* — a row outside your scope answers 404, never 403, so it cannot confirm the row exists at all | `{"detail": "..."}` |
| `405` | The verb is not supported on this endpoint | `{"detail": "Method not allowed."}`, with an `Allow` header |
| `409` | The record changed since the `version` you read (see [Concurrency](#concurrency)), or deleting it would leave a `PROTECT`/`RESTRICT` relation dangling | see below |
| `422` | The submitted values do not validate | `{"detail": "Validation failed.", "errors": {"field": ["message", ...], "__all__": [...]}}` |

A `409` conflict carries the other side of the story:

```json
{
  "detail": "This record was changed by someone else since you read it.",
  "conflict": {
    "pk": 42,
    "version": "2026-08-14T09:03:11.482Z",
    "label": "Order #4821",
    "fields": {
      "status": {
        "mine": "paid", "mine_display": "Paid",
        "theirs": "refunded", "theirs_display": "Refunded",
        "conflict": true
      }
    }
  }
}
```

`errors` and `conflict.fields` are always keyed by field name, plus `__all__` for a
cross-field error — the same shape `vantage.concurrency.field_diff` and the change
form's own validation already use, so a client that renders one renders the other.

## Endpoints

| Method | Path | Does |
|---|---|---|
| `GET` | `/_/api/schema/` | Every model this caller may view — fields, kinds, choices, permissions |
| `GET` | `/_/api/<app_label>/<model_name>/` | List: filtered, searched, sorted, cursor-paged |
| `POST` | `/_/api/<app_label>/<model_name>/` | Create |
| `GET` | `/_/api/<app_label>/<model_name>/<pk>/` | Retrieve one record, in full |
| `PUT` | `/_/api/<app_label>/<model_name>/<pk>/` | Replace — every editable field is required |
| `PATCH` | `/_/api/<app_label>/<model_name>/<pk>/` | Update only the fields sent |
| `DELETE` | `/_/api/<app_label>/<model_name>/<pk>/` | Delete |

`<app_label>`/`<model_name>` are the same two words that make up a Django model label —
`shop.Order` is `shop`/`order`. Get the exact set of models, and their labels, from the
schema endpoint rather than guessing.

### Schema

```pycon
>>> GET /admin/_/api/schema/
{
  "models": {
    "shop.order": {
      "model": "shop.order",
      "label": "Order",
      "label_plural": "Orders",
      "actions": ["view", "change"],
      "list_url": "/admin/_/api/shop/order/",
      "list_display": ["number", "customer", "status", "total"],
      "list_filter": ["status", "created_at"],
      "list_search": ["number", "customer__name"],
      "fields": [
        {
          "name": "status",
          "label": "Status",
          "kind": "choice",
          "readonly": false,
          "required": true,
          "help_text": "",
          "widget": {
            "kind": "choice",
            "choices": [
              {"value": "pending", "label": "Pending"},
              {"value": "paid", "label": "Paid"}
            ],
            "...": "..."
          },
          "initial": "pending"
        }
      ]
    }
  }
}
```

Only models the caller may `view` appear at all — the same
[`visible_panels`](permissions.md) rule the sidebar and global search use. `actions` is
this caller's own allowed verbs on that model, so a client can grey out a "New" button
without a failed request first. A relation field's widget carries `options_url`: the
[autocomplete endpoint](panels.md), pre-filled with `?model=`, for looking up what a
foreign key or many-to-many may point at without loading every row.

### List

```
GET /_/api/shop/order/?status=paid&q=ACME&sort=-created_at&page_size=25&fields=number,customer,total
```

- **Filtering** — one query parameter per declared filter, exactly what the list page's
  URL carries; a filter with more than one value ORs them, several filters AND
  together. `GET` the endpoint with no filter params and read `filters` in the response
  to see what is available and how each is currently applied — the same `Filter`
  objects [Lists](lists.md#filtering) documents.
- **Search** — `?q=`, across the panel's `list_search` fields.
- **Sort** — `?sort=field` or `?sort=-field` for descending, restricted to whatever
  `?fields=` (or the panel's own columns, by default) named — a client can sort by
  anything it can read. A sort across a relation (`?sort=customer__name`) cannot be
  cursor-paged and is refused with `400` rather than silently falling back to an
  expensive `OFFSET`; choose a plain column instead.
- **Columns** — `?fields=a,b,c` picks which columns come back in each row's `cells`;
  omit it and the panel's own `list_display` is used. Asking for more columns costs
  more `SELECT`/prefetch joins and nothing else — see
  [Query cost](#query-cost-does-not-grow-with-row-count).

A row:

```json
{
  "pk": 4821,
  "display": "Order #4821",
  "version": "2026-08-14T09:03:11.482Z",
  "url": "/admin/_/api/shop/order/4821/",
  "html_url": "/admin/shop/order/4821/",
  "cells": {
    "number": {"value": "4821", "display": "4821"},
    "status": {"value": "paid", "display": "Paid"},
    "total": {"value": "129.00", "display": "$129.00"}
  }
}
```

Every value comes twice: `value` is what a client hands back on a write; `display` is
what a person reads, rendered by the same function the HTML table cell uses. `version`
is the [optimistic-lock token](#concurrency) — on every row, not only on retrieve, so a
client editing straight from a list never needs a round trip just to learn it.

### Pagination is cursor-only

`LIMIT 20 OFFSET 100000` makes the database produce and discard a hundred thousand rows
to hand back twenty. There is no page-number mode here to fall back to:

```json
{
  "results": [ ... ],
  "next": "eyJ2IjpbIjIwMjYtMDgtMTQiLCA0ODIxXSwgImQiOiAibmV4dCJ9",
  "previous": null,
  "query": "ACME",
  "sort": {"path": "created_at", "descending": true, "order_by": ["-created_at", "-pk"]},
  "filters": [ ... ]
}
```

Pass `next` back as `?cursor=` for the following page. There is no total count, and no
way to jump to "page 40" — a page is defined relative to the row before it, and a
cursor anchored to a row cannot skip or duplicate under concurrent writes the way an
`OFFSET` page can. `?page_size=` defaults to the panel's own page size and is clamped
to between 1 and 200 rather than rejected outright — a value below 1, above 200, or not
a number at all silently becomes the nearest valid size instead of a `400`, which is the
right trade for a parameter that only ever *shrinks or grows a response*, never changes
what it means. See [`vantage.pagination.KeysetPaginator`](../src/vantage/pagination.py)
for the mechanism.

### Retrieve

```pycon
>>> GET /admin/_/api/shop/order/4821/
{
  "pk": 4821,
  "display": "Order #4821",
  "version": "2026-08-14T09:03:11.482Z",
  "url": "/admin/_/api/shop/order/4821/",
  "html_url": "/admin/shop/order/4821/",
  "fields": {
    "status": {"label": "Status", "kind": "choice", "value": "paid", "display": "Paid"},
    "customer": {"label": "Customer", "kind": "relation_one", "value": 12, "display": "Acme Ltd"}
  }
}
```

A row you may not see does not exist: an out-of-scope or nonexistent primary key both
answer `404`, never `403` — the same rule
[`vantage.formviews.get_record`](../src/vantage/formviews.py) applies to the HTML
detail page, so this cannot leak that a record exists and is merely off-limits. A
primary key of the wrong *shape* — a letter where the model expects an integer, say —
answers `404` too rather than a `500`; the lookup is refused before it ever reaches the
database.

### Create, update, partial update

The body is always `{"fields": {...}}`. A relation is its primary key (or a list of
them for a many-to-many); a boolean is a real `true`/`false`; a JSON field is a real
object or array, not a string.

```pycon
>>> POST /admin/_/api/shop/order/
{"fields": {"number": "4900", "customer": 12, "status": "pending", "total": "42.00"}}

201 Created
{"pk": 4900, "display": "Order #4900", "version": null, "fields": {...}}
```

A field the caller leaves out on **create** falls back to the model's own default when
it has one — an HTML create page can never actually omit such a field either, because
its widget renders pre-filled with that default and an untouched control still submits
it. A field with no default and no `blank=True` is required and reports itself with
`422` like any other missing value.

`PUT` is a full replacement: every editable field must be present, and a client that
only wants to change one thing should use `PATCH` instead, which validates and writes
only the field names actually sent — the same narrowing `vantage.inline`'s row editor
uses for the same reason: a partial write must not demand a value for a field nobody
is touching.

A field that is **read-only** — an auto-generated primary key, an `auto_now`/
`auto_now_add` timestamp, anything the panel declared in `readonly_fields` — is not in
the form at all, on any of the three verbs. Sending one back is a `400`, not a value
that gets quietly dropped: hiding a control is a UI affordance, refusing the field
entirely is the security boundary, and it is the same boundary
[`vantage.formviews`](../src/vantage/formviews.py) documents for the change form.

### Delete

```
DELETE /admin/_/api/shop/order/4821/  →  204 No Content
```

If deleting would leave a `PROTECT`/`RESTRICT` relation dangling, nothing is deleted
and the response is `409` naming what is in the way — the same check the HTML confirm
page runs via `vantage.formviews.collect_cascade`.

## Concurrency

Every row carries `version` — an opaque token off the model's own `auto_now` timestamp
or an explicit integer counter column, whichever it has; see
[`vantage.concurrency`](../src/vantage/concurrency.py). Send it back on `PUT`/`PATCH`
to get a compare-and-set: the write only lands if nobody else's write landed first.

```json
{"fields": {"status": "paid"}, "version": "2026-08-14T09:03:11.482Z"}
```

Omit `version` and the write proceeds unguarded — last writer wins, exactly what the
HTML change form does today. Passing it is how a script opts into the same protection
[`vantage.inline`](../src/vantage/inline.py)'s row editor already gives the grid.

## Query cost does not grow with row count

The list endpoint derives `select_related`/`prefetch_related` from the columns actually
requested — the panel's own inferred joins, plus
[`vantage.optimizer.plan_related`](../src/vantage/optimizer.py) over whatever
`?fields=` asked for beyond them. Asking for two more relation columns over a
fifteen-row page costs the same number of queries as asking for none; it never costs
fifteen more. Paging five cursors deep costs the same as paging one deep. Both are
asserted in `tests/test_api.py` with `django.test.utils.CaptureQueriesContext`, and
mirror the guarantees `tests/test_scale.py` already holds the HTML list page to.

## Multi-tenancy

If [`VANTAGE["TENANT_MODEL"]`](permissions.md) is set, every endpoint here inherits row
scoping automatically: list, retrieve, create and update all go through
`get_permissions().filter_queryset()`, which `vantage.tenancy.enforce_on` wraps for
every configured resolver. Nothing in `vantage.api` mentions tenancy at all — that is
the point, and `tests/test_api.py::TestTenancyComposesAutomatically` proves it end to
end: a user of one tenant reading another tenant's row through the API gets the same
`404` a mis-scoped HTML page would give, not a leak.

## What this API does not do

- **No file or image uploads.** A JSON body cannot carry binary data. A model with a
  `FileField`/`ImageField` is still fully readable and its other fields are still
  writable through this API; use the HTML form for the upload itself.
- **No bulk actions, saved views or import/export.** Those are list-page features with
  their own request shapes; nothing here stops a future endpoint from exposing them
  the same way this one exposes create/read/update/delete.
