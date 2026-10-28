"""Mailing a saved view as a CSV, on a schedule, as the person who subscribed.

A report is a :class:`~vantage.models.ReportSubscription` — a saved view, a person, a
schedule and some addresses — and the interesting question is whose eyes the rows are
filtered through when nobody is at the keyboard. Everything here is the answer to it.

**The export runs as the subscriber, as they are *now*.** The user is loaded afresh from
the database at send time and the view is resolved through the same code the list page
and its Export button use (:meth:`vantage.views.ListView.export_parts`): their model
permission, their row scoping, their tenant, their column choices, and the same masking
of sensitive fields. Nothing about what they were allowed at subscribe time is stored, so
there is nothing to be stale. An inactive account, a user who has lost the panel, the
model's ``view`` permission or access to the saved view, is skipped, and the reason goes
in the send log. Skipping is not an error: the system did what it was asked to.

**A view that no longer means what it did is skipped, not widened.** If a field the view
filtered on has since been renamed, the list page shows a banner and carries on with more
rows than were asked for. A banner is no use in an inbox, so a report whose filter lost a
predicate is not sent at all.

**Due reports are found by cron, and claimed by a conditional update.** ``next_run_at``
is the schedule. :func:`run_due` finds the active rows whose time has come and moves each
one's ``next_run_at`` forward with ``UPDATE ... WHERE next_run_at = <the value it read>``;
only the process whose update changed a row goes on to queue a send, so two overlapping
cron runs, or one run twice in a minute, send once. A server that was down for a week
sends one report when it comes back, not seven: the next time is worked out from *now*.

**The work goes through the job backend**, exactly as webhook deliveries do
(:func:`vantage.jobs.detect_backend`): a task on ``django.tasks``, Celery or RQ where one
is configured, in the command's own process where not. A queued send that never ran is
found again by :func:`requeue_stale`.

**The email never carries more than it is allowed to.** One message per recipient, so
nobody learns who else gets it. Past ``REPORTS_MAX_ATTACHMENT_MB`` the file is not
attached and the email says so. Every email has an unsubscribe link signed with
``SECRET_KEY``, which pauses that one subscription and needs no sign-in.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import time
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dates import WEEKDAYS
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from vantage.models import ReportRun, ReportSubscription

__all__ = [
    "enabled",
    "enqueue",
    "may_manage",
    "may_send_to_others",
    "next_occurrence",
    "read_unsubscribe_token",
    "requeue_stale",
    "run_due",
    "run_report",
    "schedule_label",
    "sweep",
    "unsubscribe_token",
]

logger = logging.getLogger("vantage.reports")

#: Signing salt for the unsubscribe link. Its own, so a token minted for anything else
#: in the project can never be replayed here.
UNSUBSCRIBE_SALT = "vantage.reports.unsubscribe"

#: A queued send nobody picked up in this long is submitted again.
STALE_PENDING_SECONDS = 300

#: A run still "running" after this long lost its worker, and is closed as failed.
STALE_RUNNING_SECONDS = 3600

#: Addresses one subscription may name besides the subscriber.
MAX_RECIPIENTS = 20


# ── settings and access ─────────────────────────────────────────────────────


def _setting(name: str):
    from vantage.conf import vantage_settings

    return getattr(vantage_settings, name)


def enabled() -> bool:
    return bool(_setting("REPORTS"))


def max_attachment_bytes() -> int:
    return int(float(_setting("REPORTS_MAX_ATTACHMENT_MB")) * 1024 * 1024)


def max_per_user() -> int:
    return max(int(_setting("REPORTS_MAX_PER_USER")), 1)


def _active_user(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_active)


def may_manage(request) -> bool:
    """Superusers, or anyone holding ``vantage.manage_reports``.

    Managers see every subscription and may pause, resume, delete and send them. They
    cannot change one: what a report contains is decided by whose it is.
    """
    if not _active_user(request):
        return False
    return bool(request.user.is_superuser) or request.user.has_perm("vantage.manage_reports")


def may_send_to_others(user) -> bool:
    """Whether ``user`` may name addresses other than their own.

    A report is a file of rows going to a mailbox, and a mailbox is not necessarily the
    person's. Held as its own permission so it can be given to the people who run a
    weekly revenue mail without handing it to everyone who may subscribe.
    """
    if user is None or not user.is_active:
        return False
    return bool(user.is_superuser) or user.has_perm("vantage.send_reports_to_others")


def parse_recipients(raw: str) -> list[str]:
    """Split a comma, space or newline separated list into valid, distinct addresses.

    Raises :class:`~django.core.exceptions.ValidationError` naming the first bad one.
    """
    seen: dict[str, str] = {}
    for part in raw.replace(";", ",").replace("\n", ",").replace(" ", ",").split(","):
        address = part.strip()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError:
            raise ValidationError(
                _("“%(address)s” is not a valid email address."), params={"address": address}
            ) from None
        seen.setdefault(address.lower(), address)
    return list(seen.values())


# ── the schedule ────────────────────────────────────────────────────────────


def _last_day(year: int, month: int) -> int:
    first_of_next = dt.date(year + (month == 12), month % 12 + 1, 1)
    return (first_of_next - dt.timedelta(days=1)).day


def next_occurrence(
    frequency: str,
    send_time: dt.time,
    *,
    weekday: int | None = None,
    day_of_month: int | None = None,
    after: dt.datetime,
) -> dt.datetime:
    """The first moment strictly after ``after`` that the schedule names.

    The wall-clock time is read in the site timezone (``TIME_ZONE``), so "08:00" stays
    08:00 across a change of clocks. A time that does not exist on the day the clocks go
    forward lands an hour later; one that happens twice uses the first.
    """
    tz = timezone.get_default_timezone()
    local = after.astimezone(tz)
    time_of_day = send_time.replace(tzinfo=None, second=0, microsecond=0)

    def at(day: dt.date) -> dt.datetime:
        return dt.datetime.combine(day, time_of_day, tzinfo=tz)

    today = local.date()
    if frequency == ReportSubscription.Frequency.DAILY:
        for offset in (0, 1):
            candidate = at(today + dt.timedelta(days=offset))
            if candidate > after:
                return candidate
    elif frequency == ReportSubscription.Frequency.WEEKLY:
        wanted = 0 if weekday is None else weekday
        ahead = (wanted - today.weekday()) % 7
        for offset in (ahead, ahead + 7):
            candidate = at(today + dt.timedelta(days=offset))
            if candidate > after:
                return candidate
    else:
        wanted_day = 1 if day_of_month is None else day_of_month
        year, month = today.year, today.month
        for _step in range(3):
            candidate = at(dt.date(year, month, min(wanted_day, _last_day(year, month))))
            if candidate > after:
                return candidate
            year, month = year + (month == 12), month % 12 + 1
    raise ValueError(f"Unknown frequency {frequency!r}.")  # pragma: no cover


def next_run_for(sub: ReportSubscription, *, after: dt.datetime | None = None) -> dt.datetime:
    return next_occurrence(
        sub.frequency,
        sub.send_time,
        weekday=sub.weekday,
        day_of_month=sub.day_of_month,
        after=after or timezone.now(),
    )


def schedule_label(sub: ReportSubscription) -> str:
    """ "Every week on Monday at 08:00", for the pages and the email."""
    when = sub.send_time.strftime("%H:%M")
    if sub.frequency == ReportSubscription.Frequency.DAILY:
        return _("Every day at %(time)s") % {"time": when}
    if sub.frequency == ReportSubscription.Frequency.WEEKLY:
        return _("Every week on %(day)s at %(time)s") % {
            "day": WEEKDAYS[sub.weekday or 0],
            "time": when,
        }
    return _("Every month on day %(day)s at %(time)s") % {
        "day": sub.day_of_month or 1,
        "time": when,
    }


# ── the unsubscribe link ────────────────────────────────────────────────────


def unsubscribe_token(sub: ReportSubscription) -> str:
    """Names this one subscription and nothing else. Does not expire: an old email's
    link should still work, and all it can do is stop a mailing."""
    return signing.dumps({"report": sub.pk}, salt=UNSUBSCRIBE_SALT, compress=True)


def read_unsubscribe_token(token: str) -> int | None:
    try:
        data = signing.loads(token, salt=UNSUBSCRIBE_SALT)
    except signing.BadSignature:
        return None
    pk = data.get("report") if isinstance(data, dict) else None
    return pk if isinstance(pk, int) else None


# ── finding what is due ─────────────────────────────────────────────────────


def run_due(*, now: dt.datetime | None = None, limit: int = 500) -> int:
    """Queue every active subscription whose time has come. Returns how many were queued.

    Safe to run as often and as concurrently as cron likes. A row is claimed by moving
    its ``next_run_at`` with a conditional update; a process that loses the race changes
    no row and queues nothing.
    """
    now = now or timezone.now()
    due = list(
        ReportSubscription.objects.filter(active=True, next_run_at__lte=now)
        .select_related("view", "user")
        .order_by("next_run_at", "pk")[:limit]
    )
    queued = 0
    for sub in due:
        with transaction.atomic():
            claimed = ReportSubscription.objects.filter(
                pk=sub.pk, active=True, next_run_at=sub.next_run_at
            ).update(next_run_at=next_run_for(sub, after=now))
            run = (
                ReportRun.objects.create(subscription=sub, trigger=ReportRun.Trigger.SCHEDULE)
                if claimed
                else None
            )
        if run is not None:
            _queue(run.pk)
            queued += 1
    return queued


def enqueue(sub: ReportSubscription, *, trigger: str = "manual") -> ReportRun:
    """Queue a send of ``sub`` now, without touching its schedule."""
    run = ReportRun.objects.create(subscription=sub, trigger=trigger)
    _queue(run.pk)
    return run


def _queue(run_id: int) -> None:
    transaction.on_commit(lambda: _submit(run_id))


def _submit(run_id: int) -> None:
    """Hand a run to whichever backend :func:`vantage.jobs.detect_backend` finds.

    Never raises: a run that could not be queued stays ``pending`` and
    :func:`requeue_stale` finds it.
    """
    from vantage import jobs

    try:
        backend = jobs.detect_backend()
        if not backend.deferred:
            run_report(run_id)
            return
        queue = jobs.setting("JOB_QUEUE", "default")
        if backend.name == jobs.DJANGO_TASKS.name:
            from django.tasks import task

            task(queue_name=queue)(run_report).enqueue(run_id)
        elif backend.name == jobs.CELERY.name and _celery_report is not None:
            _celery_report.delay(run_id)
        elif backend.name == jobs.RQ.name:
            import django_rq

            django_rq.get_queue(queue).enqueue(run_report, run_id)
    except Exception:
        logger.exception("Could not queue scheduled report run %s.", run_id)


def requeue_stale(*, now: dt.datetime | None = None) -> int:
    """Submit runs nobody picked up, and close ones whose worker vanished."""
    now = now or timezone.now()
    lost = ReportRun.objects.filter(
        status=ReportRun.Status.RUNNING,
        started_at__lt=now - dt.timedelta(seconds=STALE_RUNNING_SECONDS),
    ).update(
        status=ReportRun.Status.FAILED,
        finished_at=now,
        error=_("The worker stopped before this report finished."),
    )
    if lost:
        logger.warning("Closed %s scheduled report run(s) whose worker vanished.", lost)
    stale = list(
        ReportRun.objects.filter(
            status=ReportRun.Status.PENDING,
            created_at__lt=now - dt.timedelta(seconds=STALE_PENDING_SECONDS),
        ).values_list("pk", flat=True)[:200]
    )
    for pk in stale:
        _submit(pk)
    return len(stale)


def sweep(*, days: float | None = None) -> int:
    """Delete finished runs past the retention window. Returns how many went."""
    window = days or float(_setting("REPORTS_RETENTION_DAYS"))
    horizon = timezone.now() - dt.timedelta(days=window)
    deleted, _per_model = ReportRun.objects.filter(
        status__in=ReportRun.TERMINAL, created_at__lt=horizon
    ).delete()
    return deleted


# ── sending ─────────────────────────────────────────────────────────────────


class Skip(Exception):
    """This report should not go out, and why. Not a failure."""


@dataclass
class Outcome:
    status: str
    rows: int | None = None
    attached: bool = False
    size: int | None = None
    recipients: int = 0
    reason: str = ""
    error: str = ""


def run_report(run_id: int) -> ReportRun | None:
    """The task a worker runs. Module-level so every backend can import it by name.

    Claims the row with a conditional update, which is what makes a duplicate delivery
    harmless: every queue here promises at-least-once, not exactly-once.
    """
    claimed = ReportRun.objects.filter(pk=run_id, status=ReportRun.Status.PENDING).update(
        status=ReportRun.Status.RUNNING, started_at=timezone.now()
    )
    if not claimed:
        return ReportRun.objects.filter(pk=run_id).first()

    run = ReportRun.objects.select_related("subscription__view").get(pk=run_id)
    began = time.monotonic()
    try:
        outcome = _execute(run.subscription)
    except Skip as exc:
        outcome = Outcome(ReportRun.Status.SKIPPED, reason=str(exc))
    # A worker must never die on one report: a raise here would leave the row "running"
    # and, on some backends, retry it until it gave up.
    except Exception as exc:
        logger.exception("Scheduled report %s failed.", run.subscription_id)
        outcome = Outcome(ReportRun.Status.FAILED, error=f"{type(exc).__name__}: {exc}")

    finished = timezone.now()
    ReportRun.objects.filter(pk=run.pk).update(
        status=outcome.status,
        rows=outcome.rows,
        attached=outcome.attached,
        size_bytes=outcome.size,
        recipients_count=outcome.recipients,
        reason=outcome.reason[:300],
        error=outcome.error[:300],
        finished_at=finished,
        duration_ms=int((time.monotonic() - began) * 1000),
    )
    ReportSubscription.objects.filter(pk=run.subscription_id).update(
        last_run_at=finished, last_status=outcome.status
    )
    run.refresh_from_db()
    return run


def _execute(sub: ReportSubscription) -> Outcome:
    from vantage.jobs import JobRequest, _panel_for, _site_for
    from vantage.permissions import get_permissions
    from vantage.savedviews import can_use, hydrate
    from vantage.tenancy import restore as tenancy_restore

    # Loaded again, here, and never taken from the subscription: the account as the
    # database has it at this moment is the only one whose rights count.
    user = get_user_model()._default_manager.filter(pk=sub.user_id).first()
    if user is None or not user.is_active:
        raise Skip(_("The account that subscribed is inactive or gone."))
    if not user.is_staff:
        raise Skip(_("The account that subscribed can no longer sign in to the panel."))

    view = sub.view
    panel = _panel_for(view.model_label)
    if panel is None:
        raise Skip(
            _("No panel is registered for %(model)s any more.") % {"model": view.model_label}
        )

    hydrated = hydrate(panel, view.state)
    if hydrated.widened:
        raise Skip(
            _(
                "Part of the saved view no longer applies, so it would send more rows "
                "than it was set up to."
            )
        )

    request = JobRequest(user=user, query=hydrated.query)
    try:
        tenancy_restore(request, sub.tenant or None)
    except Exception as exc:
        raise Skip(
            _("The tenant this report was set up under no longer applies: %s") % exc
        ) from exc

    if not get_permissions().has_model_perm(request, panel.model, "view"):
        raise Skip(
            _("%(user)s may no longer view %(records)s.")
            % {"user": user.get_username(), "records": panel.get_label_plural().lower()}
        )
    if not can_use(view, request):
        raise Skip(_("The saved view is no longer shared with %s.") % user.get_username())

    addresses, note = _addresses(sub, user)
    if not addresses:
        raise Skip(_("There is nobody to send it to."))
    base = (_setting("REPORTS_BASE_URL") or sub.origin).rstrip("/")
    if not base:
        raise Skip(
            _("There is no address to build the unsubscribe link from; set REPORTS_BASE_URL.")
        )

    from vantage import listprefs
    from vantage.views import ListView

    listprefs.resolve(panel, request)
    list_view = ListView(site=_site_for(panel), panel=panel)
    list_view.request = request
    queryset, columns, headers, masked = list_view.export_parts(request)

    rows = queryset.count()
    content = _csv_bytes(queryset, columns, headers, masked, limit=max_attachment_bytes())
    sent = _send(sub, panel, user, addresses, base, rows, content)
    failed = len(addresses) - sent
    if not sent:
        return Outcome(
            ReportRun.Status.FAILED,
            rows=rows,
            recipients=0,
            error=_("The email backend refused every message."),
        )
    reason = note
    if failed:
        reason = f"{reason} " if reason else ""
        reason += ngettext(
            "%(count)s message could not be sent.",
            "%(count)s messages could not be sent.",
            failed,
        ) % {"count": failed}
    return Outcome(
        ReportRun.Status.SENT,
        rows=rows,
        attached=content is not None,
        size=len(content) if content is not None else None,
        recipients=sent,
        reason=reason,
    )


def _addresses(sub: ReportSubscription, user) -> tuple[list[str], str]:
    """Where it goes, as of now, plus a note when something was held back.

    The subscriber's own address is read from the account. Other addresses go only if
    the subscriber *currently* holds ``send_reports_to_others``: the permission was
    checked when they were typed in, and it is checked again here because it can be
    taken away.
    """
    addresses: dict[str, str] = {}
    note = ""
    if sub.to_self and user.email:
        addresses[user.email.lower()] = user.email
    others = [str(address) for address in (sub.recipients or [])]
    if others and not may_send_to_others(user):
        note = _("Other recipients were left out: %s may no longer send reports to them.") % (
            user.get_username()
        )
        others = []
    for address in others:
        addresses.setdefault(address.lower(), address)
    return list(addresses.values()), note


class _Collector:
    """Where ``csv.writer`` writes: counts bytes and gives up past the limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.size = 0
        self.parts: list[bytes] = []

    def write(self, text: str) -> int:
        encoded = text.encode("utf-8")
        self.size += len(encoded)
        if self.size > self.limit:
            raise _TooLarge
        self.parts.append(encoded)
        return len(text)


class _TooLarge(Exception):
    pass


def _csv_bytes(queryset, columns, headers, masked, *, limit: int) -> bytes | None:
    """The CSV, or ``None`` once it grows past ``limit`` bytes.

    Built from the export's own row generator, so the file has the same cells, masking
    and header labels as the Export button. Stops reading rows the moment it is over,
    rather than building a file only to throw it away.
    """
    from vantage.export import _rows

    sink = _Collector(limit)
    writer = csv.writer(sink)
    try:
        for row in _rows(queryset, columns, headers, frozenset(masked)):
            writer.writerow(row)
    except _TooLarge:
        return None
    return b"".join(sink.parts)


def _send(sub, panel, user, addresses, base, rows, content) -> int:
    """One email per recipient. Returns how many the backend accepted."""
    from vantage.conf import vantage_settings
    from vantage.savedviews import apply_url

    view = sub.view
    local_today = timezone.localdate()
    filename = f"{slugify(view.name) or slugify(panel.get_label_plural())}-{local_today}.csv"
    try:
        unsubscribe = base + reverse("vantage:report-unsubscribe", args=[unsubscribe_token(sub)])
    except NoReverseMatch:  # pragma: no cover - the site always builds this route
        raise Skip(_("The unsubscribe page is not mounted.")) from None
    try:
        open_url = base + apply_url(view)
    except NoReverseMatch:  # pragma: no cover
        open_url = ""

    title = str(vantage_settings.SITE_TITLE)
    subject = _("%(site)s: %(view)s") % {"site": title, "view": view.name}
    lines = [
        ngettext("%(view)s: %(count)s row.", "%(view)s: %(count)s rows.", rows)
        % {"view": view.name, "count": rows},
        schedule_label(sub) + ".",
        "",
    ]
    if content is not None:
        lines.append(_("The file is attached."))
    else:
        lines.append(
            _(
                "The file is larger than %(limit)s MB, so it is not attached. "
                "Open the view in the panel and use Export to download it."
            )
            % {"limit": f"{float(_setting('REPORTS_MAX_ATTACHMENT_MB')):g}"}
        )
    if open_url:
        lines += ["", open_url]
    lines += ["", _("Stop receiving this report:"), unsubscribe]
    body = "\n".join(lines)

    sent = 0
    for address in addresses:
        message = EmailMessage(
            subject,
            body,
            to=[address],
            headers={
                "List-Unsubscribe": f"<{unsubscribe}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        )
        if content is not None:
            message.attach(filename, content, "text/csv")
        try:
            sent += message.send()
        except Exception:
            logger.exception("Could not email scheduled report %s to one recipient.", sub.pk)
    return sent


#: Bound at import for the reason :mod:`vantage.jobs` gives: a remote worker learns a
#: task exists by importing the module that declares it.
_celery_report = None

try:  # pragma: no cover - exercised only on a project with Celery installed
    from celery import shared_task as _shared_task
except ImportError:  # pragma: no cover
    _shared_task = None

if _shared_task is not None:  # pragma: no cover - only with Celery installed
    _celery_report = _shared_task(name="vantage.reports.run_report")(run_report)
