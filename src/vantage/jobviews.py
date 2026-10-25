"""The jobs page, the endpoint its progress bars poll, and the error report.

Three routes, and the interesting decisions are all in the second one.

**Poll, do not stream.** Server-sent events are the obvious fit for progress and are
the wrong choice here: SSE holds a connection open, which requires ASGI, and this
package targets WSGI deployments too — §B11 keeps the core sync for exactly that
reason. A held-open connection also consumes a worker thread per watching browser,
which on a synchronous server means five people watching a job can exhaust the pool.
A 1.5-second poll of a single indexed row is cheap and works everywhere.

**Stop polling.** The endpoint reports ``terminal`` and the script obeys it. A progress
bar that keeps polling after the job has finished is not a cosmetic problem: it is a
request every second, per open tab, forever, and it is the single most common way a
progress UI turns into a load problem.

**A job is visible to its actor and to superusers, and to nobody else.** Not to anyone
holding the model permission the action needed: permissions are granted and revoked,
and a job row names what one person did. Every one of the three routes asks the same
:meth:`~vantage.models.VantageJob.may_be_viewed_by` question, and the answer to a job
that is not yours is 404 rather than 403 — 403 confirms the job exists, which is the
one bit of information the check is there to withhold.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.translation import gettext_lazy as _

from vantage import jobs
from vantage.models import VantageJob
from vantage.views import SiteView

__all__ = [
    "POLL_MS",
    "JobErrorsView",
    "JobStatusView",
    "JobsView",
    "build_urls",
]

#: Jobs listed per page.
PER_PAGE = 50

#: How often the bar asks, in milliseconds. Fast enough that a 20-second job looks
#: alive, slow enough that a hundred rows of finished jobs are not a hundred requests
#: a second. The script backs this off when the tab is hidden and stops entirely once
#: the job is terminal.
POLL_MS = 1500


class JobsBase(SiteView):
    """Shared gate: signed in, and the jobs feature is on."""

    def check_permission(self, request):
        if not jobs.jobs_enabled():
            raise PermissionDenied(_("Background jobs are switched off for this panel."))
        return None

    def get_job(self) -> VantageJob:
        """The requested job, or 404 — including when it is somebody else's.

        Deliberately not 403. A distinct "you may not see this" tells whoever is
        guessing job ids which ones exist and who has been running bulk deletes, which
        is precisely the thing the ownership check protects.
        """
        job = VantageJob.objects.filter(pk=self.kwargs["pk"]).first()
        if job is None or not job.may_be_viewed_by(getattr(self.request, "user", None)):
            raise Http404(_("No such job."))
        return job


class JobsView(JobsBase):
    """Recent jobs, newest first, with a live bar on anything still running."""

    template_name = "vantage/jobs.html"
    page_title = _("Background jobs")
    page_description = _("Bulk actions too large to run in a request, and how they went.")

    def get_breadcrumbs(self):
        return [(_("Background jobs"), None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = jobs.visible_jobs(self.request.user).select_related("actor")
        paginator = Paginator(queryset, PER_PAGE)
        page = paginator.get_page(self.request.GET.get("page"))

        context.update(
            jobs=[_row(job) for job in page.object_list],
            page=page,
            paginator=paginator,
            poll_ms=POLL_MS,
            # Named on the page, because "running in the background" is not true on a
            # project without a worker and the difference is worth one line of text.
            backend=jobs.detect_backend(),
            # A superuser is looking at everyone's jobs, which changes what the actor
            # column means; the template says so rather than leaving it ambiguous.
            showing_everyone=bool(getattr(self.request.user, "is_superuser", False)),
        )
        return context


class JobStatusView(JobsBase):
    """The polled endpoint: one indexed row in, six numbers out, no template.

    By request count this is the busiest route in the package — one hit per running
    job per browser per second and a half — so it renders nothing, follows no
    relations, and touches exactly the row it was asked about.
    """

    def get(self, request, *args, **kwargs):
        job = self.get_job()
        response = JsonResponse(
            {
                "id": job.pk,
                "status": job.status,
                "status_label": job.get_status_display(),
                "processed": job.processed,
                "total": job.total,
                "failed": job.failed,
                "succeeded": job.succeeded,
                "percent": job.percent,
                "summary": job.summary,
                "error": job.error,
                # The script reads this and stops. It is a server-side fact rather
                # than a list of statuses duplicated in JavaScript, so adding a status
                # later cannot leave a bar polling forever.
                "terminal": job.is_terminal,
                "poll_ms": POLL_MS,
                "errors_url": (_errors_url(job) if job.failed and job.errors else ""),
            }
        )
        # A progress reading that a proxy is allowed to keep is a bar that stops
        # moving for reasons nobody can see from the browser.
        response["Cache-Control"] = "no-store"
        return response


class JobErrorsView(JobsBase):
    """The failures as a CSV, every cell defused against formula injection."""

    def get(self, request, *args, **kwargs):
        job = self.get_job()
        response = HttpResponse(jobs.error_csv_bytes(job), content_type="text/csv; charset=utf-8")
        # ``attachment`` and an ASCII-safe name. A response that renders in the browser
        # would be an attacker-influenced document served from the panel's own origin.
        response["Content-Disposition"] = f'attachment; filename="job-{job.pk}-errors.csv"'
        return response


def _row(job: VantageJob) -> dict:
    """One job shaped for the template, with its URLs resolved where a failure shows.

    ``reverse`` raising inside template variable resolution is swallowed into a blank,
    so the URLs are built here — the same reason :func:`vantage.auditviews._row` does it.
    """
    from django.urls import reverse

    return {
        "job": job,
        "status_url": reverse("vantage:job-status", args=[job.pk]),
        "errors_url": _errors_url(job) if job.failed and job.errors else "",
    }


def _errors_url(job: VantageJob) -> str:
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse("vantage:job-errors", args=[job.pk])
    except NoReverseMatch:  # pragma: no cover - only if the routes are not mounted
        return ""


def build_urls(site) -> list:
    """The three routes, under the ``_/`` prefix :mod:`vantage.urls` reserves for panel
    machinery so they can never collide with an app label."""
    from django.urls import path

    return [
        path("_/jobs/", JobsView.as_view(site=site), name="jobs"),
        path("_/jobs/<int:pk>/status/", JobStatusView.as_view(site=site), name="job-status"),
        path("_/jobs/<int:pk>/errors.csv", JobErrorsView.as_view(site=site), name="job-errors"),
    ]
