"""Field-level permissions: "Support can see the total but not change it".

Organised by enforcement point, because that is where this feature fails: a field
hidden on the list page and printed in the CSV, read-only on the form and writable
through the API. Each class names one surface and asserts the refusal there — the
forged POST, the hand-built query string, the payload a client was never offered —
rather than only the control the page draws.

The fixture is the roadmap's own sentence. ``Support`` may view, add and change
orders; on ``Order`` it cannot see ``total`` (hidden) and cannot change ``status``
(read-only).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.checks import Error
from django.test import RequestFactory
from django.urls import reverse

from tests.testapp.models import Author, Order, SaleLine
from vantage import fieldperms, history, records, roles, site
from vantage.models import FieldPermission, VantageLogEntry
from vantage.panel_checks import ModelPanelChecks
from vantage.panels import ModelPanel

pytestmark = pytest.mark.django_db

ORDER_LIST = "vantage:testapp-order-list"
ORDER_CT = None  # resolved per test; content types are created on migrate


def perm(label: str) -> Permission:
    app_label, codename = label.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


def restrict(group: Group, model, **levels) -> None:
    ct = ContentType.objects.get_for_model(model)
    for name, level in levels.items():
        FieldPermission.objects.create(group=group, content_type=ct, field_name=name, level=level)


def ct_id(model) -> int:
    return ContentType.objects.get_for_model(model).pk


def order_panel():
    return site.get_panel(Order)


@pytest.fixture
def support(db):
    group = Group.objects.create(name="Support")
    group.permissions.add(
        perm("testapp.view_order"),
        perm("testapp.add_order"),
        perm("testapp.change_order"),
        perm("testapp.view_author"),
        perm("testapp.view_saleline"),
        perm("testapp.change_saleline"),
        perm("testapp.add_saleline"),
    )
    restrict(group, Order, total="hidden", status="readonly")
    return group


@pytest.fixture
def agent(support):
    user = User.objects.create_user("agent", "agent@example.com", "x", is_staff=True)
    user.groups.add(support)
    return User.objects.get(pk=user.pk)


@pytest.fixture
def as_agent(client, agent):
    client.force_login(agent)
    return client


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def as_boss(client, boss):
    client.force_login(boss)
    return client


@pytest.fixture
def order(db):
    customer = Author.objects.create(name="Grace Hopper", email="grace@example.com")
    return Order.objects.create(
        number="A-1001", customer=customer, status="pending", total=Decimal("4321.77")
    )


def request_for(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return request


# ── who is restricted, and how several roles combine ────────────────────────


class TestResolution:
    def test_no_rule_is_no_restriction(self, db):
        user = User.objects.create_user("plain", is_staff=True)
        assert not fieldperms.access(request_for(user), Order)

    def test_a_role_rule_applies_to_its_members(self, agent):
        answer = fieldperms.access(request_for(agent), Order)
        assert answer.hidden == {"total"}
        assert answer.readonly == {"status"}
        assert answer.can_view("status") and not answer.can_change("status")
        assert not answer.can_view("total") and not answer.can_change("total")

    def test_superusers_are_never_restricted(self, boss, support):
        boss.groups.add(support)
        assert not fieldperms.access(request_for(boss), Order)

    def test_most_permissive_wins_across_roles_that_grant_the_model(self, agent):
        managers = Group.objects.create(name="Managers")
        managers.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        agent.groups.add(managers)
        assert not fieldperms.access(request_for(agent), Order)

    def test_a_role_with_no_grant_on_the_model_cannot_lift_a_rule(self, agent):
        newsletter = Group.objects.create(name="Newsletter")
        newsletter.permissions.add(perm("testapp.view_author"))
        agent.groups.add(newsletter)
        answer = fieldperms.access(request_for(agent), Order)
        assert answer.hidden == {"total"} and answer.readonly == {"status"}

    def test_a_view_only_role_lifts_hidden_but_not_read_only(self, agent):
        """Viewing through one role and changing through another must not combine
        into changing a field the change role made read-only."""
        viewers = Group.objects.create(name="Viewers")
        viewers.permissions.add(perm("testapp.view_order"))
        agent.groups.add(viewers)
        answer = fieldperms.access(request_for(agent), Order)
        assert answer.hidden == frozenset()
        assert answer.readonly == {"total", "status"}

    def test_two_restricting_roles_take_the_milder_level(self, agent):
        other = Group.objects.create(name="Billing")
        other.permissions.add(perm("testapp.view_order"), perm("testapp.change_order"))
        restrict(other, Order, total="readonly", status="hidden")
        agent.groups.add(other)
        answer = fieldperms.access(request_for(agent), Order)
        assert answer.hidden == frozenset()
        assert answer.readonly == {"total", "status"}

    def test_rules_from_code_apply_and_are_a_floor(self, agent, monkeypatch):
        FieldPermission.objects.filter(field_name="total").update(level="readonly")
        rules = {"Support": {"number": "read-only", "total": "hidden"}}
        monkeypatch.setattr(order_panel(), "field_permissions", rules)
        answer = fieldperms.access(request_for(agent), Order)
        assert answer.hidden == {"total"}  # code says hidden; the stored rule is milder
        assert answer.readonly == {"status", "number"}

    def test_answered_once_per_request(self, agent, django_assert_max_num_queries):
        request = request_for(agent)
        fieldperms.access(request, Order)
        with django_assert_max_num_queries(0):
            for _ in range(20):
                fieldperms.access(request, Order)
                fieldperms.is_hidden_path(request, order_panel(), "total")

    def test_the_permission_layer_answers_per_field(self, agent):
        from vantage.permissions import get_permissions

        resolver = get_permissions()
        request = request_for(agent)
        assert resolver.has_field_perm(request, Order, "status", "view")
        assert not resolver.has_field_perm(request, Order, "status", "change")
        assert not resolver.has_field_perm(request, Order, "total", "view")
        assert resolver.has_field_perm(request, Order, "number", "change")

    def test_paths_are_judged_hop_by_hop(self, agent, support):
        restrict(support, Author, email="hidden")
        request = request_for(agent)
        assert fieldperms.is_hidden_path(request, order_panel(), "customer__email")
        assert fieldperms.is_hidden_path(request, order_panel(), "-total")
        assert not fieldperms.is_hidden_path(request, order_panel(), "customer__name")


# ── the list page ───────────────────────────────────────────────────────────


class TestListPage:
    def test_a_hidden_column_is_not_drawn_at_all(self, as_agent, order):
        body = as_agent.get(reverse(ORDER_LIST)).content.decode()
        assert "4321.77" not in body and "4,321.77" not in body
        assert "A-1001" in body

    def test_the_superuser_still_sees_it(self, as_boss, order):
        body = as_boss.get(reverse(ORDER_LIST)).content.decode()
        assert "4321.77" in body or "4,321.77" in body

    def test_getters_drop_hidden_paths(self, agent, monkeypatch):
        panel = order_panel()
        monkeypatch.setattr(panel, "list_display", ["number", "total", "status"])
        monkeypatch.setattr(panel, "list_filter", ["status", "total"])
        monkeypatch.setattr(panel, "list_search", ["number", "total"])
        monkeypatch.setattr(panel, "ordering", ["-total"])
        request = request_for(agent)
        assert panel.get_list_display(request) == ["number", "status"]
        assert panel.get_list_filter(request) == ["status"]
        assert panel.get_list_search(request) == ["number"]
        assert panel.get_ordering(request) == ["-pk"]
        # Without a request — checks, scaffolding — the declaration stands.
        assert panel.get_list_display() == ["number", "total", "status"]

    def test_sorting_by_a_hidden_field_is_ignored(self, as_agent, order, monkeypatch):
        from vantage.listquery import resolve_sort

        monkeypatch.setattr(order_panel(), "list_display", ["number", "total"])
        request = request_for(User.objects.get(username="agent"), "/?sort=total")
        assert resolve_sort(order_panel(), request, allowed=["number", "total"]) is None

    def test_searching_a_hidden_field_matches_nothing(self, as_agent, order, monkeypatch):
        monkeypatch.setattr(order_panel(), "list_search", ["total"])
        body = as_agent.get(reverse(ORDER_LIST), {"q": "4321"}).content.decode()
        assert "A-1001" in body  # no search fields left, so the term filters nothing...
        from vantage.listquery import search_q

        request = request_for(User.objects.get(username="agent"))
        assert search_q(order_panel(), "4321", request).children == []  # ...and builds no SQL

    def test_filtering_by_a_hidden_field_is_not_offered(self, as_agent, order, monkeypatch):
        from vantage.filters import build_filters

        monkeypatch.setattr(order_panel(), "list_filter", ["status", "total"])
        request = request_for(User.objects.get(username="agent"), "/?total=4321.77")
        assert [f.path for f in build_filters(order_panel(), request)] == ["status"]

    def test_export_leaves_the_column_out(self, as_agent, order, monkeypatch):
        monkeypatch.setattr(order_panel(), "list_display", ["number", "total"])
        response = as_agent.get(reverse(ORDER_LIST), {"export": "csv"})
        text = b"".join(response.streaming_content).decode()
        assert "A-1001" in text
        assert "4321" not in text
        assert "Total" not in text

    def test_phone_cards_and_card_fields_drop_it(self, agent, monkeypatch):
        panel = order_panel()
        monkeypatch.setattr(panel, "card_fields", ["number", "total", "status"])
        assert panel.get_card_fields(request_for(agent)) == ["number", "status"]

    def test_a_board_grouped_by_a_hidden_field_is_not_offered(self, agent, support):
        FieldPermission.objects.filter(field_name="status").update(level="hidden")
        request = request_for(agent)
        assert "kanban" not in order_panel().get_list_views(request)

    def test_a_board_on_a_read_only_field_cannot_be_dragged(self, agent):
        from vantage.listviews.kanban import KanbanMode

        mode = KanbanMode(order_panel(), request_for(agent))
        if mode.field is None:  # pragma: no cover - the board is inferred from status
            pytest.skip("No board on orders.")
        assert not mode.is_writable(request_for(agent))

    def test_a_stat_over_a_hidden_field_is_dropped(self, agent, monkeypatch):
        from django.db.models import Count, Sum

        from vantage import listprefs
        from vantage.stats import Stat

        monkeypatch.setattr(
            order_panel(),
            "list_stats",
            [Stat("Revenue", Sum("total")), Stat("Orders", Count("pk"))],
        )
        spec = listprefs.resolve(order_panel(), request_for(agent))
        assert [stat.label for stat in spec.stats] == ["Orders"]


# ── the detail page and the edit form ───────────────────────────────────────


class TestDetailAndForm:
    def test_detail_leaves_a_hidden_field_out(self, as_agent, order):
        url = reverse("vantage:testapp-order-detail", args=[order.pk])
        body = as_agent.get(url).content.decode()
        assert "4321" not in body
        assert "Pending" in body

    def test_the_form_renders_read_only_as_text_and_hidden_not_at_all(self, as_agent, order):
        body = as_agent.get(reverse("vantage:testapp-order-change", args=[order.pk])).content
        body = body.decode()
        assert 'name="number"' in body
        assert 'name="status"' not in body
        assert 'name="total"' not in body
        assert "4321" not in body

    def test_a_forged_post_cannot_write_either(self, as_agent, order):
        response = as_agent.post(
            reverse("vantage:testapp-order-change", args=[order.pk]),
            {
                "number": "A-2002",
                "customer": order.customer_id,
                "status": "shipped",
                "total": "1.00",
            },
        )
        assert response.status_code == 302
        order.refresh_from_db()
        assert order.number == "A-2002"
        assert order.status == "pending"
        assert order.total == Decimal("4321.77")

    def test_create_uses_the_default_for_a_read_only_field(self, as_agent, support, order):
        FieldPermission.objects.filter(field_name="total").delete()
        as_agent.post(
            reverse("vantage:testapp-order-add"),
            {"number": "A-3003", "customer": order.customer_id, "status": "paid", "total": "5"},
        )
        created = Order.objects.get(number="A-3003")
        assert created.status == "pending"  # the model default, not what was posted
        assert created.total == Decimal("5")

    def test_create_says_why_when_a_locked_field_has_no_default(self, as_agent, order):
        """``total`` is hidden and has no default: the record cannot be created here,
        and the page says so instead of failing in the database."""
        response = as_agent.post(
            reverse("vantage:testapp-order-add"),
            {"number": "A-3003", "customer": order.customer_id},
        )
        assert response.status_code == 200
        assert "cannot be created" in response.content.decode()
        assert not Order.objects.filter(number="A-3003").exists()

    def test_child_inline_fields_follow_the_child_models_rules(
        self, as_agent, support, order, monkeypatch
    ):
        from vantage import inlines

        class LineInline(inlines.TabularInline):
            model = SaleLine
            fields = ["quantity", "unit_price"]
            extra = 0
            max_num = 20

        line = SaleLine.objects.create(order=order, quantity=2, unit_price=Decimal("9.99"))
        restrict(support, SaleLine, unit_price="readonly", quantity="hidden")
        monkeypatch.setattr(order_panel(), "inlines", [LineInline])
        url = reverse("vantage:testapp-order-change", args=[order.pk])
        body = as_agent.get(url).content.decode()
        assert "-quantity" not in body
        prefix = "lines"
        as_agent.post(
            url,
            {
                "number": order.number,
                "customer": order.customer_id,
                f"{prefix}-TOTAL_FORMS": "1",
                f"{prefix}-INITIAL_FORMS": "1",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "20",
                f"{prefix}-0-id": line.pk,
                f"{prefix}-0-unit_price": "0.01",
                f"{prefix}-0-quantity": "99",
            },
        )
        line.refresh_from_db()
        assert line.unit_price == Decimal("9.99")
        assert line.quantity == 2

    def test_duplicate_does_not_offer_a_read_only_field(self, as_agent, order):
        body = as_agent.get(reverse("vantage:testapp-order-duplicate", args=[order.pk])).content
        assert b'name="status"' not in body
        assert b"4321" not in body


# ── inline cell editing and bulk edit ───────────────────────────────────────


class TestInlineAndBulk:
    def test_a_read_only_column_is_not_editable_in_place(self, agent, monkeypatch):
        from vantage.inline import get_config

        panel = order_panel()
        monkeypatch.setattr(panel, "list_display", ["number", "status"])
        monkeypatch.setattr(panel, "list_editable", ["status"])
        assert get_config(panel, request_for(agent)).field("status") is None

    def test_a_forged_inline_write_is_dropped(self, as_agent, order, monkeypatch):
        panel = order_panel()
        monkeypatch.setattr(panel, "list_display", ["number", "status"])
        monkeypatch.setattr(panel, "list_editable", ["status"])
        response = as_agent.patch(
            reverse("vantage:testapp-order-inline"),
            data=json.dumps({"changes": [{"pk": order.pk, "fields": {"status": "shipped"}}]}),
            content_type="application/json",
        )
        assert response.json()["saved"] == 0
        order.refresh_from_db()
        assert order.status == "pending"

    def test_bulk_edit_offers_neither_and_ignores_a_forged_tick(self, as_agent, agent, order):
        assert "status" not in records.bulk_edit_fields(order_panel(), request_for(agent))
        assert "status" in records.bulk_edit_fields(order_panel())
        as_agent.post(
            reverse("vantage:testapp-order-bulk-edit"),
            {
                "select_all": "1",
                "_bulk_apply": "1",
                records.BULK_EDIT_ENABLE: ["status"],
                f"{records.BULK_EDIT_PREFIX}-status": "shipped",
            },
        )
        order.refresh_from_db()
        assert order.status == "pending"


# ── the JSON API ────────────────────────────────────────────────────────────


class TestApi:
    def detail(self, order):
        return reverse(
            "vantage:api-detail",
            kwargs={"app_label": "testapp", "model_name": "order", "pk": order.pk},
        )

    def test_a_read_leaves_the_hidden_field_out(self, as_agent, order):
        fields = as_agent.get(self.detail(order)).json()["fields"]
        assert "total" not in fields
        assert fields["status"]["value"] == "pending"

    def test_asking_for_it_by_name_is_refused(self, as_agent, order):
        url = reverse("vantage:api-list", kwargs={"app_label": "testapp", "model_name": "order"})
        assert as_agent.get(url, {"fields": "number,total"}).status_code == 400
        rows = as_agent.get(url).json()["results"]
        assert all("total" not in row["cells"] for row in rows)

    def test_writing_a_read_only_or_hidden_field_is_refused(self, as_agent, order):
        for name, value in (("status", "shipped"), ("total", "1.00")):
            response = as_agent.patch(
                self.detail(order),
                data=json.dumps({"fields": {name: value}}),
                content_type="application/json",
            )
            assert response.status_code == 400
        order.refresh_from_db()
        assert (order.status, order.total) == ("pending", Decimal("4321.77"))

    def test_a_put_keeps_the_read_only_value(self, as_agent, order):
        response = as_agent.put(
            self.detail(order),
            data=json.dumps({"fields": {"number": "A-9", "customer": order.customer_id}}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        order.refresh_from_db()
        assert (order.number, order.status) == ("A-9", "pending")
        assert "total" not in response.json()["fields"]

    def test_the_schema_omits_hidden_and_marks_read_only(self, as_agent):
        schema = as_agent.get(reverse("vantage:api-schema")).json()["models"]["testapp.order"]
        fields = {entry["name"]: entry for entry in schema["fields"]}
        assert "total" not in fields
        assert fields["status"]["readonly"] is True
        assert fields["number"]["readonly"] is False


# ── import ──────────────────────────────────────────────────────────────────


class TestImport:
    def test_neither_is_an_import_target(self, agent):
        from vantage.importer import importable_fields
        from vantage.permissions import get_permissions

        names = {
            option.name
            for option in importable_fields(order_panel(), request_for(agent), get_permissions())
        }
        assert "number" in names
        assert not names & {"status", "total"}


# ── history, the audit log and webhooks ─────────────────────────────────────


@pytest.fixture
def edited(as_boss, order):
    """An order with two audited edits, made by a superuser."""
    url = reverse("vantage:testapp-order-change", args=[order.pk])
    for status, total in (("paid", "10.00"), ("shipped", "20.00")):
        as_boss.post(
            url,
            {
                "number": order.number,
                "customer": order.customer_id,
                "status": status,
                "total": total,
            },
        )
    order.refresh_from_db()
    return order


class TestHistory:
    def test_a_hidden_field_is_not_in_the_reconstruction(self, agent, edited):
        entries, _ = history.timeline(edited)
        position = history.position_from({"entry": str(entries[0].pk)}, entries)
        result = history.reconstruct(
            order_panel(), edited, position, entries=entries, request=request_for(agent)
        )
        names = {state.name for state in result.fields}
        assert "total" not in names
        assert "Total" not in result.unrecorded

    def test_a_read_only_field_is_shown_but_not_restorable(self, agent, edited):
        entries, _ = history.timeline(edited)
        position = history.position_from({"entry": str(entries[0].pk)}, entries)
        result = history.reconstruct(
            order_panel(), edited, position, entries=entries, request=request_for(agent)
        )
        status = next(state for state in result.fields if state.name == "status")
        assert status.shown and status.changed
        assert not status.restorable

    def test_a_forged_restore_of_a_read_only_field_writes_nothing(self, client, agent, edited):
        agent.user_permissions.add(perm("vantage.view_vantagelogentry"))
        client.force_login(agent)
        entries, _ = history.timeline(edited)
        position = history.position_from({"entry": str(entries[0].pk)}, entries)
        result = history.reconstruct(
            order_panel(), edited, position, entries=entries, request=request_for(agent)
        )
        client.post(
            reverse("vantage:testapp-order-history", args=[edited.pk]),
            {
                "entry": entries[0].pk,
                "fingerprint": history.fingerprint(result),
                "fields": ["status", "total"],
            },
        )
        edited.refresh_from_db()
        assert (edited.status, edited.total) == ("shipped", Decimal("20.00"))

    def test_the_history_page_and_timeline_leave_it_out(self, client, agent, edited):
        agent.user_permissions.add(perm("vantage.view_vantagelogentry"))
        client.force_login(agent)
        body = client.get(
            reverse("vantage:testapp-order-history", args=[edited.pk])
        ).content.decode()
        assert "20.00" not in body and "10.00" not in body
        assert "shipped" in body.lower()

    def test_audit_entries_drop_the_field_for_the_reader_only(self, agent, edited):
        entry = VantageLogEntry.objects.filter(action="update").first()
        assert "total" in {row["field"] for row in entry.change_list}
        fieldperms.hide_entry_fields(request_for(agent), [entry])
        assert "total" not in {row["field"] for row in entry.change_list}

    def test_the_detail_pages_history_panel_drops_it(self, client, agent, edited):
        agent.user_permissions.add(perm("vantage.view_vantagelogentry"))
        client.force_login(agent)
        body = client.get(reverse("vantage:testapp-order-detail", args=[edited.pk])).content
        assert b"20.00" not in body

    def test_webhook_delivery_log_redacts_for_the_reader(self, agent):
        from vantage.webhookviews import _visible_payload

        payload = {
            "model": "testapp.order",
            "changes": {"total": {"from": "1", "to": "2"}, "number": {"to": "A"}},
        }
        shown = _visible_payload(request_for(agent), payload)
        assert set(shown["changes"]) == {"number"}
        assert set(payload["changes"]) == {"total", "number"}  # the stored body is untouched


# ── search, autocomplete and reveal ─────────────────────────────────────────


class TestLabelsAndLookups:
    def test_autocomplete_neither_searches_nor_prints_a_hidden_field(
        self, as_agent, support, order
    ):
        restrict(support, Author, name="hidden")
        response = as_agent.get(
            reverse("vantage:autocomplete"), {"model": "testapp.Author", "q": "Grace"}
        )
        assert response.status_code == 200
        texts = [row["text"] for row in response.json()["results"]]
        assert all("Grace" not in text for text in texts)
        everything = as_agent.get(reverse("vantage:autocomplete"), {"model": "testapp.Author"})
        assert [row["text"] for row in everything.json()["results"]] == [
            f"Author #{order.customer_id}"
        ]

    def test_reveal_refuses_a_hidden_sensitive_field(self, as_agent, support, order, monkeypatch):
        restrict(support, Author, email="hidden")
        monkeypatch.setattr(site.get_panel(Author), "sensitive_fields", ["email"])
        response = as_agent.post(
            reverse("vantage:testapp-author-reveal", args=[order.customer_id, "email"])
        )
        assert response.status_code == 404


# ── data quality ────────────────────────────────────────────────────────────


class TestDataQuality:
    def test_findings_about_a_hidden_field_are_dropped(self, agent):
        from vantage.dataquality import Finding, ModelReport, _without_hidden

        report = ModelReport(
            model="testapp.order",
            label="Orders",
            url=None,
            rows=1,
            rows_kind="exact",
            rows_display="1",
            findings=[
                Finding("nulls", "warn", "empty", field="total"),
                Finding("nulls", "warn", "empty", field="number"),
            ],
        )
        kept = _without_hidden(report, request_for(agent))
        assert [f.field for f in kept.findings] == ["number"]
        assert len(report.findings) == 2  # a cached report is shared; never mutated


# ── the roles matrix ────────────────────────────────────────────────────────


def save(client, group, *, fields=(), token=None, **extra):
    data = {
        "token": token
        if token is not None
        else roles.fingerprint(group, roles.current_permission_ids(group)),
        "mode": "diff",
        "add": "",
        "remove": "",
        "fields": ",".join(fields),
        **extra,
    }
    return client.post(reverse("vantage:role-edit", args=[group.pk]), data)


class TestMatrix:
    def test_the_matrix_offers_field_rules_for_registered_models(self, as_boss, support):
        body = as_boss.get(reverse("vantage:role-edit", args=[support.pk])).content.decode()
        assert f'value="{ct_id(Order)}:total:hidden" selected' in body
        assert f'value="{ct_id(Order)}:status:readonly" selected' in body
        assert "2 restricted" in body

    def test_saving_a_rule_goes_through_the_reviewed_diff(self, as_boss, support):
        response = save(
            as_boss,
            support,
            fields=[f"{ct_id(Order)}:number:readonly", f"{ct_id(Order)}:total:"],
        )
        assert response.status_code == 302
        assert roles.current_field_rules(support) == {
            (ct_id(Order), "number"): "readonly",
            (ct_id(Order), "status"): "readonly",
        }
        entry = VantageLogEntry.objects.get(action_name="role.permissions")
        change = entry.changes["Field access"]
        assert "testapp.order.total: hidden" in change["from"]
        assert "testapp.order.total: full access" in change["to"]

    def test_without_javascript_the_selects_are_diffed_against_the_base(self, as_boss, support):
        ct = ct_id(Order)
        as_boss.post(
            reverse("vantage:role-edit", args=[support.pk]),
            {
                "token": roles.fingerprint(support, roles.current_permission_ids(support)),
                "base": ",".join(str(i) for i in roles.current_permission_ids(support)),
                "perm": [str(i) for i in roles.current_permission_ids(support)],
                "field_base": f"{ct}:status:readonly,{ct}:total:hidden",
                "field": [f"{ct}:status:", f"{ct}:total:hidden", f"{ct}:number:"],
            },
        )
        assert roles.current_field_rules(support) == {(ct, "total"): "hidden"}

    def test_a_rule_change_is_a_conflict_for_a_stale_page(self, as_boss, support):
        stale = roles.fingerprint(support, roles.current_permission_ids(support))
        restrict(support, Order, number="readonly")
        response = save(as_boss, support, fields=[f"{ct_id(Order)}:total:"], token=stale)
        assert response.status_code == 409
        assert (ct_id(Order), "total") in roles.current_field_rules(support)

    def test_unknown_fields_and_unregistered_models_are_ignored(self, as_boss, support):
        unregistered = ct_id(Permission)  # never registered on any panel
        save(
            as_boss,
            support,
            fields=[f"{ct_id(Order)}:no_such_field:hidden", f"{unregistered}:name:hidden"],
        )
        assert set(roles.current_field_rules(support)) == {
            (ct_id(Order), "total"),
            (ct_id(Order), "status"),
        }

    def test_a_non_superuser_cannot_lift_a_rule_they_are_under(
        self, client, agent, support, settings
    ):
        """Someone who cannot see ``total`` must not be able to unhide it for others."""
        settings.VANTAGE = {"ROLES_PERMISSION": "auth.change_group"}
        agent.user_permissions.add(perm("auth.change_group"), perm("auth.view_group"))
        target = Group.objects.create(name="Interns")
        restrict(target, Order, total="hidden", number="hidden")
        client.force_login(User.objects.get(pk=agent.pk))
        save(client, target, fields=[f"{ct_id(Order)}:total:", f"{ct_id(Order)}:number:"])
        # number: the agent holds change_order and is unrestricted on it, so allowed.
        assert roles.current_field_rules(target) == {(ct_id(Order), "total"): "hidden"}

    def test_a_locked_rule_renders_disabled(self, client, agent, support, settings):
        settings.VANTAGE = {"ROLES_PERMISSION": "auth.change_group"}
        agent.user_permissions.add(perm("auth.change_group"), perm("auth.view_group"))
        client.force_login(User.objects.get(pk=agent.pk))
        body = client.get(reverse("vantage:role-edit", args=[support.pk])).content.decode()
        select = body[body.index(f'data-key="{ct_id(Order)}:total"') :][:400]
        assert "disabled" in select

    def test_duplicating_a_role_copies_its_field_rules(self, as_boss, support):
        as_boss.post(reverse("vantage:role-duplicate", args=[support.pk]))
        copy = Group.objects.get(name="Copy of Support")
        assert roles.current_field_rules(copy) == roles.current_field_rules(support)

    def test_deleting_a_role_deletes_its_rules(self, support):
        support.delete()
        assert not FieldPermission.objects.exists()

    def test_effective_permissions_list_the_restrictions(self, as_boss, agent):
        body = as_boss.get(reverse("vantage:role-user", args=[agent.pk])).content.decode()
        assert "Field access" in body
        assert "Total" in body and "Status" in body


# ── manage.py check ─────────────────────────────────────────────────────────


class TestChecks:
    def check(self, rules):
        panel = type("Panel", (ModelPanel,), {"field_permissions": rules})(Order, site)
        return [error.id for error in ModelPanelChecks().check(panel) if isinstance(error, Error)]

    def test_a_valid_declaration_passes(self):
        assert self.check({"Support": {"total": "hidden", "status": "read-only"}}) == []

    @pytest.mark.parametrize(
        ("rules", "ident"),
        [
            (["Support"], "vantage.E110"),
            ({"Support": ["total"]}, "vantage.E111"),
            ({"Support": {"totl": "hidden"}}, "vantage.E112"),
            ({"Support": {"total": "secret"}}, "vantage.E113"),
            ({"Support": {"id": "hidden"}}, "vantage.E114"),
        ],
    )
    def test_each_mistake_has_its_own_id(self, rules, ident):
        assert ident in self.check(rules)
