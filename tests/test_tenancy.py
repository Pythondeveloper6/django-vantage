"""Row scoping for multi-tenant projects — the module's own design rules, held to.

``vantage.tenancy`` states its rules in its own docstring: fail closed on every one of
four situations, compose onto the permission layer rather than beside it, and treat
reads and writes as different problems. This file tries to break each of those claims
rather than restate them — a tenant-scoped model that shows another tenant's row through
any path is a data breach, not a cosmetic bug, so the tests below are written to be
hostile: wrong FK on a POST, another tenant's pk typed directly into a selection, a
resolver that lies or blows up, a permission subclass that forgets ``super()``.

Fixtures live in ``tests/testapp/models.py`` under the ``Tnt`` prefix (see the comment
there) rather than reusing ``Snippet`` — the pre-existing tenancy fixture ``tests/
test_api.py`` builds by conscripting ``auth.User`` as its own tenant — because several
of the scenarios here (an ambiguous double foreign key, a multi-hop relation, an exempt
model, an orphaned one) have no honest model to reuse there.

Two tenancy configurations recur through the file:

* Most tests set ``VANTAGE`` directly through the ``settings`` fixture and read
  ``vantage.tenancy`` functions directly, or drive a bare request built by hand — no
  HTTP round trip, no middleware, nothing but the module under test.
* The HTTP-level classes use ``http_tenancy``, which turns on the full stack: a
  resolver that reads the signed-in user's username against ``TntOrg.slug`` (durable
  across a real ``Client`` round trip, unlike a Python attribute stapled onto a request),
  ``TenantMiddleware``, and the ``TENANT_SCOPE``/``TENANT_EXEMPT`` declarations a real
  project would have written after running ``manage.py check``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db.models import Count
from django.test import Client, RequestFactory
from django.urls import reverse

from tests.testapp.models import (
    TntAmbiguous,
    TntGadget,
    TntOrg,
    TntOrphan,
    TntReference,
    TntSecret,
    TntWidget,
)
from vantage import tenancy
from vantage.tenancy import (
    ALL_TENANTS,
    HeaderResolver,
    ScopeKind,
    SubdomainResolver,
    TenantMismatch,
    TenantRequired,
    TenantResolver,
    UserAttributeResolver,
    check_tenancy,
    current,
    enabled,
    enforce,
    enforce_on,
    scope_choices,
    scope_for,
    scope_queryset,
    tenant_context,
)

pytestmark = pytest.mark.django_db


# ── helpers ─────────────────────────────────────────────────────────────────


def grant(user, model, *actions):
    """Grant ``user`` the given verbs ("view", "change", ...) on ``model``."""
    ct = ContentType.objects.get_for_model(model)
    codenames = [f"{action}_{model._meta.model_name}" for action in actions]
    user.user_permissions.add(*Permission.objects.filter(content_type=ct, codename__in=codenames))
    return user


def _slug(model) -> str:
    return f"{model._meta.app_label}-{model._meta.model_name}"


def list_url(model) -> str:
    return reverse(f"vantage:{_slug(model)}-list")


def add_url(model) -> str:
    return reverse(f"vantage:{_slug(model)}-add")


def change_url(obj) -> str:
    return reverse(f"vantage:{_slug(type(obj))}-change", args=[obj.pk])


def delete_url(obj) -> str:
    return reverse(f"vantage:{_slug(type(obj))}-delete", args=[obj.pk])


def detail_url(obj) -> str:
    return reverse(f"vantage:{_slug(type(obj))}-detail", args=[obj.pk])


def client_as(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def as_csv(rows) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


class FakeUser:
    """A signed-in caller, without the ceremony of a real ``auth.User`` row.

    ``UserAttributeResolver`` reads an arbitrary attribute off ``request.user`` — this
    is what lets a test hand it exactly the attribute (or callable, or nothing at all)
    the scenario needs, including shapes a real ``User`` cannot easily carry.
    """

    def __init__(self, authenticated: bool = True, **attrs):
        self.is_authenticated = authenticated
        self.__dict__.update(attrs)


class FakeRequest:
    """Just enough of a request for a resolver: ``.user``, ``.get_host()``, ``.META``."""

    def __init__(self, user=None, host=None, headers=None):
        self.user = user
        self._host = host
        self.META = {
            f"HTTP_{key.upper().replace('-', '_')}": value for key, value in (headers or {}).items()
        }

    def get_host(self) -> str:
        # A disallowed Host header is exactly what django's own get_host() refuses.
        if self._host is None:
            raise Exception("disallowed host")
        return self._host


# ── module-level resolvers, for TENANT_RESOLVER dotted paths ───────────────


class UsernameOrgResolver(TenantResolver):
    """Test-only resolver: the org whose slug equals the signed-in user's username.

    A real per-request attribute (``bind()``'s job, or a Python attribute stapled onto
    ``request.user``) would not survive a fresh ``django.test.Client`` round trip, since
    each request reloads the user from the database through the session. Keying off a
    durable column instead is what lets every HTTP-level test below use one ordinary
    resolver.

    Returns the *instance*, which every resolver this package ships also does
    (``TenantResolver``'s own docstring lists it as the first of four legal answers).
    Used only by the dedicated reproduction below of the bug this uncovers — see
    ``UsernamePkResolver`` for the resolver every other HTTP-level test in this file
    uses instead, and the report for the writeup.
    """

    def resolve(self, request):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return TntOrg.objects.filter(slug=user.username).first()


class UsernamePkResolver(TenantResolver):
    """Same as :class:`UsernameOrgResolver`, but answers with the tenant's primary key.

    ``TenantResolver``'s own docstring says a resolver may "return the tenant instance,
    its primary key, ALL_TENANTS, or None" and treats the two as equivalent. They are
    not, currently, for a model scoped to *itself* (``TENANT_MODEL`` registered as its
    own panel, or reachable through a relation filter/FK dropdown, as
    ``TntWidget.org`` makes it here) — see
    ``TestTenantModelSelfScopingCrashesOnAnInstance`` for the isolated reproduction and
    the report for the full writeup. Every other HTTP-level test in this file uses this
    pk-returning resolver so that unrelated coverage is not collateral damage to a bug
    that is one file's own finding to report, not to route around silently.
    """

    def resolve(self, request):
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None
        return TntOrg.objects.filter(slug=user.username).values_list("pk", flat=True).first()


class RaisingResolver(TenantResolver):
    """A resolver that blows up, for the "a resolver that raises" requirement."""

    def resolve(self, request):
        raise RuntimeError("this resolver is broken")


class NonsenseResolver(TenantResolver):
    """A resolver that answers with something that is not a primary key at all."""

    def resolve(self, request):
        return "not-a-real-pk"


def function_resolver(request):
    """A bare function used as ``TENANT_RESOLVER`` — the "any callable" branch."""
    return TntOrg.objects.filter(slug="acme").first()


# ── HTTP fixtures ────────────────────────────────────────────────────────────

TENANT_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "vantage.tenancy.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

#: What a real project would have written after ``manage.py check`` named the gaps:
#: TntGadget's path is two hops, past what inference alone can see, and TntReference is
#: shared reference data. TntAmbiguous and TntOrphan are deliberately left undeclared —
#: they are the "forgot to configure it" fixtures, and the whole point is that they
#: must fail closed without anyone writing a line for them.
CONFIGURED_TENANT_SETTINGS = {
    "TENANT_MODEL": "testapp.TntOrg",
    "TENANT_RESOLVER": "tests.test_tenancy.UsernamePkResolver",
    "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
    "TENANT_EXEMPT": ["testapp.tntreference"],
}


@pytest.fixture
def http_tenancy(settings):
    """Full stack: middleware installed, resolver and declarations configured."""
    settings.VANTAGE = CONFIGURED_TENANT_SETTINGS
    settings.MIDDLEWARE = TENANT_MIDDLEWARE
    return CONFIGURED_TENANT_SETTINGS


@pytest.fixture
def orgs():
    return (
        TntOrg.objects.create(name="Acme", slug="acme"),
        TntOrg.objects.create(name="Globex", slug="globex"),
    )


@pytest.fixture
def tenants(orgs):
    """Two tenants, two staff users each named after their own org, and one row apiece
    on every scoped model — the fixture set the HTTP-level classes below share."""
    org_a, org_b = orgs
    alice = User.objects.create_user("acme", password="x", is_staff=True)
    bob = User.objects.create_user("globex", password="x", is_staff=True)
    for user in (alice, bob):
        grant(user, TntWidget, "view", "add", "change", "delete")
        grant(user, TntGadget, "view", "add", "change", "delete")
        grant(user, TntSecret, "view", "add", "change", "delete")
        grant(user, TntReference, "view", "add", "change", "delete")
    widget_a = TntWidget.objects.create(org=org_a, name="Widget A")
    widget_b = TntWidget.objects.create(org=org_b, name="Widget B")
    gadget_a = TntGadget.objects.create(widget=widget_a, name="Gadget A")
    gadget_b = TntGadget.objects.create(widget=widget_b, name="Gadget B")
    return SimpleNamespace(
        org_a=org_a,
        org_b=org_b,
        alice=alice,
        bob=bob,
        widget_a=widget_a,
        widget_b=widget_b,
        gadget_a=gadget_a,
        gadget_b=gadget_b,
    )


# ── scope inference ──────────────────────────────────────────────────────────


class TestScopeInference:
    """``scope_for``: declared, then inferred, then a refusal. Never a guess."""

    def test_a_single_foreign_key_is_inferred(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        scope = scope_for(TntWidget)
        assert scope.kind is ScopeKind.SCOPED
        assert scope.path == "org"

    def test_two_foreign_keys_cannot_be_inferred(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        scope = scope_for(TntAmbiguous)
        assert scope.kind is ScopeKind.UNRESOLVED
        assert "2 foreign keys" in scope.reason

    def test_no_foreign_key_cannot_be_inferred(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        scope = scope_for(TntOrphan)
        assert scope.kind is ScopeKind.UNRESOLVED
        assert "no foreign key" in scope.reason

    def test_a_multi_hop_relation_is_not_inferred_on_its_own(self, settings):
        """Inference is deliberately shallow: only a direct foreign key. A model owned
        through an intermediate one is UNRESOLVED until a project spells it out."""
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert scope_for(TntGadget).kind is ScopeKind.UNRESOLVED

    def test_a_declared_path_resolves_the_multi_hop_case(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        scope = scope_for(TntGadget)
        assert scope.kind is ScopeKind.SCOPED
        assert scope.path == "widget__org"

    def test_a_declared_global_wins_over_an_inferable_path(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntwidget": None},
        }
        assert scope_for(TntWidget).kind is ScopeKind.GLOBAL

    def test_an_invalid_declared_path_is_unresolved_not_raised(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntwidget": "not_a_real_field"},
        }
        scope = scope_for(TntWidget)
        assert scope.kind is ScopeKind.UNRESOLVED
        assert "not_a_real_field" in scope.reason

    def test_exempt_marks_only_the_named_model_as_global(self, settings):
        """Marking TntReference exempt must not quietly widen to TntOrphan too."""
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_EXEMPT": ["testapp.tntreference"],
        }
        assert scope_for(TntReference).kind is ScopeKind.GLOBAL
        assert scope_for(TntOrphan).kind is ScopeKind.UNRESOLVED

    def test_infrastructure_models_are_global_without_any_declaration(self, settings):
        from vantage.models import VantageLogEntry

        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert scope_for(VantageLogEntry).kind is ScopeKind.GLOBAL

    def test_the_tenant_model_scopes_to_itself_by_pk(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        scope = scope_for(TntOrg)
        assert scope.kind is ScopeKind.SCOPED
        assert scope.path == "pk"

    def test_tenancy_off_makes_every_model_global(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": None}
        assert enabled() is False
        assert scope_for(TntWidget).kind is ScopeKind.GLOBAL
        assert scope_for(TntOrphan).kind is ScopeKind.GLOBAL

    def test_an_unresolvable_tenant_model_label_is_treated_as_off(self, settings):
        """A typo in TENANT_MODEL is a check error (vantage.E061), not a 500 from
        inside a queryset filter — see TestChecks below for the check itself."""
        settings.VANTAGE = {"TENANT_MODEL": "testapp.NopeNotAModel"}
        assert enabled() is False


# ── fail closed on the read path ─────────────────────────────────────────────


class TestFailClosedReads:
    """``scope_queryset``: the four rows of the module's own design table."""

    def test_an_unresolved_model_denies_by_default(self, settings, orgs):
        org_a, _ = orgs
        TntOrphan.objects.create(name="x")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(org_a):
            assert list(scope_queryset(TntOrphan.objects.all())) == []

    def test_tenant_unscoped_allow_is_the_only_way_to_widen_it(self, settings, orgs):
        """The opt-in leak, proven to actually work as documented — not assumed."""
        org_a, _ = orgs
        TntOrphan.objects.create(name="x")
        TntOrphan.objects.create(name="y")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg", "TENANT_UNSCOPED": "allow"}
        with tenant_context(org_a):
            assert scope_queryset(TntOrphan.objects.all()).count() == 2

    def test_an_unresolvable_tenant_denies_a_scoped_model(self, settings, orgs):
        org_a, _ = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(None):
            assert list(scope_queryset(TntWidget.objects.all())) == []

    def test_all_tenants_bypasses_scoping(self, settings, orgs):
        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(ALL_TENANTS):
            assert scope_queryset(TntWidget.objects.all()).count() == 2

    def test_a_global_model_ignores_the_tenant_entirely(self, settings, orgs):
        TntReference.objects.create(label="shared")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_EXEMPT": ["testapp.tntreference"],
        }
        with tenant_context(None):  # even with no tenant resolvable at all
            assert scope_queryset(TntReference.objects.all()).count() == 1

    def test_scoping_never_runs_when_tenancy_is_off(self, settings, orgs):
        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": None}
        assert scope_queryset(TntWidget.objects.all()).count() == 2

    def test_a_refusal_is_logged_and_left_on_the_request(self, settings, orgs, caplog):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser())  # no resolvable tenant
        with caplog.at_level(logging.ERROR, logger="vantage.tenancy"):
            scope_queryset(TntWidget.objects.all(), request)
        assert getattr(request, tenancy.ERROR_ATTR)
        assert "No tenant could be determined" in caplog.text

    def test_an_unresolved_models_refusal_names_itself_in_the_log(self, settings, caplog):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser())
        with tenant_context(None):
            with caplog.at_level(logging.ERROR, logger="vantage.tenancy"):
                scope_queryset(TntOrphan.objects.all(), request)
        assert "testapp.TntOrphan is not tenant-scoped" in caplog.text


# ── resolvers ─────────────────────────────────────────────────────────────────


class TestUserAttributeResolver:
    def test_reads_the_configured_attribute(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser(tenant=org_a))
        assert UserAttributeResolver().resolve(request) is org_a

    def test_falls_back_to_the_attname_when_the_plain_attribute_is_absent(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser(tenant_id=org_a.pk))
        assert UserAttributeResolver().resolve(request) == org_a.pk

    def test_a_callable_attribute_is_called(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser(tenant=lambda: org_a))
        assert UserAttributeResolver().resolve(request) is org_a

    def test_an_anonymous_caller_resolves_to_none(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser(authenticated=False, tenant=object()))
        assert UserAttributeResolver().resolve(request) is None

    def test_no_user_at_all_resolves_to_none(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert UserAttributeResolver().resolve(FakeRequest(user=None)) is None

    def test_a_superuser_is_not_secretly_exempt(self, settings):
        """The module's own docstring: superusers are not exempt from resolution.
        A superuser with no ``tenant`` attribute resolves to nobody, same as anyone
        else — widening them is something a project has to opt into explicitly."""
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(user=FakeUser(is_superuser=True))
        assert UserAttributeResolver().resolve(request) is None


class TestSubdomainResolver:
    def test_matches_the_slug_from_the_host(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(host="acme.example.com")
        assert SubdomainResolver().resolve(request) == org_a

    def test_an_ignored_label_resolves_to_none(self, settings, orgs):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(host="www.example.com")
        assert SubdomainResolver().resolve(request) is None

    def test_an_unknown_subdomain_resolves_to_none(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(host="ghost.example.com")
        assert SubdomainResolver().resolve(request) is None

    def test_a_disallowed_host_resolves_to_none(self, settings):
        """get_host() raises for a Host header outside ALLOWED_HOSTS; the resolver must
        not fall back to reading the raw, unvalidated header instead."""
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert SubdomainResolver().resolve(FakeRequest(host=None)) is None

    def test_returns_none_when_tenancy_is_off(self, settings, orgs):
        settings.VANTAGE = {"TENANT_MODEL": None}
        assert SubdomainResolver().resolve(FakeRequest(host="acme.example.com")) is None


class TestHeaderResolver:
    def test_resolves_by_primary_key(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(headers={"X-Tenant": str(org_a.pk)})
        assert HeaderResolver().resolve(request) == org_a

    def test_a_missing_header_resolves_to_none(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert HeaderResolver().resolve(FakeRequest()) is None

    def test_an_unknown_id_resolves_to_none(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(headers={"X-Tenant": "999999"})
        assert HeaderResolver().resolve(request) is None

    def test_the_default_permitted_hook_trusts_any_header_value(self, settings, orgs):
        """Documents the danger the class's own docstring warns about: out of the box
        this resolver trusts the header verbatim, so any signed-in caller who can set
        it can claim any tenant. Safe only behind a proxy that sets the header itself
        and strips an inbound copy - see test below for the fix a project must apply."""
        _org_a, org_b = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        request = FakeRequest(headers={"X-Tenant": str(org_b.pk)})
        assert HeaderResolver().resolve(request) == org_b

    def test_a_subclass_can_restrict_which_tenants_may_be_claimed(self, settings, orgs):
        org_a, _org_b = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}

        class Locked(HeaderResolver):
            def permitted(self, request, tenant):
                return False

        request = FakeRequest(headers={"X-Tenant": str(org_a.pk)})
        assert Locked().resolve(request) is None


class TestCustomResolver:
    def test_a_bare_function_is_used_directly(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.function_resolver",
        }
        assert current(FakeRequest(user=FakeUser())) == org_a

    def test_a_resolver_that_raises_fails_closed_and_is_logged(self, settings, caplog):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.RaisingResolver",
        }
        request = FakeRequest(user=FakeUser())
        with caplog.at_level(logging.ERROR, logger="vantage.tenancy"):
            assert current(request) is None
        assert "resolver raised" in caplog.text

    def test_a_resolver_that_raises_denies_rows_rather_than_crashing_the_page(
        self, settings, orgs
    ):
        org_a, _ = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.RaisingResolver",
        }
        request = FakeRequest(user=FakeUser())
        assert list(scope_queryset(TntWidget.objects.all(), request)) == []

    def test_a_resolver_returning_a_value_that_is_not_a_key_fails_closed(self, settings, orgs):
        """A resolver is project code and can hand back anything.

        ``None`` always failed closed. A value that merely fails to *look* like a key
        used to reach the ORM and raise from inside the query — an error page, which is
        the one outcome this module is written to avoid, because an error page is
        something a retry might get past while an empty one is a decision. It now
        refuses the same way, and records why on the request.
        """
        org_a, _ = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.NonsenseResolver",
        }
        request = FakeRequest(user=FakeUser())
        assert list(scope_queryset(TntWidget.objects.all(), request)) == []
        assert "not a usable key" in getattr(request, tenancy.ERROR_ATTR, "")


# ── the write path ────────────────────────────────────────────────────────────


class TestEnforceWritePath:
    def test_stamps_the_tenant_on_a_create(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        obj = TntWidget(name="New")
        with tenant_context(org_a):
            enforce(None, obj)
        assert obj.org_id == org_a.pk

    def test_refuses_to_move_a_row_to_another_tenant(self, settings, orgs):
        org_a, org_b = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        widget = TntWidget.objects.create(org=org_a, name="Widget A")
        widget.org = org_b
        with tenant_context(org_a):
            with pytest.raises(TenantMismatch):
                enforce(None, widget)

    def test_requires_a_tenant_to_write_at_all(self, settings, orgs):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(None):
            with pytest.raises(TenantRequired):
                enforce(None, TntWidget(name="New"))

    def test_all_tenants_leaves_the_posted_value_alone(self, settings, orgs):
        """An operator acting across tenants: there is no single tenant to stamp, and
        inventing one would be worse than leaving whatever was posted untouched."""
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        obj = TntWidget(name="New")
        with tenant_context(ALL_TENANTS):
            enforce(None, obj)
        assert obj.org_id is None

    def test_the_tenant_model_itself_refuses_to_be_moved(self, settings, orgs):
        org_a, org_b = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(org_a):
            with pytest.raises(TenantMismatch):
                enforce(None, org_b)

    def test_the_tenant_model_itself_is_fine_when_it_matches(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        with tenant_context(org_a):
            enforce(None, org_a)  # no exception

    def test_an_unresolved_model_denies_the_write_by_default(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}  # TntOrphan undeclared
        with tenant_context(org_a):
            with pytest.raises(TenantRequired):
                enforce(None, TntOrphan(name="x"))

    def test_an_unresolved_model_is_let_through_when_configured_to_allow(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg", "TENANT_UNSCOPED": "allow"}
        obj = TntOrphan(name="x")
        with tenant_context(org_a):
            enforce(None, obj)  # no exception, and nothing to stamp it with either

    def test_a_global_model_is_untouched_even_with_no_tenant_at_all(self, settings, orgs):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_EXEMPT": ["testapp.tntreference"],
        }
        obj = TntReference(label="x")
        with tenant_context(None):
            enforce(None, obj)  # no exception
        assert obj.label == "x"

    def test_multi_hop_refuses_a_parent_from_another_tenant(self, settings, orgs):
        org_a, org_b = orgs
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        widget_b = TntWidget.objects.create(org=org_b, name="Widget B")
        with tenant_context(org_a):
            with pytest.raises(TenantMismatch):
                enforce(None, TntGadget(widget=widget_b, name="x"))

    def test_multi_hop_accepts_a_parent_from_the_same_tenant(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        widget_a = TntWidget.objects.create(org=org_a, name="Widget A")
        with tenant_context(org_a):
            enforce(None, TntGadget(widget=widget_a, name="x"))  # no exception

    def test_multi_hop_requires_the_parent_to_be_set(self, settings, orgs):
        org_a, _ = orgs
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        with tenant_context(org_a):
            with pytest.raises(TenantRequired):
                enforce(None, TntGadget(name="x"))


# ── scope_choices: the FK dropdown ───────────────────────────────────────────


class TestScopeChoices:
    def test_narrows_a_modelchoicefield_queryset(self, settings, orgs):
        from django import forms

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        field = forms.ModelChoiceField(queryset=TntWidget.objects.all())
        with tenant_context(org_a):
            scope_choices(field)
        assert [obj.org_id for obj in field.queryset] == [org_a.pk]

    def test_a_field_without_a_queryset_passes_through(self, settings, orgs):
        from django import forms

        org_a, _ = orgs
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        field = forms.CharField()
        with tenant_context(org_a):
            assert scope_choices(field) is field

    def test_a_none_field_is_a_no_op(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        assert scope_choices(None) is None

    def test_is_a_no_op_when_tenancy_is_off(self, settings, orgs):
        from django import forms

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": None}
        field = forms.ModelChoiceField(queryset=TntWidget.objects.all())
        scope_choices(field)
        assert field.queryset.count() == 2


# ── composition onto the permission layer ────────────────────────────────────


class TestComposesOntoPermissions:
    def test_wrapping_twice_does_not_double_filter_or_break(self, settings, orgs):
        from vantage.permissions import PanelPermissions

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        permissions = enforce_on(enforce_on(PanelPermissions()))
        request = FakeRequest(user=FakeUser())
        tenancy.bind(request, org_a)
        result = permissions.filter_queryset(request, TntWidget.objects.all())
        assert result.count() == 1

    def test_a_subclass_that_forgets_super_is_still_scoped(self, settings, orgs):
        """The module's central claim: enforce_on wraps the bound *instance*, so even a
        PanelPermissions subclass that overrides filter_queryset without calling
        super() cannot accidentally unscope every model in the panel."""
        from vantage.permissions import PanelPermissions

        class Forgetful(PanelPermissions):
            def filter_queryset(self, request, queryset, action="view"):
                return queryset  # the mistake this composition exists to survive

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        permissions = enforce_on(Forgetful())
        request = FakeRequest(user=FakeUser())
        tenancy.bind(request, org_a)
        result = permissions.filter_queryset(request, TntWidget.objects.all())
        assert result.count() == 1


# ── HTTP: every read path ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("http_tenancy")
class TestHttpReadPaths:
    def test_the_list_page_shows_only_this_tenants_rows(self, tenants):
        body = client_as(tenants.alice).get(list_url(TntWidget)).content.decode()
        assert tenants.widget_a.name in body
        assert tenants.widget_b.name not in body

    def test_another_tenants_row_is_a_404_not_a_403(self, tenants):
        """This package's rule: a row you may not see does not exist. Answering 403
        would confirm that it does."""
        assert client_as(tenants.alice).get(detail_url(tenants.widget_b)).status_code == 404

    def test_the_same_row_is_reachable_by_its_own_tenant(self, tenants):
        assert client_as(tenants.bob).get(detail_url(tenants.widget_b)).status_code == 200

    def test_a_missing_permission_is_still_403_the_contrast_that_matters(self, tenants):
        """A model this user has no permission for at all is refused with 403 - which
        is what makes the tenancy 404 above a deliberate answer, not an accident of
        every refusal reading the same."""
        stranger = User.objects.create_user("acme-stranger", password="x", is_staff=True)
        assert client_as(stranger).get(detail_url(tenants.widget_a)).status_code == 403

    def test_the_edit_page_for_another_tenants_row_is_a_404(self, tenants):
        assert client_as(tenants.alice).get(change_url(tenants.widget_b)).status_code == 404

    def test_the_delete_page_for_another_tenants_row_is_a_404(self, tenants):
        assert client_as(tenants.alice).get(delete_url(tenants.widget_b)).status_code == 404

    def test_autocomplete_never_offers_another_tenants_rows(self, tenants):
        response = client_as(tenants.alice).get(
            reverse("vantage:autocomplete"), {"model": "testapp.TntWidget", "q": ""}
        )
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()["results"]}
        assert str(tenants.widget_a.pk) in ids
        assert str(tenants.widget_b.pk) not in ids

    def test_export_never_includes_another_tenants_rows(self, tenants):
        response = client_as(tenants.alice).get(list_url(TntWidget), {"export": "csv"})
        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode()
        assert tenants.widget_a.name in body
        assert tenants.widget_b.name not in body

    def test_the_add_forms_relation_dropdown_never_offers_another_tenants_rows(self, tenants):
        """An FK dropdown offering another tenant's records is a leak on its own terms,
        even though saving it would separately be refused."""
        response = client_as(tenants.alice).get(add_url(TntGadget))
        assert response.status_code == 200
        choices = {obj.pk for obj in response.context["form"].fields["widget"].queryset}
        assert choices == {tenants.widget_a.pk}

    def test_an_exempt_models_list_page_shows_every_tenants_rows(self, tenants):
        TntReference.objects.create(label="Shared A")
        TntReference.objects.create(label="Shared B")
        body = client_as(tenants.alice).get(list_url(TntReference)).content.decode()
        assert "Shared A" in body
        assert "Shared B" in body


# ── HTTP: every write path ────────────────────────────────────────────────────


@pytest.mark.usefixtures("http_tenancy")
class TestHttpWritePaths:
    def test_create_stamps_the_tenant(self, tenants):
        response = client_as(tenants.alice).post(
            add_url(TntWidget), {"name": "New Widget", "org": tenants.org_a.pk}
        )
        assert response.status_code == 302
        assert TntWidget.objects.get(name="New Widget").org_id == tenants.org_a.pk

    def test_create_stamps_the_tenant_even_when_the_field_is_never_on_the_form(self, tenants):
        """TntSecret.org is editable=False, so it is never rendered - this is
        vantage.tenancy.enforce doing the whole job with no dropdown to help it."""
        response = client_as(tenants.alice).post(add_url(TntSecret), {"label": "shh"})
        assert response.status_code == 302
        assert TntSecret.objects.get(label="shh").org_id == tenants.org_a.pk

    def test_posting_another_tenants_org_on_create_does_not_create_it_there(self, tenants):
        """Try it, do not assume: the dropdown only offers org_a, so org_b's pk is not
        a valid choice and the form must refuse it rather than silently accept it."""
        response = client_as(tenants.alice).post(
            add_url(TntWidget), {"name": "Sneaky", "org": tenants.org_b.pk}
        )
        assert response.status_code == 200  # redisplayed with an error, not a redirect
        assert not TntWidget.objects.filter(name="Sneaky").exists()

    def test_editing_a_row_cannot_move_it_to_another_tenant_by_posting_a_different_fk(
        self, tenants
    ):
        response = client_as(tenants.alice).post(
            change_url(tenants.widget_a),
            {"name": tenants.widget_a.name, "org": tenants.org_b.pk},
        )
        assert response.status_code != 302  # not accepted as a successful save
        tenants.widget_a.refresh_from_db()
        assert tenants.widget_a.org_id == tenants.org_a.pk

    def test_editing_another_tenants_row_directly_is_a_404_and_it_survives(self, tenants):
        response = client_as(tenants.alice).post(
            change_url(tenants.widget_b), {"name": "hijacked"}
        )
        assert response.status_code == 404
        tenants.widget_b.refresh_from_db()
        assert tenants.widget_b.name != "hijacked"

    def test_deleting_another_tenants_row_is_a_404_and_it_survives(self, tenants):
        response = client_as(tenants.alice).post(delete_url(tenants.widget_b))
        assert response.status_code == 404
        assert TntWidget.objects.filter(pk=tenants.widget_b.pk).exists()


# ── HTTP: bulk actions and "select all" ───────────────────────────────────────


@pytest.mark.usefixtures("http_tenancy")
class TestBulkActionsRespectTenancy:
    def test_select_all_never_reaches_another_tenants_rows(self, tenants):
        client_as(tenants.alice).post(list_url(TntWidget), {"action": "publish", "select_all": "1"})
        tenants.widget_a.refresh_from_db()
        tenants.widget_b.refresh_from_db()
        assert tenants.widget_a.is_published is True
        assert tenants.widget_b.is_published is False

    def test_naming_another_tenants_row_explicitly_has_no_effect(self, tenants):
        """The predicate is resolved against the already tenant-scoped queryset, so a
        pk typed straight into the selection does not reach a row outside it."""
        client_as(tenants.alice).post(
            list_url(TntWidget),
            {"action": "publish", "selected": [str(tenants.widget_b.pk)]},
        )
        tenants.widget_b.refresh_from_db()
        assert tenants.widget_b.is_published is False


# ── HTTP: the JSON API ────────────────────────────────────────────────────────
#
# ``vantage.api`` composes onto ``get_permissions().filter_queryset()`` the same as
# every other read path (its own module docstring: "Nothing here re-decides who may
# see or change a row"), so nothing in this module should be a special case — this
# section exists to prove that claim rather than assume it, and to check the one
# thing flagged as the same shape as a real, fixed leak: the API's ``?fields=``
# parameter used to be split on commas and walked with ``resolve_path``, which knows
# Django's ``_meta`` graph but nothing about the registry, permissions, or tenancy, so
# ``?fields=number,customer__api_token`` returned a live token through a plain
# relation hop. See ``TestRequestedFieldsAreBounded`` in ``tests/test_api.py`` for the
# fix. The allowlist it was given — the panel's own ``list_display`` plus its model's
# own direct fields, never a ``__`` walk into a related model — closes the same hop
# for a tenant-scoped model's data, and the test below tries it rather than assuming
# the fix generalises.


def api_list_url(model) -> str:
    opts = model._meta
    return reverse(
        "vantage:api-list", kwargs={"app_label": opts.app_label, "model_name": opts.model_name}
    )


def api_detail_url(obj) -> str:
    opts = type(obj)._meta
    return reverse(
        "vantage:api-detail",
        kwargs={"app_label": opts.app_label, "model_name": opts.model_name, "pk": obj.pk},
    )


def api_body(response) -> dict:
    return json.loads(response.content)


def post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type="application/json", **extra)


def patch_json(client, url, data, **extra):
    return client.patch(url, data=json.dumps(data), content_type="application/json", **extra)


@pytest.mark.usefixtures("http_tenancy")
class TestJsonApiRespectsTenancy:
    def test_the_list_endpoint_shows_only_this_tenants_rows(self, tenants):
        data = api_body(client_as(tenants.alice).get(api_list_url(TntWidget)))
        pks = {row["pk"] for row in data["results"]}
        assert tenants.widget_a.pk in pks
        assert tenants.widget_b.pk not in pks

    def test_another_tenants_row_is_a_404_not_a_403_through_the_api(self, tenants):
        client = client_as(tenants.alice)
        assert client.get(api_detail_url(tenants.widget_b)).status_code == 404
        assert client.get(api_detail_url(tenants.widget_a)).status_code == 200

    def test_a_multi_hop_models_list_endpoint_only_shows_this_tenants_rows(self, tenants):
        data = api_body(client_as(tenants.alice).get(api_list_url(TntGadget)))
        pks = {row["pk"] for row in data["results"]}
        assert tenants.gadget_a.pk in pks
        assert tenants.gadget_b.pk not in pks

    def test_an_exempt_models_list_endpoint_shows_every_tenants_rows(self, tenants):
        TntReference.objects.create(label="Shared A")
        TntReference.objects.create(label="Shared B")
        data = api_body(client_as(tenants.alice).get(api_list_url(TntReference)))
        assert {row["display"] for row in data["results"]} == {"Shared A", "Shared B"}

    def test_an_unresolved_models_list_endpoint_is_empty_not_an_error(self, tenants):
        """TntOrphan is undeclared in CONFIGURED_TENANT_SETTINGS, so it fails closed
        (an empty page, per the module's own design table) rather than 500ing or
        showing every tenant's rows just because the request reached it over HTTP."""
        TntOrphan.objects.create(name="x")
        grant(tenants.alice, TntOrphan, "view")
        response = client_as(tenants.alice).get(api_list_url(TntOrphan))
        assert response.status_code == 200
        assert api_body(response)["results"] == []

    def test_create_through_the_api_stamps_the_tenant(self, tenants):
        response = post_json(
            client_as(tenants.alice),
            api_list_url(TntWidget),
            {"fields": {"name": "New Widget", "org": tenants.org_a.pk}},
        )
        assert response.status_code == 201
        assert TntWidget.objects.get(name="New Widget").org_id == tenants.org_a.pk

    def test_create_through_the_api_stamps_the_tenant_even_when_the_field_is_never_on_the_form(
        self, tenants
    ):
        """TntSecret.org is editable=False, so it is never an editable API field either
        - enforce() alone does the whole job, with no submitted value to help it."""
        response = post_json(
            client_as(tenants.alice), api_list_url(TntSecret), {"fields": {"label": "shh"}}
        )
        assert response.status_code == 201
        assert TntSecret.objects.get(label="shh").org_id == tenants.org_a.pk

    def test_posting_another_tenants_org_through_the_api_create_is_refused(self, tenants):
        """Try it, do not assume: the field's choices are narrowed to org_a by the same
        formfield_callback the HTML form uses, so org_b's pk is not a valid choice."""
        response = post_json(
            client_as(tenants.alice),
            api_list_url(TntWidget),
            {"fields": {"name": "Sneaky", "org": tenants.org_b.pk}},
        )
        assert response.status_code == 422
        assert "org" in api_body(response)["errors"]
        assert not TntWidget.objects.filter(name="Sneaky").exists()

    def test_updating_through_the_api_cannot_move_a_row_to_another_tenant(self, tenants):
        response = patch_json(
            client_as(tenants.alice),
            api_detail_url(tenants.widget_a),
            {"fields": {"org": tenants.org_b.pk}},
        )
        assert response.status_code == 422
        tenants.widget_a.refresh_from_db()
        assert tenants.widget_a.org_id == tenants.org_a.pk

    def test_editing_another_tenants_row_through_the_api_is_a_404_and_it_survives(self, tenants):
        response = patch_json(
            client_as(tenants.alice),
            api_detail_url(tenants.widget_b),
            {"fields": {"name": "hijacked"}},
        )
        assert response.status_code == 404
        tenants.widget_b.refresh_from_db()
        assert tenants.widget_b.name != "hijacked"

    def test_deleting_another_tenants_row_through_the_api_is_a_404_and_it_survives(self, tenants):
        response = client_as(tenants.alice).delete(api_detail_url(tenants.widget_b))
        assert response.status_code == 404
        assert TntWidget.objects.filter(pk=tenants.widget_b.pk).exists()

    def test_fields_cannot_walk_a_relation_hop_off_a_tenant_scoped_model(self, tenants):
        """The exposure the ?fields= fix closed generically, tried here directly: even
        though TntWidget has a real foreign key to walk, the allowlist only contains
        its own list_display and direct field names, never a __ path, so this is
        refused with 400 rather than handed back org's own columns unfiltered."""
        response = client_as(tenants.alice).get(
            api_list_url(TntWidget), {"fields": "name,org__slug"}
        )
        assert response.status_code == 400
        assert "org__slug" in api_body(response)["detail"]


# ── the CSV importer ───────────────────────────────────────────────────────────


class TestImportRespectsTenancy:
    def test_a_create_import_stamps_the_tenant_when_the_column_is_omittable(
        self, settings, orgs
    ):
        """TntSecret.org is editable=False, so it never becomes an importable column
        at all (importable_fields drops it, same as it drops it from every form) - the
        importer's "this column is required" guard therefore never fires on it, and
        vantage.tenancy.enforce is free to stamp it exactly as it does on a web create.
        See the test below for a required, mapped column, which does not work this way."""
        from vantage import importer, site

        org_a, _ = orgs
        User.objects.create_user("acme", password="x")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
        }
        panel = site.get_panel(TntSecret)
        request = RequestFactory().post("/")
        request.user = SimpleNamespace(username="acme", is_authenticated=True)

        parsed = importer.read_csv(as_csv([["Label"], ["shh"]]))
        options = importer.importable_fields(panel)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            importer.infer_mapping(parsed.headers, options),
            key_field=None,
            mode="create",
        )
        result = importer.commit(panel, parsed, mapping, request, None)
        assert result.created == 1
        assert TntSecret.objects.get(label="shh").org_id == org_a.pk

    def test_a_required_tenant_owning_column_left_unmapped_is_wrongly_rejected(
        self, settings, orgs
    ):
        """KNOWN GAP, reported rather than patched (tenancy.py and importer.py are not
        this task's to change): the web create form and the JSON API both simply leave
        a tenant-scoped foreign key off the form and let vantage.tenancy.enforce stamp
        it - see TestHttpWritePaths.test_create_stamps_the_tenant_even_when_the_field_
        is_never_on_the_form for TntSecret's editable=False version of exactly that. A
        CSV import relying on the same convention for an *editable* required foreign
        key is refused outright instead: importer._missing_required does not know
        enforce() will supply the value, its guard only steps aside for a panel's own
        save_model hook (importer.py's own comment: "stamping the acting user onto an
        author field is the canonical example"), and tenancy's stamp is not that hook.
        The whole file is rejected with "must have a value" rather than created with
        the tenant filled in, even though the exact same create through the HTML form
        or the JSON API succeeds. See the written report for the full writeup."""
        from vantage import importer, site

        User.objects.create_user("acme", password="x")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
        }
        panel = site.get_panel(TntWidget)
        request = RequestFactory().post("/")
        request.user = SimpleNamespace(username="acme", is_authenticated=True)

        parsed = importer.read_csv(as_csv([["Name"], ["Widget C"]]))
        options = importer.importable_fields(panel)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            importer.infer_mapping(parsed.headers, options),
            key_field=None,
            mode="create",
        )
        assert mapping.missing_required  # confirms tenancy.py did not exempt "org"
        result = importer.commit(panel, parsed, mapping, request, None)
        assert result.created == 0
        assert result.failed == 1
        assert not TntWidget.objects.filter(name="Widget C").exists()

    def test_a_row_cannot_reach_another_tenants_parent_by_name(self, settings, orgs):
        """Export writes a related object as its __str__, so import accepts a label -
        but the lookup is scoped, so a row cannot name a parent belonging to another
        tenant into existence: it reports "no match", exactly like a row that does not
        exist for this user should."""
        from vantage import importer, site

        org_a, org_b = orgs
        User.objects.create_user("acme", password="x")
        widget_b = TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        panel = site.get_panel(TntGadget)
        request = RequestFactory().post("/")
        request.user = SimpleNamespace(username="acme", is_authenticated=True)

        parsed = importer.read_csv(as_csv([["Name", "Widget"], ["Gadget X", widget_b.name]]))
        options = importer.importable_fields(panel)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            importer.infer_mapping(parsed.headers, options),
            key_field=None,
            mode="create",
        )
        result = importer.commit(panel, parsed, mapping, request, None)
        assert result.created == 0
        assert result.failed == 1
        assert not TntGadget.objects.filter(name="Gadget X").exists()
        assert org_a  # the org whose tenant the importing user acted as


# ── saved views ────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("http_tenancy")
class TestSavedViewsCannotWidenAccess:
    def test_applying_another_tenants_shared_view_still_only_shows_your_own_rows(self, tenants):
        """SavedView's own docstring: "a view is a stored query string, and the rows it
        returns are filtered ... on every request regardless of who saved it." Proven
        here across the one boundary that would make it matter: a view shared by a
        user in a *different* tenant."""
        from vantage.models import SavedView

        view = SavedView.objects.create(
            model_label="testapp.tntwidget",
            name="Everything",
            owner=tenants.bob,
            visibility=SavedView.Visibility.SHARED,
            state={},
        )
        client = client_as(tenants.alice)
        applied = client.get(reverse("vantage:saved-view-apply", args=[view.pk]))
        assert applied.status_code == 302
        body = client.get(applied["Location"]).content.decode()
        assert tenants.widget_a.name in body
        assert tenants.widget_b.name not in body


# ── stats, aggregates and the dashboard ─────────────────────────────────────────


class TestStatsAndDashboardAreScoped:
    def test_an_aggregate_over_the_scoped_queryset_only_counts_this_tenants_rows(
        self, settings, orgs
    ):
        from vantage.permissions import get_permissions
        from vantage.stats import Stat, compute_panel

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        TntWidget.objects.create(org=org_b, name="Widget B2")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
        }
        request = FakeRequest(user=FakeUser(username="acme"))
        queryset = get_permissions().filter_queryset(request, TntWidget.objects.all(), "view")
        panel = compute_panel(queryset, stats=[Stat(label="Widgets", value=Count("pk"))])
        assert panel["stats"][0]["value"] == 1

    def test_a_dashboard_stat_card_only_counts_this_tenants_rows(self, settings, orgs):
        """StatCard.get_queryset composes onto get_permissions().filter_queryset, the
        same choke point everything else in this file goes through - proven directly
        against the widget rather than the (capped) DefaultDashboard selection, which
        this fixture set is too far down the alphabet to land in."""
        from vantage.dashboards import StatCard

        org_a, org_b = orgs
        TntWidget.objects.create(org=org_a, name="Widget A")
        TntWidget.objects.create(org=org_b, name="Widget B")
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
        }
        request = FakeRequest(user=FakeUser(username="acme"))
        card = StatCard("Widgets", TntWidget.objects.all(), model=TntWidget)
        result = card.render(request)
        assert result.stat["value"] == 1


# ── manage.py check ───────────────────────────────────────────────────────────


class TestChecksNameEveryGap:
    def test_an_unresolved_registered_model_is_an_error_by_default(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        ids = {p.obj: p.id for p in check_tenancy(None) if getattr(p, "obj", None) in (
            TntOrphan, TntAmbiguous
        )}
        assert ids[TntOrphan] == "vantage.E060"
        assert ids[TntAmbiguous] == "vantage.E060"

    def test_the_same_gap_is_only_a_warning_when_configured_to_allow(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg", "TENANT_UNSCOPED": "allow"}
        ids = {p.obj: p.id for p in check_tenancy(None) if getattr(p, "obj", None) is TntOrphan}
        assert ids[TntOrphan] == "vantage.W060"

    def test_declaring_the_scope_clears_the_error(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntgadget": "widget__org"},
        }
        problems = [p for p in check_tenancy(None) if getattr(p, "obj", None) is TntGadget]
        assert problems == []

    def test_exempting_one_model_does_not_exempt_the_others(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_EXEMPT": ["testapp.tntreference"],
        }
        by_model = {
            p.obj: p.id
            for p in check_tenancy(None)
            if getattr(p, "obj", None) in (TntOrphan, TntReference)
        }
        assert TntReference not in by_model
        assert by_model[TntOrphan] == "vantage.E060"

    def test_missing_middleware_is_reported(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        settings.MIDDLEWARE = ["django.contrib.auth.middleware.AuthenticationMiddleware"]
        assert any(p.id == "vantage.E065" for p in check_tenancy(None))

    def test_middleware_present_clears_that_check(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg"}
        settings.MIDDLEWARE = TENANT_MIDDLEWARE
        assert not any(p.id == "vantage.E065" for p in check_tenancy(None))

    def test_a_bad_resolver_path_is_reported(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg", "TENANT_RESOLVER": "nope.NotAModule"}
        assert any(p.id == "vantage.E062" for p in check_tenancy(None))

    def test_a_bad_declared_path_is_reported(self, settings):
        settings.VANTAGE = {
            "TENANT_MODEL": "testapp.TntOrg",
            "TENANT_SCOPE": {"testapp.tntwidget": "not_a_real_field"},
        }
        assert any(p.id == "vantage.E064" for p in check_tenancy(None))

    def test_an_unknown_tenant_model_label_is_reported_and_nothing_else_runs(self, settings):
        settings.VANTAGE = {"TENANT_MODEL": "testapp.NopeNotAModel"}
        assert [p.id for p in check_tenancy(None)] == ["vantage.E061"]


# ── a known bug: the tenant model scoped to itself, given an instance ─────────


@pytest.fixture
def http_tenancy_with_instance_resolver(settings):
    """Same shape as ``http_tenancy``, but with the resolver every one of this
    package's own resolvers is shaped like: it answers with the tenant *instance*."""
    settings.VANTAGE = {
        "TENANT_MODEL": "testapp.TntOrg",
        "TENANT_RESOLVER": "tests.test_tenancy.UsernameOrgResolver",
    }
    settings.MIDDLEWARE = TENANT_MIDDLEWARE


@pytest.mark.usefixtures("http_tenancy_with_instance_resolver")
class TestTheTenantModelScopesToItself:
    """The tenant model's own page, which used to 500 for everyone using the defaults.

    ``scope_for()`` resolves the tenant model's own scope to ``path="pk"`` (see
    ``TestScopeInference.test_the_tenant_model_scopes_to_itself_by_pk``). ``pk`` is not
    a relation, and Django's ORM only accepts a model instance on the right-hand side
    of a *relation* lookup — so ``filter(pk=<instance>)`` was a ``TypeError``.

    That made it reachable through ordinary use rather than only by a direct call:
    every resolver the package ships (``UserAttributeResolver``, ``SubdomainResolver``,
    ``HeaderResolver``) looks the tenant up and returns the instance, so any project on
    one of them crashed the moment the tenant model's own list page was opened, or a
    relation filter, an FK dropdown or autocomplete pointed *at* the tenant model —
    each of those being ``scope_queryset(TenantModel.objects.all())`` underneath.

    ``scope_queryset`` now unwraps to the key before filtering, exactly as ``enforce``
    already did before comparing.
    """

    def test_scope_queryset_returns_the_single_row(self, orgs):
        org_a, _org_b = orgs
        request = FakeRequest(user=FakeUser(username=org_a.slug))
        assert list(scope_queryset(TntOrg.objects.all(), request)) == [org_a]

    def test_the_tenant_models_own_list_page_shows_one_row(self, orgs):
        # "acme" matches org_a's slug, established by the ``orgs`` fixture.
        org_a, org_b = orgs
        user = User.objects.create_user("acme", password="x", is_staff=True)
        grant(user, TntOrg, "view")
        body = client_as(user).get(list_url(TntOrg)).content.decode()
        assert org_a.slug in body
        assert org_b.slug not in body
