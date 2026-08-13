"""Moving a bulk action off the request, without acquiring a queue as a dependency.

A bulk action over nine rows and a bulk action over ninety thousand are the same
feature and must not be the same code path. Nine rows is 40ms and belongs in the
request, where the user gets an answer and a redirect. Ninety thousand is minutes,
and running it in the request means a gateway timeout half way through with no record
of how far it got. §11 of the list-page decisions draws the line at roughly **two
seconds of work**: below it, run inline and pay nothing for the machinery; above it,
write a :class:`~vantage.models.VantageJob` and let something else do the work.

**Backends are detected, not configured.** The package depends on Django and nothing
else, so requiring Celery to make bulk actions work at scale is not available to us.
Four are tried in order:

1. ``django.tasks`` (Django 6.0+) configured with a real queue backend. This is the
   answer §B11 of the platform decisions settled on: a backend-agnostic enqueue API in
   Django itself, so Vantage does not have to pick a queue on the project's behalf.
   A Celery project pointing ``TASKS`` at a Celery-backed task backend arrives here too.
2. Celery, when it is importable *and* has a broker configured. Merely being on the
   path is not a worker — celery is a transitive dependency of several unrelated
   packages. A Celery worker needs ``imports = ("vantage.jobs",)`` (or an equivalent
   autodiscovery entry) so that :func:`run_job` is registered on its side as well.
3. RQ, via ``django_rq`` in ``INSTALLED_APPS``.
4. **In-process, synchronously.** Not a degraded mode to be ashamed of: it is what
   makes the feature work on the overwhelmingly common Django project that has no
   worker at all. The request pays for the work — which is exactly what would have
   happened anyway — but the job row, the honest counts and the downloadable error
   report all exist afterwards, which is the part that was missing.

``django.tasks``' own ``ImmediateBackend`` is deliberately *not* treated as a queue.
It runs the task in the calling process, which is what option 4 does with less
indirection and with our own progress rows; treating it as a real backend would make
"a queue is configured" true on every Django 6.x project by default and hide the fact
that nothing is deferred.

**Chunking, and what it does and does not promise.** The runner slices the selection
into chunks and gives each one its own transaction, mirroring
:func:`vantage.importer.commit` — deliberately, because a second, subtly different
partial-failure story in the same package is how two screens end up disagreeing about
what "failed" means. So:

* a **chunk** is one transaction. A failure at row 39,000 rolls back the 500 rows
  around it and nothing else, and the 38,500 already committed are a real resume point;
* a failed chunk is then **retried row by row**, each in its own transaction, so the
  report can say *which* rows failed rather than condemning the chunk;
* the **job** is not one transaction, and cannot be. That is the trade that turns
  "the whole thing blew up" into "38,000 succeeded, 12 failed".

**The permission is re-checked as the job runs, not only when it is enqueued.** A job
can sit in a queue for an hour, and an hour is long enough for someone to be moved out
of a group. The check runs before every chunk against a freshly loaded user, because
``ModelBackend`` caches permissions on the user object and a cached answer is not a
re-check.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from django.db import router, transaction
from django.http import HttpRequest, QueryDict
from django.utils import timezone

from vantage.actions import Selection
from vantage.models import VantageJob

__all__ = [
    "Backend",
    "CHUNK_SIZE",
    "COST_PER_ROW",
    "DEFAULT_THRESHOLD_SECONDS",
    "MAX_STORED_ERRORS",
    "detect_backend",
    "enqueue",
    "error_csv_bytes",
    "error_csv_rows",
    "JobRequest",
    "estimate_seconds",
    "jobs_enabled",
    "run_job",
    "selection_from_payload",
    "selection_payload",
    "should_enqueue",
    "sweep",
    "visible_jobs",
]


# ── limits ──────────────────────────────────────────────────────────────────

#: §11's threshold, in seconds of estimated work. Under it the action runs in the
#: request; over it, a job row is written. Two seconds is the point at which a page
#: stops feeling slow and starts feeling broken.
DEFAULT_THRESHOLD_SECONDS = 2.0

#: What one row costs, per declared bulk strategy. Coarse on purpose — this is a
#: routing decision between "instant" and "minutes", and it only has to be right to
#: within an order of magnitude to make it correctly.
#:
#: ``"sql"`` is a single ``UPDATE ... WHERE``: the row count barely moves the cost, so
#: the per-row figure is the database's marginal cost of touching one more row.
#: ``"instances"`` loads and saves each record, running signals and ``full_clean``,
#: which is three orders of magnitude more. An action that declares neither is assumed
#: to be the expensive kind: guessing "cheap" and being wrong means a timeout, while
#: guessing "expensive" and being wrong means a job row nobody needed.
COST_PER_ROW: dict[str, float] = {"sql": 0.00002, "instances": 0.002}

#: Rows per transaction. The same figure :mod:`vantage.importer` uses, and for the same
#: reason: small enough that a late failure leaves a useful resume point, large enough
#: that per-transaction overhead disappears into the work.
CHUNK_SIZE = 500

#: How many individual failures are kept on the job row. ``failed`` stays exact past
#: this; it is only the per-row messages that stop being stored, and the CSV says so.
#: A job that fails 40,000 rows would otherwise write a megabyte of JSON into one
#: column, which is a second outage on top of the first.
MAX_STORED_ERRORS = 1000

#: Jobs older than this are swept. Long enough to answer "what happened last week",
#: short enough that the table does not become a log.
RETENTION_DAYS = 30


def setting(name: str, default: Any) -> Any:
    """A configurable value, asked for rather than assumed.

    :data:`vantage.conf.DEFAULTS` rejects unknown keys by design, so this module reads
    its settings with a fallback exactly as :func:`vantage.importer.limit` does: the
    defaults above work today, and ``VANTAGE["JOB_CHUNK_SIZE"]`` and friends start
    working the moment those names are added to ``DEFAULTS``, with no change here.
    """
    from vantage.conf import vantage_settings

    value = getattr(vantage_settings, name, None)
    return default if value is None else value


def jobs_enabled() -> bool:
    return bool(setting("JOBS", True))


def chunk_size() -> int:
    return max(int(setting("JOB_CHUNK_SIZE", CHUNK_SIZE)), 1)


def threshold_seconds() -> float:
    return float(setting("JOB_THRESHOLD_SECONDS", DEFAULT_THRESHOLD_SECONDS))


def max_stored_errors() -> int:
    return max(int(setting("JOB_MAX_STORED_ERRORS", MAX_STORED_ERRORS)), 0)


# ── the threshold rule ──────────────────────────────────────────────────────


def estimate_seconds(action, count: int) -> float:
    """Roughly how long ``action`` will take over ``count`` rows.

    Read off the action's declared ``bulk_strategy`` rather than measured, because the
    only honest measurement would be to run it, and running it is the thing being
    decided. §11 requires the strategy to be declared per action precisely so that
    ``queryset.update()`` bypassing ``save()`` is never a silent choice; this reuses
    that declaration instead of asking for a second one.
    """
    strategy = getattr(action, "bulk_strategy", "instances")
    return COST_PER_ROW.get(strategy, COST_PER_ROW["instances"]) * max(count, 0)


def should_enqueue(action, count: int) -> bool:
    """Whether this run belongs in the background.

    Small actions must not pay for any of the machinery below — no job row, no polling,
    no redirect to a progress page for something that finished before the page did.
    """
    if not jobs_enabled():
        return False
    return estimate_seconds(action, count) > threshold_seconds()


# ── serialising the selection ───────────────────────────────────────────────


def selection_payload(selection: Selection, request, *, count: int | None = None) -> dict:
    """The selection, plus the predicate it is relative to, as stored JSON.

    :class:`~vantage.actions.Selection` is only half of the scope. ``select_all`` means
    "everything matching the current filters", and the current filters live in the
    request's query string — so the query string is what has to be stored beside it.
    That is what keeps the whole thing a few hundred bytes at any row count, and it is
    the entire reason this feature can be a database row at all.

    ``count`` and ``selected_at`` are recorded but never trusted as the affected count.
    They exist so the job can say "you selected 40,000; 39,988 still matched when this
    ran", which is the sentence §11 asks for and the one Django's own ``select_across``
    bug made impossible to write.
    """
    return {
        "select_all": selection.select_all,
        # Only ever populated for an explicit tick-box selection, which is bounded by
        # the page size. ``select_all`` never materialises ids — that is the point.
        "ids": [] if selection.select_all else [str(pk) for pk in selection.ids],
        "excluded": [str(pk) for pk in selection.excluded],
        "query": _query_string(request),
        "path": getattr(request, "path", "") or "",
        "count": count,
        "selected_at": timezone.now().isoformat(),
    }


def selection_from_payload(payload: dict) -> Selection:
    return Selection(
        select_all=bool(payload.get("select_all")),
        ids=list(payload.get("ids") or []),
        excluded=list(payload.get("excluded") or []),
    )


def _query_string(request) -> str:
    """The list page's own filters, verbatim.

    ``request.GET.urlencode()`` and not a re-derived filter dict: the job has to
    resolve to the rows the user was looking at, and the only description of those rows
    that cannot drift from the page is the one the page itself was rendered from.
    """
    get = getattr(request, "GET", None)
    return get.urlencode() if get is not None else ""


class JobRequest(HttpRequest):
    """A request rebuilt from a stored job, for code that has only ever seen real ones.

    Every permission check, every filter, every panel hook in this package takes a
    request. A worker has none. Rather than growing a request-free variant of all of
    them — which would be a second implementation of the queryset, and the first thing
    to drift — the job carries the query string that produced the page and this
    reconstitutes enough of a request to feed the *existing* code path. That is what
    makes "the selection round-trips to the same rows" a property of the design rather
    than a hope.

    It is deliberately thin. There is no session, no cookies and no ``POST``: a job is
    not a browser, and anything reaching for those has confused the two.
    """

    def __init__(self, *, user, query: str = "", path: str = "/") -> None:
        super().__init__()
        self.method = "GET"
        self.path = self.path_info = path or "/"
        self.GET = QueryDict(query)
        self.META = {"QUERY_STRING": query, "SCRIPT_NAME": ""}
        self.user = user
        # A plain dict, not a session store. ``vantage.safety.impersonating`` and
        # friends only ever read from it, and a job runs as its actor — never as
        # somebody the actor was impersonating when they pressed the button.
        self.session = {}


# ── backends ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Backend:
    """Which queue took the job, and whether the work actually leaves the request."""

    name: str
    label: str
    #: False for the in-process runner, and *only* for it. The jobs page says so out
    #: loud, because "your bulk action is running in the background" is a lie on a
    #: project with no worker and the user is entitled to know which one they have.
    deferred: bool

    @property
    def hint(self) -> str:
        if self.deferred:
            return ""
        return (
            "No task backend is configured, so this ran in the web request. "
            "Configure TASKS, Celery or RQ to move it off."
        )


DJANGO_TASKS = Backend("django_tasks", "Django Tasks", True)
CELERY = Backend("celery", "Celery", True)
RQ = Backend("rq", "RQ", True)
IN_PROCESS = Backend("inprocess", "In-process", False)

BACKENDS = {b.name: b for b in (DJANGO_TASKS, CELERY, RQ, IN_PROCESS)}


def detect_backend() -> Backend:
    """The best available queue, decided by what is installed and configured.

    Detection rather than a settings key, because a settings key would be a fifth
    place for the answer to be wrong. :func:`vantage.health.check_tasks` reports the
    same finding on the health page; its ``_importable`` probe is imported here rather
    than rewritten so the two can never disagree about whether a package is present.
    """
    from vantage.health import _importable

    if _django_tasks_configured():
        return DJANGO_TASKS
    if _importable("celery") and _celery_has_broker():
        return CELERY
    if _importable("django_rq") and _installed("django_rq"):
        return RQ
    return IN_PROCESS


def _installed(app_label: str) -> bool:
    from django.apps import apps as django_apps

    return django_apps.is_installed(app_label)


def _django_tasks_configured() -> bool:
    """``django.tasks`` present *and* pointed at something that actually defers.

    ``TASKS`` has a default on Django 6.0+, so its mere presence proves nothing; the
    default is ``ImmediateBackend``, which runs the task in the calling thread. Calling
    that "a background task backend" would make every Django 6 project look like it had
    a worker.
    """
    try:
        from django.tasks import task_backends
        from django.tasks.backends.immediate import ImmediateBackend
    except ImportError:  # Django 5.2 — the version gate §B11 asks for.
        return False
    try:
        backend = task_backends["default"]
    except Exception:
        return False
    return not isinstance(backend, ImmediateBackend)


def _celery_has_broker() -> bool:
    try:
        from celery import current_app

        return bool(current_app.conf.broker_url)
    except Exception:
        return False


# The Celery registration happens at import time, not at enqueue time, and that is not
# an accident: a remote worker learns a task exists by importing the module that
# declares it. Registering inside ``enqueue`` would work in the web process and produce
# "Received unregistered task" in the worker, which is the classic way this goes wrong.
#: The registered Celery task, or None. Assigned at the foot of this module, once
#: :func:`run_job` exists to be wrapped.
_celery_job = None

try:  # pragma: no cover - exercised only on a project with Celery installed
    from celery import shared_task as _shared_task
except ImportError:  # pragma: no cover
    _shared_task = None


def _submit(job: VantageJob) -> None:
    """Hand the job to its backend.

    Deferred backends are submitted from ``on_commit``. A worker that picks the job up
    before the transaction that created it has committed sees no row at all — a race
    that is rare on a fast queue, reliable on a fast queue with a slow transaction, and
    baffling either way. The in-process runner is called directly instead: there is
    nothing to race, and deferring it to commit time would mean it never ran inside a
    ``TestCase``.
    """
    if job.backend == IN_PROCESS.name:
        run_job(job.pk)
        return
    job_id = job.pk
    name = job.backend
    transaction.on_commit(lambda: _submit_deferred(job_id, name))


def _submit_deferred(job_id: int, backend: str) -> None:  # pragma: no cover - needs a queue
    task_id = ""
    if backend == DJANGO_TASKS.name:
        from django.tasks import task

        queue = setting("JOB_QUEUE", "default")
        task_id = str(getattr(task(queue_name=queue)(run_job).enqueue(job_id), "id", ""))
    elif backend == CELERY.name and _celery_job is not None:
        task_id = str(getattr(_celery_job.delay(job_id), "id", ""))
    elif backend == RQ.name:
        import django_rq

        queue = django_rq.get_queue(setting("JOB_QUEUE", "default"))
        task_id = str(getattr(queue.enqueue(run_job, job_id), "id", ""))
    if task_id:
        VantageJob.objects.filter(pk=job_id).update(task_id=task_id[:255])


# ── enqueueing ──────────────────────────────────────────────────────────────


def enqueue(*, request, panel, action, selection: Selection, count: int) -> VantageJob:
    """Write the job row and hand it to whichever backend exists.

    ``count`` is what the user was shown at selection time. It is stored for the
    comparison the job makes later and is never reported as the affected count — the
    worker re-resolves the predicate and reports what it actually found, because rows
    move between the click and the run and §11 is explicit that echoing the click-time
    number back is the failure mode to avoid.
    """
    from vantage.listprefs import resource_key

    user = getattr(request, "user", None)
    backend = detect_backend()
    job = VantageJob.objects.create(
        actor=user if getattr(user, "is_authenticated", False) else None,
        actor_repr=user.get_username() if getattr(user, "is_authenticated", False) else "",
        resource_key=resource_key(panel),
        action_name=action.name,
        action_label=action.label or action.name,
        selection=selection_payload(selection, request, count=count),
        status=VantageJob.Status.PENDING,
        total=count,
        backend=backend.name,
    )
    _submit(job)
    job.refresh_from_db()
    return job


def visible_jobs(user, queryset=None):
    """The jobs this person may see: their own, or everything for a superuser.

    The same rule as :meth:`~vantage.models.VantageJob.may_be_viewed_by`, expressed in
    SQL so the list page never asks it once per row.
    """
    queryset = VantageJob.objects.all() if queryset is None else queryset
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    if getattr(user, "is_superuser", False):
        return queryset
    return queryset.filter(actor=user)


# ── running ─────────────────────────────────────────────────────────────────


class JobDenied(Exception):
    """The actor may no longer do this. Not an error — a withdrawn answer."""


class JobBroken(Exception):
    """The job cannot be resolved at all: the panel, the action or the actor is gone."""


def run_job(job_id: int) -> VantageJob | None:
    """The one entry point every backend calls. Must stay importable by dotted path.

    Takes an id rather than a job, because a queue serialises its arguments and a
    pickled model instance is a snapshot that is stale by the time it is unpickled.

    Claiming the row with a conditional ``UPDATE`` rather than a read-then-write is
    what makes a duplicate delivery harmless: at-least-once is what every queue here
    actually promises, and a job that runs twice would double-apply the action.
    """
    claimed = VantageJob.objects.filter(
        pk=job_id, status=VantageJob.Status.PENDING
    ).update(status=VantageJob.Status.RUNNING, started_at=timezone.now())
    if not claimed:
        return VantageJob.objects.filter(pk=job_id).first()

    job = VantageJob.objects.get(pk=job_id)
    try:
        _execute(job)
    except JobDenied as exc:
        _finish(job, VantageJob.Status.DENIED, error=str(exc))
    except JobBroken as exc:
        _finish(job, VantageJob.Status.FAILED, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a worker must never die on one job
        _finish(job, VantageJob.Status.FAILED, error=f"{type(exc).__name__}: {exc}")
    return job


def _finish(job: VantageJob, status: str, *, error: str = "") -> None:
    job.status = status
    job.error = error
    job.finished_at = timezone.now()
    job.summary = _summary(job, status)
    job.save(
        update_fields=[
            "status",
            "error",
            "finished_at",
            "summary",
            "processed",
            "failed",
            "total",
            "errors",
        ]
    )


def _summary(job: VantageJob, status: str) -> str:
    """A sentence that names every non-zero outcome and rounds none of them away.

    Modelled on :meth:`vantage.importer.ImportResult.summary`, which exists to make
    "some rows failed" unsayable. The same rule applies here for the same reason: at
    scale, partial failure *is* the outcome, and a summary that hides the twelve is
    worse than no summary at all.
    """
    if status == VantageJob.Status.FAILED and not job.processed:
        return "Nothing ran."
    parts = []
    if job.succeeded:
        parts.append(f"{job.succeeded:,} succeeded")
    if job.failed:
        parts.append(f"{job.failed:,} failed")
    if status == VantageJob.Status.DENIED:
        parts.append("stopped: permission withdrawn")
    elif status == VantageJob.Status.FAILED:
        parts.append("stopped: the job could not continue")
    stated = ", ".join(parts) if parts else "nothing to do"

    # The one honest use of the click-time count: naming the drift rather than hiding
    # it. Never the reverse — the count reported is always the one that ran.
    claimed = (job.selection or {}).get("count")
    if isinstance(claimed, int) and job.total and claimed != job.total:
        stated += f" ({claimed:,} were selected; {job.total:,} still matched)"
    return stated[:300]


def _execute(job: VantageJob) -> None:
    panel, action, request = _rehydrate(job)
    queryset = _resolve_queryset(panel, action, request, job)

    # Snapshotted once, and this is the one place a list of ids is legitimate: it is
    # worker-local and transient, never transmitted or stored. Keyset-paging the live
    # queryset instead would silently lose rows the action itself moves out of the
    # filter — mark 40,000 orders paid while paging a "status=unpaid" predicate and
    # you skip half of them.
    pks: list[Any] = list(queryset.order_by("pk").values_list("pk", flat=True).iterator())
    job.total = len(pks)
    job.save(update_fields=["total"])

    model = panel.model
    db = router.db_for_write(model)
    size = chunk_size()
    errors: list[dict] = []
    cap = max_stored_errors()
    position = 0
    denied: str | None = None

    for chunk in _chunks(pks, size):
        # Before every chunk, against a user loaded from the database. Rights get
        # revoked while a job sits in a queue, and ``ModelBackend`` caches its answer
        # on the user object — so a re-check that reuses the in-memory user is not a
        # re-check at all.
        refusal = _refusal(request, panel, action, job)
        if refusal is not None:
            denied = refusal
            break

        rows = model._default_manager.using(db).filter(pk__in=chunk)
        _, failure = _attempt(db, lambda rows=rows: action.run(panel, request, rows))
        if failure is None:
            position += len(chunk)
            job.processed = position
        else:
            # The chunk rolled back whole, so nothing in it has been applied. Re-run it
            # a row at a time to find out which rows are actually bad — the difference
            # between "500 failed" and "3 failed" on the same data.
            for pk in chunk:
                position += 1
                one = model._default_manager.using(db).filter(pk=pk)
                _, row_failure = _attempt(db, lambda one=one: action.run(panel, request, one))
                if row_failure is not None:
                    job.failed += 1
                    if len(errors) < cap:
                        errors.append({"row": position, "pk": str(pk), "error": row_failure})
            job.processed = position

        # Outside the chunk's transaction, so progress that has been committed is what
        # the poller sees. A progress write inside a transaction that later rolls back
        # is a bar that goes backwards.
        VantageJob.objects.filter(pk=job.pk).update(
            processed=job.processed, failed=job.failed, errors=errors
        )

    job.errors = errors
    if denied is not None:
        raise JobDenied(denied)
    _finish(job, VantageJob.Status.SUCCEEDED)
    _log(job, request, panel, action)


def _rehydrate(job: VantageJob):
    """Panel, action and request, or a clear refusal saying which one is gone."""
    from vantage.actions import resolve_actions

    panel = _panel_for(job.resource_key)
    if panel is None:
        raise JobBroken(
            f"No panel is registered for {job.resource_key}; this job can no longer run."
        )

    # Unfiltered on purpose: "does this action still exist" and "may this person still
    # run it" are two questions, and answering them together would report a revoked
    # permission as a deleted action.
    action = next((a for a in resolve_actions(panel) if a.name == job.action_name), None)
    if action is None:
        raise JobBroken(f"The action {job.action_name!r} no longer exists on this panel.")

    actor = _load_actor(job)
    if actor is None:
        raise JobDenied("The account that started this job is gone or has been disabled.")

    payload = job.selection or {}
    request = JobRequest(
        user=actor, query=payload.get("query", ""), path=payload.get("path", "/")
    )
    return panel, action, request


def _resolve_queryset(panel, action, request, job: VantageJob):
    """The rows the job applies to, resolved through the list page's own code.

    :meth:`vantage.views.ListView.build_queryset` is what rendered the page the
    selection was made on — permissions, search, filters, trash and sort — so calling
    it is the only way to be sure the job resolves to the same rows. A reimplementation
    here would be a second answer to the same question, and the two would drift on the
    first day someone adds a filter.
    """
    from vantage import listprefs
    from vantage.views import ListView

    refusal = _refusal(request, panel, action, job, reload_user=False)
    if refusal is not None:
        raise JobDenied(refusal)

    listprefs.resolve(panel, request)
    view = ListView(site=_site_for(panel), panel=panel)
    view.request = request
    queryset, *_ = view.build_queryset(request)
    return selection_from_payload(job.selection or {}).apply(queryset)


def _refusal(request, panel, action, job, *, reload_user: bool = True) -> str | None:
    """Why this job may not proceed, or None. Reloads the actor before deciding."""
    from vantage.permissions import get_permissions

    if reload_user:
        actor = _load_actor(job)
        if actor is None:
            return "The account that started this job is gone or has been disabled."
        request.user = actor
    if action.permission is None:
        return None
    if get_permissions().has_model_perm(request, panel.model, action.permission):
        return None
    return (
        f"{job.actor_repr or 'The actor'} may no longer "
        f"{action.permission} {panel.get_label_plural().lower()}."
    )


def _load_actor(job: VantageJob):
    """The actor as the database has them *now*, not as they were at enqueue time."""
    if job.actor_id is None:
        return None
    from django.contrib.auth import get_user_model

    user = get_user_model()._default_manager.filter(pk=job.actor_id).first()
    if user is None or not getattr(user, "is_active", True):
        return None
    return user


def _panel_for(resource_key: str):
    from vantage.sites import all_sites
    from vantage.sites import site as default_site

    for candidate in (default_site, *all_sites):
        for panel in candidate.panels():
            if panel.opts.label_lower == resource_key:
                return panel
    return None


def _site_for(panel):
    from vantage.sites import all_sites
    from vantage.sites import site as default_site

    for candidate in (default_site, *all_sites):
        if candidate.is_registered(panel.model):
            return candidate
    return default_site


def _attempt(db: str | None, work: Callable[[], Any]) -> tuple[Any, str | None]:
    """Run ``work`` in its own transaction; return its value, or why it failed.

    The exception is allowed to leave the ``atomic`` block before it is caught, which
    is what rolls the block back. Catching inside and swallowing would leave a broken
    transaction that poisons everything after it — the mistake that turns one bad row
    into ``TransactionManagementError`` for the rest of the run.
    """
    from django.core.exceptions import ValidationError

    try:
        with transaction.atomic(using=db):
            return work(), None
    except ValidationError as exc:
        return None, "; ".join(exc.messages)
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        return None, str(exc).strip() or type(exc).__name__


def _chunks(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _log(job: VantageJob, request, panel, action) -> None:
    """One audit entry per job, written when it finishes rather than when it starts.

    The count recorded is the one that actually ran. Writing the entry at enqueue time
    would log a number nobody has verified yet, and writing one per row would put forty
    thousand entries in the audit log for one click.
    """
    from vantage import audit

    try:
        audit.log_action(request, panel.model, action, job.succeeded)
    except Exception:  # noqa: BLE001 - a failed audit write must not fail the job
        pass


# ── the error report ────────────────────────────────────────────────────────


def error_csv_rows(job: VantageJob) -> list[list[str]]:
    """``(row, record, error)`` per failure, every cell defused.

    Every value here is attacker-influenced: an exception message quotes the data that
    caused it, so a record whose name is ``=cmd|'/c calc'!A1`` arrives in the report as
    part of an error string. :func:`vantage.importer.armour` is reused rather than
    reimplemented — one place in this package decides what a spreadsheet may not be
    handed, and this is a caller of it, not a second copy.
    """
    from vantage.importer import armour

    rows = [["Row", "Record", "Error"]]
    stored = job.errors or []
    rows.extend(
        [
            armour(entry.get("row", "")),
            armour(entry.get("pk", "")),
            armour(entry.get("error", "")),
        ]
        for entry in stored
    )
    hidden = max(job.failed - len(stored), 0)
    if hidden:
        rows.append(
            ["", "", armour(f"{hidden:,} further failures were not recorded individually.")]
        )
    return rows


def error_csv_bytes(job: VantageJob) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(error_csv_rows(job))
    return buffer.getvalue().encode("utf-8")


# ── housekeeping ────────────────────────────────────────────────────────────


def sweep(*, days: int | None = None) -> int:
    """Delete finished jobs older than the retention window. Returns how many went.

    Only terminal ones: a job stuck in ``running`` because its worker was killed is
    evidence, and deleting it would hide the outage it is the only record of.
    """
    import datetime as dt

    window = days or int(setting("JOB_RETENTION_DAYS", RETENTION_DAYS))
    horizon = timezone.now() - dt.timedelta(days=window)
    deleted, _ = VantageJob.objects.filter(
        status__in=VantageJob.TERMINAL, created_at__lt=horizon
    ).delete()
    return deleted


if _shared_task is not None:  # pragma: no cover - only with Celery installed
    _celery_job = _shared_task(name="vantage.jobs.run_job")(run_job)
