"""The access review: who can see and change which model and guarded field.

The report must say what the panel enforces, so most assertions compare it with the
resolver's own answer for the same subject, and with the cases that resolver handles
specially: superusers, field rules, sensitive fields and a role reviewed on its own.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.test import Client

from tests.testapp.models import Order, ZCredential
from vantage import accessreview
from vantage.models import FieldPermission, VantageLogEntry

pytestmark = pytest.mark.django_db

PAGE = "/admin/_/access-review/"
EXPORT = "/admin/_/access-review/export/"


def perm(label: str) -> Permission:
    app_label, codename = label.split(".")
    return Permission.objects.get(content_type__app_label=app_label, codename=codename)


@pytest.fixture
def support(db):
    group = Group.objects.create(name="Support")
    group.permissions.add(
        perm("testapp.view_order"),
        perm("testapp.change_order"),
        perm("testapp.view_zcredential"),
    )
    ct = ContentType.objects.get_for_model(Order)
    FieldPermission.objects.create(group=group, content_type=ct, field_name="total", level="hidden")
    FieldPermission.objects.create(
        group=group, content_type=ct, field_name="status", level="readonly"
    )
    return group


@pytest.fixture
def people(support):
    root = User.objects.create_superuser("root", "root@example.com", None)
    agent = User.objects.create_user("agent", "agent@example.com", None, is_staff=True)
    agent.groups.add(support)
    User.objects.create_user("outsider", "o@example.com", None)  # not staff: not reviewed
    User.objects.create_user("gone", "g@example.com", None, is_staff=True, is_active=False)
    return root, agent


def subject(report, name):
    return next(s for s in report.subjects if s.name == name)


def model_entry(sub, label):
    return next((m for m in sub.models if m.model == label), None)


class TestByUser:
    def test_only_active_staff_are_reviewed_and_superusers_flagged(self, people):
        report = accessreview.build_report()
        names = {s.name for s in report.subjects}
        assert names == {"root", "agent"}
        assert subject(report, "root").superuser
        assert not subject(report, "agent").superuser

    def test_verbs_and_field_rules_match_the_resolver(self, people):
        report = accessreview.build_report(models=["testapp.order"])
        entry = model_entry(subject(report, "agent"), "testapp.order")
        assert (entry.view, entry.add, entry.change, entry.delete) == (True, False, True, False)
        fields = {f.name: f for f in entry.fields}
        assert not fields["total"].visible and not fields["total"].editable
        assert fields["status"].visible and not fields["status"].editable
        root = model_entry(subject(report, "root"), "testapp.order")
        assert root.delete and all(f.visible and f.editable for f in root.fields)

    def test_models_without_access_are_left_out(self, people):
        report = accessreview.build_report()
        assert model_entry(subject(report, "agent"), "testapp.post") is None

    def test_sensitive_fields_and_reveal_rights(self, people):
        report = accessreview.build_report(models=["testapp.zcredential"])
        agent = {
            f.name: f for f in model_entry(subject(report, "agent"), "testapp.zcredential").fields
        }
        assert agent["api_token"].sensitive and agent["api_token"].visible
        assert agent["api_token"].reveal is False
        assert "token_type" not in agent  # a near miss is not a guarded field
        root = {
            f.name: f for f in model_entry(subject(report, "root"), "testapp.zcredential").fields
        }
        assert root["api_token"].reveal is True
        assert root["pin_hash"].reveal is False  # a hash is never revealable

    def test_reveal_permission_is_reflected(self, people, support):
        support.permissions.add(perm("vantage.reveal_sensitive"))
        report = accessreview.build_report(models=["testapp.zcredential"])
        fields = {
            f.name: f
            for f in model_entry(subject(report, "agent"), ZCredential._meta.label_lower).fields
        }
        assert fields["api_token"].reveal is True

    def test_query_cost_grows_with_users_not_fields(
        self, people, support, django_assert_max_num_queries
    ):
        accessreview.build_report()
        for i in range(5):
            u = User.objects.create_user(f"extra{i}", is_staff=True)
            u.groups.add(support)
        with django_assert_max_num_queries(120):
            accessreview.build_report()


class TestByRole:
    def test_a_role_is_reviewed_as_its_own_permissions(self, people, support):
        report = accessreview.build_report(by="role", models=["testapp.order"])
        role = subject(report, "Support")
        assert role.kind == "role"
        entry = model_entry(role, "testapp.order")
        assert (entry.view, entry.change, entry.delete) == (True, True, False)
        fields = {f.name: f for f in entry.fields}
        assert not fields["total"].visible
        assert fields["status"].visible and not fields["status"].editable


class TestOutput:
    def test_csv_is_armoured_against_formulas(self, people):
        Group.objects.create(name='=HYPERLINK("http://x")').permissions.add(
            perm("testapp.view_order")
        )
        text = accessreview.to_csv(accessreview.build_report(by="role"))
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == list(accessreview.CSV_COLUMNS)
        names = {r[1] for r in rows[1:]}
        assert '\'=HYPERLINK("http://x")' in names
        assert not any(cell.startswith("=") for r in rows for cell in r)

    def test_json_has_timestamp_and_version(self, people):
        from vantage import __version__

        data = json.loads(accessreview.to_json(accessreview.build_report()))
        assert data["version"] == __version__
        assert data["generated_at"]
        assert data["by"] == "user"


class TestCommand:
    def test_csv_to_stdout(self, people, capsys):
        call_command("vantage_access_review", "--model", "testapp.order")
        out = capsys.readouterr().out
        assert out.startswith("subject_type,subject,")
        assert "agent" in out and "testapp.order" in out

    def test_json_by_role_to_a_file(self, people, tmp_path):
        target = tmp_path / "review.json"
        call_command(
            "vantage_access_review", "--format", "json", "--by", "role", "--output", str(target)
        )
        data = json.loads(target.read_text())
        assert data["by"] == "role"
        assert {s["name"] for s in data["subjects"]} == {"Support"}

    def test_unknown_model(self, people):
        with pytest.raises(CommandError):
            call_command("vantage_access_review", "--model", "nope.nothing")


class TestScreen:
    def test_superusers_only_by_default(self, people):
        root, agent = people
        client = Client()
        client.force_login(agent)
        assert client.get(PAGE).status_code == 403
        assert client.get(EXPORT).status_code == 403
        client.force_login(root)
        page = client.get(PAGE)
        assert page.status_code == 200
        assert "agent" in page.content.decode()

    def test_setting_widens_it(self, people, settings):
        _root, agent = people
        settings.VANTAGE = {"ACCESS_REVIEW_PERMISSION": "testapp.view_order"}
        client = Client()
        client.force_login(agent)
        assert client.get(PAGE).status_code == 200

    def test_download_is_audited(self, people):
        root, _agent = people
        client = Client()
        client.force_login(root)
        response = client.get(EXPORT, {"format": "csv", "by": "role"})
        assert response.status_code == 200
        assert "attachment" in response["Content-Disposition"]
        assert b"Support" in response.content
        entry = VantageLogEntry.objects.get(action_name="access-review.export")
        assert entry.actor == root

    def test_linked_from_the_roles_screen(self, people):
        root, _agent = people
        client = Client()
        client.force_login(root)
        assert PAGE in client.get("/admin/_/roles/").content.decode()
