"""Row scoping for multi-tenant projects, applied structurally rather than by hand.

The hand-written version of this feature is a ``get_queryset`` override on every panel::

    @register(Invoice)
    class InvoicePanel(ModelPanel):
        def get_queryset(self, request):
            return super().get_queryset(request).filter(tenant=request.user.tenant)

That works, right up until someone adds the thirty-first model, or an autocomplete, or
an export, or a foreign-key dropdown. Every one of those is a separate place to
remember, and the one that gets forgotten is a data breach rather than a bug. This
module inverts the default: **a model that this layer cannot scope returns no rows at
all**, and ``manage.py check`` names it. Forgetting is safe; the failure mode of a
mistake is an empty page, never somebody else's data.

Three ideas carry the design.

**It is the queryset layer of the existing permission model, not a fourth layer.**
:mod:`vantage.permissions` already answers "which rows may this user see?" in bulk,
once per query, and every read path in the package already goes through it. Tenancy is
composed onto :meth:`~vantage.permissions.PanelPermissions.filter_queryset` by
:func:`enforce_on`, which wraps the *instance* the site resolved rather than the class —
so a project that subclasses ``PanelPermissions`` and forgets to call ``super()`` is
still scoped. There is exactly one way to be unscoped, and it is to write
``VANTAGE["TENANT_MODEL"] = None``.

**Fail closed, and say so.** Four things can go wrong, and none of them may result in an
unfiltered queryset:

===============================  =========================================
Situation                        Result
===============================  =========================================
tenancy is off                   no change at all — the package is unaware
the tenant cannot be resolved    ``.none()`` and an ``ERROR`` log line
the model has no path to it      ``.none()`` and a ``vantage.E060`` check
a write cannot be attributed     :class:`TenantRequired` — a 403, never a save
===============================  =========================================

**Reads and writes are different problems.** Scoping a queryset keeps another tenant's
rows off the page. It does nothing about a ``POST`` that moves one of *your* rows into
somebody else's tenant, and nothing about a dropdown that offers their rows as options.
Those are :func:`enforce` and :func:`scope_choices`, and both are wired into the same
places the reads are.

Turning it on is one setting::

    VANTAGE = {"TENANT_MODEL": "accounts.Organisation"}

    MIDDLEWARE = [
        ...,
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "vantage.tenancy.TenantMiddleware",
    ]

The middleware is not optional when tenancy is on, and ``manage.py check`` refuses to
pass without it. Not everything that reads the database has a request in scope — a
foreign-key dropdown is built by a ``formfield_callback`` that Django never hands one
to — so the request is published to a :class:`~contextvars.ContextVar` for the duration
of the call, and code without a request reads it from there. Without the middleware
those paths would resolve no tenant and, being fail-closed, render empty controls.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.signals import setting_changed
from django.db.models import ForeignKey, Model, OneToOneField, QuerySet
from django.dispatch import receiver

__all__ = [
    "ALL_TENANTS",
    "HeaderResolver",
    "Scope",
    "ScopeKind",
    "SubdomainResolver",
    "TenantMiddleware",
    "TenantMismatch",
    "TenantRequired",
    "TenantResolver",
    "UserAttributeResolver",
    "bind",
    "check_tenancy",
    "current",
    "enabled",
    "enforce",
    "enforce_on",
    "scope_choices",
    "scope_for",
    "scope_queryset",
    "tenant_context",
    "tenant_model",
]

logger = logging.getLogger("vantage.tenancy")

#: Where the resolved tenant is cached for the life of one request.
REQUEST_ATTR = "_vantage_tenant"

#: Set on the request when scoping refused, so a view can explain itself.
ERROR_ATTR = "_vantage_tenant_error"


class _Unset:
    """Distinguishes "not resolved yet" from the perfectly ordinary "no tenant"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"

    def __bool__(self) -> bool:
        return False


_UNSET = _Unset()


class _AllTenants:
    """Cross-tenant access, for the provider's own staff.

    Returned by a resolver that has decided this request is not acting *as* a tenant.
    Deliberately not the same value as "no tenant": ``None`` means the question could
    not be answered and everything must close, while this means it was answered with
    "all of them". Confusing the two is how a misconfiguration becomes a breach, so
    they are different objects and neither is falsy by accident.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<all tenants>"


#: Sentinel a resolver returns to lift scoping for one request. See :class:`_AllTenants`.
ALL_TENANTS = _AllTenants()


# ── failures ────────────────────────────────────────────────────────────────
#
# Both are PermissionDenied subclasses so Django's own handler turns them into a 403
# without any view knowing they exist. A tenancy failure on a write is never allowed to
# degrade into a successful save with a surprising value in it.


class TenantRequired(PermissionDenied):
    """A write was attempted with no tenant in scope, or on an unscopable model."""


class TenantMismatch(PermissionDenied):
    """A write tried to attribute a record to a tenant other than the current one."""


# ── configuration ───────────────────────────────────────────────────────────


def _setting(name: str) -> Any:
    """A ``TENANT_*`` setting, read from the declared dict rather than the cascade.

    Every key here lives in :data:`vantage.conf.DEFAULTS` like any other — that is what
    documents it and rejects the typo — but it is read *around* ``vantage_settings``
    rather than through it, for two reasons.

    The first is cost. That layer merges the overrides stored in the database, so the
    first read after an invalidation is a query, and :func:`enabled` is asked on every
    queryset the panel builds. Paying a query to discover that a project is not
    multi-tenant is exactly the kind of tax this module must not levy — the same
    reasoning that keeps :func:`vantage.permissions.PanelPermissions.is_read_only` from
    touching settings on a read.

    The second is that none of these is editable from the settings screen, and none of
    them should ever become editable: turning tenant scoping off from a web form is a
    self-inflicted data breach, and a value that decides who sees what belongs in the
    file that is code-reviewed and deployed.
    """
    from django.conf import settings as django_settings

    from vantage.conf import DEFAULTS

    declared = getattr(django_settings, "VANTAGE", None)
    if not isinstance(declared, dict):
        declared = {}
    return declared.get(name, DEFAULTS[name])


def enabled() -> bool:
    """Whether this project is multi-tenant at all.

    One setting turns the whole module on, and every entry point below returns its
    input unchanged when it is off. That is what keeps tenancy free — in behaviour and
    in queries — for the projects that do not need it.
    """
    return tenant_model() is not None


def tenant_model() -> type[Model] | None:
    """The model rows are scoped *to*, or ``None`` when tenancy is off.

    A bad label is reported by :func:`check_tenancy` and treated as "off" here rather
    than raised: an exception thrown from inside a queryset filter surfaces as a 500 on
    an unrelated page, which is a worse way to learn about a typo than ``manage.py
    check`` telling you directly.
    """
    label = _setting("TENANT_MODEL")
    if not label:
        return None
    from django.apps import apps as django_apps

    try:
        return django_apps.get_model(label)
    except (LookupError, ValueError):
        logger.error(
            "VANTAGE['TENANT_MODEL'] = %r does not name an installed model, so tenant "
            "scoping is not being applied. Run manage.py check.",
            label,
        )
        return None


def _unscoped_mode() -> str:
    return _setting("TENANT_UNSCOPED")


#: Apps whose models are infrastructure rather than tenant data. Scoping them would
#: break the panel's own pages (a job row, an audit entry, a stored list preference)
#: for no gain, and none of them is a place customer records live.
INFRASTRUCTURE_APPS = frozenset({"vantage", "contenttypes", "sessions", "admin"})


# ── resolution ──────────────────────────────────────────────────────────────


class TenantResolver:
    """Answers "whose request is this?".

    The protocol is one method. Point ``VANTAGE["TENANT_RESOLVER"]`` at a subclass, or
    at any callable taking a request — projects differ enough about this that shipping
    only one answer would be wrong, and every one of the three shipped below is a real
    deployment shape.

    Return the tenant instance, its primary key, :data:`ALL_TENANTS`, or ``None``.
    ``None`` is not a failure to be papered over: it closes every scoped queryset for
    the request, which is the correct outcome for a caller nobody can place.
    """

    def resolve(self, request) -> Any:
        raise NotImplementedError

    def __call__(self, request) -> Any:
        return self.resolve(request)


class UserAttributeResolver(TenantResolver):
    """The default: read the tenant off the signed-in user.

    Covers the common shape where the user table carries a foreign key to the tenant,
    and it is the only one of the three that keeps working in a background worker,
    where there is a user but no host and no headers.

    Superusers are *not* exempt. A resolver that quietly widened them to every tenant
    would mean the safest-looking account in the project is the one place scoping does
    not apply — and on most SaaS projects the tenant's own administrators are staff.
    Subclass and return :data:`ALL_TENANTS` when your operators genuinely need it::

        class OperatorsSeeEverything(UserAttributeResolver):
            def resolve(self, request):
                user = getattr(request, "user", None)
                if user is not None and user.is_superuser:
                    return ALL_TENANTS
                return super().resolve(request)
    """

    def resolve(self, request) -> Any:
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        field = _setting("TENANT_FIELD")
        value = getattr(user, field, _UNSET)
        if value is _UNSET:
            # ``_id`` next, so a project that stores the key without a relation — or
            # wants to avoid the join on every request — is not forced into a subclass.
            value = getattr(user, f"{field}_id", None)
        if callable(value):
            value = value()
        return value or None


class SubdomainResolver(TenantResolver):
    """``acme.example.com`` → the tenant whose ``slug`` is ``acme``.

    Set :attr:`lookup` to match your column, and :attr:`ignore` to the labels that are
    not tenants. The host is read through ``request.get_host()``, which validates
    against ``ALLOWED_HOSTS`` — reading the ``Host`` header directly would let a
    request name any tenant it liked.
    """

    #: The field the subdomain is matched against.
    lookup = "slug"

    #: Labels that never name a tenant. ``www`` is the marketing site, not a customer.
    ignore = frozenset({"www", "admin", "app", "api", "static", "media"})

    def resolve(self, request) -> Any:
        model = tenant_model()
        if model is None:
            return None
        try:
            host = request.get_host()
        except Exception:  # a disallowed host is not a tenant
            return None
        label = host.split(":")[0].split(".")[0].lower()
        if not label or label in self.ignore:
            return None
        return model._default_manager.filter(**{self.lookup: label}).first()


class HeaderResolver(TenantResolver):
    """``X-Tenant: 42`` → tenant 42, for service-to-service calls and reverse proxies.

    **Only safe when something in front of the panel sets that header and strips any
    inbound copy of it.** A browser session can send whatever it likes, so on its own
    this resolver lets any signed-in user name any tenant. It is here because a proxy
    that authenticates the tenant is a real and common architecture, and because
    building it wrongly by hand is worse; :meth:`permitted` is the hook where you say
    which tenants this caller may claim, and it refuses everything by default unless
    the header is trusted.
    """

    #: Header name in its HTTP spelling; converted to the ``META`` key on use.
    header = "X-Tenant"

    #: The field the header value is matched against.
    lookup = "pk"

    def resolve(self, request) -> Any:
        model = tenant_model()
        if model is None:
            return None
        key = "HTTP_" + self.header.upper().replace("-", "_")
        raw = (getattr(request, "META", None) or {}).get(key)
        if not raw:
            return None
        tenant = model._default_manager.filter(**{self.lookup: raw}).first()
        if tenant is None:
            return None
        return tenant if self.permitted(request, tenant) else None

    def permitted(self, request, tenant) -> bool:
        """Whether this caller may claim the tenant the header names.

        The default is "yes", because the documented deployment for this resolver has a
        proxy that already decided. Override it the moment the header can reach you
        from a browser.
        """
        return True


_resolver_cache: TenantResolver | None = None


def get_resolver():
    """The configured resolver, instantiated once.

    A dotted path to a class is instantiated; a dotted path to a function is used as
    it stands, so ``TENANT_RESOLVER = "myapp.tenancy.from_request"`` is a complete
    implementation.
    """
    global _resolver_cache
    if _resolver_cache is None:
        path = _setting("TENANT_RESOLVER")
        if not path:
            _resolver_cache = UserAttributeResolver()
        else:
            from django.utils.module_loading import import_string

            candidate = import_string(path)
            _resolver_cache = (
                candidate() if isinstance(candidate, type) else candidate
            )
    return _resolver_cache


# ── the ambient tenant ──────────────────────────────────────────────────────
#
# A ContextVar rather than a thread local: it is what Django's own async paths respect,
# and its ``reset`` is exact even when requests nest. It holds either a tenant or the
# request to resolve one from, so the middleware costs no query on a request that never
# touches a scoped model.

_ambient: ContextVar[Any] = ContextVar("vantage_tenant", default=_UNSET)


def bind(request, tenant) -> None:
    """Pin ``tenant`` to ``request``, skipping the resolver for the rest of it.

    For code that already knows the answer — a background job replaying a stored
    selection, a test, a management command that built its own request.
    """
    setattr(request, REQUEST_ATTR, tenant)


@contextmanager
def tenant_context(tenant):
    """Run a block as ``tenant``, for code with no request in scope.

    ::

        with tenant_context(org):
            Invoice.objects.count()   # still unscoped: this is not a manager

    It sets what :func:`current` returns when nothing else answers, which is what the
    request-less entry points below read. Pass ``None`` to run a block explicitly
    tenantless — every scoped queryset in it is empty.
    """
    token = _ambient.set(tenant)
    try:
        yield tenant
    finally:
        _ambient.reset(token)


def current(request=None) -> Any:
    """The tenant this call is acting as, or ``None`` if that cannot be answered.

    Resolved at most once per request and cached on it, because the resolver may well
    hit the database and a list page asks this question a dozen times — once for the
    rows, once for the count, once per facet, once per dropdown.
    """
    if not enabled():
        return None
    if request is not None:
        cached = getattr(request, REQUEST_ATTR, _UNSET)
        if cached is not _UNSET:
            return cached
        tenant = _resolve(request)
        try:
            setattr(request, REQUEST_ATTR, tenant)
        except AttributeError:  # pragma: no cover - an exotic request-like object
            pass
        return tenant

    ambient = _ambient.get()
    if ambient is _UNSET:
        return None
    # The middleware publishes the *request*, so a request that never reads a scoped
    # model never pays for the resolver.
    if hasattr(ambient, "META") or hasattr(ambient, "user"):
        return current(ambient)
    return ambient


def _resolve(request) -> Any:
    resolver = get_resolver()
    try:
        return resolver(request) or None
    except Exception:
        # A resolver that raises must not hand back an unscoped page. Closing and
        # logging is the only safe reading of "I do not know whose request this is".
        logger.exception("The tenant resolver raised; treating the request as tenantless.")
        return None


def require(request=None):
    """The current tenant, or :class:`TenantRequired`.

    The write-path counterpart of :func:`current`. A read with no tenant shows nothing;
    a write with no tenant cannot be attributed to anyone and must not happen at all.
    """
    tenant = current(request)
    if tenant is None:
        raise TenantRequired(
            "This record cannot be saved because no tenant could be determined for "
            "the request."
        )
    return tenant


# ── which models are scoped, and how ────────────────────────────────────────


class ScopeKind(Enum):
    """How a model relates to the tenant."""

    #: Shared reference data. Never filtered.
    GLOBAL = "global"
    #: Reachable from the tenant by a lookup path. Always filtered.
    SCOPED = "scoped"
    #: Nobody has said. Fails closed, and ``manage.py check`` reports it.
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Scope:
    """One model's relationship to the tenant, and how that was decided."""

    kind: ScopeKind
    #: The ORM lookup path from this model to the tenant. ``None`` unless SCOPED.
    path: str | None = None
    #: Human-readable provenance, quoted verbatim by the checks and the logs.
    reason: str = ""

    @property
    def is_scoped(self) -> bool:
        return self.kind is ScopeKind.SCOPED


_scope_cache: dict[str, Scope] = {}


def scope_for(model: type[Model]) -> Scope:
    """How ``model`` is scoped, resolved once and cached.

    The order is: what you declared, then what the model plainly says, then a refusal.
    Inference is deliberately shallow — a single direct foreign key to the tenant, and
    nothing cleverer. A layer that guessed its way across two relations would sooner or
    later guess a path that filters on the wrong thing, and a wrong filter is a leak
    dressed as a feature. :func:`suggest_paths` does the guessing instead, in the
    checks output, where a human reads it before it takes effect.
    """
    label = model._meta.label_lower
    cached = _scope_cache.get(label)
    if cached is None:
        cached = _scope_cache[label] = _compute_scope(model)
    return cached


def _compute_scope(model: type[Model]) -> Scope:
    tenant = tenant_model()
    if tenant is None:
        return Scope(ScopeKind.GLOBAL, reason="tenancy is off")

    label = model._meta.label_lower

    declared = {str(k).lower(): v for k, v in (_setting("TENANT_SCOPE") or {}).items()}
    exempt = {str(k).lower() for k in (_setting("TENANT_EXEMPT") or ())}

    # Declared first, always. An explicit answer outranks both inference and the
    # built-in exemptions, so a project that really does scope its own audit trail can
    # say so.
    if label in declared:
        path = declared[label]
        if path is None:
            return Scope(ScopeKind.GLOBAL, reason="declared in TENANT_SCOPE as global")
        problem = _validate_path(model, path, tenant)
        if problem:
            return Scope(ScopeKind.UNRESOLVED, reason=problem)
        return Scope(ScopeKind.SCOPED, path=path, reason="declared in TENANT_SCOPE")

    if label in exempt:
        return Scope(ScopeKind.GLOBAL, reason="declared in TENANT_EXEMPT")

    if model._meta.app_label in INFRASTRUCTURE_APPS:
        return Scope(ScopeKind.GLOBAL, reason="an infrastructure model")

    if model is tenant or issubclass(model, tenant):
        return Scope(ScopeKind.SCOPED, path="pk", reason="is the tenant model")

    links = _tenant_links(model, tenant)
    if len(links) == 1:
        return Scope(
            ScopeKind.SCOPED,
            path=links[0],
            reason=f"has a single foreign key to the tenant ({links[0]})",
        )
    if len(links) > 1:
        return Scope(
            ScopeKind.UNRESOLVED,
            reason=(
                f"has {len(links)} foreign keys to {tenant._meta.label} "
                f"({', '.join(links)}); which one owns the row is not knowable from "
                "the model"
            ),
        )
    return Scope(
        ScopeKind.UNRESOLVED,
        reason=f"has no foreign key to {tenant._meta.label}",
    )


def _tenant_links(model: type[Model], tenant: type[Model]) -> list[str]:
    """Names of the concrete forward relations from ``model`` to the tenant."""
    return [
        field.name
        for field in model._meta.get_fields()
        if isinstance(field, ForeignKey | OneToOneField)
        and field.related_model is not None
        and (field.related_model is tenant or issubclass(field.related_model, tenant))
    ]


def _validate_path(model: type[Model], path: str, tenant: type[Model]) -> str | None:
    """``None`` if ``path`` walks from ``model`` to the tenant, else why it does not."""
    if path == "pk":
        return None if model is tenant or issubclass(model, tenant) else (
            f"the path 'pk' only scopes the tenant model itself, and "
            f"{model._meta.label} is not {tenant._meta.label}"
        )
    current_model = model
    for segment in path.split("__"):
        if current_model is None:
            return f"the path {path!r} walks past a non-relation field"
        try:
            field = current_model._meta.get_field(segment)
        except Exception:
            return f"{current_model._meta.label} has no field {segment!r} (in {path!r})"
        related = getattr(field, "related_model", None)
        if related is None:
            return f"{current_model._meta.label}.{segment} is not a relation (in {path!r})"
        current_model = related
    if current_model is not tenant and not issubclass(current_model, tenant):
        return (
            f"the path {path!r} ends at {current_model._meta.label}, "
            f"not at {tenant._meta.label}"
        )
    return None


def suggest_paths(model: type[Model], depth: int = 2) -> list[str]:
    """Lookup paths from ``model`` to the tenant, for the checks to recommend.

    Breadth-first over forward relations only, so what comes back is a path a human can
    read and paste into ``TENANT_SCOPE``. Reverse relations are excluded on purpose:
    filtering across one multiplies rows, and a scoping rule that silently changes the
    cardinality of every query is not a scoping rule.
    """
    tenant = tenant_model()
    if tenant is None:
        return []
    found: list[str] = []
    frontier: list[tuple[type[Model], str]] = [(model, "")]
    seen = {model._meta.label_lower}
    for _ in range(depth):
        nxt: list[tuple[type[Model], str]] = []
        for owner, prefix in frontier:
            for field in owner._meta.get_fields():
                if not isinstance(field, ForeignKey | OneToOneField):
                    continue
                related = field.related_model
                if related is None:
                    continue
                path = f"{prefix}{field.name}"
                if related is tenant or issubclass(related, tenant):
                    if path not in found:
                        found.append(path)
                    continue
                if related._meta.label_lower in seen:
                    continue
                seen.add(related._meta.label_lower)
                nxt.append((related, f"{path}__"))
        if found:
            break
        frontier = nxt
    return found


# ── the read path ───────────────────────────────────────────────────────────


def scope_queryset(queryset: QuerySet, request=None, action: str = "view") -> QuerySet:
    """Narrow ``queryset`` to the current tenant's rows. The whole feature, in one call.

    Composed onto :meth:`~vantage.permissions.PanelPermissions.filter_queryset` by
    :func:`enforce_on`, so every read path in the package — the list page, detail,
    autocomplete, export, search, dashboard widgets, saved views, the importer's
    lookups, bulk actions and the jobs that run them — is covered by that one wrapping
    rather than by nine call sites that each have to remember.

    ``action`` is accepted to match the permission signature and is deliberately
    unused: whether a row exists for you has one answer per user, not one per verb.
    """
    if not enabled():
        return queryset

    model = queryset.model
    scope = scope_for(model)

    if scope.kind is ScopeKind.GLOBAL:
        return queryset

    if scope.kind is ScopeKind.UNRESOLVED:
        if _unscoped_mode() == "allow":
            return queryset
        _refuse(
            request,
            f"{model._meta.label} is not tenant-scoped ({scope.reason}). Showing no "
            f"rows. Declare it in VANTAGE['TENANT_SCOPE'] or VANTAGE['TENANT_EXEMPT'].",
        )
        return queryset.none()

    tenant = current(request)
    if tenant is ALL_TENANTS:
        return queryset
    if tenant is None:
        _refuse(
            request,
            f"No tenant could be determined, so no {model._meta.label} rows are "
            f"visible. Check VANTAGE['TENANT_RESOLVER'] and that "
            f"vantage.tenancy.TenantMiddleware is installed.",
        )
        return queryset.none()

    return queryset.filter(**{scope.path: tenant})


def _refuse(request, message: str) -> None:
    """Empty is not enough — a closed queryset that nobody can explain is a support
    ticket. Every refusal is logged at ERROR and left on the request for the UI."""
    logger.error("%s", message)
    if request is not None:
        try:
            setattr(request, ERROR_ATTR, message)
        except AttributeError:  # pragma: no cover - an exotic request-like object
            pass


def scope_choices(field, request=None):
    """Narrow a form field's options. Returns the field, so it drops into a callback.

    A dropdown offering another tenant's records is a leak on its own terms. Saving one
    would be refused by :func:`enforce` and by the field's own validation, but by then
    the names, the numbers and the row count have already been rendered into somebody
    else's browser.

    Accepts anything with a ``queryset`` attribute, which is every relation field
    Django builds — ``ModelChoiceField`` and ``ModelMultipleChoiceField`` — and quietly
    passes everything else through.
    """
    if not enabled() or field is None:
        return field
    queryset = getattr(field, "queryset", None)
    if queryset is None:
        return field
    field.queryset = scope_queryset(queryset, request)
    return field


# ── the write path ──────────────────────────────────────────────────────────


def enforce(request, obj):
    """Attribute ``obj`` to the current tenant, or refuse the write.

    Two failures, both of which look like an ordinary save until you look:

    * **A create with nothing to attribute it to.** The tenant column is usually not on
      the form — it is not the user's business — so a create would insert a ``NULL`` or
      a default and produce a row nobody owns. Here it is stamped.
    * **An update that changes tenant.** Scoping stops you *reading* another tenant's
      invoice; it does nothing about posting ``tenant=other`` on one of your own and
      pushing it across the boundary. Here that is :class:`TenantMismatch`, a 403.

    Called before the panel's ``save_model`` hook so the hook sees a complete object,
    and returns the object for convenience.
    """
    if not enabled() or obj is None:
        return obj

    model = type(obj)
    scope = scope_for(model)
    if scope.kind is ScopeKind.GLOBAL:
        return obj
    if scope.kind is ScopeKind.UNRESOLVED:
        if _unscoped_mode() == "allow":
            return obj
        raise TenantRequired(
            f"{model._meta.label} cannot be written: it is not tenant-scoped "
            f"({scope.reason})."
        )

    tenant = current(request)
    if tenant is ALL_TENANTS:
        # An operator acting across tenants. There is no single tenant to stamp, and
        # inventing one would be worse than leaving the posted value alone.
        return obj
    if tenant is None:
        raise TenantRequired(
            f"This {model._meta.verbose_name} cannot be saved because no tenant "
            f"could be determined for the request."
        )

    tenant_pk = getattr(tenant, "pk", tenant)
    path = scope.path or ""
    head, _, rest = path.partition("__")

    if path == "pk":
        if obj.pk is not None and str(obj.pk) != str(tenant_pk):
            raise TenantMismatch("A record may not be moved to another tenant.")
        return obj

    field = model._meta.get_field(head)

    if not rest:
        held = getattr(obj, field.attname, None)
        if held is None:
            # The stamp. Set through ``attname`` when we only hold a key, so no query
            # is needed to turn it into an instance the ORM will immediately discard.
            if isinstance(tenant, Model):
                setattr(obj, field.name, tenant)
            else:
                setattr(obj, field.attname, tenant)
            return obj
        if str(held) != str(tenant_pk):
            raise TenantMismatch(
                f"This {model._meta.verbose_name} may not be attributed to another "
                f"tenant."
            )
        return obj

    # A multi-hop path: the row belongs to the tenant through its parent, so the
    # question is whether the parent does. One query, and only on the models that
    # actually need it.
    parent_pk = getattr(obj, field.attname, None)
    if parent_pk is None:
        raise TenantRequired(
            f"This {model._meta.verbose_name} has no {field.name}, so it cannot be "
            f"attributed to a tenant."
        )
    owned = field.related_model._default_manager.filter(
        pk=parent_pk, **{rest: tenant}
    ).exists()
    if not owned:
        raise TenantMismatch(
            f"The {field.name} chosen for this {model._meta.verbose_name} belongs to "
            f"another tenant."
        )
    return obj


# ── integration points ──────────────────────────────────────────────────────


def enforce_on(permissions):
    """Compose tenant scoping onto a permission resolver instance, permanently.

    The *instance*, not the class, and after whatever it does — because the thing this
    module exists to prevent is somebody forgetting. A project that subclasses
    ``PanelPermissions`` and overrides ``filter_queryset`` without calling ``super()``
    is a perfectly ordinary mistake, and under a class-level hook it would silently
    unscope every model in the panel. Wrapping the bound method means the only way to
    lose scoping is to turn it off in settings.

    Idempotent: the marker stops a second call double-wrapping a resolver.
    """
    if getattr(permissions.filter_queryset, "_vantage_tenant_scoped", False):
        return permissions
    owner = type(permissions)

    def filter_queryset(request, queryset, action="view"):
        # Resolved on the class at call time rather than captured now, so overriding
        # the method — in a subclass, or in a test — still decides the row permissions.
        # The wrapper itself lives on the instance, so neither can get out from under
        # the tenant scoping that follows it.
        inner = owner.filter_queryset
        return scope_queryset(inner(permissions, request, queryset, action), request, action)

    filter_queryset._vantage_tenant_scoped = True
    filter_queryset.__doc__ = owner.filter_queryset.__doc__
    permissions.filter_queryset = filter_queryset
    return permissions


def snapshot(request) -> str | None:
    """The current tenant as a string, for a background job to store.

    A worker has no host and no headers, so a subdomain or header resolver cannot run
    there. Recording the tenant the selection was made under keeps a deferred bulk
    action operating on exactly the rows the page was showing.
    """
    if not enabled():
        return None
    tenant = current(request)
    if tenant is ALL_TENANTS:
        return "*"
    if tenant is None:
        return None
    return str(getattr(tenant, "pk", tenant))


def restore(request, token: str | None) -> None:
    """Put a snapshotted tenant back on a rebuilt request.

    The live resolver still gets the first word: if it can place the rebuilt request
    and disagrees with the snapshot, the actor's tenant changed between enqueue and
    run and the job must not proceed on either answer.
    """
    if not enabled():
        return
    live = _resolve(request)
    if token == "*":
        bind(request, ALL_TENANTS if live is None else live)
        return
    if token is None:
        bind(request, live)
        return
    if live is not None and str(getattr(live, "pk", live)) != str(token):
        raise TenantMismatch(
            "The tenant that started this job is not the tenant its actor belongs to "
            "now, so it cannot be resumed."
        )
    model = tenant_model()
    stored = model._default_manager.filter(pk=token).first() if model else None
    bind(request, stored)


class TenantMiddleware:
    """Publishes the request so tenant-aware code without one can find it.

    Required whenever tenancy is on, and ``vantage.E065`` fails the build without it.
    Not because the queryset layer needs it — that has a request — but because three
    things that read the database do not: the foreign-key dropdown on a form, the
    relation options in the filter rail, and the importer's lookup of a related record
    by name. All three are built by code Django never hands a request to, and all three
    would otherwise render empty.

    Costs nothing on a request that never touches a scoped model: what is published is
    the request, and the resolver runs on first use.

    Install it after ``AuthenticationMiddleware`` — the default resolver reads the
    user::

        MIDDLEWARE = [
            ...,
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "vantage.tenancy.TenantMiddleware",
        ]
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _ambient.set(request)
        try:
            return self.get_response(request)
        finally:
            # In a ``finally`` and by token, so a request that raises cannot leave its
            # tenant behind for whatever the worker thread picks up next.
            _ambient.reset(token)


# ── checks ──────────────────────────────────────────────────────────────────


def check_tenancy(app_configs, **kwargs):
    """Report every model the panel would refuse, and every way the config is wrong.

    This is the half of "fail closed" that makes it usable. Closing is safe but
    invisible; a check that names the model, says why, and prints the line to paste is
    what turns a blank page into a two-minute fix.
    """
    from django.core.checks import Error, Warning

    problems: list[Any] = []
    label = _setting("TENANT_MODEL")
    if not label:
        return problems

    from django.apps import apps as django_apps

    try:
        tenant = django_apps.get_model(label)
    except (LookupError, ValueError):
        return [
            Error(
                f"VANTAGE['TENANT_MODEL'] = {label!r} does not name an installed model.",
                hint="Use 'app_label.ModelName', e.g. 'accounts.Organisation'.",
                id="vantage.E061",
            )
        ]

    problems.extend(_check_resolver())
    problems.extend(_check_declarations(tenant))
    problems.extend(_check_middleware())
    problems.extend(_check_panels(app_configs, Error, Warning))
    return problems


def _check_resolver():
    from django.core.checks import Error

    path = _setting("TENANT_RESOLVER")
    if not path:
        return []
    from django.utils.module_loading import import_string

    try:
        candidate = import_string(path)
    except ImportError as exc:
        return [
            Error(
                f"VANTAGE['TENANT_RESOLVER'] = {path!r} could not be imported ({exc}).",
                id="vantage.E062",
            )
        ]
    if not callable(candidate):
        return [
            Error(
                f"VANTAGE['TENANT_RESOLVER'] = {path!r} is not callable.",
                hint="Point it at a TenantResolver subclass or a function taking a request.",
                id="vantage.E062",
            )
        ]
    return []


def _check_declarations(tenant):
    """Every label and path in ``TENANT_SCOPE``/``TENANT_EXEMPT``, checked eagerly.

    A misspelled label in either is silent at runtime — the model simply keeps whatever
    inference gave it — and silent is the one thing a scoping rule may not be.
    """
    from django.apps import apps as django_apps
    from django.core.checks import Error

    problems = []
    for setting_name in ("TENANT_SCOPE", "TENANT_EXEMPT"):
        declared = _setting(setting_name) or ()
        for key in declared:
            try:
                model = django_apps.get_model(str(key))
            except (LookupError, ValueError):
                problems.append(
                    Error(
                        f"VANTAGE[{setting_name!r}] names {key!r}, which is not an "
                        f"installed model.",
                        id="vantage.E063",
                    )
                )
                continue
            if setting_name == "TENANT_EXEMPT":
                continue
            path = declared[key]
            if path is None:
                continue
            problem = _validate_path(model, str(path), tenant)
            if problem:
                problems.append(
                    Error(
                        f"VANTAGE['TENANT_SCOPE'][{key!r}] = {path!r} is not a valid "
                        f"path to the tenant: {problem}.",
                        hint=_suggestion_hint(model),
                        id="vantage.E064",
                    )
                )
    return problems


def _check_middleware():
    from django.conf import settings as django_settings
    from django.core.checks import Error

    installed = [str(item) for item in getattr(django_settings, "MIDDLEWARE", None) or []]
    if any(item.endswith("tenancy.TenantMiddleware") for item in installed):
        return []
    return [
        Error(
            "Tenant scoping is on but vantage.tenancy.TenantMiddleware is not "
            "installed, so anything that reads the database without a request — "
            "foreign-key dropdowns, filter options, importer lookups — will render "
            "empty.",
            hint=(
                "Add 'vantage.tenancy.TenantMiddleware' to MIDDLEWARE, after "
                "django.contrib.auth.middleware.AuthenticationMiddleware."
            ),
            id="vantage.E065",
        )
    ]


def _check_panels(app_configs, Error, Warning):
    """The important one: every registered model that would silently show nothing."""
    from vantage.sites import all_sites

    if app_configs is not None:
        app_configs = set(app_configs)

    deny = _unscoped_mode() == "deny"
    seen: set[str] = set()
    problems = []
    for site in all_sites:
        for panel in site.registry.panels():
            model = panel.model
            key = model._meta.label_lower
            if key in seen:
                continue
            if app_configs is not None and model._meta.app_config not in app_configs:
                continue
            seen.add(key)
            scope = scope_for(model)
            if scope.kind is not ScopeKind.UNRESOLVED:
                continue
            message = (
                f"{model._meta.label} is registered in the panel but has no path to "
                f"the tenant: it {scope.reason}."
            )
            problems.append(
                Error(
                    message + " Its pages will show no rows.",
                    hint=_suggestion_hint(model),
                    obj=model,
                    id="vantage.E060",
                )
                if deny
                else Warning(
                    message
                    + " VANTAGE['TENANT_UNSCOPED'] = 'allow', so every tenant sees "
                    "all of its rows.",
                    hint=_suggestion_hint(model),
                    obj=model,
                    id="vantage.W060",
                )
            )
    return problems


def _suggestion_hint(model) -> str:
    """The line to paste, worked out rather than described."""
    label = model._meta.label_lower
    paths = suggest_paths(model)
    if paths:
        options = " or ".join(repr(path) for path in paths)
        return (
            f"A path exists: VANTAGE['TENANT_SCOPE'] = {{{label!r}: {options}}}. "
            f"If these rows are shared reference data instead, add {label!r} to "
            f"VANTAGE['TENANT_EXEMPT']."
        )
    return (
        f"Add {label!r} to VANTAGE['TENANT_EXEMPT'] if these rows are shared by every "
        f"tenant, or give it a path in VANTAGE['TENANT_SCOPE']."
    )


# ── invalidation ────────────────────────────────────────────────────────────


def _reset() -> None:
    """Drop everything derived from settings. Used by the signal below and by tests."""
    global _resolver_cache
    _resolver_cache = None
    _scope_cache.clear()


@receiver(setting_changed)
def _invalidate(sender, setting, **kwargs):
    # Also on INSTALLED_APPS and MIDDLEWARE: the scope map holds model classes, and
    # ``isolate_apps`` in a test replaces them under us.
    if setting in {"VANTAGE", "INSTALLED_APPS", "MIDDLEWARE", "AUTH_USER_MODEL"}:
        _reset()
