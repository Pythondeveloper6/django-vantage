"""Approval workflows: ``requires_approval``, the queue, and every path that writes.

Grouped by promise. The weight is on the paths, because the feature is only as good as
the one that forgets it: a change that needs approval must not reach the record — or
the webhook, or the audit diff — from the edit form, the grid, the board, the JSON API,
the importer or a bulk edit, unless someone allowed to approve it does so.

``Order`` is the fixture model: ``status`` and ``total`` need approval, ``number`` does
not. Rita may change orders; Abe may also approve changes to them.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.checks import Error
from django.test import override_settings
from django.urls import resolve, reverse

from tests.testapp.models import Author, Order, Post
from vantage import approvals, importer, records, webhooks
from vantage.models import ChangeRequest, VantageLogEntry, Webhook, WebhookDelivery
from vantage.panel_checks import ModelPanelChecks
from vantage.sensitive import MASK
from vantage.sites import site

pytestmark = pytest.mark.django_db

QUEUE_URL = "vantage:page-approvals"
REVIEW_URL = "vantage:approval-review"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def fresh_resolver():
    from vantage.permissions import _reset

    _reset()
    yield
    _reset()


@pytest.fixture
def panel(monkeypatch):
    """The Order panel, with status and total guarded and the grid switched on."""
    panel = site.get_panel(Order)
    monkeypatch.setattr(panel, "requires_approval", ["status", "total"], raising=False)
    monkeypatch.setattr(panel, "list_display", ["number", "status", "total"], raising=False)
    monkeypatch.setattr(panel, "list_editable", ["number", "status"], raising=False)
    approvals.ensure_permissions()
    return panel


@pytest.fixture
def order(db):
    customer = Author.objects.create(name="Ada", email="ada@example.com")
    return Order.objects.create(
        number="A-1", customer=customer, status="pending", total=Decimal("10.00")
    )


def person(username, *codenames, model=Order):
    user = User.objects.create_user(username, f"{username}@example.com", "x", is_staff=True)
    ct = ContentType.objects.get_for_model(model)
    user.user_permissions.add(*Permission.objects.filter(content_type=ct, codename__in=codenames))
    return User.objects.get(pk=user.pk)


@pytest.fixture
def rita(panel):
    return person("rita", "view_order", "change_order", "add_order")


@pytest.fixture
def abe(panel):
    return person("abe", "view_order", "change_order", "approve_order")


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


def as_user(client, user):
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


def ask(client, rita, order, **values):
    """Rita asks for a change through the edit page. Returns the request."""
    as_user(client, rita)
    response = edit(client, order, **values)
    assert response.status_code == 302, response.content
    return ChangeRequest.objects.latest("pk")


def decide(client, change_request, decision, **extra):
    return client.post(
        reverse(REVIEW_URL, args=[change_request.pk]), {"decision": decision, **extra}
    )


def texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


# ── the edit page ───────────────────────────────────────────────────────────


class TestTheEditPage:
    def test_a_guarded_change_waits_and_the_rest_of_the_form_saves(self, client, rita, order):
        as_user(client, rita)
        response = edit(client, order, number="A-2", status="paid", _approval_note="Paid by wire")
        assert response.status_code == 302

        order.refresh_from_db()
        assert order.number == "A-2"  # not guarded: saved
        assert order.status == "pending"  # guarded: still the old value

        change_request = ChangeRequest.objects.get()
        assert change_request.is_pending
        assert change_request.requested_by == rita
        assert change_request.note == "Paid by wire"
        assert change_request.changes["status"]["from"] == "pending"
        assert change_request.changes["status"]["to"] == "paid"
        assert change_request.changes["status"]["to_display"] == "Paid"
        assert list(change_request.changes) == ["status"]  # total did not change

        said = " ".join(texts(response))
        assert "Saved changes" in said
        assert "sent for approval" in said

    def test_a_guarded_change_alone_is_not_reported_as_saved(self, client, rita, order):
        as_user(client, rita)
        response = edit(client, order, total="99.00")
        said = texts(response)
        assert not any("Saved changes" in text for text in said)
        assert any("Total" in text and "sent for approval" in text for text in said)
        order.refresh_from_db()
        assert order.total == Decimal("10.00")

    def test_an_edit_that_leaves_guarded_fields_alone_makes_no_request(self, client, rita, order):
        as_user(client, rita)
        edit(client, order, number="A-9")
        assert not ChangeRequest.objects.exists()

    def test_creating_a_record_is_not_held(self, client, rita, order):
        """There is no approved value to protect yet; the add permission is the gate."""
        as_user(client, rita)
        response = client.post(
            reverse("vantage:testapp-order-add"),
            {"number": "N-1", "customer": order.customer_id, "status": "paid", "total": "5"},
        )
        assert response.status_code == 302
        assert Order.objects.get(number="N-1").status == "paid"
        assert not ChangeRequest.objects.exists()

    def test_an_approver_and_a_superuser_change_it_directly(self, client, abe, boss, order):
        as_user(client, abe)
        edit(client, order, status="paid")
        order.refresh_from_db()
        assert order.status == "paid"

        as_user(client, boss)
        edit(client, order, status="shipped")
        order.refresh_from_db()
        assert order.status == "shipped"
        assert not ChangeRequest.objects.exists()

    def test_the_page_says_which_fields_will_wait_before_anything_is_typed(
        self, client, rita, abe, order
    ):
        as_user(client, rita)
        page = client.get(reverse("vantage:testapp-order-change", args=[order.pk]))
        html = page.content.decode()
        assert "need approval" in html and "Status" in html and "Total" in html
        assert f'name="{approvals.NOTE_FIELD}"' in html

        as_user(client, abe)
        html = client.get(reverse("vantage:testapp-order-change", args=[order.pk])).content
        assert approvals.NOTE_FIELD.encode() not in html

    def test_the_record_shows_what_is_waiting(self, client, rita, order):
        ask(client, rita, order, status="paid")
        html = client.get(reverse("vantage:testapp-order-detail", args=[order.pk])).content
        html = html.decode()
        assert "waiting for approval" in html
        assert "Paid" in html


# ── nothing announced early ─────────────────────────────────────────────────


class TestTheAuditAndWebhooks:
    @pytest.fixture
    def sent(self, monkeypatch):
        handed = []
        monkeypatch.setattr(webhooks, "enqueue", lambda delivery_id: handed.append(delivery_id))
        Webhook.objects.create(name="Shop", url="https://hooks.example.com/in")
        return handed

    def test_the_edits_own_entry_and_webhook_leave_the_held_field_out(
        self, client, rita, order, sent
    ):
        ask(client, rita, order, number="A-2", status="paid")
        update = VantageLogEntry.objects.get(action="update", object_id=str(order.pk))
        assert set(update.changes) == {"number"}
        delivery = WebhookDelivery.objects.get()
        assert set(delivery.payload["changes"]) == {"number"}

        requested = VantageLogEntry.objects.get(action="action", object_id=str(order.pk))
        assert requested.action_name.startswith("requested approval of status")
        # No from/to diff on the request's entry, so history never replays it.
        assert requested.changes == {}

    def test_a_held_change_alone_fires_no_updated_webhook(self, client, rita, order, sent):
        ask(client, rita, order, status="paid")
        assert not WebhookDelivery.objects.exists()
        assert not VantageLogEntry.objects.filter(action="update").exists()

    def test_approving_fires_updated_with_the_change(self, client, rita, abe, order, sent):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        decide(client, change_request, "approve")
        delivery = WebhookDelivery.objects.get()
        assert delivery.event == "updated"
        assert delivery.payload["changes"]["status"] == {"from": "pending", "to": "paid"}
        entry = VantageLogEntry.objects.get(action="update")
        assert entry.actor == abe
        assert entry.action_name == f"approved change to status (#{change_request.pk})"


# ── deciding ────────────────────────────────────────────────────────────────


class TestApproving:
    def test_approving_applies_the_change_through_the_save(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid", total="12.50")
        as_user(client, abe)
        response = decide(client, change_request, "approve")
        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == "paid" and order.total == Decimal("12.50")
        change_request.refresh_from_db()
        assert change_request.status == "approved"
        assert change_request.decided_by == abe and change_request.decided_at

    def test_the_panels_save_model_hook_runs_on_approval(
        self, client, rita, abe, order, monkeypatch, panel
    ):
        seen = []
        monkeypatch.setattr(
            panel,
            "save_model",
            lambda request, obj, form, change: (seen.append(obj.status), obj.save()),
            raising=False,
        )
        change_request = ask(client, rita, order, status="paid")
        assert seen == ["pending"]  # the requester's save never saw the held value
        as_user(client, abe)
        decide(client, change_request, "approve")
        assert seen == ["pending", "paid"]

    def test_a_field_changed_since_the_request_is_stale(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        edit(client, order, status="refunded")  # Abe changes it directly meanwhile

        response = decide(client, change_request, "approve")
        assert response.status_code == 302
        assert any("has changed since" in text for text in texts(response))
        order.refresh_from_db()
        assert order.status == "refunded"  # the newer value was not overwritten
        change_request.refresh_from_db()
        assert change_request.is_pending

        page = client.get(reverse(REVIEW_URL, args=[change_request.pk])).content.decode()
        assert "Changed since it was asked" in page and "Refunded" in page

    def test_a_request_is_applied_once(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        decide(client, change_request, "approve")
        response = decide(client, change_request, "approve")
        assert any("already been decided" in text for text in texts(response))
        assert VantageLogEntry.objects.filter(action="update").count() == 1

    def test_nobody_approves_their_own_request_not_even_a_superuser(
        self, client, rita, boss, order
    ):
        change_request = ask(client, rita, order, status="paid")
        rita.user_permissions.add(Permission.objects.get(codename="approve_order"))
        rita = User.objects.get(pk=rita.pk)
        as_user(client, rita)
        response = decide(client, change_request, "approve")
        assert any("your own request" in text for text in texts(response))
        order.refresh_from_db()
        assert order.status == "pending"

        ChangeRequest.objects.filter(pk=change_request.pk).update(requested_by=boss)
        as_user(client, boss)
        decide(client, change_request, "approve")
        order.refresh_from_db()
        assert order.status == "pending"

    def test_someone_without_the_approve_permission_cannot(self, client, rita, order):
        change_request = ask(client, rita, order, status="paid")
        colleague = person("carl", "view_order", "change_order")
        as_user(client, colleague)
        # Not theirs and not approvable by them: it does not exist for them.
        assert decide(client, change_request, "approve").status_code == 404
        order.refresh_from_db()
        assert order.status == "pending"

    def test_read_only_mode_refuses_approval(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        with override_settings(VANTAGE={"READ_ONLY": True}):
            decide(client, change_request, "approve")
        order.refresh_from_db()
        assert order.status == "pending"

    def test_a_value_no_longer_valid_is_not_saved(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        changes = change_request.changes
        changes["status"]["to"] = "teleported"  # a choice since removed
        ChangeRequest.objects.filter(pk=change_request.pk).update(changes=changes)
        change_request.refresh_from_db()
        as_user(client, abe)
        response = decide(client, change_request, "approve")
        assert any("no longer valid" in text for text in texts(response))
        order.refresh_from_db()
        assert order.status == "pending"
        change_request.refresh_from_db()
        assert change_request.is_pending


class TestRejectingAndWithdrawing:
    def test_rejecting_needs_a_reason_and_records_it(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        assert decide(client, change_request, "reject", reason=" ").status_code == 422
        change_request.refresh_from_db()
        assert change_request.is_pending

        decide(client, change_request, "reject", reason="Not paid yet")
        change_request.refresh_from_db()
        assert change_request.status == "rejected"
        assert change_request.reason == "Not paid yet"
        assert change_request.decided_by == abe
        order.refresh_from_db()
        assert order.status == "pending"
        entry = VantageLogEntry.objects.filter(action="action").latest("pk")
        assert entry.action_name.startswith("rejected change to status")

    def test_the_requester_can_withdraw_their_own(self, client, rita, order):
        change_request = ask(client, rita, order, status="paid")
        decide(client, change_request, "withdraw")
        change_request.refresh_from_db()
        assert change_request.status == "withdrawn"
        entry = VantageLogEntry.objects.filter(action="action").latest("pk")
        assert entry.action_name.startswith("withdrew request to change status")

    def test_an_approver_cannot_withdraw_someone_elses(self, client, rita, abe, order):
        change_request = ask(client, rita, order, status="paid")
        as_user(client, abe)
        decide(client, change_request, "withdraw")
        change_request.refresh_from_db()
        assert change_request.is_pending


# ── the queue ───────────────────────────────────────────────────────────────


class TestTheQueue:
    def test_an_approver_sees_it_waiting_and_a_count_in_the_navigation(
        self, client, rita, abe, order
    ):
        ask(client, rita, order, status="paid")
        as_user(client, abe)
        page = client.get(reverse(QUEUE_URL))
        assert page.status_code == 200
        html = page.content.decode()
        assert "Waiting for you" in html and "A-1" in html
        nav = [item for group in page.context["nav"] for item in group.items]
        approvals_item = next(item for item in nav if item.key == "page:approvals")
        assert approvals_item.badge == "1"

    def test_the_requester_sees_their_own_but_no_count(self, client, rita, order):
        ask(client, rita, order, status="paid")
        page = client.get(reverse(QUEUE_URL))
        html = page.content.decode()
        assert "Your requests" in html and "A-1" in html
        assert "Waiting for you" not in html
        nav = [item for group in page.context["nav"] for item in group.items]
        assert next(item for item in nav if item.key == "page:approvals").badge is None

    def test_someone_who_cannot_view_a_guarded_model_has_no_page(self, client, panel, order):
        outsider = person("olga", "view_post", model=Post)
        as_user(client, outsider)
        assert client.get(reverse(QUEUE_URL)).status_code == 403

    def test_a_sensitive_field_stays_masked(self, client, rita, abe, order, monkeypatch, panel):
        monkeypatch.setattr(
            panel, "requires_approval", ["status", "total", "number"], raising=False
        )
        monkeypatch.setattr(panel, "sensitive_fields", ["number"], raising=False)
        change_request = ask(client, rita, order, number="SECRET-77")
        assert change_request.changes["number"]["to_display"] is None
        as_user(client, abe)
        for url in (
            reverse(QUEUE_URL),
            reverse(REVIEW_URL, args=[change_request.pk]),
            reverse("vantage:testapp-order-detail", args=[order.pk]),
        ):
            html = client.get(url).content.decode()
            assert "SECRET-77" not in html, url
            assert MASK in html, url

    def test_a_record_out_of_scope_is_out_of_the_queue(
        self, client, rita, abe, order, monkeypatch, panel
    ):
        """Scoping, which is where tenancy lives, decides what the queue may show."""
        change_request = ask(client, rita, order, status="paid")
        hidden = order.pk
        original = type(panel).get_queryset

        def scoped(request):
            queryset = original(panel, request)
            return queryset.exclude(pk=hidden) if request.user.username == "abe" else queryset

        monkeypatch.setattr(panel, "get_queryset", scoped, raising=False)
        as_user(client, abe)
        page = client.get(reverse(QUEUE_URL))
        assert "A-1" not in page.content.decode()
        nav = [item for group in page.context["nav"] for item in group.items]
        assert next(item for item in nav if item.key == "page:approvals").badge is None
        assert client.get(reverse(REVIEW_URL, args=[change_request.pk])).status_code == 404
        assert decide(client, change_request, "approve").status_code == 404

    def test_the_review_screen_is_behind_the_two_factor_gate(self):
        match = resolve(reverse(REVIEW_URL, args=[1]))
        assert getattr(match.func, "two_factor_gated", False)


# ── the other write paths ───────────────────────────────────────────────────


class TestTheGrid:
    def save(self, client, rows):
        return client.patch(
            reverse("vantage:testapp-order-inline"),
            data=json.dumps({"changes": rows}),
            content_type="application/json",
        )

    def test_a_guarded_cell_waits_and_the_rest_of_the_row_saves(
        self, client, rita, order, monkeypatch, panel
    ):
        # The number is the row's link by inference, and a link is never also a cell.
        monkeypatch.setattr(panel, "list_link", "total", raising=False)
        as_user(client, rita)
        response = self.save(
            client, [{"pk": order.pk, "fields": {"number": "A-5", "status": "shipped"}}]
        )
        assert response.status_code == 200
        body = response.json()
        assert body["pending"] == 1
        assert "sent for approval" in body["pending_message"]
        # The stored value, in the shape the select widget wants it.
        assert body["rows"][0]["values"]["status"] == ["pending"]
        order.refresh_from_db()
        assert order.number == "A-5" and order.status == "pending"
        assert ChangeRequest.objects.get().changes["status"]["to"] == "shipped"


class TestTheBoard:
    def test_a_move_on_a_guarded_field_is_sent_for_approval(self, client, rita, order):
        as_user(client, rita)
        response = client.post(
            reverse("vantage:testapp-order-kanban-move"),
            data=json.dumps({"pk": order.pk, "value": "paid", "field": "status"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] and body["pending"] and body["value"] == "pending"
        order.refresh_from_db()
        assert order.status == "pending"
        assert ChangeRequest.objects.get().changes["status"]["to"] == "paid"


class TestTheApi:
    def url(self, order):
        return reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "order", "pk": order.pk},
        )

    def test_a_guarded_change_answers_202_and_says_what_waits(self, client, rita, order):
        as_user(client, rita)
        response = client.patch(
            self.url(order),
            data=json.dumps({"fields": {"status": "paid", "number": "A-7"}, "note": "see #4"}),
            content_type="application/json",
        )
        assert response.status_code == 202
        body = response.json()
        assert body["pending"]["fields"] == ["status"]
        assert body["pending"]["requested"] == {"status": "paid"}
        order.refresh_from_db()
        assert order.number == "A-7" and order.status == "pending"
        assert ChangeRequest.objects.get().note == "see #4"

    def test_the_schema_says_which_fields_need_approval_for_this_caller(
        self, client, rita, abe, order
    ):
        def flags(user):
            as_user(client, user)
            schema = client.get(reverse("vantage:api-schema")).json()
            return {
                f["name"]: f["needs_approval"] for f in schema["models"]["testapp.order"]["fields"]
            }

        assert flags(rita)["status"] is True and flags(rita)["number"] is False
        assert flags(abe)["status"] is False


class TestBulkPaths:
    def test_a_bulk_edit_of_a_guarded_field_is_refused_and_writes_nothing(
        self, client, rita, order
    ):
        as_user(client, rita)
        response = client.post(
            reverse("vantage:testapp-order-bulk-edit"),
            {
                "selected": [str(order.pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "paid",
            },
        )
        assert response.status_code == 200  # the picker again, with the reason
        assert "cannot be changed in bulk" in " ".join(texts(response))
        order.refresh_from_db()
        assert order.status == "pending"
        assert not ChangeRequest.objects.exists()

    def test_an_approver_may_bulk_edit_it(self, client, abe, order):
        as_user(client, abe)
        client.post(
            reverse("vantage:testapp-order-bulk-edit"),
            {
                "selected": [str(order.pk)],
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "paid",
            },
        )
        order.refresh_from_db()
        assert order.status == "paid"

    def test_an_action_that_writes_a_guarded_field_is_not_offered(self, rf, rita, abe, monkeypatch):
        from vantage.actions import resolve_actions

        post_panel = site.get_panel(Post)
        monkeypatch.setattr(post_panel, "requires_approval", ["status"], raising=False)
        pattern = post_panel.get_patterns().publishable
        if pattern is None or pattern.field != "status":  # pragma: no cover - fixture drift
            pytest.skip("Post is not publishable through status")
        approvals.ensure_permissions()
        writer = person("wendy", "view_post", "change_post", model=Post)
        request = rf.get("/")
        request.user = writer
        names = {act.name for act in resolve_actions(post_panel, request)}
        assert "publish" not in names

        writer.user_permissions.add(Permission.objects.get(codename="approve_post"))
        request.user = User.objects.get(pk=writer.pk)
        from vantage.permissions import _reset

        _reset()
        assert "publish" in {act.name for act in resolve_actions(post_panel, request)}

    def test_an_import_queues_per_record(self, rf, rita, order, panel):
        from vantage.permissions import get_permissions

        request = rf.post("/")
        request.user = rita
        raw = b"Number,Status,Total\nA-1,paid,10.00\n"
        parsed = importer.read_csv(raw)
        mapping = importer.resolve_mapping(
            panel,
            parsed,
            {"Number": "number", "Status": "status", "Total": "total"},
            key_field="number",
            mode="update",
        )

        preview = importer.dry_run(panel, parsed, mapping, request, get_permissions())
        assert preview.queued == 1 and preview.updated == 0
        assert not ChangeRequest.objects.exists()  # a dry run leaves nothing behind

        result = importer.commit(panel, parsed, mapping, request, get_permissions())
        assert result.queued == 1 and result.queued_only == 1 and result.total == 1
        assert "1 sent for approval" in result.summary()
        order.refresh_from_db()
        assert order.status == "pending"
        change_request = ChangeRequest.objects.get()
        assert change_request.changes["status"]["to"] == "paid"
        assert VantageLogEntry.objects.filter(
            action="action", action_name__startswith="requested approval"
        ).exists()


# ── configuration ───────────────────────────────────────────────────────────


class TestChecks:
    def run(self, monkeypatch, value):
        panel = site.get_panel(Order)
        monkeypatch.setattr(panel, "requires_approval", value, raising=False)
        return [e.id for e in ModelPanelChecks()._check_requires_approval(panel)]

    def test_a_good_declaration_passes(self, monkeypatch):
        assert self.run(monkeypatch, ["status", "total"]) == []

    def test_a_string_is_e105(self, monkeypatch):
        assert self.run(monkeypatch, "status") == ["vantage.E105"]

    def test_an_unknown_name_is_e106(self, monkeypatch):
        assert self.run(monkeypatch, ["stauts"]) == ["vantage.E106"]

    def test_a_field_nobody_can_edit_is_e107(self, monkeypatch):
        assert self.run(monkeypatch, ["created_at"]) == ["vantage.E107"]

    def test_a_many_to_many_is_e108(self, monkeypatch):
        panel = site.get_panel(Post)
        monkeypatch.setattr(panel, "requires_approval", ["tags"], raising=False)
        assert [e.id for e in ModelPanelChecks()._check_requires_approval(panel)] == [
            "vantage.E108"
        ]

    def test_the_errors_are_errors(self, monkeypatch):
        panel = site.get_panel(Order)
        monkeypatch.setattr(panel, "requires_approval", ["nope"], raising=False)
        assert all(isinstance(e, Error) for e in ModelPanelChecks().check(panel))

    def test_a_bad_entry_is_skipped_rather_than_breaking_the_page(self, monkeypatch):
        panel = site.get_panel(Order)
        monkeypatch.setattr(panel, "requires_approval", ["nope", "status"], raising=False)
        assert panel.get_requires_approval() == ("status",)


class TestThePermission:
    def test_it_is_created_for_a_guarded_model_and_follows_djangos_naming(self, panel):
        assert approvals.permission_name(panel) == "testapp.approve_order"
        assert Permission.objects.filter(
            content_type=ContentType.objects.get_for_model(Order), codename="approve_order"
        ).exists()

    def test_a_panel_can_answer_for_itself(self, client, rita, order, monkeypatch, panel):
        monkeypatch.setattr(
            panel, "has_approve_permission", lambda request, obj=None: True, raising=False
        )
        as_user(client, rita)
        edit(client, order, status="paid")
        order.refresh_from_db()
        assert order.status == "paid"


class TestChildFormsets:
    """``inlines = [...]`` on a parent is one more way to write the child model.

    Saving an order through its customer's form must hold a guarded field exactly as
    the order's own edit page does, or ``requires_approval`` is a suggestion.
    """

    @pytest.fixture
    def author_with_orders(self, monkeypatch, panel):
        from vantage import inlines

        class OrderInline(inlines.TabularInline):
            model = Order
            fields = ["number", "status", "total"]
            extra = 0
            max_num = 10
            can_delete = False

        author_panel = site.get_panel(Author)
        monkeypatch.setattr(author_panel, "inlines", [OrderInline], raising=False)
        return author_panel

    def _post(self, client, order, **row):
        customer = order.customer
        prefix = "order_set"
        page = client.get(reverse("vantage:testapp-author-change", args=[customer.pk]))
        for group in page.context.get("inlines") or ():
            prefix = group.prefix
        data = {
            "name": customer.name,
            "email": customer.email,
            "is_active": "on",
            f"{prefix}-TOTAL_FORMS": "1",
            f"{prefix}-INITIAL_FORMS": "1",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
            f"{prefix}-0-id": str(order.pk),
            f"{prefix}-0-customer": str(customer.pk),
            f"{prefix}-0-number": order.number,
            f"{prefix}-0-status": row.get("status", order.status),
            f"{prefix}-0-total": row.get("total", str(order.total)),
        }
        return client.post(reverse("vantage:testapp-author-change", args=[customer.pk]), data)

    def test_a_guarded_child_field_is_held_for_approval(self, client, order, author_with_orders):
        user = person("carl", "view_order", "change_order", "add_order")
        ct = ContentType.objects.get_for_model(Author)
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type=ct, codename__in=["view_author", "change_author"]
            )
        )
        client.force_login(User.objects.get(pk=user.pk))

        response = self._post(client, order, status="refunded", total="0.01")

        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == "pending"
        assert order.total == Decimal("10.00")
        assert ChangeRequest.objects.filter(object_id=str(order.pk)).count() == 1

    def test_an_approver_saves_straight_through(self, client, order, author_with_orders):
        client.force_login(User.objects.create_superuser("root", "r@example.com", "x"))
        response = self._post(client, order, status="refunded")
        assert response.status_code == 302
        order.refresh_from_db()
        assert order.status == "refunded"
        assert not ChangeRequest.objects.exists()
