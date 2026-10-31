"""The data quality audit: each check finds its defect, stays bounded, and leaks no values.

Every defect has one field on ``DqContact`` (see the fixture), so a test reads as
"plant it, run the check, see it". The tests that matter most are the negative ones: a
clean table scores 100, an opted-out model is absent, a user without access gets a 403,
and no report ever contains the text of a row.
"""

from __future__ import annotations

import datetime as dt
import json
from io import StringIO

import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from tests.conftest import sqlite_only
from tests.testapp.models import Author, DqContact, Tag
from vantage import dataquality
from vantage.dataquality import Check, Finding, audit_model, register_check, unregister_check
from vantage.jobs import JobRequest
from vantage.permissions import get_permissions
from vantage.sites import site

URL = "vantage:page-data-quality"


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "b@example.com", "x")


@pytest.fixture(autouse=True)
def filterable(monkeypatch):
    """Inference offers one date filter; these tests need every field filterable."""
    monkeypatch.setattr(
        type(site.get_panel(DqContact)),
        "list_filter",
        ["created_at", "born_on", "updated_at", "ghost", "status"],
        raising=False,
    )


@pytest.fixture
def request_(boss):
    return JobRequest(user=boss)


def report_for(model, request, **kwargs):
    panel = site.get_panel(model)
    return audit_model(panel, request, get_permissions(), **kwargs)


def boss_request(user):
    return JobRequest(user=user)


def findings(report, check):
    return [f for f in report.findings if f.check == check]


class TestChecks:
    def test_a_clean_table_scores_100_and_reports_nothing_serious(self, request_):
        Tag.objects.create(name="a")
        report = report_for(Tag, request_)
        assert report.issues == []
        assert report.score == 100

    def test_empty_table_is_noted_not_penalised(self, request_):
        report = report_for(DqContact, request_)
        assert [f.check for f in report.findings] == ["empty"]
        assert report.score == 100

    def test_null_rate_counts_and_links_when_the_filter_can_say_it(self, request_):
        DqContact.objects.bulk_create(
            [DqContact(name="a", created_at=timezone.now()), DqContact(name="b")]
        )
        found = {f.field: f for f in findings(report_for(DqContact, request_), "nulls")}
        created = found["created_at"]
        assert (created.count, created.scanned) == (1, 2)
        assert created.url.endswith("?created_at=null")
        assert found["note"].severity == "warn"  # 100% empty
        # "" is not expressible by the filter, so a text field offers no link
        assert found["nickname"].url is None

    def test_duplicate_candidates_ignore_case_and_blank(self, request_):
        DqContact.objects.bulk_create(
            [
                DqContact(name="a", email="Dup@x.com"),
                DqContact(name="b", email="dup@x.com"),
                DqContact(name="c", email=""),
                DqContact(name="d", email=""),
            ]
        )
        (dup,) = findings(report_for(DqContact, request_), "duplicates")
        assert (dup.field, dup.count) == ("email", 2)

    def test_a_unique_field_is_not_a_duplicate_candidate(self, request_):
        Author.objects.bulk_create(
            [Author(name="a", email="a@x.com"), Author(name="b", email="b@x.com")]
        )
        assert findings(report_for(Author, request_), "duplicates") == []

    def test_orphaned_fk_without_a_constraint(self, request_):
        real = Author.objects.create(name="r", email="r@x.com")
        DqContact.objects.bulk_create(
            [DqContact(name="ok", ghost=real), DqContact(name="bad", ghost_id=99999)]
        )
        (orphan,) = findings(report_for(DqContact, request_), "orphans")
        assert (orphan.field, orphan.count, orphan.severity) == ("ghost", 1, "fail")

    def test_orphaned_generic_foreign_key_including_a_dead_content_type(self, request_):
        author_ct = ContentType.objects.get_for_model(Author)
        real = Author.objects.create(name="r", email="r@x.com")
        DqContact.objects.bulk_create(
            [
                DqContact(name="ok", content_type=author_ct, object_id=real.pk),
                DqContact(name="gone", content_type=author_ct, object_id=424242),
                DqContact(
                    name="nomodel",
                    content_type_id=ContentType.objects.create(app_label="nope", model="nope").pk,
                    object_id=1,
                ),
            ]
        )
        (orphan,) = findings(report_for(DqContact, request_), "orphans")
        assert (orphan.field, orphan.count) == ("target", 2)

    def test_values_outside_choices(self, request_):
        DqContact.objects.bulk_create(
            [DqContact(name="a", status="new"), DqContact(name="b", status="legacy")]
        )
        (bad,) = findings(report_for(DqContact, request_), "choices")
        assert (bad.field, bad.count) == ("status", 1)

    def test_suspicious_dates_link_to_a_window_the_filter_understands(self, request_):
        now = timezone.now()
        DqContact.objects.bulk_create(
            [
                DqContact(name="future", created_at=now + dt.timedelta(days=30)),
                DqContact(name="skew", created_at=now + dt.timedelta(hours=2)),
                DqContact(name="ancient", born_on=dt.date(1850, 1, 1)),
            ]
        )
        found = {f.field: f for f in findings(report_for(DqContact, request_), "dates")}
        assert found["created_at"].count == 1  # a few hours of skew is tolerated
        assert ".." in found["created_at"].url
        assert found["born_on"].count == 1
        assert found["born_on"].url.endswith("..1899-12-31")

    def test_stale_table(self, request_):
        DqContact.objects.create(name="old")
        DqContact.objects.update(updated_at=timezone.now() - dt.timedelta(days=900))
        (stale,) = findings(report_for(DqContact, request_), "stale")
        assert "900" in stale.message
        assert stale.url

    def test_a_fresh_table_is_not_stale(self, request_):
        DqContact.objects.create(name="new")
        assert findings(report_for(DqContact, request_), "stale") == []

    def test_the_linked_list_page_really_filters_to_the_offending_rows(self, client, boss):
        DqContact.objects.bulk_create(
            [DqContact(name="alpha", created_at=timezone.now()), DqContact(name="bravo")]
        )
        client.force_login(boss)
        link = {
            f.field: f.url for f in findings(report_for(DqContact, boss_request(boss)), "nulls")
        }
        body = client.get(link["created_at"]).content.decode()
        assert "bravo" in body and "alpha" not in body


class TestNoValuesLeak:
    @sqlite_only
    def test_no_report_text_contains_row_data(self, client, boss):
        secret = "zzz-very-secret-value"
        DqContact.objects.bulk_create(
            [DqContact(name=secret, email=secret), DqContact(name="b", email=secret, status=secret)]
        )
        client.force_login(boss)
        page = client.get(reverse(URL)).content.decode()
        detail = client.get(reverse(URL), {"model": "testapp.dqcontact"}).content.decode()
        assert secret not in page and secret not in detail
        assert "email" in detail

        out = StringIO()
        call_command("vantage_data_quality", "--format", "json", stdout=out)
        assert secret not in out.getvalue()


class TestBounds:
    @sqlite_only
    def test_large_tables_are_sampled_and_say_so(self, request_):
        DqContact.objects.bulk_create([DqContact(name=str(i), email="e") for i in range(30)])
        with override_settings(VANTAGE={"DATA_QUALITY": {"SAMPLE_SIZE": 10}}):
            report = report_for(DqContact, request_)
        assert report.sampled and report.scanned == 10
        assert report.rows_display == "10+"
        dup = findings(report, "duplicates")[0]
        assert dup.sampled and dup.count == 10

    def test_estimated_giant_tables_are_skipped(self, request_, monkeypatch):
        from vantage import scale

        DqContact.objects.create(name="a")
        monkeypatch.setattr(
            scale,
            "count_rows",
            lambda *a, **k: scale.RowCount(9_000_000, exact=False, estimated=True),
        )
        with override_settings(VANTAGE={"DATA_QUALITY": {"SKIP_ABOVE": 1000}}):
            report = report_for(DqContact, request_)
        assert report.skipped and report.score is None
        assert {c.status for c in report.checks if c.name != "empty"} == {"skipped"}
        assert report.rows_display == "~9,000,000"

    def test_a_check_past_its_budget_keeps_what_it_found_and_is_marked(self, request_):
        DqContact.objects.create(name="a")

        class Slow(Check):
            name = "slow"
            label = "Slow"

            def run(self, ctx):
                yield ctx.finding("slow", "info", "before")
                ctx._deadline = 0
                ctx.tick()
                yield ctx.finding("slow", "info", "after")  # pragma: no cover

        report = report_for(DqContact, request_, checks=[Slow()])
        assert [f.message for f in report.findings] == ["before"]
        assert report.checks[0].status == "timeout"
        assert report.partial

    def test_a_raising_check_does_not_take_the_report_down(self, request_):
        class Boom(Check):
            name = "boom"

            def run(self, ctx):
                raise RuntimeError("secret detail")

        report = report_for(DqContact, request_, checks=[Boom()])
        assert report.checks[0].status == "error"
        assert report.checks[0].reason == "RuntimeError"  # the class, never the message

    @sqlite_only
    def test_query_count_is_flat_in_row_count(self, request_, django_assert_max_num_queries):
        DqContact.objects.bulk_create([DqContact(name=str(i), email="e") for i in range(5)])
        with django_assert_max_num_queries(40) as small:
            report_for(DqContact, request_)
        DqContact.objects.bulk_create([DqContact(name=str(i), email="e") for i in range(200)])
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as big:
            report_for(DqContact, request_)
        assert len(big) == len(small.captured_queries)


class TestCaching:
    def test_second_run_is_served_from_cache_and_force_bypasses_it(self, request_):
        DqContact.objects.create(name="a")
        first = dataquality.run_audit(site, request_)
        DqContact.objects.bulk_create([DqContact(name="b"), DqContact(name="c")])
        cached = dataquality.run_audit(site, request_)
        dq = lambda audit: next(r for r in audit.reports if r.model == "testapp.dqcontact")  # noqa: E731
        assert dq(cached).rows == dq(first).rows == 1
        assert dq(dataquality.run_audit(site, request_, force=True)).rows == 3

    def test_page_budget_leaves_the_rest_pending_not_failed(self, request_):
        audit = dataquality.run_audit(site, request_, page_budget=-1)
        assert audit.reports == [] and audit.pending

    def test_thresholds_are_part_of_the_cache_key(self, request_):
        DqContact.objects.create(name="a")
        dataquality.run_audit(site, request_)
        with override_settings(VANTAGE={"DATA_QUALITY": {"STALE_DAYS": 1}}):
            audit = dataquality.run_audit(site, request_, labels=["testapp.dqcontact"])
        assert audit.reports[0].ran_at >= audit.reports[0].ran_at  # recomputed, no error


class TestOptOut:
    def test_panel_false_removes_the_model(self, request_, monkeypatch):
        monkeypatch.setattr(type(site.get_panel(DqContact)), "data_quality", False, raising=False)
        audit = dataquality.run_audit(site, request_)
        assert "testapp.dqcontact" not in {r.model for r in audit.reports}

    def test_panel_list_skips_named_checks_only(self, request_, monkeypatch):
        monkeypatch.setattr(
            type(site.get_panel(DqContact)), "data_quality", ["nulls", "dates"], raising=False
        )
        DqContact.objects.create(name="a")
        names = {c.name for c in report_for(DqContact, request_).checks}
        assert "nulls" not in names and "dates" not in names and "choices" in names

    def test_global_exclude(self, request_):
        DqContact.objects.create(name="a")
        with override_settings(VANTAGE={"DATA_QUALITY": {"CHECKS_EXCLUDE": ["stale"]}}):
            assert "stale" not in {c.name for c in report_for(DqContact, request_).checks}

    def test_panel_check_rejects_unknown_names(self):
        from vantage.panel_checks import ModelPanelChecks

        panel = site.get_panel(DqContact)
        panel.data_quality = ["nope"]
        try:
            errors = ModelPanelChecks()._check_data_quality(panel)
            assert [e.id for e in errors] == ["vantage.E063"]
            panel.data_quality = ["nulls"]
            assert ModelPanelChecks()._check_data_quality(panel) == []
            panel.data_quality = True
            assert ModelPanelChecks()._check_data_quality(panel)
        finally:
            del panel.data_quality


class TestExtensibility:
    def test_a_third_party_check_registers_and_runs(self, request_):
        @register_check
        class AlwaysCheck(Check):
            name = "always"
            label = "Always"

            def run(self, ctx):
                yield Finding("always", "warn", "custom", count=1)

        try:
            DqContact.objects.create(name="a")
            report = report_for(DqContact, request_)
            assert findings(report, "always")
        finally:
            unregister_check("always")


class TestPage:
    def test_superuser_sees_overview_and_drilldown(self, client, boss):
        DqContact.objects.bulk_create(
            [DqContact(name="a", email="x"), DqContact(name="b", email="x")]
        )
        client.force_login(boss)
        response = client.get(reverse(URL))
        assert response.status_code == 200
        assert "Dq contacts" in response.content.decode()
        drill = client.get(reverse(URL), {"model": "testapp.dqcontact"})
        text = drill.content.decode()
        assert "duplicates" in text and "View rows" in text

    def test_staff_without_permission_is_denied(self, client, db):
        user = User.objects.create_user("s", "s@x.com", "x", is_staff=True)
        client.force_login(user)
        assert client.get(reverse(URL)).status_code == 403

    def test_permission_setting_widens_access_but_models_stay_gated(self, client, db):
        DqContact.objects.create(name="a")
        user = User.objects.create_user("s", "s@x.com", "x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_dqcontact"))
        client.force_login(user)
        with override_settings(VANTAGE={"DATA_QUALITY": {"PERMISSION": lambda r: True}}):
            text = client.get(reverse(URL)).content.decode()
        assert "Dq contacts" in text
        assert "Authors" not in text

    def test_disabled_hides_the_page(self, client, boss):
        client.force_login(boss)
        with override_settings(VANTAGE={"DATA_QUALITY": {"ENABLED": False}}):
            assert client.get(reverse(URL)).status_code == 403

    def test_rerun_posts_then_redirects_and_refreshes(self, client, boss):
        DqContact.objects.create(name="a")
        client.force_login(boss)
        client.get(reverse(URL))
        DqContact.objects.create(name="b")
        response = client.post(reverse(URL), {"model": "testapp.dqcontact", "stay": "1"})
        assert response.status_code == 302 and response["Location"].endswith("dqcontact")
        text = client.get(reverse(URL), {"model": "testapp.dqcontact"}).content.decode()
        assert ">2<" in text.replace(" ", "").replace("\n", "")

    def test_get_only_pages_still_refuse_post(self, client, boss):
        client.force_login(boss)
        assert client.post(reverse("vantage:page-health")).status_code == 405

    def test_unknown_setting_key_is_rejected_with_a_hint(self):
        from django.core.exceptions import ImproperlyConfigured

        with override_settings(VANTAGE={"DATA_QUALITY": {"SAMPLE_SIZES": 1}}):
            with pytest.raises(ImproperlyConfigured, match="SAMPLE_SIZE"):
                dataquality.config()


class TestCommand:
    def run(self, *args):
        out = StringIO()
        call_command("vantage_data_quality", *args, stdout=out)
        return out.getvalue()

    def test_prints_the_report(self, boss):
        DqContact.objects.create(name="a", status="bad")
        text = self.run("testapp.DqContact")
        assert "testapp.dqcontact" in text and "[warn] choices" in text

    def test_json(self, boss):
        DqContact.objects.create(name="a")
        data = json.loads(self.run("--format", "json", "testapp.dqcontact"))
        assert data[0]["model"] == "testapp.dqcontact"

    def test_exit_status_for_ci(self, boss):
        DqContact.objects.create(name="a", status="bad")
        with pytest.raises(SystemExit) as exc:
            self.run("--fail-on", "warn", "testapp.dqcontact")
        assert exc.value.code == 1
        assert self.run("--fail-on", "fail", "testapp.dqcontact")  # warn < fail: exits 0

    def test_needs_a_user_and_a_known_model(self, db):
        with pytest.raises(CommandError, match="superuser"):
            self.run()
        User.objects.create_superuser("boss", "b@x.com", "x")
        with pytest.raises(CommandError, match="Not an auditable"):
            self.run("testapp.nothing")

    def test_disabled(self, boss):
        with override_settings(VANTAGE={"DATA_QUALITY": {"ENABLED": False}}):
            with pytest.raises(CommandError, match="disabled"):
                self.run()
