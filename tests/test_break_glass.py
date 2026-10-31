"""Break glass: time-boxed elevated access.

The security properties first, each asserted where it can fail: an expired or revoked
grant gives nothing (with no job run in between), a granter cannot hand out more than
they hold, a reason and a duration cap are enforced, every step is audited, and the
JSON API, field rules and tenancy all see an elevation exactly the way the pages do.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, Order
from vantage import breakglass
from vantage.models import ElevatedAccess, FieldPermission, VantageLogEntry
from vantage.permissions import get_permissions

pytestmark = pytest.mark.django_db

REASON = "Incident 42: refund stuck orders"
PAGE = "/admin/_/elevated-access/"


def perm(label: str) -> Permission:
    app_label, codename = label.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


def req(user):
    request = RequestFactory().get("/")
    request.user = user
    request.session = {}
    return request


def fresh(user):
    """The user as a new request would load them: no permission caches."""
    return User.objects.get(pk=user.pk)


def can(user, action="change", model=Order) -> bool:
    return get_permissions().has_model_perm(req(fresh(user)), model, action)


def entries(name: str):
    return VantageLogEntry.objects.filter(action_name=name)


@pytest.fixture
def admin(db):
    return User.objects.create_superuser("root", "root@example.com", None)


@pytest.fixture
def staff(db):
    return User.objects.create_user("ada", "ada@example.com", None, is_staff=True)


@pytest.fixture
def order_admins(db):
    group = Group.objects.create(name="Order admins")
    group.permissions.add(
        *(perm(f"testapp.{verb}_order") for verb in ("view", "add", "change", "delete"))
    )
    return group


@pytest.fixture
def lead(db):
    """A granter who is not a superuser, and holds only view and change on orders."""
    user = User.objects.create_user("lead", "lead@example.com", None, is_staff=True)
    user.user_permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
    return fresh(user)


def give(actor, user, *, role=None, permissions=(), minutes=30):
    return breakglass.grant(
        req(actor), user=user, role=role, permissions=permissions, minutes=minutes, reason=REASON
    )


def age(grant, *, by: timedelta) -> None:
    """Move a grant's window into the past, as the clock would."""
    ElevatedAccess.objects.filter(pk=grant.pk).update(
        starts_at=grant.starts_at - by, expires_at=grant.expires_at - by
    )


class TestEffectivePermissions:
    def test_an_active_role_grant_gives_the_roles_permissions(self, admin, staff, order_admins):
        assert not can(staff)
        give(admin, staff, role=order_admins)
        assert can(staff, "change") and can(staff, "delete")

    def test_explicit_permissions_give_exactly_those(self, admin, staff):
        give(admin, staff, permissions=["testapp.view_order"])
        assert can(staff, "view")
        assert not can(staff, "change")

    def test_an_expired_grant_gives_nothing_without_any_job(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins, minutes=10)
        age(grant, by=timedelta(minutes=11))
        assert not can(staff)

    def test_a_revoked_grant_gives_nothing(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins)
        breakglass.revoke(req(admin), grant)
        assert not can(staff)

    def test_a_pending_request_gives_nothing(self, staff, order_admins):
        request = req(staff)
        breakglass.request_elevation(request, role=order_admins, minutes=10, reason=REASON)
        assert not can(staff)

    def test_a_scheduled_grant_gives_nothing_until_it_starts(self, admin, staff, order_admins):
        breakglass.grant(
            req(admin),
            user=staff,
            role=order_admins,
            minutes=10,
            reason=REASON,
            starts_at=timezone.now() + timedelta(minutes=5),
        )
        assert not can(staff)

    def test_never_confers_superuser(self, admin, staff, order_admins):
        give(admin, staff, role=order_admins)
        user = fresh(staff)
        assert not user.is_superuser
        assert not can(staff, "view", Author)

    def test_an_inactive_user_gets_nothing(self, admin, staff, order_admins):
        give(admin, staff, role=order_admins)
        User.objects.filter(pk=staff.pk).update(is_active=False)
        assert not can(staff)

    def test_cached_per_request_only(self, admin, staff, order_admins, django_assert_num_queries):
        give(admin, staff, role=order_admins)
        request = req(fresh(staff))
        resolver = get_permissions()
        assert resolver.has_model_perm(request, Order, "change")
        with django_assert_num_queries(0):
            assert resolver.has_model_perm(request, Order, "change")
        ElevatedAccess.objects.update(revoked_at=timezone.now())
        # The same request keeps its answer; the next one asks again.
        assert not resolver.has_model_perm(req(fresh(staff)), Order, "change")

    def test_a_role_grant_lifts_field_rules_but_never_narrows(self, admin, staff):
        support = Group.objects.create(name="Support")
        support.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        ct = ContentType.objects.get_for_model(Order)
        FieldPermission.objects.create(
            group=support, content_type=ct, field_name="total", level="hidden"
        )
        staff.groups.add(support)
        finance = Group.objects.create(name="Finance")
        finance.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        FieldPermission.objects.create(
            group=finance, content_type=ct, field_name="status", level="hidden"
        )
        resolver = get_permissions()
        assert "total" in resolver.field_access(req(fresh(staff)), Order).hidden
        give(admin, staff, role=finance)
        answer = resolver.field_access(req(fresh(staff)), Order)
        assert "total" not in answer.hidden
        assert "status" not in answer.hidden  # visible before, still visible


class TestGranting:
    def test_reason_is_required(self, admin, staff, order_admins):
        for reason in ("", "   ", "fix"):
            with pytest.raises(ValidationError):
                breakglass.grant(
                    req(admin), user=staff, role=order_admins, minutes=5, reason=reason
                )
        assert not ElevatedAccess.objects.exists()

    def test_duration_is_capped(self, admin, staff, order_admins, settings):
        settings.VANTAGE = {"BREAK_GLASS_MAX_MINUTES": 15}
        with pytest.raises(ValidationError):
            give(admin, staff, role=order_admins, minutes=16)
        with pytest.raises(ValidationError):
            give(admin, staff, role=order_admins, minutes=0)
        grant = give(admin, staff, role=order_admins, minutes=15)
        assert grant.expires_at - grant.starts_at == timedelta(minutes=15)

    def test_approval_clamps_to_the_cap_in_force(self, admin, staff, order_admins, settings):
        request = req(staff)
        pending = breakglass.request_elevation(
            request, role=order_admins, minutes=60, reason=REASON
        )
        settings.VANTAGE = {"BREAK_GLASS_MAX_MINUTES": 5}
        breakglass.approve(req(admin), pending)
        assert pending.expires_at - pending.starts_at == timedelta(minutes=5)

    def test_cannot_grant_beyond_own_permissions(self, lead, staff, order_admins, settings):
        settings.VANTAGE = {"BREAK_GLASS_PERMISSION": "testapp.change_order"}
        with pytest.raises(PermissionDenied):
            give(lead, staff, role=order_admins)  # the role includes delete
        with pytest.raises(PermissionDenied):
            give(lead, staff, permissions=["testapp.delete_order"])
        give(lead, staff, permissions=["testapp.view_order"])
        assert can(staff, "view") and not can(staff, "delete")

    def test_elevation_does_not_count_towards_granting(self, admin, lead, staff, settings):
        settings.VANTAGE = {"BREAK_GLASS_PERMISSION": "testapp.change_order"}
        give(admin, lead, permissions=["testapp.delete_order"])
        with pytest.raises(PermissionDenied):
            give(fresh(lead), staff, permissions=["testapp.delete_order"])

    def test_only_superusers_by_default(self, lead, staff):
        with pytest.raises(PermissionDenied):
            give(lead, staff, permissions=["testapp.view_order"])

    def test_cannot_approve_own_request_without_self_service(self, admin, order_admins):
        other = User.objects.create_superuser("root2", "r2@example.com", None)
        lead = User.objects.create_user("on-call", is_staff=True)
        pending = breakglass.request_elevation(
            req(lead), role=order_admins, minutes=5, reason=REASON
        )
        assert not breakglass.may_approve(req(lead), pending)
        assert breakglass.may_approve(req(other), pending)

    def test_self_service_activates_own_request(self, order_admins, settings):
        settings.VANTAGE = {
            "BREAK_GLASS_PERMISSION": "testapp.view_order",
            "BREAK_GLASS_SELF_SERVICE": True,
        }
        on_call = User.objects.create_user("on-call", is_staff=True)
        on_call.user_permissions.add(perm("testapp.view_order"))
        grant = breakglass.request_elevation(
            req(fresh(on_call)), role=order_admins, minutes=5, reason=REASON
        )
        assert grant.status == "active"
        assert can(on_call, "delete")

    def test_self_service_off_leaves_request_pending(self, order_admins, settings):
        settings.VANTAGE = {"BREAK_GLASS_PERMISSION": "testapp.view_order"}
        on_call = User.objects.create_user("on-call", is_staff=True)
        on_call.user_permissions.add(perm("testapp.view_order"))
        grant = breakglass.request_elevation(
            req(fresh(on_call)), role=order_admins, minutes=5, reason=REASON
        )
        assert grant.status == "pending"

    def test_superusers_and_disabled_accounts_are_refused(self, admin, order_admins):
        other = User.objects.create_superuser("root2", "r2@example.com", None)
        with pytest.raises(ValidationError):
            give(admin, other, role=order_admins)
        gone = User.objects.create_user("gone", is_staff=True, is_active=False)
        with pytest.raises(ValidationError):
            give(admin, gone, role=order_admins)

    def test_unknown_permissions_are_an_error(self, admin, staff):
        with pytest.raises(ValidationError):
            give(admin, staff, permissions=["testapp.fly_order"])
        with pytest.raises(ValidationError):
            give(admin, staff)


class TestAudit:
    def test_grant_activation_use_and_revocation(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins)
        assert entries("break-glass.granted").count() == 1
        assert entries("break-glass.activated").count() == 1
        assert can(staff, "delete")
        assert can(staff, "delete")
        used = entries("break-glass.used")
        assert used.count() == 1
        assert "testapp.delete_order" in json.dumps(used.get().changes)
        breakglass.revoke(req(admin), grant, reason="Incident closed")
        entry = entries("break-glass.revoked").get()
        assert entry.actor == admin
        granted = entries("break-glass.granted").get()
        assert REASON in json.dumps(granted.changes)

    def test_expiry_is_recorded_once_when_observed(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins, minutes=5)
        age(grant, by=timedelta(minutes=6))
        assert not can(staff)
        assert not can(staff)
        breakglass.observe_all()
        assert entries("break-glass.expired").count() == 1

    def test_list_screen_records_expiry_nobody_observed(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins, minutes=5)
        age(grant, by=timedelta(minutes=6))
        client = Client()
        client.force_login(admin)
        assert client.get(PAGE).status_code == 200
        assert entries("break-glass.expired").count() == 1

    def test_request_and_drop(self, admin, staff, order_admins):
        pending = breakglass.request_elevation(
            req(staff), role=order_admins, minutes=5, reason=REASON
        )
        assert entries("break-glass.requested").count() == 1
        breakglass.approve(req(admin), pending)
        breakglass.revoke(req(fresh(staff)), pending)
        assert entries("break-glass.dropped").count() == 1


class TestScreens:
    def client(self, user) -> Client:
        client = Client(enforce_csrf_checks=False)
        client.force_login(user)
        return client

    def test_banner_shows_while_elevated_and_drop_ends_it(self, admin, staff, order_admins):
        give(admin, staff, role=order_admins, minutes=30)
        client = self.client(staff)
        page = client.get("/admin/").content.decode()
        assert "v-elevation-banner" in page
        assert "Order admins" in page
        assert "30 minutes left" in page
        response = client.post(reverse("vantage:break-glass-drop"), {"next": "/admin/"})
        assert response.status_code == 302 and response["Location"] == "/admin/"
        assert "v-elevation-banner" not in client.get("/admin/").content.decode()
        assert not can(staff)

    def test_no_banner_without_elevation(self, staff):
        assert "v-elevation-banner" not in self.client(staff).get("/admin/").content.decode()

    def test_drop_refuses_an_offsite_next(self, admin, staff, order_admins):
        give(admin, staff, role=order_admins)
        response = self.client(staff).post(
            reverse("vantage:break-glass-drop"), {"next": "https://evil.example/"}
        )
        assert response["Location"] == PAGE

    def test_request_grant_approve_revoke_over_http(self, admin, staff, order_admins):
        client = self.client(staff)
        assert client.get(PAGE).status_code == 200
        client.post(
            reverse("vantage:break-glass-request"),
            {"role": order_admins.pk, "minutes": "10", "reason": REASON},
        )
        pending = ElevatedAccess.objects.get()
        assert pending.status == "pending"
        # The requester cannot approve their own request.
        assert (
            client.post(reverse("vantage:break-glass-approve", args=[pending.pk])).status_code
            == 403
        )
        boss = self.client(admin)
        assert "Approve" in boss.get(PAGE).content.decode()
        boss.post(reverse("vantage:break-glass-approve", args=[pending.pk]))
        pending.refresh_from_db()
        assert pending.status == "active"
        boss.post(reverse("vantage:break-glass-revoke", args=[pending.pk]), {"reason": "done"})
        pending.refresh_from_db()
        assert pending.status == "revoked"

    def test_grant_form_requires_a_granter(self, staff, order_admins):
        response = self.client(staff).post(
            reverse("vantage:break-glass-grant"),
            {"username": "ada", "role": order_admins.pk, "minutes": "5", "reason": REASON},
        )
        assert response.status_code == 403
        assert not ElevatedAccess.objects.exists()

    def test_grant_form_reports_a_short_reason(self, admin, staff, order_admins):
        response = self.client(admin).post(
            reverse("vantage:break-glass-grant"),
            {"username": "ada", "role": order_admins.pk, "minutes": "5", "reason": "x"},
            follow=True,
        )
        assert "at least" in response.content.decode()
        assert not ElevatedAccess.objects.exists()

    def test_mutations_are_post_only_and_csrf_protected(self, admin, staff, order_admins):
        grant = give(admin, staff, role=order_admins)
        url = reverse("vantage:break-glass-revoke", args=[grant.pk])
        assert self.client(admin).get(url).status_code == 405
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(admin)
        assert strict.post(url).status_code == 403
        grant.refresh_from_db()
        assert grant.revoked_at is None

    def test_a_non_granter_sees_only_their_own(self, admin, staff, order_admins):
        other = User.objects.create_user("zelda-the-other", is_staff=True)
        give(admin, other, role=order_admins)
        page = self.client(staff).get(PAGE).content.decode()
        assert "zelda-the-other" not in page


class TestApiAndTenancy:
    def test_api_honours_elevation_and_its_expiry(self, admin, staff, order_admins):
        author = Author.objects.create(name="A")
        Order.objects.create(number="N1", customer=author, total=Decimal("1"))
        client = Client()
        client.force_login(staff)
        url = "/admin/_/api/testapp/order/"
        assert client.get(url).status_code == 403
        grant = give(admin, staff, role=order_admins)
        response = client.get(url)
        assert response.status_code == 200
        assert len(response.json()["results"]) == 1
        age(grant, by=timedelta(hours=2))
        assert client.get(url).status_code == 403

    def test_elevation_does_not_cross_tenants(self, admin, settings):
        from tests.test_tenancy import CONFIGURED_TENANT_SETTINGS, TENANT_MIDDLEWARE
        from tests.testapp.models import TntOrg, TntWidget

        settings.VANTAGE = CONFIGURED_TENANT_SETTINGS
        settings.MIDDLEWARE = TENANT_MIDDLEWARE
        acme = TntOrg.objects.create(name="Acme", slug="acme")
        globex = TntOrg.objects.create(name="Globex", slug="globex")
        TntWidget.objects.create(org=acme, name="Widget A")
        TntWidget.objects.create(org=globex, name="Widget B")
        alice = User.objects.create_user("acme", is_staff=True)
        give(admin, alice, permissions=["testapp.view_tntwidget"])
        client = Client()
        client.force_login(alice)
        rows = client.get("/admin/_/api/testapp/tntwidget/").json()["results"]
        assert len(rows) == 1
        assert "Widget B" not in json.dumps(rows)


class TestCommand:
    def test_grant_list_revoke(self, staff, order_admins, capsys):
        call_command(
            "vantage_break_glass",
            "grant",
            "ada",
            "--role",
            "Order admins",
            "--minutes",
            "5",
            "--reason",
            REASON,
        )
        grant = ElevatedAccess.objects.get()
        assert grant.status == "active" and can(staff, "delete")
        call_command("vantage_break_glass", "list")
        assert f"#{grant.pk}" in capsys.readouterr().out
        call_command("vantage_break_glass", "revoke", str(grant.pk), "--reason", "done")
        assert not can(staff, "delete")
        assert entries("break-glass.revoked").count() == 1

    def test_reason_and_cap_still_apply(self, staff, order_admins):
        with pytest.raises(CommandError):
            call_command(
                "vantage_break_glass", "grant", "ada", "--role", "Order admins",
                "--minutes", "5", "--reason", "x",
            )  # fmt: skip
        with pytest.raises(CommandError):
            call_command(
                "vantage_break_glass", "grant", "ada", "--role", "Order admins",
                "--minutes", "500", "--reason", REASON,
            )  # fmt: skip
        assert not ElevatedAccess.objects.exists()
