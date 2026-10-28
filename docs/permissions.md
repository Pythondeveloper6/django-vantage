# Permissions

[← Index](index.md)

Every access decision in the panel goes through one object. There is no second place
where a view decides for itself whether you may do something, and that is the point: a
guard in the views has to be remembered on every new write path, and the one that gets
forgotten is the one that matters.

## The three layers

Three concerns that look alike and must not be conflated.

| Layer | Question | Called | Method |
|---|---|---|---|
| **Model** | May this user see this section at all? | once per page | `has_model_perm(request, model, action)` |
| **Object** | May this user act on *this* row? | only where N is 1 | `has_object_perm(request, model, action, obj)` |
| **Queryset** | Which rows may this user see? | once per query | `filter_queryset(request, queryset, action)` |

Keeping them apart is not tidiness. It is the difference between a list page that runs
two queries and one that runs a hundred.

The four actions are `view`, `add`, `change` and `delete`, mapped to Django's codenames
through `get_permission_codename` — never an f-string on `model_name`, because the
helper respects proxy models and a custom `Meta.model_name`.

```pycon
>>> PanelPermissions().perm_name(Order, "change")
'shop.change_order'
```

## Why `filter_queryset` must be bulk

This is the single most important thing on the page.

Django's `ModelBackend` returns an **empty permission set whenever `obj is not None`**,
*and* only populates its permission cache when `obj is None`. So calling
`has_perm(perm, obj)` once per row on a hundred-row page executes a hundred uncached
queries under django-guardian. That is the most common performance failure in
object-permission admin UIs.

So: **override `filter_queryset`, never a loop over `has_object_perm`.**

```python
from vantage.permissions import PanelPermissions


class OwnerScopedPermissions(PanelPermissions):
    def filter_queryset(self, request, queryset, action="view"):
        if hasattr(queryset.model, "owner"):
            return queryset.filter(owner=request.user)
        return queryset
```

```pycon
>>> Order.objects.count()
200
>>> permissions.filter_queryset(request, Order.objects.all()).count()
37
```

> If what you actually want is **multi-tenancy** — every model scoped to the signed-in
> user's organisation, with fail-closed checks that name every model tenancy cannot
> place — do not hand-roll it the way the example above does. `hasattr(model,
> "tenant")` guesses at which field is the tenant column and says nothing when it
> guesses wrong; [`vantage.tenancy`](tenancy.md) is the real subsystem this package
> ships for exactly that job, with resolvers for the common ways a tenant is
> identified (a user attribute, a subdomain, a trusted header), and it composes onto
> this same `filter_queryset` seam automatically once `VANTAGE["TENANT_MODEL"]` is set.
> The pattern above is still the right shape for row scoping that is not
> tenancy — "only your own orders", say.

The base implementation does no row filtering at all: model-level permissions have
already gated entry, which is exactly Django's own model. Because it is a no-op by
default, the call costs nothing until a project overrides it — which is why every call
site can make it unconditionally, and does:

- the list page, before filters and search;
- the detail, edit and delete pages, before the object is fetched;
- the exporter and the importer;
- global search and the relation autocomplete;
- dashboard widgets, so a number computed over rows you cannot list is not a leak;
- saved views, on every request, which is why a shared view can never widen access.

### Alternatively, `get_queryset`

For scoping that belongs to one model rather than to the whole panel, override
`get_queryset` on the panel:

```python
@register(Invoice)
class InvoicePanel(ModelPanel):
    def get_queryset(self, request):
        return super().get_queryset(request).filter(owner=request.user)
```

`get_queryset` is the base for **every** view of that model. `filter_queryset` is the
policy applied to every model. Use whichever matches the shape of your rule; they
compose.

## The resolution order

`has_model_perm` answers in this order, and the order is deliberate:

1. **Not authenticated** → no.
2. **[Read-only mode](#read-only-mode) and this is a write** → no. Before the superuser
   bypass, because read-only is an operational choice about the deployment, not about
   who is asking.
3. **The panel's own `has_<action>_permission` hook**, if it has one. Returning `None`
   means "no opinion".
4. **Superuser** → yes.
5. `user.has_perm("app.action_model")`.

`has_object_perm` runs the same sequence with the object passed through, and then
short-circuits to the model-level answer if **no configured backend can actually answer
an object-level question**. `ModelBackend` demonstrably cannot; if it is the only
backend, object checks are dead weight and walking the chain per row is pointless.

```pycon
>>> PanelPermissions().supports_object_permissions
False                     # only ModelBackend is installed
```

## Per-panel hooks

Define any of these on a `ModelPanel` and it is consulted **before the superuser
bypass**:

```python
@register(Invoice)
class InvoicePanel(ModelPanel):
    def has_delete_permission(self, request, obj=None):
        return False
```

```pycon
>>> permissions.has_model_perm(request_as_superuser, Invoice, "delete")
False
>>> permissions.has_model_perm(request_as_superuser, Order, "delete")   # no hook
True
```

A panel that says nobody may delete these rows means **nobody**. Adopting an
`admin.py` into Vantage must not quietly widen a refusal to every superuser.

The signature is `(request, obj=None)` for all four verbs, including `add` —
`contrib.admin` omits `obj` on that one alone, and one uniform signature is worth more
than bug-compatibility with an inconsistency. The
[`contrib.admin` adapter](extending.md) bridges the difference so an adopted
`has_add_permission(self, request)` still works.

Do **not** put row scoping in these hooks. They are the N-is-1 layer.

## Superusers

`superuser_bypass = True` by default. Django's own admin does the same, and a panel that
can lock out the superuser is a support burden rather than a security feature.

Two things sit above the bypass and are not negotiable by it: read-only mode, and a
panel hook that refuses.

One thing sits *outside* it entirely: **editing somebody else's saved view**. Being able
to rebrand the site is not a reason to be able to rewrite a colleague's private view,
and Save-as is one click away, so a bypass there would buy nothing and cost the only
guarantee the feature makes.

Set `superuser_bypass = False` on your resolver subclass if you need superusers held to
the same permission checks as everyone else.

## Swapping the resolver

```python
# settings.py
VANTAGE = {
    "PERMISSIONS": "myproject.permissions.TenantPermissions",
}
```

A dotted path to a `PanelPermissions` subclass. It is instantiated once and cached; the
cache is dropped when the setting changes, which is what makes `override_settings` work
in tests.

## Third-party backends

Adopted rather than reimplemented. django-guardian and django-rules both work by being
in `AUTHENTICATION_BACKENDS`, so `has_perm` already routes to them and both the model
and object layers pick them up with no configuration.

Only the **bulk** path needs an adapter, because `rules` has no queryset story at all
and guardian's lives in a shortcut function.

### django-guardian

```python
VANTAGE = {
    "PERMISSIONS": "vantage.permissions.GuardianPermissions",
}
```

That is the only supported bulk adapter, because guardian is the only one of the two
with a bulk API. It narrows every queryset with `get_objects_for_user`, and:

- `accept_global_perms=True` — a model-level grant should still mean "all rows", or
  granting someone blanket access would paradoxically show them nothing;
- `with_superuser=True`;
- an anonymous or unauthenticated request gets an empty queryset.

It is **not** auto-enabled when guardian is installed. Whether object permissions should
narrow the admin is a policy decision, not a consequence of having the package on the
path.

### django-rules

Add `rules.permissions.ObjectPermissionBackend` to `AUTHENTICATION_BACKENDS` and your
predicates apply at the model and object layers automatically —
`supports_object_permissions` becomes true, and per-row checks start routing to your
rules.

`rules` has no way to express "which rows", so it cannot scope a list page on its own.
Write the queryset half yourself:

```python
class RulesPermissions(PanelPermissions):
    def filter_queryset(self, request, queryset, action="view"):
        if queryset.model is Project:
            return queryset.filter(members=request.user)
        return queryset
```

## Read-only mode

```python
VANTAGE = {"READ_ONLY": True}
```

or, better, with a reason:

```python
VANTAGE = {"READ_ONLY": "Frozen during the migration window."}
```

A string doubles as the explanation shown to the user, which beats a bare "permission
denied" for something that is a deliberate operational choice.

**It is enforced in the permission layer, not in the views.** Denying the permission
means every existing check — buttons, actions, forms (including
[editable inlines](forms.md#editable-inlines), which submit as part of the parent
form), [inline cell editing](lists.md#inline-cell-editing), the importer,
[duplicate](forms.md#duplicating-a-record) and [bulk edit](forms.md#bulk-edit) —
refuses at once, and a new write path inherits the refusal without anyone thinking
about it.

It refuses exactly `add`, `change` and `delete`. `view` is conspicuously absent.

```pycon
>>> read_only_reason()
'Frozen during the migration window.'
>>> permissions.allowed_actions(request_as_superuser, Order)
{'view'}                                        # superuser, still read-only
>>> client.get("/admin/shop/order/").status_code
200
>>> client.get("/admin/shop/order/add/").status_code
403
```

Superusers are not exempt, deliberately. The whole value of the switch is that it holds
for everyone; an exemption for the account most likely to be signed in during an
incident would make it decorative.

`READ_ONLY` is also editable from the [settings screen](theming.md#the-settings-screen),
so an operator can freeze the panel without a deploy.

## Impersonation

Deliberately narrow: superusers only, never onto another superuser, loud while it is
happening, and recorded. The failure mode being guarded against is someone quietly
acquiring rights they do not have and leaving no trace.

```pycon
>>> can_impersonate(admin)
True
>>> can_impersonate(editor)
False
```

Refusals, each with its own message:

| Attempt | Result |
|---|---|
| Not a superuser | `Only superusers may impersonate.` |
| Yourself | `You are already signed in as that user.` |
| Another superuser | `Superusers cannot be impersonated.` |
| An inactive account | `That account is inactive.` |

Refusing superuser-to-superuser matters: it would be a lateral move that muddies the
audit trail without granting anything useful.

Django's own `login()` machinery is used rather than a request-level user swap, so every
downstream permission check sees the target naturally and **nothing has to know
impersonation exists**. Two session keys track it, written after `login()` because
`login()` flushes the session.

While it is active, a banner across the top of every page says *Acting as X — signed in
as Y*. Loud on purpose: someone acting as another account needs to be unable to forget
it, and anyone looking over their shoulder needs to see it too. Both the start and the
end are logged at `warning` level to the `vantage.security` logger with both usernames
and both ids.

`real_user(request)` returns the account actually signed in, whoever is being
impersonated.

## The audit log

`VANTAGE["AUDIT_LOG"]` is on by default. It records **what a person did through this
admin** — one row per create, edit, delete or bulk action, with the actor.

That is a much smaller thing than row history, and the difference is why
django-simple-history is soft-detected rather than required: it creates a history model
*in your own app*, which means a migration in your code and roughly double the write
volume on every table it touches. Making that the price of installing an admin panel is
not a trade a panel gets to make for you. When you have chosen it, its timeline renders
next to Vantage's own.

Three rules follow from the scope.

**Never a `post_save` signal.** A receiver cannot see who was logged in — the actor is
the single most valuable column in the table. It would also faithfully log the nightly
import job and every `shell` session, which is the row history we just declined to
build, minus the actor, at the same cost. Entries are written from the two places a
person's intent is actually known: the form views and the bulk-action runner.

**The diff never carries a secret and never carries a file.** Field names are matched
against the same [`SENSITIVE` pattern](inference.md#the-sensitive-name-exclusion) that
keeps a password column out of a list page — imported rather than restated, so the two
can never drift apart — and a file field records the *name* it was given, never a byte
of its contents. An audit log is read by more people than the records it describes.

**Logging never breaks the write it describes.** Every entry goes through a writer that
catches everything, logs it and returns `None`, and it is called *outside* the
transaction that performed the write.

The log has its own page and its own gate, which is stricter than a model permission:
the log spans every model in the panel, so someone who may not see orders can still
learn a great deal from a list of who has been editing them. It is deliberately not a
registered panel — a generic list page over it would come with an Add button, an edit
form and a delete action, and an audit log you can edit is not an audit log.

Each record's [History](history.md) page replays its entries to show the record as it
was at any earlier point, and restores fields from there.

## Panels a user cannot see

Never rendered. `visible_panels` filters the sidebar, the command palette, global
search, the schema map and the default dashboard through `has_model_perm`.

```pycon
>>> len(permissions.visible_panels(request_as_admin, list(site.panels())))
9
>>> len(permissions.visible_panels(request_as_plain_staff, list(site.panels())))
0
```

Two details worth stating.

**A declared `NAV` can reorder, rename and hide — it can never grant access.** Every
entry still passes through the permission layer, so a nav entry for a model you cannot
open does not render.

**The default dashboard filters by permission *before* capping.** Capping first would
mean a user who can see only one model, and whose model sorts outside the first twelve,
gets an entirely empty dashboard — the cap would silently have become an access rule.

**A dashboard widget is gated before it is computed**, not after. A card that computes a
number and then hides it has already leaked it into the query log and paid for it.

## A row you may not see does not exist

Every object lookup goes through `filter_queryset` before any object-level check runs,
so an out-of-scope record answers **404**, not 403. Answering 403 would confirm that
record 12 exists and is merely off-limits, which is the leak the scoping was for.

Once the row is known to be in scope, an object-level refusal *is* a 403 — by that point
you already know the record is there.

The same reasoning shapes two other refusals:

- A bulk action you may not run and a bulk action that no longer exists both answer
  "that action is not available". Nothing happens, and we do not say which it was.
- The autocomplete endpoint answers `Unknown model.` for a model that is not registered
  and for one you cannot view, so it cannot be used to probe what exists.

## Sign-in

Every panel view requires an authenticated user with `is_staff`. Anonymous visitors are
redirected to `settings.LOGIN_URL` with a `next` parameter. Vantage ships styled
sign-in, sign-out and change-password views over `django.contrib.auth`'s own forms —
mount them, or keep your own. See
[Signing in](getting-started.md#signing-in).

## See also

- [Multi-tenancy](tenancy.md) — row scoping by tenant, built on `filter_queryset`
- [How configuration resolves](configuration.md) — why permissions and tenancy stay
  Python-only while other things gained a UI layer
- [Settings](settings.md#behaviour) — `PERMISSIONS`, `READ_ONLY`, `SETTINGS_PERMISSION`, `LIST_PREFS_PERMISSION`, `AUDIT_LOG`
- [Panels](panels.md#permission-hooks) — the per-panel hooks
- [Security](security.md) — login throttling, security headers, and the `--deploy` checks
- [Deployment](deployment.md) — what to switch on before going live
