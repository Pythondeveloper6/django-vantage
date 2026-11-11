"""Agent activity: the export command, the admin page, and the badge in the audit log."""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, ZCredential
from vantage import agentactivity, agents
from vantage.models import AgentRun, VantageLogEntry

pytestmark = pytest.mark.django_db

SECRET = "sk-live-4242-do-not-leak"


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.VANTAGE = {"AGENTS": True, "AGENT_DEFAULT_ACCESS": "write"}


@pytest.fixture
def owner():
    return User.objects.create_user("olivia", is_staff=True)


@pytest.fixture
def svc():
    user = User.objects.create_user("svc", is_staff=True)
    user.set_unusable_password()
    user.save()
    return user


@pytest.fixture
def bot(svc, owner):
    identity, _token = agents.create_identity("bot", svc, owner, access="write")
    return identity


def entry(identity, run=None, *, model=Author, action="update", repr_="Ada", changes=None, **kw):
    run = run or AgentRun.objects.get_or_create(identity=identity, run_key="r1")[0]
    return VantageLogEntry.objects.create(
        actor=identity.user,
        actor_repr=agents.actor_label(identity),
        agent=identity,
        agent_run=run,
        action=action,
        content_type=ContentType.objects.get_for_model(model),
        object_id=kw.pop("object_id", "1"),
        object_repr=repr_,
        changes=changes if changes is not None else {"name": {"from": "A", "to": "B"}},
        ip_address=kw.pop("ip_address", "10.0.0.5"),
        **kw,
    )


def run_command(*args, **options):
    out = io.StringIO()
    call_command("vantage_agent_activity", *args, stdout=out, **options)
    return out.getvalue()


# ── the command ─────────────────────────────────────────────────────────────


class TestCommand:
    def test_ndjson_is_the_default_and_every_line_parses(self, bot):
        entry(bot)
        entry(bot, action="create", repr_="Grace", object_id="2")
        lines = run_command().splitlines()
        rows = [json.loads(line) for line in lines]
        assert [r["object_repr"] for r in rows] == ["Ada", "Grace"]
        row = rows[0]
        assert set(row) == set(agentactivity.ENTRY_COLUMNS)
        assert row["agent"] == "bot" and row["owner"] == "olivia"
        assert row["service_user"] == "svc" and row["run_key"] == "r1"
        assert row["model"] == "testapp.author" and row["ip"] == "10.0.0.5"
        assert row["changes"] == "name: A -> B"

    def test_json_is_one_array(self, bot):
        entry(bot)
        assert len(json.loads(run_command("--format", "json"))) == 1
        assert json.loads(run_command("--format", "json", "--agent", "nobody")) == []

    def test_csv_has_a_header_and_neutralises_formulas(self, bot):
        entry(bot, repr_="=cmd|'/c calc'!A1", changes={"name": {"from": "x", "to": "-2+3"}})
        rows = list(csv.reader(io.StringIO(run_command("--format", "csv"))))
        assert tuple(rows[0]) == agentactivity.ENTRY_COLUMNS
        cells = dict(zip(rows[0], rows[1], strict=True))
        assert cells["object_repr"].startswith("'=")
        assert cells["changes"].startswith("name:")  # no leading formula character
        entry(bot, repr_="@SUM(1)", object_id="9", changes={"x": {"from": None, "to": "+1"}})
        assert "'@SUM(1)" in run_command("--format", "csv")

    def test_control_characters_are_scrubbed(self, bot):
        entry(bot, repr_="Ada‮evil\x07​")
        row = json.loads(run_command())
        assert row["object_repr"] == "Adaevil"

    def test_a_sensitive_field_has_no_value_anywhere(self, bot):
        # Written before the field was known to be sensitive: the value is on disk.
        entry(
            bot,
            model=ZCredential,
            repr_="vault",
            changes={
                "api_token": {"from": "old-" + SECRET, "to": SECRET},
                "name": {"from": "a", "to": "b"},
            },
        )
        for fmt in ("csv", "json", "ndjson"):
            out = run_command("--format", fmt)
            assert SECRET not in out
            assert "api_token: changed (value not recorded)" in out
            assert "name: a -> b" in out

    def test_filters(self, bot, svc, owner):
        other, _t = agents.create_identity("other", svc, owner)
        old = entry(bot, repr_="old")
        VantageLogEntry.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        entry(bot, repr_="recent")
        run2 = AgentRun.objects.create(identity=bot, run_key="r2")
        entry(bot, run2, repr_="in-r2", action="delete", model=ZCredential)
        entry(other, repr_="theirs")
        # a human's entry never appears
        VantageLogEntry.objects.create(actor=svc, action="update", object_repr="human")

        def names(*args):
            return [json.loads(line)["object_repr"] for line in run_command(*args).splitlines()]

        assert names() == ["old", "recent", "in-r2", "theirs"]
        assert names("--since", "7d") == ["recent", "in-r2", "theirs"]
        assert names("--since", "24h", "--until", "1h") == []
        assert names("--agent", "other") == ["theirs"]
        assert names("--agent", "other", "--agent", "bot", "--run", "r2") == ["in-r2"]
        assert names("--model", "testapp.zcredential") == ["in-r2"]
        assert names("--action", "delete") == ["in-r2"]
        today = timezone.localdate().isoformat()
        assert names("--since", today, "--until", today) == ["recent", "in-r2", "theirs"]

    def test_bad_filters_are_command_errors(self, bot):
        for args in (
            ("--since", "yesterday-ish"),
            ("--model", "nope.nothing"),
            ("--action", "explode"),
            ("--runs", "--action", "update"),
        ):
            with pytest.raises(CommandError):
                run_command(*args)

    def test_runs_mode(self, bot):
        run = AgentRun.objects.create(
            identity=bot, run_key="batch-1", writes=4, rows_changed=9, deletes=1
        )
        rows = [json.loads(line) for line in run_command("--runs").splitlines()]
        assert rows == [
            {
                "agent": "bot",
                "run_key": "batch-1",
                "started": run.started_at.isoformat(),
                "last_seen": run.last_seen_at.isoformat(),
                "writes": 4,
                "rows_changed": 9,
                "deletes": 1,
            }
        ]
        assert run_command("--runs", "--run", "other") == ""

    def test_order_is_deterministic(self, bot):
        stamp = timezone.now()
        ids = [entry(bot, repr_=f"e{i}", object_id=str(i)).pk for i in range(4)]
        VantageLogEntry.objects.update(created_at=stamp)
        got = [json.loads(line)["object_id"] for line in run_command().splitlines()]
        assert got == [str(i) for i in range(4)]
        assert ids == sorted(ids)

    def test_output_file_and_the_force_guard(self, bot, tmp_path):
        entry(bot)
        target = tmp_path / "out.ndjson"
        call_command("vantage_agent_activity", output=str(target), stderr=io.StringIO())
        assert json.loads(target.read_text().splitlines()[0])["agent"] == "bot"
        before = target.read_text()
        with pytest.raises(CommandError, match="--force"):
            call_command("vantage_agent_activity", output=str(target), stderr=io.StringIO())
        assert target.read_text() == before
        entry(bot, repr_="again", object_id="3")
        call_command("vantage_agent_activity", output=str(target), force=True, stderr=io.StringIO())
        assert len(target.read_text().splitlines()) == 2

    def test_it_streams_in_one_query(self, bot):
        for i in range(30):
            entry(bot, repr_=f"e{i}", object_id=str(i))
        ContentType.objects.clear_cache()
        queryset = agentactivity.entry_queryset()
        with CaptureQueriesContext(connection) as queries:
            consumed = sum(
                1
                for _ in agentactivity.render(
                    agentactivity.ENTRY_COLUMNS, agentactivity.iter_entry_rows(queryset), "ndjson"
                )
            )
        assert consumed == 30
        on_log = [q for q in queries if "vantage_vantagelogentry" in q["sql"].lower()]
        assert len(on_log) == 1
        assert " LIMIT " not in on_log[0]["sql"].upper()

    def test_the_rows_are_lazy(self, bot):
        entry(bot)
        rows = agentactivity.iter_entry_rows(agentactivity.entry_queryset())
        assert iter(rows) is rows  # a generator, not a list


# ── the page ────────────────────────────────────────────────────────────────


@pytest.fixture
def boss(client):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def page_url():
    return reverse("vantage:page-agent-activity")


class TestPage:
    def test_it_renders_for_a_superuser(self, boss, bot, page_url):
        run = AgentRun.objects.create(identity=bot, run_key="nightly", writes=3, deletes=1)
        entry(bot, run, repr_="Ada")
        response = boss.get(page_url)
        assert response.status_code == 200
        body = response.content.decode()
        assert "bot" in body and "olivia" in body and "svc" in body
        assert "nightly" in body
        assert reverse("vantage:agent-activity-run", args=[run.pk]) in body
        assert "Download CSV" in body
        for word in ("Rotate", "Revoke this"):
            assert word not in body

    def test_it_is_in_the_navigation_only_when_agents_are_on(self, boss, page_url, settings):
        assert page_url in boss.get(reverse("vantage:dashboard")).content.decode()
        settings.VANTAGE = {"AGENTS": False}
        assert page_url not in boss.get(reverse("vantage:dashboard")).content.decode()

    def test_it_is_a_404_when_agents_are_off(self, boss, bot, page_url, settings):
        run = AgentRun.objects.create(identity=bot, run_key="k")
        settings.VANTAGE = {"AGENTS": False}
        assert boss.get(page_url).status_code == 404
        assert boss.get(reverse("vantage:agent-activity-run", args=[run.pk])).status_code == 404
        assert boss.get(reverse("vantage:agent-activity-download")).status_code == 404

    def test_a_user_without_the_permission_is_refused(self, client, bot, page_url):
        user = User.objects.create_user("plain", password="x", is_staff=True)
        client.force_login(user)
        run = AgentRun.objects.create(identity=bot, run_key="k")
        assert client.get(page_url).status_code == 403
        assert client.get(reverse("vantage:agent-activity-run", args=[run.pk])).status_code == 403
        assert client.get(reverse("vantage:agent-activity-download")).status_code == 403
        assert page_url not in client.get(reverse("vantage:dashboard")).content.decode()

    def test_the_audit_permission_opens_it(self, client, page_url):
        user = User.objects.create_user("auditor", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_vantagelogentry"))
        client.force_login(User.objects.get(pk=user.pk))
        assert client.get(page_url).status_code == 200

    def test_the_agent_filter_narrows_the_runs(self, boss, bot, svc, owner, page_url):
        other, _t = agents.create_identity("other", svc, owner)
        AgentRun.objects.create(identity=bot, run_key="bot-run")
        AgentRun.objects.create(identity=other, run_key="other-run")
        body = boss.get(page_url, {"agent": "other"}).content.decode()
        assert "other-run" in body and "bot-run" not in body

    def test_run_detail_lists_its_entries(self, boss, bot):
        run = AgentRun.objects.create(identity=bot, run_key="detail")
        entry(bot, run, repr_="Ada Lovelace", changes={"name": {"from": "A", "to": "Bee"}})
        entry(bot, AgentRun.objects.create(identity=bot, run_key="elsewhere"), repr_="Elsewhere")
        response = boss.get(reverse("vantage:agent-activity-run", args=[run.pk]))
        body = response.content.decode()
        assert response.status_code == 200
        assert "Ada Lovelace" in body and "name: A -&gt; Bee" in body
        assert "Elsewhere" not in body
        assert boss.get(reverse("vantage:agent-activity-run", args=[0])).status_code == 404

    def test_run_detail_masks_sensitive_values(self, boss, bot):
        run = AgentRun.objects.create(identity=bot, run_key="vault")
        entry(bot, run, model=ZCredential, changes={"api_token": {"from": None, "to": SECRET}})
        body = boss.get(reverse("vantage:agent-activity-run", args=[run.pk])).content.decode()
        assert SECRET not in body and "value not recorded" in body

    def test_the_download_uses_the_command_code_path(self, boss, bot):
        entry(bot, repr_="=evil()")
        url = reverse("vantage:agent-activity-download")
        response = boss.get(url, {"format": "csv"})
        assert response.status_code == 200 and response.streaming
        text = b"".join(response.streaming_content).decode()
        assert text == run_command("--format", "csv") or "'=evil()" in text
        assert "'=evil()" in text
        nd = b"".join(boss.get(url, {"format": "ndjson"}).streaming_content).decode()
        assert json.loads(nd.splitlines()[0])["agent"] == "bot"
        runs = b"".join(boss.get(url, {"format": "csv", "runs": "1"}).streaming_content).decode()
        assert runs.splitlines()[0].startswith("agent,run_key")
        # a download is itself audited
        assert VantageLogEntry.objects.filter(action_name="agent-activity.export").count() == 3

    def test_query_count_does_not_grow_with_runs(self, boss, bot, svc, owner, page_url):
        def seed(n):
            for i in range(n):
                other, _t = agents.create_identity(f"a{n}-{i}", svc, owner)
                AgentRun.objects.create(identity=other, run_key=f"k{n}-{i}", writes=i)

        seed(2)
        with CaptureQueriesContext(connection) as small:
            assert boss.get(page_url).status_code == 200
        seed(12)
        with CaptureQueriesContext(connection) as large:
            assert boss.get(page_url).status_code == 200
        assert len(large) == len(small)

    def test_nothing_on_the_page_mutates_an_identity(self, boss, bot, page_url):
        response = boss.post(page_url, {"revoke": "bot"})
        assert response.status_code == 405
        bot.refresh_from_db()
        assert bot.revoked_at is None


# ── the audit pages ─────────────────────────────────────────────────────────


class TestBadge:
    def test_an_agent_entry_shows_the_badge_and_run_key(self, boss, bot):
        entry(bot, AgentRun.objects.create(identity=bot, run_key="run-xyz"))
        body = boss.get(reverse("vantage:audit")).content.decode()
        assert 'data-tone="primary">Agent</span>' in body
        assert "run-xyz" in body

    def test_a_human_entry_has_none(self, boss, svc):
        VantageLogEntry.objects.create(
            actor=svc, actor_repr="svc", action="update", object_repr="Ada"
        )
        body = boss.get(reverse("vantage:audit")).content.decode()
        assert "Ada" in body and ">Agent</span>" not in body


def test_off_means_no_page_registered_routes_still_resolve():
    with override_settings(VANTAGE={"AGENTS": False}):
        assert reverse("vantage:agent-activity-download")
