"""Background jobs: the threshold, the chunked runner, and what it refuses to hide.

The routes are mounted by this module rather than by ``tests/urls.py``. ``vantage.urls``
does not yet include ``jobviews.build_urls`` — that is one line the integrator adds —
so the urlconf below is the same composition, built here so the views can be exercised
before the wiring lands. Everything else resolves exactly as it does in ``tests.urls``.
"""

import csv
import io

import pytest
from django.contrib.auth.models import Permission, User
from django.urls import include, path, reverse

from tests.testapp.models import Archive, Promo
from vantage import jobs, jobviews
from vantage import urls as vantage_urls
from vantage.actions import Selection, action
from vantage.models import VantageJob
from vantage.panels import ModelPanel
from vantage.sites import site

_patterns = vantage_urls.build(site) + jobviews.build_urls(site)
urlpatterns = [path("admin/", include((_patterns, "vantage"), namespace="vantage"))]


@pytest.fixture(autouse=True)
def _mounted(settings):
    settings.ROOT_URLCONF = "tests.test_jobs"


# ── panels the tests hang actions on ────────────────────────────────────────


class TouchPanel(ModelPanel):
    """A plain action over ``Promo``, cheap and always succeeding."""

    @action("Touch", permission="change")
    def touch(self, request, queryset):
        return f"Touched {queryset.update(headline='touched')}."


class FlakyPanel(ModelPanel):
    """Fails on any row whose headline contains ``bad``, and only on those rows.

    The point of the fixture is that the failures are *scattered*: they land in
    different chunks, so a runner that gave up on the first bad chunk, or that
    condemned the whole chunk around one bad row, would produce visibly wrong counts.
    """

    @action("Publish", permission="change")
    def publish(self, request, queryset):
        for promo in queryset:
            if "bad" in promo.headline:
                raise ValueError(f"{promo.headline} cannot be published.")
            promo.is_published = True
            promo.save(update_fields=["is_published"])
        return f"Published {queryset.count()}."


class InjectingPanel(ModelPanel):
    """Raises with the record's own name in the message — the CSV-injection path."""

    @action("Check", permission="change")
    def check(self, request, queryset):
        for promo in queryset:
            raise ValueError(f"{promo.headline} is not a valid headline.")
        return "ok"


class RevokingPanel(ModelPanel):
    """Takes the actor's permission away half way through its own run.

    A contrived way to reach an entirely uncontrived situation: a job sits in a queue
    for an hour, and somebody is moved out of a group while it is running.
    """

    calls = 0

    @action("Sweep", permission="change")
    def sweep(self, request, queryset):
        RevokingPanel.calls += 1
        if RevokingPanel.calls == 1:
            request.user.user_permissions.clear()
        return f"Swept {queryset.update(headline='swept')}."


@pytest.fixture
def use_panel():
    """Swap the registered panel for one model, and put the original back.

    Direct registry surgery rather than register/unregister: every model in the test
    app is auto-registered, so ``register`` would raise, and ``unregister`` followed by
    ``register`` would leave the site in a different state if the test failed between
    the two.
    """
    swapped = []

    def _swap(model, panel_class):
        panel = panel_class(model, site)
        swapped.append((model, site.registry._panels.get(model)))
        site.registry._panels[model] = panel
        return panel

    yield _swap

    for model, original in reversed(swapped):
        if original is None:
            site.registry._panels.pop(model, None)
        else:
            site.registry._panels[model] = original


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def clerk(db):
    """Staff, with ``change_promo`` and nothing else."""
    user = User.objects.create_user("clerk", "clerk@example.com", "x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="change_promo"))
    user.user_permissions.add(Permission.objects.get(codename="view_promo"))
    return user


def promos(count, *, bad=()):
    return [
        Promo.objects.create(headline=f"bad {i}" if i in bad else f"promo {i}")
        for i in range(count)
    ]


def request_for(user, query=""):
    return jobs.JobRequest(user=user, query=query, path="/admin/testapp/promo/")


def all_selected():
    return Selection(select_all=True, ids=[], excluded=[])


def enqueue(panel, user, *, name, query="", excluded=(), count=None):
    request = request_for(user, query)
    chosen = next(a for a in _actions(panel) if a.name == name)
    selection = Selection(select_all=True, ids=[], excluded=[str(pk) for pk in excluded])
    total = count if count is not None else panel.model._default_manager.count()
    return jobs.enqueue(
        request=request, panel=panel, action=chosen, selection=selection, count=total
    )


def _actions(panel):
    from vantage.actions import resolve_actions

    return resolve_actions(panel)


# ── the threshold ───────────────────────────────────────────────────────────


class TestThreshold:
    """§11: under ~2 seconds of work, run inline; above it, enqueue."""

    def small(self):
        return _actions(TouchPanel(Promo))[0]

    def test_a_small_action_still_runs_inline(self):
        """Nine rows must not acquire a job row, a poller and a redirect."""
        assert jobs.should_enqueue(self.small(), 9) is False
        assert jobs.should_enqueue(self.small(), 200) is False

    def test_a_large_action_is_enqueued(self):
        assert jobs.should_enqueue(self.small(), 40_000) is True

    def test_the_boundary_is_the_two_second_budget_and_not_a_row_count(self):
        """1,000 instance saves is exactly the budget, so it is the last inline run."""
        act = self.small()
        assert jobs.estimate_seconds(act, 1_000) == pytest.approx(2.0)
        assert jobs.should_enqueue(act, 1_000) is False
        assert jobs.should_enqueue(act, 1_001) is True

    def test_a_sql_strategy_action_stays_inline_far_longer(self):
        """``queryset.update()`` is one statement; charging it per row would enqueue
        work that finishes before the redirect does."""
        act = self.small()
        act.bulk_strategy = "sql"
        assert jobs.should_enqueue(act, 40_000) is False
        assert jobs.should_enqueue(act, 200_000) is True

    def test_an_undeclared_strategy_is_assumed_expensive(self):
        """Guessing cheap and being wrong is a timeout; guessing expensive is a row.

        Written before ``bulk_strategy`` was declared on ``Action``, when the point
        was that a missing attribute still costs the expensive way. It is now a real
        field defaulting to the same answer, so the assertion moved from "the
        attribute is absent" to "an action that says nothing is costed as instances".
        """
        act = self.small()
        assert act.bulk_strategy == "instances"
        assert jobs.estimate_seconds(act, 100) == pytest.approx(0.2)

    def test_an_action_object_with_the_field_stripped_still_costs_the_safe_way(self):
        """jobs.py reads it defensively, so an Action from an older release is safe."""
        act = self.small()
        del act.bulk_strategy
        assert jobs.estimate_seconds(act, 100) == pytest.approx(0.2)

    def test_switching_jobs_off_keeps_everything_inline(self, monkeypatch):
        real = jobs.setting
        monkeypatch.setattr(
            jobs, "setting", lambda name, default: False if name == "JOBS" else real(name, default)
        )
        assert jobs.should_enqueue(self.small(), 40_000) is False


# ── backend detection ───────────────────────────────────────────────────────


class TestBackendDetection:
    def test_with_no_worker_the_in_process_runner_is_chosen(self):
        backend = jobs.detect_backend()
        assert backend is jobs.IN_PROCESS
        assert backend.deferred is False
        assert "no task backend" in backend.hint.lower()

    def test_django_tasks_immediate_backend_is_not_counted_as_a_queue(self):
        """``TASKS`` has a default on Django 6.x. Its presence proves nothing."""
        assert jobs._django_tasks_configured() is False

    def test_a_configured_django_tasks_backend_wins(self, monkeypatch):
        monkeypatch.setattr(jobs, "_django_tasks_configured", lambda: True)
        assert jobs.detect_backend() is jobs.DJANGO_TASKS

    def test_celery_on_the_path_without_a_broker_is_not_a_queue(self, monkeypatch):
        monkeypatch.setattr("vantage.health._importable", lambda name: name == "celery")
        monkeypatch.setattr(jobs, "_celery_has_broker", lambda: False)
        assert jobs.detect_backend() is jobs.IN_PROCESS

    def test_celery_with_a_broker_is(self, monkeypatch):
        monkeypatch.setattr("vantage.health._importable", lambda name: name == "celery")
        monkeypatch.setattr(jobs, "_celery_has_broker", lambda: True)
        assert jobs.detect_backend() is jobs.CELERY

    def test_the_django_tasks_submit_path_actually_enqueues(self, db, boss, use_panel):
        """Exercises the ``django.tasks`` call signature for real.

        Detection would not pick this backend here — the configured one is
        ``ImmediateBackend``, which is not a queue — but the enqueue call is the same
        one a project with a worker makes, and getting its shape wrong is a failure
        that only shows up in production. ``ImmediateBackend`` runs the task in-thread,
        so the job also completes, which is the second half of the proof.
        """
        pytest.importorskip("django.tasks", reason="django.tasks arrived in Django 6.0")
        panel = use_panel(Promo, TouchPanel)
        promos(3)
        job = enqueue(panel, boss, name="touch")
        VantageJob.objects.filter(pk=job.pk).update(
            status=VantageJob.Status.PENDING, processed=0, backend="django_tasks"
        )

        jobs._submit_deferred(job.pk, "django_tasks")

        job.refresh_from_db()
        assert job.status == VantageJob.Status.SUCCEEDED
        assert job.processed == 3
        assert job.task_id  # the backend's own identifier, recorded

    def test_a_deferred_backend_is_submitted_only_after_the_row_is_committed(
        self, db, boss, django_capture_on_commit_callbacks, monkeypatch
    ):
        """A worker that reads the row before the transaction commits sees nothing."""
        sent = []
        monkeypatch.setattr(jobs, "_submit_deferred", lambda pk, name: sent.append((pk, name)))
        job = VantageJob.objects.create(
            actor=boss, resource_key="testapp.promo", action_name="touch", backend="celery"
        )

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            jobs._submit(job)
        assert sent == []
        assert len(callbacks) == 1

        callbacks[0]()
        assert sent == [(job.pk, "celery")]

    def test_the_in_process_runner_is_not_deferred_to_commit(self, db, boss, use_panel):
        """Deferring it would mean it never ran inside a ``TestCase`` — or a request
        that rolled back — which is the one case it exists to cover."""
        panel = use_panel(Promo, TouchPanel)
        promos(2)
        job = enqueue(panel, boss, name="touch")
        assert job.status == VantageJob.Status.SUCCEEDED

    def test_rq_requires_the_app_to_be_installed_not_merely_importable(self, monkeypatch):
        monkeypatch.setattr("vantage.health._importable", lambda name: name == "django_rq")
        monkeypatch.setattr(jobs, "_installed", lambda label: False)
        assert jobs.detect_backend() is jobs.IN_PROCESS
        monkeypatch.setattr(jobs, "_installed", lambda label: True)
        assert jobs.detect_backend() is jobs.RQ


# ── serialisation ───────────────────────────────────────────────────────────


class TestSelectionRoundTrip:
    def test_a_select_all_payload_never_materialises_ids(self, db, boss):
        promos(50)
        payload = jobs.selection_payload(all_selected(), request_for(boss, "q=promo"), count=50)
        assert payload["ids"] == []
        assert payload["select_all"] is True
        assert payload["query"] == "q=promo"
        assert payload["count"] == 50

    def test_the_stored_selection_resolves_to_exactly_the_same_rows(self, db, boss, use_panel):
        """The property the whole design rests on.

        The job stores a predicate and an exclusion set; running it must touch the same
        records the list page would have, filters and unticked rows included.
        """
        panel = use_panel(Promo, TouchPanel)
        made = promos(12)
        excluded = [made[0].pk, made[5].pk]

        expected = set(Promo.objects.exclude(pk__in=excluded).values_list("pk", flat=True))

        job = enqueue(panel, boss, name="touch", excluded=excluded)

        assert job.status == VantageJob.Status.SUCCEEDED
        assert job.total == len(expected)
        touched = set(Promo.objects.filter(headline="touched").values_list("pk", flat=True))
        assert touched == expected

    def test_a_filter_in_the_stored_query_string_narrows_the_job(self, db, boss, use_panel):
        panel = use_panel(Promo, TouchPanel)
        promos(6)
        Promo.objects.filter(headline__in=["promo 0", "promo 1"]).update(is_published=True)

        request = request_for(boss, "is_published=1")
        chosen = next(a for a in _actions(panel) if a.name == "touch")
        job = jobs.enqueue(
            request=request, panel=panel, action=chosen, selection=all_selected(), count=2
        )

        assert job.total == 2
        assert Promo.objects.filter(headline="touched").count() == 2


# ── the in-process fallback ─────────────────────────────────────────────────


class TestInProcessFallback:
    def test_a_job_runs_end_to_end_with_no_worker_configured(self, db, boss, use_panel):
        panel = use_panel(Promo, TouchPanel)
        promos(7)

        job = enqueue(panel, boss, name="touch")

        assert job.backend == "inprocess"
        assert job.status == VantageJob.Status.SUCCEEDED
        assert (job.processed, job.total, job.failed) == (7, 7, 0)
        assert job.summary == "7 succeeded"
        assert job.started_at is not None and job.finished_at is not None
        assert Promo.objects.filter(headline="touched").count() == 7

    def test_the_job_records_who_ran_it_and_over_what(self, db, boss, use_panel):
        panel = use_panel(Promo, TouchPanel)
        promos(2)
        job = enqueue(panel, boss, name="touch")
        assert job.actor == boss
        assert job.actor_repr == "boss"
        assert job.resource_key == "testapp.promo"
        assert job.action_name == "touch"
        assert job.action_label == "Touch"

    def test_a_duplicate_delivery_does_not_apply_the_action_twice(self, db, boss, use_panel):
        """Every queue here promises at-least-once. The claim has to be conditional."""
        panel = use_panel(Promo, RevokingPanel)
        RevokingPanel.calls = 0
        promos(3)
        job = enqueue(panel, boss, name="sweep")
        assert job.status == VantageJob.Status.SUCCEEDED

        jobs.run_job(job.pk)
        assert RevokingPanel.calls == 1

    def test_an_empty_selection_finishes_honestly_rather_than_erroring(self, db, boss, use_panel):
        panel = use_panel(Promo, TouchPanel)
        job = enqueue(panel, boss, name="touch", count=0)
        assert job.status == VantageJob.Status.SUCCEEDED
        assert job.summary == "nothing to do"

    def test_the_count_shown_at_selection_time_is_never_reported_as_the_result(
        self, db, boss, use_panel
    ):
        """§11: rows move between the click and the run. Say so; do not echo the click."""
        panel = use_panel(Promo, TouchPanel)
        promos(4)
        job = enqueue(panel, boss, name="touch", count=99)
        assert job.total == 4
        assert "99 were selected; 4 still matched" in job.summary


# ── partial failure ─────────────────────────────────────────────────────────


class TestPartialFailure:
    @pytest.fixture(autouse=True)
    def _small_chunks(self, monkeypatch):
        """Two rows per chunk, so the failures straddle chunk boundaries."""
        monkeypatch.setattr(jobs, "chunk_size", lambda: 2)

    def test_the_counts_are_honest_and_name_both_outcomes(self, db, boss, use_panel):
        panel = use_panel(Promo, FlakyPanel)
        promos(10, bad={1, 4, 9})

        job = enqueue(panel, boss, name="publish")

        assert (job.processed, job.total, job.failed) == (10, 10, 3)
        assert job.succeeded == 7
        assert job.summary == "7 succeeded, 3 failed"
        assert job.status == VantageJob.Status.SUCCEEDED

    def test_a_failing_chunk_does_not_condemn_the_rows_around_it(self, db, boss, use_panel):
        """The per-chunk rollback plus the row-by-row retry, observed on the data."""
        panel = use_panel(Promo, FlakyPanel)
        promos(10, bad={1, 4, 9})

        enqueue(panel, boss, name="publish")

        published = set(Promo.objects.filter(is_published=True).values_list("headline", flat=True))
        assert published == {
            "promo 0",
            "promo 2",
            "promo 3",
            "promo 5",
            "promo 6",
            "promo 7",
            "promo 8",
        }
        assert Promo.objects.filter(is_published=True, headline__contains="bad").count() == 0

    def test_every_failure_is_recorded_with_its_row_and_its_reason(self, db, boss, use_panel):
        """ "Some rows failed" is the sentence this makes impossible to produce."""
        panel = use_panel(Promo, FlakyPanel)
        promos(6, bad={2})

        job = enqueue(panel, boss, name="publish")

        assert len(job.errors) == 1
        entry = job.errors[0]
        assert entry["pk"] == str(Promo.objects.get(headline="bad 2").pk)
        assert entry["error"] == "bad 2 cannot be published."
        assert entry["row"] == 3

    def test_the_error_csv_lists_each_failure(self, db, boss, use_panel):
        panel = use_panel(Promo, FlakyPanel)
        promos(6, bad={0, 3})

        job = enqueue(panel, boss, name="publish")
        rows = list(csv.reader(io.StringIO(jobs.error_csv_bytes(job).decode("utf-8"))))

        assert rows[0] == ["Row", "Record", "Error"]
        assert len(rows) == 3
        assert [row[2] for row in rows[1:]] == [
            "bad 0 cannot be published.",
            "bad 3 cannot be published.",
        ]

    def test_truncation_is_declared_rather_than_silent(self, db, boss, use_panel, monkeypatch):
        """The count stays exact past the cap; only the individual messages stop."""
        monkeypatch.setattr(jobs, "max_stored_errors", lambda: 2)
        panel = use_panel(Promo, FlakyPanel)
        promos(6, bad={0, 1, 2, 3})

        job = enqueue(panel, boss, name="publish")

        assert job.failed == 4
        assert len(job.errors) == 2
        rows = list(csv.reader(io.StringIO(jobs.error_csv_bytes(job).decode("utf-8"))))
        assert "2 further failures were not recorded individually." in rows[-1][2]

    def test_a_whole_job_failure_is_not_reported_as_a_success(self, db, boss):
        promos(3)
        job = VantageJob.objects.create(
            actor=boss,
            actor_repr="boss",
            resource_key="testapp.nosuchmodel",
            action_name="touch",
            selection={"select_all": True},
            backend="inprocess",
        )
        jobs.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == VantageJob.Status.FAILED
        assert "No panel is registered" in job.error
        assert job.summary == "Nothing ran."


class TestCsvInjection:
    def test_a_formula_cell_is_neutralised(self, db, boss, use_panel):
        """The error report writes attacker-influenced text back out as CSV.

        Excel, LibreOffice and Sheets all evaluate a leading ``=``, ``+``, ``-`` or
        ``@`` on open, so the prefix goes on before the cell is written.
        """
        panel = use_panel(Promo, InjectingPanel)
        Promo.objects.create(headline="=cmd|'/c calc'!A1")
        Promo.objects.create(headline="+1+1")
        Promo.objects.create(headline="@SUM(A1)")
        Promo.objects.create(headline="-2")

        job = enqueue(panel, boss, name="check")
        rows = list(csv.reader(io.StringIO(jobs.error_csv_bytes(job).decode("utf-8"))))

        assert job.failed == 4
        messages = sorted(row[2] for row in rows[1:])
        assert messages == [
            "'+1+1 is not a valid headline.",
            "'-2 is not a valid headline.",
            "'=cmd|'/c calc'!A1 is not a valid headline.",
            "'@SUM(A1) is not a valid headline.",
        ]


# ── permissions ─────────────────────────────────────────────────────────────


class TestPermissionIsRecheckedAtExecutionTime:
    """Rights can be revoked between enqueue and execution. Enqueue-time is not enough."""

    def test_a_permission_revoked_mid_job_stops_it_where_it_stands(
        self, db, clerk, use_panel, monkeypatch
    ):
        """The named test. Two rows in, the actor loses ``change_promo``.

        The run stops, the status says why, and the two rows already committed stay
        committed — they were legitimate work done while the permission held.
        """
        monkeypatch.setattr(jobs, "chunk_size", lambda: 2)
        panel = use_panel(Promo, RevokingPanel)
        RevokingPanel.calls = 0
        promos(8)

        job = enqueue(panel, clerk, name="sweep")

        assert job.status == VantageJob.Status.DENIED
        assert job.processed == 2
        assert Promo.objects.filter(headline="swept").count() == 2
        assert "may no longer change" in job.error
        assert "stopped: permission withdrawn" in job.summary

    def test_a_permission_revoked_before_the_job_runs_stops_it_before_any_row(
        self, db, clerk, use_panel, monkeypatch
    ):
        monkeypatch.setattr(jobs, "_submit", lambda job: None)
        panel = use_panel(Promo, TouchPanel)
        promos(4)

        job = enqueue(panel, clerk, name="touch")
        assert job.status == VantageJob.Status.PENDING

        clerk.user_permissions.clear()
        jobs.run_job(job.pk)
        job.refresh_from_db()

        assert job.status == VantageJob.Status.DENIED
        assert job.processed == 0
        assert Promo.objects.filter(headline="touched").count() == 0

    def test_a_deactivated_actor_stops_the_job(self, db, clerk, use_panel, monkeypatch):
        monkeypatch.setattr(jobs, "_submit", lambda job: None)
        panel = use_panel(Promo, TouchPanel)
        promos(3)
        job = enqueue(panel, clerk, name="touch")

        User.objects.filter(pk=clerk.pk).update(is_active=False)
        jobs.run_job(job.pk)
        job.refresh_from_db()

        assert job.status == VantageJob.Status.DENIED
        assert Promo.objects.filter(headline="touched").count() == 0

    def test_the_check_is_not_satisfied_by_a_cached_answer(self, db, clerk):
        """``ModelBackend`` caches permissions on the user object it was handed.

        Reloading the actor each chunk is the only thing that makes the re-check real,
        so this asserts the reload rather than the outcome: the stale in-memory user
        still says yes at the moment the fresh one says no.
        """
        assert clerk.has_perm("testapp.change_promo") is True
        clerk.user_permissions.clear()
        assert clerk.has_perm("testapp.change_promo") is True  # the cache, lying
        reloaded = User.objects.get(pk=clerk.pk)
        assert reloaded.has_perm("testapp.change_promo") is False


class TestJobVisibility:
    @pytest.fixture
    def other(self, db):
        return User.objects.create_user("other", "o@example.com", "x", is_staff=True)

    @pytest.fixture
    def job(self, db, clerk, use_panel):
        panel = use_panel(Promo, TouchPanel)
        promos(2)
        return enqueue(panel, clerk, name="touch")

    def test_another_user_cannot_read_someone_elses_job(self, client, job, other):
        """404, not 403: a 403 confirms the job exists, which is what is being hidden."""
        client.force_login(other)
        assert client.get(reverse("vantage:job-status", args=[job.pk])).status_code == 404
        assert client.get(reverse("vantage:job-errors", args=[job.pk])).status_code == 404

    def test_another_user_does_not_see_it_listed(self, client, job, other):
        client.force_login(other)
        page = client.get(reverse("vantage:jobs"))
        assert page.status_code == 200
        assert f'data-job-row="{job.pk}"' not in page.content.decode()

    def test_its_own_actor_can(self, client, job, clerk):
        client.force_login(clerk)
        assert client.get(reverse("vantage:job-status", args=[job.pk])).status_code == 200

    def test_a_superuser_can(self, client, job, boss):
        client.force_login(boss)
        assert client.get(reverse("vantage:job-status", args=[job.pk])).status_code == 200

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, client, job):
        assert client.get(reverse("vantage:jobs")).status_code == 302

    def test_the_queryset_helper_agrees_with_the_per_row_rule(self, db, job, clerk, other):
        assert list(jobs.visible_jobs(clerk)) == [job]
        assert list(jobs.visible_jobs(other)) == []
        assert job.may_be_viewed_by(clerk) is True
        assert job.may_be_viewed_by(other) is False


# ── the status endpoint and the page ────────────────────────────────────────


class TestStatusEndpoint:
    def test_it_reports_the_counts_and_says_when_to_stop(self, client, db, boss, use_panel):
        panel = use_panel(Promo, TouchPanel)
        promos(5)
        job = enqueue(panel, boss, name="touch")

        client.force_login(boss)
        body = client.get(reverse("vantage:job-status", args=[job.pk])).json()

        assert body["status"] == "succeeded"
        assert (body["processed"], body["total"], body["failed"]) == (5, 5, 0)
        assert body["percent"] == 100
        assert body["terminal"] is True
        assert body["poll_ms"] == jobviews.POLL_MS

    def test_a_running_job_is_not_terminal(self, db, boss):
        job = VantageJob.objects.create(
            actor=boss,
            resource_key="testapp.promo",
            action_name="touch",
            status=VantageJob.Status.RUNNING,
            total=100,
            processed=25,
        )
        assert job.is_terminal is False
        assert job.percent == 25

    def test_it_offers_the_error_report_only_when_there_is_one(
        self, client, db, boss, use_panel, monkeypatch
    ):
        monkeypatch.setattr(jobs, "chunk_size", lambda: 2)
        panel = use_panel(Promo, FlakyPanel)
        promos(4, bad={1})
        job = enqueue(panel, boss, name="publish")

        client.force_login(boss)
        body = client.get(reverse("vantage:job-status", args=[job.pk])).json()
        assert body["errors_url"] == reverse("vantage:job-errors", args=[job.pk])

        Promo.objects.filter(headline__contains="bad").delete()
        clean = enqueue(panel, boss, name="publish")
        body = client.get(reverse("vantage:job-status", args=[clean.pk])).json()
        assert body["errors_url"] == ""

    def test_the_csv_is_served_as_an_attachment(self, client, db, boss, use_panel):
        panel = use_panel(Promo, FlakyPanel)
        promos(2, bad={0})
        job = enqueue(panel, boss, name="publish")

        client.force_login(boss)
        response = client.get(reverse("vantage:job-errors", args=[job.pk]))
        assert response["Content-Type"].startswith("text/csv")
        assert response["Content-Disposition"] == f'attachment; filename="job-{job.pk}-errors.csv"'


class TestJobsPage:
    def test_it_renders_a_bar_that_stops_polling_when_the_job_is_done(
        self, client, db, boss, use_panel
    ):
        panel = use_panel(Promo, TouchPanel)
        promos(3)
        job = enqueue(panel, boss, name="touch")

        client.force_login(boss)
        html = client.get(reverse("vantage:jobs")).content.decode()

        assert f'id="job-{job.pk}"' in html
        # The poll attribute is the only thing that starts a timer, and a finished job
        # must not carry one. This is the "a bar that polls forever is a bug" assertion.
        assert "data-job-poll" not in html
        assert 'data-job-status="succeeded"' in html

    def test_a_running_job_carries_its_poll_url(self, client, db, boss):
        job = VantageJob.objects.create(
            actor=boss,
            resource_key="testapp.promo",
            action_name="touch",
            action_label="Touch",
            status=VantageJob.Status.RUNNING,
            total=10,
            processed=4,
        )
        client.force_login(boss)
        html = client.get(reverse("vantage:jobs")).content.decode()
        assert f'data-job-poll="{reverse("vantage:job-status", args=[job.pk])}"' in html
        assert "--v-job-percent: 40" in html

    def test_it_names_the_backend_rather_than_implying_a_worker(self, client, db, boss):
        client.force_login(boss)
        html = client.get(reverse("vantage:jobs")).content.decode()
        assert "No task backend is configured" in html

    def test_the_empty_state_explains_why_small_actions_are_absent(self, client, db, boss):
        client.force_login(boss)
        html = client.get(reverse("vantage:jobs")).content.decode()
        assert "No background jobs yet." in html


# ── housekeeping ────────────────────────────────────────────────────────────


class TestSweep:
    def test_it_removes_finished_jobs_past_the_window(self, db, boss):
        import datetime as dt

        from django.utils import timezone

        old = VantageJob.objects.create(
            actor=boss,
            resource_key="testapp.promo",
            action_name="touch",
            status=VantageJob.Status.SUCCEEDED,
        )
        stuck = VantageJob.objects.create(
            actor=boss,
            resource_key="testapp.promo",
            action_name="touch",
            status=VantageJob.Status.RUNNING,
        )
        long_ago = timezone.now() - dt.timedelta(days=90)
        VantageJob.objects.update(created_at=long_ago)

        assert jobs.sweep(days=30) == 1
        assert list(VantageJob.objects.values_list("pk", flat=True)) == [stuck.pk]
        assert not VantageJob.objects.filter(pk=old.pk).exists()


class TestArchivePanelStillWorks:
    """The registered panel from ``tests/testapp/panel.py``, driven as a job.

    Not a duplicate of the tests above: it proves the runner works against a panel it
    did not have a hand in constructing, including one whose model carries a
    soft-delete pattern the list queryset filters on.
    """

    def test_restoring_from_the_trash_view_resolves_the_same_rows(self, db, boss):
        from django.utils import timezone

        panel = site.get_panel(Archive)
        for i in range(5):
            Archive.objects.create(title=f"A{i}", deleted_at=timezone.now())

        chosen = next(a for a in _actions(panel) if a.name == "restore")
        job = jobs.enqueue(
            request=request_for(boss, "trash=1"),
            panel=panel,
            action=chosen,
            selection=all_selected(),
            count=5,
        )

        assert job.status == VantageJob.Status.SUCCEEDED
        assert job.total == 5
        assert Archive.objects.filter(deleted_at__isnull=True).count() == 5


class TestImpersonatedJobs:
    """A job started while impersonating is audited as the person really signed in."""

    def test_the_audit_entry_names_the_impersonator_not_the_target(
        self, db, boss, clerk, use_panel
    ):
        from vantage.models import VantageLogEntry
        from vantage.safety import IMPERSONATE_SESSION_KEY, IMPERSONATOR_SESSION_KEY

        panel = use_panel(Promo, TouchPanel)
        promos(3)
        request = request_for(clerk)
        # What start_impersonation leaves behind: signed in as the target, with the
        # real account remembered in the session.
        request.session = {IMPERSONATOR_SESSION_KEY: boss.pk, IMPERSONATE_SESSION_KEY: clerk.pk}
        chosen = next(a for a in _actions(panel) if a.name == "touch")
        job = jobs.enqueue(
            request=request, panel=panel, action=chosen, selection=all_selected(), count=3
        )

        assert job.status == VantageJob.Status.SUCCEEDED
        entry = VantageLogEntry.objects.latest("pk")
        assert entry.actor_repr == "boss"

    def test_an_ordinary_job_is_still_audited_as_its_actor(self, db, clerk, use_panel):
        from vantage.models import VantageLogEntry

        panel = use_panel(Promo, TouchPanel)
        promos(2)
        enqueue(panel, clerk, name="touch")
        assert VantageLogEntry.objects.latest("pk").actor_repr == "clerk"
