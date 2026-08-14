# Multi-tenancy

[← Index](index.md)

One panel, many tenants, and a row that belongs to the wrong one is not merely a bug —
it is a customer reading another customer's data. `vantage.tenancy` exists because the
alternative — overriding `get_queryset` on every panel by hand — is one rule that has
to be remembered on every single model forever, and the one panel where somebody
forgets is the one that leaks.

Off by default. Setting `VANTAGE["TENANT_MODEL"]` is the only thing that turns any of
this on; every project that does not set it is unaffected by any of what follows.

## Turning it on

```python
# settings.py
VANTAGE = {"TENANT_MODEL": "accounts.Organisation"}

MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.tenancy.TenantMiddleware",
    ...,
]
```

That is enough, with the default resolver, for every registered model with a single
foreign key to `Organisation` to be scoped automatically:

```pycon
>>> Invoice.objects.count()
4000
>>> # request.user.tenant is the Acme organisation
>>> permissions.filter_queryset(request, Invoice.objects.all()).count()
37
```

No line of this appears in `InvoicePanel`. Scoping composes onto
[`filter_queryset`](permissions.md#why-filter_queryset-must-be-bulk) — the same layer
every list page, detail page, export, autocomplete and dashboard widget already calls —
so a model with a path to the tenant is scoped everywhere at once, the moment the
setting is on.

## Fails closed, and says so

The design decision that makes this usable rather than merely safe: **a model tenancy
cannot place is hidden, not exposed.** `TENANT_UNSCOPED = "deny"` (the default) means a
registered model with no path to the tenant and no exemption returns zero rows from
every page — which would be a confusing, silent bug in most systems, except that
`manage.py check` names every such model at startup as `vantage.E060`, so "confusing"
never actually happens. The alternative default — showing every row until someone
notices — is the one that leaks in production; failing closed fails in code review
instead.

```pycon
>>> manage.py check
ERRORS:
?: (vantage.E060) 'blog.Comment' has no path to the tenant model and is not exempt.
    HINT: Add 'blog.comment' to VANTAGE['TENANT_EXEMPT'], or set
    VANTAGE['TENANT_SCOPE'] = {'blog.comment': 'post__author__tenant'}.
```

Set `TENANT_UNSCOPED = "allow"` to downgrade the block to a warning
(`vantage.W060`) instead — every tenant sees every row of that model, which is the
right choice for a genuinely shared table you would rather not enumerate one at a time.

## Settings

| Key | Default | Effect |
|---|---|---|
| `TENANT_MODEL` | `None` | `"app_label.ModelName"`. Unset means tenancy is entirely off |
| `TENANT_RESOLVER` | `None` | Dotted path to a [`TenantResolver`](#choosing-a-resolver) subclass, or to any callable taking a request. `None` means [`UserAttributeResolver`](#choosing-a-resolver) |
| `TENANT_FIELD` | `"tenant"` | The attribute the default resolver reads off `request.user`, and the conventional column name Vantage looks for on a tenant-owned model |
| `TENANT_SCOPE` | `{}` | `{"app.model": "lookup__path"}` for a model inference cannot see a single direct FK on, or `{"app.model": None}` to mark a model deliberately global |
| `TENANT_EXEMPT` | `[]` | Models every tenant shares — reference data, mostly |
| `TENANT_UNSCOPED` | `"deny"` | `"deny"` (fail closed, `E060`) or `"allow"` (show every row, `W060`) for a model tenancy cannot place |

None of these six are editable from the panel's own UI, and that is deliberate: a row
scoping decision is a security boundary, and every layer that reads settings from a
database is one a signed-in user with the right permission could, in principle, edit —
tenancy answers this the same way [`PERMISSIONS`](settings.md#behaviour) does, by
staying Python-only.

## How a model gets scoped

`scope_for(model)` decides, in this order, cached per model label:

1. An explicit entry in `TENANT_SCOPE` — validated at startup, so a broken path is
   `vantage.E064`, not a `FieldError` at request time.
2. An explicit entry in `TENANT_EXEMPT` — global, no filtering.
3. Vantage's own framework apps (`vantage`, `contenttypes`, `sessions`, `admin`) —
   global, always, whether or not you listed them.
4. The tenant model itself — scoped by its own primary key.
5. **Exactly one** direct foreign key or one-to-one to the tenant model — scoped
   automatically. This is the only case inference guesses at, and only one hop: a
   `Comment` that reaches its tenant through `post__author__tenant` needs an explicit
   `TENANT_SCOPE` entry, because guessing across two hops is exactly the kind of
   silent wrong answer this module exists to refuse.
6. **Two or more** foreign keys to the tenant model, or none at all — unresolved.
   Which one is authoritative is not knowable from the schema alone, so this is where
   `E060`/`W060` fire rather than a guess being made.

## Choosing a resolver

A resolver answers one question — "which tenant is this request for?" — and nothing
else. It never sees a queryset.

**`UserAttributeResolver`** (the default). Reads `TENANT_FIELD` off `request.user` —
`request.user.tenant`, or `request.user.tenant_id` if the attribute itself is absent,
calling it if it turns out to be callable. It does **not** exempt superusers; if you
want a provider account that can see every tenant, subclass it:

```python
from vantage.tenancy import ALL_TENANTS, UserAttributeResolver


class SupportResolver(UserAttributeResolver):
    def resolve(self, request):
        if request.user.is_superuser:
            return ALL_TENANTS
        return super().resolve(request)
```

Returning `ALL_TENANTS` — a sentinel, not a real tenant — passes every queryset through
unfiltered for that one request. Returning `None` closes every scoped queryset, the
same as an unresolvable tenant.

**`SubdomainResolver`.** `acme.example.com` → the tenant whose `lookup` field (`slug`
by default) equals `acme`. Reads `request.get_host()`, which Django has already
validated against `ALLOWED_HOSTS`, never the raw `Host` header. A fixed set of
subdomains — `www`, `admin`, `app`, `api`, `static`, `media` — is ignored rather than
treated as a tenant slug.

```python
VANTAGE = {
    "TENANT_MODEL": "accounts.Organisation",
    "TENANT_RESOLVER": "vantage.tenancy.SubdomainResolver",
}
```

**`HeaderResolver`.** `X-Tenant: 42` → the tenant matched on `lookup` (`"pk"` by
default). Documented as unsafe on its own: a header a client can set is a header a
client can forge. Use it only behind a trusted proxy that sets and strips it before the
request reaches Django, and override `permitted(request, tenant)` — `True` for every
tenant by default — to restrict which callers may claim which tenant at all.

A resolver can also be a bare callable taking the request and returning a tenant, a
pk, `ALL_TENANTS` or `None` — the class-based resolvers above are the three
conventional shapes, not the only ones.

## The middleware

`vantage.tenancy.TenantMiddleware` is **required**, not optional, the moment
`TENANT_MODEL` is set — `manage.py check` fails with `vantage.E065` without it. It
publishes the current request onto a context variable for the duration of the request,
lazily: the resolver does not run until something actually asks for the tenant, so a
request that never touches a scoped model pays nothing.

The reason it has to exist at all, rather than resolving straight from
`request` wherever needed: several code paths never receive a request the ordinary
way — a foreign-key dropdown's `formfield_callback`, the filter rail's relation option
list, and the CSV importer's by-name lookups all run several calls away from the view.
Without the middleware, those degrade to "no tenant" and render empty — fail closed
again, but silently confusing rather than caught at startup, which is exactly why the
check exists.

Install it after `AuthenticationMiddleware`, because the default resolver needs
`request.user`.

## Writes

Reads are scoped through `filter_queryset`; writes go through a second, narrower check
so a create can never land in the wrong tenant and an edit can never move a row out of
its own:

- **On create**, the tenant foreign key is stamped automatically when the form left it
  unset — one line the developer does not have to write on every `save_model`.
- **On update**, changing a row's tenant field to a different tenant than the one it
  already belongs to is refused with a `403` (`TenantMismatch`), before the write
  reaches the database.
- **When no tenant can be determined at all** for a model that needs one — an
  unauthenticated request in a code path that should never reach this, say — the write
  is refused (`TenantRequired`, also `403`) rather than landing tenant-less.

A relation dropdown is scoped the same way: a `ModelChoiceField` pointing at another
tenant-owned model only ever offers rows from the current tenant, so a form cannot even
render an option that would fail the check above on save.

## Background jobs

A [background job](lists.md#bulk-actions) runs after the request that enqueued it has
finished, in a worker that may not share anything with that request — so the tenant is
captured at enqueue time and restored before the worker touches a single row. If the
resolved tenant has changed in between — the actor's own tenant assignment moved, say —
the job refuses with the same `TenantMismatch` a live request would raise, rather than
running against whichever tenant the worker happens to resolve to right now.

## Composing with the JSON API

Nothing in [`vantage.api`](api.md#multi-tenancy) mentions tenancy at all, and that is
the point: the API's list, retrieve, create and update endpoints all go through
`get_permissions().filter_queryset()`, the same call every HTML page makes, so a
tenant boundary that holds for the panel holds for a script hitting the API with the
same session cookie. A caller in one tenant reading another tenant's row by primary key
gets the same `404` a mis-scoped HTML page would give — never a `403` that would
confirm the row exists.

## What this does not do

- **It is not a database-per-tenant or schema-per-tenant story.** Every tenant's rows
  live in the same tables, distinguished by the scoping column. If you need physical
  isolation, this module is the wrong tool.
- **It does not validate that your `AUTHENTICATION_BACKENDS` or login flow assigns
  `request.user.tenant` correctly.** Resolution trusts whatever is on the user object;
  getting a user attached to the right tenant in the first place is your project's own
  concern.
- **It has no dedicated test module of its own** as of this writing — its coverage
  lives in `tests/test_api.py`'s `TestTenancyComposesAutomatically`, which proves the
  API composes with it end to end, rather than in a `test_tenancy.py` exercising the
  module's own surface directly.

## See also

- [How configuration resolves](configuration.md) — tenancy is Python-only, deliberately,
  and this page explains why some things stay that way
- [Permissions](permissions.md#why-filter_queryset-must-be-bulk) — the layer tenancy
  composes onto
- [The JSON API](api.md#multi-tenancy) — the same scoping, over HTTP
- [Settings](settings.md#multi-tenancy) — the six keys, in the full reference
