"""History for changes made outside the panel (``TRACK_EXTERNAL_CHANGES``).

The promises, in the order a failure would hurt: a panel write is never recorded twice,
nothing is recorded (or queried) for a model that was not asked for, a secret never
reaches the log, and the recorder can never break the host application's save.
"""

import pytest
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core import serializers
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from tests import test_tenancy
from tests.testapp.models import Author, Post, Preference, Tag, ZCredential
from vantage import external, history
from vantage.models import VantageLogEntry, Webhook, WebhookDelivery
from vantage.sites import site

pytestmark = pytest.mark.django_db

_n = iter(range(10_000))

http_tenancy = test_tenancy.http_tenancy
orgs = test_tenancy.orgs
tenants = test_tenancy.tenants


@pytest.fixture
def tracked(settings):
    settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": ["testapp.preference", "testapp.zcredential"]}
    return settings


@pytest.fixture
def boss(client):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


def entries(obj=None, model=None):
    model = model or type(obj)
    found = VantageLogEntry.objects.filter(content_type=ContentType.objects.get_for_model(model))
    return found.filter(object_id=str(obj.pk)) if obj is not None else found


def outside(qs):
    return [e for e in qs if e.is_outside_panel]


class TestRecorded:
    def test_a_shell_create_is_recorded_as_outside_the_panel(self, tracked):
        pref = Preference.objects.create(key="theme", value="dark")
        (entry,) = entries(pref)
        assert entry.action == "create"
        assert entry.is_outside_panel and entry.action_name == "Created outside the panel"
        assert entry.outside_label == "Created outside the panel"
        assert entry.actor is None and entry.actor_repr == "system / unknown"
        assert entry.changes["value"] == {"from": None, "to": "dark"}

    def test_a_shell_update_records_before_and_after(self, tracked):
        pref = Preference.objects.create(key="theme", value="dark")
        pref.value = "light"
        pref.save()
        update = entries(pref).filter(action="update").get()
        assert update.is_outside_panel
        assert update.changes == {"value": {"from": "dark", "to": "light"}}

    def test_a_save_that_changed_nothing_writes_no_entry(self, tracked):
        pref = Preference.objects.create(key="theme", value="dark")
        before = entries(pref).count()
        pref.save()
        assert entries(pref).count() == before

    def test_update_fields_limits_the_diff(self, tracked):
        pref = Preference.objects.create(key="theme", value="dark")
        pref.key, pref.value = "colour", "light"
        pref.save(update_fields=["value"])
        update = entries(pref).filter(action="update").get()
        assert set(update.changes) == {"value"}

    def test_a_shell_delete_is_recorded_with_a_snapshot(self, tracked):
        pref = Preference.objects.create(key="theme", value="dark")
        pk = pref.pk
        pref.delete()
        delete = VantageLogEntry.objects.get(action="delete", object_id=str(pk))
        assert delete.is_outside_panel and delete.object_repr == "theme"
        assert delete.changes["value"] == {"from": "dark"}

    def test_a_many_to_many_change_is_recorded(self, settings):
        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": ["testapp.post"]}
        author = Author.objects.create(name="A", email="a@example.com")
        post = Post.objects.create(title="T", slug="t", body="b", author=author)
        tag = Tag.objects.create(name="news")
        post.tags.add(tag)
        change = entries(post).filter(action="update").get()
        assert change.changes["tags"] == {"from": [], "to": ["news"]}
        post.tags.add(tag)  # nothing moved
        assert entries(post).filter(action="update").count() == 1

    def test_the_request_user_is_the_actor_when_the_request_is_visible(self, tracked):
        from types import SimpleNamespace

        from vantage import tenancy

        user = User.objects.create_user("sam", password="x")
        request = SimpleNamespace(user=user, META={"REMOTE_ADDR": "10.0.0.5"})
        token = tenancy._ambient.set(request)
        try:
            pref = Preference.objects.create(key="k", value="v")
        finally:
            tenancy._ambient.reset(token)
        (entry,) = entries(pref)
        assert entry.actor == user and entry.actor_repr == "sam"

    def test_it_feeds_the_point_in_time_view(self, tracked, boss):
        pref = Preference.objects.create(key="theme", value="dark")
        first = entries(pref).get()
        pref.value = "light"
        pref.save()
        pref.refresh_from_db()
        panel = site.get_panel(Preference)
        rebuilt = history.reconstruct(panel, pref, history.Position(first.created_at, first.pk))
        state = next(s for s in rebuilt.fields if s.name == "value")
        assert state.known and state.value == "dark" and state.current == "light"


class TestNeverTwice:
    def test_a_panel_edit_is_recorded_once_and_by_the_panel(self, tracked, boss):
        pref = Preference.objects.create(key="theme", value="dark")
        VantageLogEntry.objects.all().delete()
        url = reverse("vantage:testapp-preference-change", args=[pref.pk])
        assert boss.post(url, {"key": "theme", "value": "light"}).status_code == 302
        (entry,) = VantageLogEntry.objects.all()
        assert not entry.is_outside_panel and entry.actor_repr == "boss"

    def test_a_panel_create_and_delete_are_recorded_once(self, tracked, boss):
        add = reverse("vantage:testapp-preference-add")
        assert boss.post(add, {"key": "a", "value": "b"}).status_code == 302
        pref = Preference.objects.get(key="a")
        assert [e.action for e in entries(pref)] == ["create"]
        assert not outside(entries(pref))
        delete = reverse("vantage:testapp-preference-delete", args=[pref.pk])
        assert boss.post(delete).status_code == 302
        assert VantageLogEntry.objects.filter(action="delete").count() == 1
        assert not outside(VantageLogEntry.objects.all())

    def test_the_panel_scope_stands_the_receivers_down(self, tracked):
        with external.panel_scope():
            Preference.objects.create(key="k", value="v")
        assert not VantageLogEntry.objects.exists()

    def test_a_background_job_is_inside_the_scope(self, tracked, monkeypatch):
        from vantage import jobs

        seen = []
        monkeypatch.setattr(jobs, "_execute", lambda job: seen.append(external.in_panel()))
        from vantage.models import VantageJob

        job = VantageJob.objects.create(
            actor=User.objects.create_user("w"), resource_key="testapp.preference", action_name="x"
        )
        jobs.run_job(job.pk)
        assert seen == [True]


class TestNotTracked:
    def test_an_untracked_model_is_not_recorded(self, tracked):
        Author.objects.create(name="A", email="a@example.com")
        assert not VantageLogEntry.objects.exists()

    def test_off_by_default_records_nothing(self, settings):
        settings.VANTAGE = {}
        Preference.objects.create(key="k", value="v")
        assert not VantageLogEntry.objects.exists()

    def test_an_untracked_save_costs_no_extra_queries(self, settings):
        def count():
            with CaptureQueriesContext(transaction.get_connection()) as ctx:
                author = Author.objects.create(name="A", email=f"a{next(_n)}@example.com")
                author.name = "B"
                author.save()
                author.delete()
            return len(ctx)

        settings.VANTAGE = {}
        baseline = count()
        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": ["testapp.preference"]}
        assert count() == baseline

    def test_star_means_every_registered_panel_model_and_never_the_audit_tables(self, settings):
        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": "*"}
        Preference.objects.create(key="k", value="v")
        assert VantageLogEntry.objects.count() == 1  # no entry about the entry

    def test_the_audit_model_cannot_be_tracked(self, settings):
        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": ["vantage.vantagelogentry"]}
        VantageLogEntry.objects.create(action="update")
        assert VantageLogEntry.objects.count() == 1

    def test_the_recorder_does_not_recurse(self, tracked, monkeypatch):
        calls = []
        real = external._write

        def spy(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(external, "_write", spy)
        Preference.objects.create(key="k", value="v")
        assert len(calls) == 1


class TestSkipped:
    def test_a_fixture_load_is_skipped(self, tracked):
        payload = serializers.serialize(
            "json", [Preference(pk=9, key="k", value="v", updated_at=timezone.now())]
        )
        for obj in serializers.deserialize("json", payload):
            obj.save()
        assert Preference.objects.filter(pk=9).exists()
        assert not VantageLogEntry.objects.exists()

    def test_a_raw_signal_is_skipped(self, tracked):
        pref = Preference(pk=3, key="k", value="v")
        post_save.send(Preference, instance=pref, created=True, raw=True, using="default")
        assert not VantageLogEntry.objects.exists()

    def test_a_historical_model_is_skipped(self, tracked):
        from django.apps.registry import Apps

        class Fake:
            class _meta:
                app_label = "testapp"
                abstract = False
                swapped = False
                apps = Apps()
                label_lower = "testapp.preference"

        assert external._is_tracked(Fake) is False  # type: ignore[arg-type]


class TestNeverBreaksTheSave:
    def test_an_error_while_building_the_entry_is_swallowed(self, tracked, monkeypatch, caplog):
        def boom(*args, **kwargs):
            raise RuntimeError("audit exploded")

        monkeypatch.setattr("vantage.audit._entry_kwargs", boom)
        pref = Preference.objects.create(key="k", value="v")
        assert Preference.objects.filter(pk=pref.pk).exists()
        assert "outside-the-panel" in caplog.text

    def test_a_database_error_in_the_recorder_does_not_poison_the_transaction(
        self, tracked, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise IntegrityError("log table gone")

        monkeypatch.setattr("vantage.models.VantageLogEntry.objects.using", lambda alias: _Boom())
        with transaction.atomic():
            pref = Preference.objects.create(key="k", value="v")
            # Still usable: the host's transaction was not broken.
            assert Preference.objects.filter(pk=pref.pk).exists()
        assert Preference.objects.filter(pk=pref.pk).exists()


class _Boom:
    def create(self, **kwargs):
        with transaction.atomic():
            Preference.objects.filter(pk=0).update(key=None)  # a real DB error
        raise IntegrityError("boom")


class TestSensitive:
    def test_a_secret_is_never_recorded_on_create_or_update(self, tracked):
        cred = ZCredential.objects.create(name="svc", api_token="tok-SECRET-1")
        cred.api_token = "tok-SECRET-2"
        cred.save()
        blob = " ".join(str(e.changes) for e in entries(cred))
        assert "SECRET" not in blob
        update = entries(cred).filter(action="update").get()
        assert update.changes == {"api_token": {"redacted": True}}
        cred.delete()
        assert "SECRET" not in " ".join(str(e.changes) for e in VantageLogEntry.objects.all())


class TestSwitches:
    def test_audit_off_records_nothing(self, settings):
        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": "*", "AUDIT_LOG": False}
        Preference.objects.create(key="k", value="v")
        assert not VantageLogEntry.objects.exists()

    @pytest.mark.parametrize("bad", ["yes", True, [1], ["nodot"], {"a": 1}])
    def test_a_bad_value_is_rejected(self, settings, bad):
        from vantage.conf import vantage_settings

        settings.VANTAGE = {"TRACK_EXTERNAL_CHANGES": bad}
        with pytest.raises(ImproperlyConfigured):
            vantage_settings.reload() if hasattr(vantage_settings, "reload") else (
                vantage_settings.TRACK_EXTERNAL_CHANGES
            )

    def test_webhooks_stay_quiet_unless_asked(self, settings, monkeypatch):
        from vantage import webhooks

        monkeypatch.setattr(webhooks, "_post", lambda *a, **k: (200, "ok"))
        Webhook.objects.create(
            name="h", url="https://hooks.example.com/in", secret="s", events=["created"]
        )
        base = {"TRACK_EXTERNAL_CHANGES": ["testapp.preference"], "WEBHOOKS": True}
        settings.VANTAGE = base
        Preference.objects.create(key="a", value="v")
        assert WebhookDelivery.objects.count() == 0
        with override_settings(VANTAGE={**base, "TRACK_EXTERNAL_WEBHOOKS": True}):
            Preference.objects.create(key="b", value="v")
        assert WebhookDelivery.objects.count() == 1


class TestTenancy:
    @pytest.mark.usefixtures("http_tenancy")
    def test_another_tenants_history_stays_hidden(self, settings, client, tenants):
        settings.VANTAGE = {**settings.VANTAGE, "TRACK_EXTERNAL_CHANGES": ["testapp.tntwidget"]}
        widget = test_tenancy.TntWidget.objects.create(org=tenants.org_a, name="W2")
        assert entries(widget).get().is_outside_panel
        client.force_login(User.objects.get(username="globex"))
        denied = client.get(
            reverse("vantage:testapp-tntwidget-history", args=[widget.pk]),
        )
        assert denied.status_code in (403, 404)


class TestSurfaces:
    def test_the_audit_page_filters_and_badges_outside_entries(self, tracked, boss):
        Preference.objects.create(key="shell", value="v")
        with external.panel_scope():
            pass
        boss.post(reverse("vantage:testapp-preference-add"), {"key": "panel", "value": "v"})
        url = reverse("vantage:audit") if _audit_mounted() else None
        if url is None:
            pytest.skip("audit page is not mounted in the test urlconf")
        page = boss.get(url, {"source": "outside"})
        keys = [row["entry"].object_repr for row in page.context["entries"]]
        assert keys == ["shell"]
        assert b"Outside the panel" in page.content
        panel_only = boss.get(url, {"source": "panel"})
        assert [r["entry"].object_repr for r in panel_only.context["entries"]] == ["panel"]

    def test_only_the_outside_rows_get_the_wrapping_cell(self, tracked, boss):
        import re

        Preference.objects.create(key="shell", value="v")
        boss.post(reverse("vantage:testapp-preference-add"), {"key": "panel", "value": "v"})
        body = boss.get(reverse("vantage:audit")).content.decode()
        cells = re.findall(
            r"<td(?: class=\"v-audit-what-wrap\")?>\s*<span class=\"v-audit-verb\".*?</td>",
            body,
            flags=re.S,
        )
        wrapped = [c for c in cells if "Outside the panel" in c]
        plain = [c for c in cells if "Outside the panel" not in c]
        assert len(wrapped) == 1 and wrapped[0].startswith('<td class="v-audit-what-wrap">')
        assert len(plain) == 1 and plain[0].startswith(
            '<td>\n              <span class="v-audit-verb"'
        )
        assert body.count("v-audit-what-wrap") == 1

    def test_the_record_history_panel_shows_the_badge(self, tracked, boss):
        pref = Preference.objects.create(key="theme", value="dark")
        page = boss.get(reverse("vantage:testapp-preference-history", args=[pref.pk]))
        assert page.status_code == 200
        assert b"Outside the panel" in page.content

    def test_a_history_entry_keeps_its_time_and_link_on_one_meta_row(self, tracked, boss):
        pref = Preference.objects.create(key="theme", value="dark")
        page = boss.get(reverse("vantage:testapp-preference-history", args=[pref.pk]))
        body = page.content.decode()
        head_end = body.index('<div class="v-history-meta">')
        assert body.index("</div>", body.index('class="v-history-head"')) < head_end
        meta = body[head_end : body.index("</div>", head_end)]
        assert "<time" in meta and 'class="v-history-asof"' in meta
        assert "v-btn" not in meta


def _audit_mounted() -> bool:
    try:
        reverse("vantage:audit")
        return True
    except Exception:
        return False


class TestNoCostWhenOff:
    def test_nothing_is_connected_while_tracking_is_off(self):
        from django.db.models.signals import post_delete, post_save, pre_save

        from vantage import external

        external.connect()
        for signal in (pre_save, post_save, post_delete):
            modules = {getattr(entry[1], "__module__", "") for entry in signal.receivers}
            assert "vantage.external" not in modules, (
                "a receiver on every model would switch off Django's fast paths"
            )

    def test_the_check_is_not_vacuous(self):
        from django.db.models.signals import post_save

        with override_settings(VANTAGE={"TRACK_EXTERNAL_CHANGES": "*"}):
            modules = {getattr(e[1], "__module__", "") for e in post_save.receivers}
            assert "vantage.external" in modules
