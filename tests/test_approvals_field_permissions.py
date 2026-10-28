"""Approvals and field permissions together: who sees a request, and who may decide it.

The two features meet wherever a change request names a field. The rules, which the
tests are grouped by:

* A field **hidden** from someone is never named or shown on an approvals screen — the
  queue, the review page, the record's banner, the API — and neither are the note and
  the reason written about it. The same goes for a requester whose role has since
  hidden a field they asked about.
* A field that is **read-only** for someone cannot be asked for at all: every write path
  refuses it before the approval hold sees it, so the answer is "not editable", never
  "sent for approval".
* **Deciding needs write access to every field in the request.** An approver a field is
  hidden from, or read-only for, cannot approve or reject it, and it is not counted as
  waiting for them.
* Approving applies the whole request or nothing.

``Order`` is the fixture model: ``status`` and ``total`` need approval. Rita may change
orders; Abe may approve them with no field rules. Hal approves through a role that hides
``total``; Rory approves through a role that makes ``status`` read-only.
"""

import csv
import io
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.urls import reverse

from tests.testapp.models import Author, Order
from vantage import approvals, fieldperms, history, importer, reports
from vantage.models import (
    ChangeRequest,
    FieldPermission,
    ReportRun,
    ReportSubscription,
    SavedView,
    VantageLogEntry,
)
from vantage.sites import site

pytestmark = pytest.mark.django_db

QUEUE_URL = "vantage:page-approvals"
REVIEW_URL = "vantage:approval-review"

#: A total nobody would type by accident, so finding it in a page means it leaked.
SECRET_TOTAL = "98765.43"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def panel(monkeypatch):
    panel = site.get_panel(Order)
    monkeypatch.setattr(panel, "requires_approval", ["status", "total"], raising=False)
    monkeypatch.setattr(panel, "list_display", ["number", "status", "total"], raising=False)
    approvals.ensure_permissions()
    return panel


@pytest.fixture
def order(db):
    customer = Author.objects.create(name="Ada", email="ada@example.com")
    return Order.objects.create(
        number="A-1", customer=customer, status="pending", total=Decimal("10.00")
    )


def codenames(*names):
    ct = ContentType.objects.get_for_model(Order)
    return Permission.objects.filter(content_type=ct, codename__in=names)


def role(name, *perms, **levels):
    group = Group.objects.create(name=name)
    group.permissions.add(*codenames(*perms))
    ct = ContentType.objects.get_for_model(Order)
    for field_name, level in levels.items():
        FieldPermission.objects.create(
            group=group, content_type=ct, field_name=field_name, level=level
        )
    return group


def member(username, *groups, perms=()):
    user = User.objects.create_user(username, f"{username}@example.com", "x", is_staff=True)
    user.groups.add(*groups)
    user.user_permissions.add(*codenames(*perms))
    return User.objects.get(pk=user.pk)


APPROVER = ("view_order", "change_order", "approve_order")
EDITOR = ("view_order", "change_order", "add_order")


@pytest.fixture
def rita(panel):
    return member("rita", perms=EDITOR)


@pytest.fixture
def abe(panel):
    return member("abe", perms=APPROVER)


@pytest.fixture
def hal(panel):
    """An approver who cannot see ``total``."""
    return member("hal", role("Hidden total approvers", *APPROVER, total="hidden"))


@pytest.fixture
def rory(panel):
    """An approver who may see ``status`` but not change it."""
    return member("rory", role("Read-only status approvers", *APPROVER, status="readonly"))


@pytest.fixture
def rhonda(panel):
    """An editor who may see ``status`` but not change it."""
    return member("rhonda", role("Read-only status editors", *EDITOR, status="readonly"))


def login(client, user):
    # A fresh session each time, so one person's flash messages are not read as another's.
    client.logout()
    client.cookies.clear()
    client.force_login(user)
    return client


def edit(client, order, **values):
    data = {
        "number": order.number,
        "customer": order.customer_id,
        "status": order.status,
        "total": str(order.total),
        **values,
    }
    return client.post(reverse("vantage:testapp-order-change", args=[order.pk]), data)


def ask(client, user, order, note="", **values):
    login(client, user)
    response = edit(client, order, _approval_note=note, **values)
    assert response.status_code == 302, response.content
    return ChangeRequest.objects.latest("pk")


def decide(client, change_request, decision, **extra):
    return client.post(
        reverse(REVIEW_URL, args=[change_request.pk]), {"decision": decision, **extra}
    )


def texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def badge(response):
    nav = [item for group in response.context["nav"] for item in group.items]
    return next(item for item in nav if item.key == "page:approvals").badge


def request_for(user):
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    return request


# ── 1. a hidden field is not shown on any approvals screen ──────────────────


class TestAHiddenField:
    def test_is_not_waiting_for_an_approver_it_is_hidden_from(self, client, rita, hal, order):
        ask(client, rita, order, total=SECRET_TOTAL)
        login(client, hal)
        page = client.get(reverse(QUEUE_URL))
        assert page.context["waiting"] == []
        assert badge(page) is None
        assert SECRET_TOTAL not in page.content.decode()

    def test_the_review_screen_shows_neither_its_name_nor_its_values(
        self, client, rita, hal, order
    ):
        change_request = ask(
            client, rita, order, note=f"Make it {SECRET_TOTAL}", total=SECRET_TOTAL
        )
        login(client, hal)
        response = client.get(reverse(REVIEW_URL, args=[change_request.pk]))
        assert response.status_code == 200
        html = response.content.decode()
        assert SECRET_TOTAL not in html and "10.00" not in html
        assert "Total" not in html
        assert "1 field you cannot see" in html
        assert 'value="approve"' not in html and 'value="reject"' not in html
        assert "you cannot approve or reject it" in html

    def test_an_approver_it_is_hidden_from_cannot_decide(self, client, rita, hal, order):
        change_request = ask(client, rita, order, total=SECRET_TOTAL)
        login(client, hal)
        response = decide(client, change_request, "approve")
        assert any("field you cannot change" in text for text in texts(response))
        response = decide(client, change_request, "reject", reason="no")
        assert any("field you cannot change" in text for text in texts(response))
        change_request.refresh_from_db()
        order.refresh_from_db()
        assert change_request.is_pending and order.total == Decimal("10.00")

    def test_a_mixed_request_shows_the_visible_field_and_counts_the_rest(
        self, client, rita, hal, order
    ):
        change_request = ask(client, rita, order, status="paid", total=SECRET_TOTAL)
        rows = approvals.rows(site.get_panel(Order), change_request, order, request_for(hal))
        assert [row["name"] for row in rows] == ["status", ""]
        assert rows[-1]["hidden"] and SECRET_TOTAL not in json.dumps(rows)
        # Still not theirs to decide: approving would change the hidden total too.
        assert not approvals.may_decide(
            request_for(hal), site.get_panel(Order), order, change_request
        )

    def test_a_request_that_leaves_it_alone_is_theirs_to_decide(self, client, rita, hal, order):
        change_request = ask(client, rita, order, status="paid")
        login(client, hal)
        page = client.get(reverse(QUEUE_URL))
        assert badge(page) == "1"
        decide(client, change_request, "approve")
        order.refresh_from_db()
        assert order.status == "paid"

    def test_the_record_banner_does_not_show_it(self, client, rita, hal, order, monkeypatch):
        ask(client, rita, order, note=f"Make it {SECRET_TOTAL}", total=SECRET_TOTAL)
        login(client, hal)
        html = client.get(reverse("vantage:testapp-order-detail", args=[order.pk])).content
        html = html.decode()
        banner = html[html.index("v-approval-banner") :]
        assert SECRET_TOTAL not in html
        assert "Total" not in banner[: banner.index("</section>")]
        assert "1 field you cannot see" in banner

    def test_the_requesters_own_view_follows_the_same_rule(self, client, rita, abe, order):
        change_request = ask(client, rita, order, note=SECRET_TOTAL, total=SECRET_TOTAL)
        # Since asking, Rita has been put in a role that hides the total.
        rita.groups.add(role("Hidden total editors", *EDITOR, total="hidden"))
        login(client, User.objects.get(pk=rita.pk))
        for url in (reverse(QUEUE_URL), reverse(REVIEW_URL, args=[change_request.pk])):
            html = client.get(url).content.decode()
            assert SECRET_TOTAL not in html, url
            assert "1 field you cannot see" in html, url
        # Withdrawing is still hers to do.
        decide(client, change_request, "withdraw")
        change_request.refresh_from_db()
        assert change_request.status == "withdrawn"

    def test_the_api_never_puts_it_in_a_pending_object(self, client, panel, order):
        editor = member("hed", role("Hidden total editors", *EDITOR, total="hidden"))
        login(client, editor)
        url = reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "order", "pk": order.pk},
        )
        refused = client.patch(
            url,
            data=json.dumps({"fields": {"total": SECRET_TOTAL}}),
            content_type="application/json",
        )
        assert refused.status_code == 400
        held = client.patch(
            url, data=json.dumps({"fields": {"status": "paid"}}), content_type="application/json"
        )
        assert held.status_code == 202
        assert set(held.json()["pending"]["requested"]) == {"status"}
        assert ChangeRequest.objects.get().field_names == ["status"]


# ── 2. a read-only field is refused, not sent for approval ──────────────────


class TestAReadOnlyFieldForTheRequester:
    def test_the_edit_page_does_not_say_it_will_wait(self, client, rhonda, order):
        login(client, rhonda)
        page = client.get(reverse("vantage:testapp-order-change", args=[order.pk]))
        assert page.context["approval_banner"]["held_names"] == ["total"]

    def test_a_forged_form_value_is_ignored_not_held(self, client, rhonda, order):
        login(client, rhonda)
        response = edit(client, order, status="paid")
        assert response.status_code == 302
        assert not any("sent for approval" in text for text in texts(response))
        assert not ChangeRequest.objects.exists()
        order.refresh_from_db()
        assert order.status == "pending"

    def test_an_inline_cell_is_dropped_not_held(self, client, rhonda, order, panel, monkeypatch):
        monkeypatch.setattr(panel, "list_editable", ["status"], raising=False)
        monkeypatch.setattr(panel, "list_link", "total", raising=False)
        login(client, rhonda)
        response = client.patch(
            reverse("vantage:testapp-order-inline"),
            data=json.dumps({"changes": [{"pk": order.pk, "fields": {"status": "shipped"}}]}),
            content_type="application/json",
        )
        assert not response.json().get("pending")
        assert not ChangeRequest.objects.exists()

    def test_the_api_answers_400_not_202(self, client, rhonda, order):
        login(client, rhonda)
        response = client.patch(
            reverse(
                "vantage:api-detail",
                kwargs={"app_label": "testapp", "model_name": "order", "pk": order.pk},
            ),
            data=json.dumps({"fields": {"status": "paid"}}),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "read-only" in response.json()["detail"]
        assert not ChangeRequest.objects.exists()

    def test_the_schema_does_not_flag_it_as_needing_approval(self, client, rhonda):
        login(client, rhonda)
        schema = client.get(reverse("vantage:api-schema")).json()
        fields = {f["name"]: f for f in schema["models"]["testapp.order"]["fields"]}
        assert fields["status"]["readonly"] and not fields["status"]["needs_approval"]
        assert fields["total"]["needs_approval"]

    def test_it_is_not_an_import_target(self, rhonda, panel):
        from vantage.permissions import get_permissions

        options = importer.importable_fields(panel, request_for(rhonda), get_permissions())
        names = {option.name for option in options}
        assert "status" not in names and "total" in names

    def test_a_forged_history_restore_is_dropped_not_held(self, client, rhonda, order, panel):
        boss = User.objects.create_superuser("boss", "boss@example.com", "x")
        login(client, boss)
        edit(client, order, status="paid")
        edit(client, Order.objects.get(pk=order.pk), status="shipped")
        order.refresh_from_db()
        rhonda.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="vantage", codename="view_vantagelogentry"
            )
        )
        login(client, User.objects.get(pk=rhonda.pk))
        entries, _ = history.timeline(order)
        position = history.position_from({"entry": str(entries[0].pk)}, entries)
        result = history.reconstruct(
            panel, order, position, entries=entries, request=request_for(rhonda)
        )
        response = client.post(
            reverse("vantage:testapp-order-history", args=[order.pk]),
            {
                "entry": entries[0].pk,
                "fingerprint": history.fingerprint(result),
                "fields": ["status"],
            },
        )
        assert not any("sent for approval" in text for text in texts(response))
        assert not ChangeRequest.objects.exists()
        order.refresh_from_db()
        assert order.status == "shipped"

    def test_a_board_move_is_refused_not_held(self, client, rhonda, order):
        login(client, rhonda)
        response = client.post(
            reverse("vantage:testapp-order-kanban-move"),
            data=json.dumps({"pk": order.pk, "value": "paid", "field": "status"}),
            content_type="application/json",
        )
        assert not response.json().get("ok")
        assert not ChangeRequest.objects.exists()


# ── 3. an approver needs write access to every field in the request ─────────


class TestAReadOnlyFieldForTheApprover:
    def test_it_is_not_waiting_for_them(self, client, rita, rory, order):
        ask(client, rita, order, status="paid")
        login(client, rory)
        page = client.get(reverse(QUEUE_URL))
        assert page.context["waiting"] == [] and badge(page) is None

    def test_they_see_it_but_cannot_approve_it(self, client, rita, rory, order):
        change_request = ask(client, rita, order, status="paid")
        login(client, rory)
        html = client.get(reverse(REVIEW_URL, args=[change_request.pk])).content.decode()
        assert "Status" in html and 'value="approve"' not in html
        response = decide(client, change_request, "approve")
        assert any("field you cannot change" in text for text in texts(response))
        order.refresh_from_db()
        assert order.status == "pending"
        assert ChangeRequest.objects.get().is_pending

    def test_a_request_on_fields_they_may_write_is_theirs(self, client, rita, rory, order):
        change_request = ask(client, rita, order, total="12.50")
        login(client, rory)
        decide(client, change_request, "approve")
        order.refresh_from_db()
        assert order.total == Decimal("12.50")


# ── 4. approving applies all of it, or none of it ───────────────────────────


class TestApplyingWhole:
    def test_an_unrestricted_approver_applies_every_field(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid", total="12.50")
        login(client, abe)
        decide(client, change_request, "approve")
        order.refresh_from_db()
        assert (order.status, order.total) == ("paid", Decimal("12.50"))

    def test_approve_refuses_whole_rather_than_dropping_a_field(
        self, client, rita, rory, panel, order
    ):
        change_request = ask(client, rita, order, status="paid", total="12.50")
        outcome = approvals.approve(request_for(rory), panel, order, change_request)
        assert not outcome.ok
        assert "cannot change" in outcome.message
        order.refresh_from_db()
        change_request.refresh_from_db()
        assert (order.status, order.total) == ("pending", Decimal("10.00"))
        assert change_request.is_pending


# ── 5. the roles matrix ─────────────────────────────────────────────────────


def test_the_roles_matrix_shows_approve_and_field_rules_on_one_row(client, panel):
    group = role("Finance", *APPROVER, status="readonly", total="hidden")
    login(client, User.objects.create_superuser("boss", "boss@example.com", "x"))
    response = client.get(reverse("vantage:role-edit", args=[group.pk]))
    assert response.status_code == 200
    html = response.content.decode()
    ct = ContentType.objects.get_for_model(Order).pk
    approve = Permission.objects.get(content_type_id=ct, codename="approve_order")
    assert f'value="{approve.pk}"' in html
    assert f'value="{ct}:total:hidden" selected' in html
    assert f'value="{ct}:status:readonly" selected' in html


# ── 6. scheduled reports ────────────────────────────────────────────────────


def test_a_report_leaves_out_a_column_hidden_from_its_subscriber(panel, order, mailoutbox):
    user = member("sam", role("Hidden total viewers", "view_order", total="hidden"))
    view = SavedView.objects.create(
        model_label="testapp.order",
        name="Orders",
        owner=user,
        state={"version": 1, "filters": {}},
    )
    order.total = Decimal(SECRET_TOTAL)
    order.save()
    from django.utils import timezone

    sub = ReportSubscription.objects.create(
        view=view,
        user=user,
        frequency="daily",
        send_time=timezone.now().time(),
        origin="https://panel.example.com",
        next_run_at=timezone.now(),
    )
    run = reports.run_report(ReportRun.objects.create(subscription=sub).pk)
    assert run.status == ReportRun.Status.SENT, run.error or run.reason
    _name, content, _mimetype = mailoutbox[0].attachments[0]
    text = content if isinstance(content, str) else content.decode()
    rows = list(csv.reader(io.StringIO(text)))
    assert "Number" in rows[0] and "Total" not in rows[0]
    assert SECRET_TOTAL not in text


# ── 7. history and the audit log ────────────────────────────────────────────


class TestTheLog:
    @pytest.fixture
    def logged(self, client, rita, abe, order):
        """One request rejected, one withdrawn, one approved: every entry an approval writes."""
        rejected = ask(client, rita, order, status="paid", total=SECRET_TOTAL)
        login(client, abe)
        decide(client, rejected, "reject", reason=f"not {SECRET_TOTAL}")
        withdrawn = ask(client, rita, order, total=SECRET_TOTAL)
        decide(login(client, rita), withdrawn, "withdraw")
        approved = ask(client, rita, order, status="paid", total=SECRET_TOTAL)
        decide(login(client, abe), approved, "approve")
        order.refresh_from_db()
        assert order.total == Decimal(SECRET_TOTAL)
        return order

    def reader(self, hal):
        hal.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="vantage", codename="view_vantagelogentry"
            )
        )
        return User.objects.get(pk=hal.pk)

    def test_action_names_drop_the_hidden_field(self, hal, logged):
        entries = fieldperms.hide_entry_fields(
            request_for(hal), VantageLogEntry.objects.filter(action_name__contains="(#")
        )
        names = [entry.action_name for entry in entries]
        assert len(names) == 6
        assert all("total" not in name for name in names), names
        assert any(
            name.startswith("requested approval of status, 1 hidden field (#") for name in names
        )
        assert any(
            name.startswith("withdrew request to change 1 hidden field (#") for name in names
        )
        # The stored entry is untouched; only the copy that was read is redacted.
        assert VantageLogEntry.objects.filter(action_name__contains="status, total").exists()

    def test_the_history_page_shows_no_hidden_name_or_value(self, client, hal, logged):
        login(client, self.reader(hal))
        html = client.get(reverse("vantage:testapp-order-history", args=[logged.pk])).content
        html = html.decode()
        assert SECRET_TOTAL not in html
        assert "status, total" not in html and "change to total" not in html
        assert "hidden field" in html

    def test_the_audit_page_shows_no_hidden_name_or_value(self, client, hal, logged):
        login(client, self.reader(hal))
        html = client.get(reverse("vantage:audit")).content.decode()
        assert SECRET_TOTAL not in html
        assert "status, total" not in html and "change to total" not in html

    def test_someone_who_may_see_it_still_reads_it_all(self, abe, logged):
        entries = fieldperms.hide_entry_fields(
            request_for(abe), VantageLogEntry.objects.filter(action_name__contains="(#")
        )
        assert any("status, total" in entry.action_name for entry in entries)


class TestRedactActionName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (
                "requested approval of price, cost (#3)",
                "requested approval of price, 1 hidden field (#3)",
            ),
            ("approved change to cost (#3)", "approved change to 1 hidden field (#3)"),
            ("rejected change to a, cost, b (#9)", "rejected change to a, b, 1 hidden field (#9)"),
            ("withdrew request to change price (#3)", "withdrew request to change price (#3)"),
            ("revealed cost on", "revealed cost on"),
            ("", ""),
        ],
    )
    def test_only_approval_entries_and_only_hidden_names(self, name, expected):
        assert approvals.redact_action_name(name, {"cost"}) == expected
