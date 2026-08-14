"""The audit page: everything anyone did in the panel, filtered three ways.

Deliberately not a registered :class:`~vantage.panels.ModelPanel`. A generic list page
over :class:`~vantage.models.VantageLogEntry` would come with an Add button, an edit
form and a delete action, and an audit log you can edit is not an audit log. It would
also route around :func:`~vantage.audit.may_view_audit`, which is a stricter gate than
the model permission a generic panel checks — the log spans every model in the panel,
so someone who may not see orders can still learn a great deal from a list of who has
been editing them.

The three filters are actor, action and date, and they are the three because they are
the three questions people actually arrive with: *what did this person do*, *who
deleted things*, and *what happened on Tuesday*. Anything not recognised is dropped
rather than raised on, the same rule the list page's filter rail follows: a stale
bookmark should degrade to a broader result set, never to a 500.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from vantage.audit import may_view_audit
from vantage.conf import vantage_settings
from vantage.models import VantageLogEntry
from vantage.views import SiteView

__all__ = ["DATE_WINDOWS", "AuditLogView", "build_urls"]

#: Relative windows, keyed by ISO-8601 duration exactly as the list page's date filter
#: does, so the two never disagree about what "last 7 days" means. Whole local days,
#: including today.
DATE_WINDOWS: dict[str, tuple[str | Promise, int]] = {
    "P1D": (_("Today"), 1),
    "P7D": (_("Last 7 days"), 7),
    "P30D": (_("Last 30 days"), 30),
    "P90D": (_("Last 90 days"), 90),
}

#: Entries per page. Larger than a list page's default because a log is read by
#: scanning rather than by reading, and paging through it is the slow part.
PER_PAGE = 100


class AuditLogView(SiteView):
    """Every entry, newest first, filtered by actor, action and date."""

    template_name = "vantage/audit.html"
    page_title = _("Audit log")
    page_description = _("Everything created, changed, deleted or run from this panel.")

    def check_permission(self, request):
        if not vantage_settings.AUDIT_LOG:
            raise PermissionDenied(_("The audit log is switched off for this panel."))
        if not may_view_audit(request):
            raise PermissionDenied(_("You do not have permission to read the audit log."))
        return None

    def get_breadcrumbs(self):
        return [(_("Audit log"), None)]

    # ── the queryset ────────────────────────────────────────────────────────

    def get_queryset(self):
        queryset = VantageLogEntry.objects.select_related("actor", "content_type")
        return self._apply_filters(queryset)

    def _apply_filters(self, queryset):
        actor = self._actor_id()
        if actor is not None:
            queryset = queryset.filter(actor_id=actor)

        action = self.request.GET.get("action", "")
        if action in VantageLogEntry.Action.values:
            queryset = queryset.filter(action=action)

        start, end = self._window()
        if start is not None:
            queryset = queryset.filter(created_at__gte=_day_start(start))
        if end is not None:
            # Half-open on the day *after*, so an index on created_at can serve it —
            # a ``__date`` lookup wraps the column in a function and cannot be indexed.
            queryset = queryset.filter(created_at__lt=_day_start(end + dt.timedelta(days=1)))
        return queryset

    def _actor_id(self) -> int | None:
        raw = (self.request.GET.get("actor") or "").strip()
        return int(raw) if raw.isdigit() else None

    def _window(self) -> tuple[dt.date | None, dt.date | None]:
        """The date range being asked for: a preset, or an explicit pair, or neither."""
        preset = self.request.GET.get("window", "")
        if preset in DATE_WINDOWS:
            today = timezone.localdate()
            return today - dt.timedelta(days=DATE_WINDOWS[preset][1] - 1), today
        return _date(self.request.GET.get("from")), _date(self.request.GET.get("to"))

    # ── rendering ───────────────────────────────────────────────────────────

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paginator = Paginator(self.get_queryset(), PER_PAGE)
        page = paginator.get_page(self.request.GET.get("page"))
        start, end = self._window()

        context.update(
            entries=[_row(entry) for entry in page.object_list],
            page=page,
            paginator=paginator,
            actors=self._actor_options(),
            actions=[
                {
                    "value": value,
                    "label": label,
                    "selected": self.request.GET.get("action") == value,
                }
                for value, label in VantageLogEntry.Action.choices
            ],
            windows=[
                {
                    "value": key,
                    "label": label,
                    "selected": self.request.GET.get("window") == key,
                }
                for key, (label, _days) in DATE_WINDOWS.items()
            ],
            selected_actor=self._actor_id(),
            date_from=start.isoformat() if start and not self._preset() else "",
            date_to=end.isoformat() if end and not self._preset() else "",
            has_filters=bool(
                self._actor_id() is not None
                or self.request.GET.get("action")
                or self.request.GET.get("window")
                or self.request.GET.get("from")
                or self.request.GET.get("to")
            ),
        )
        return context

    def _preset(self) -> str:
        preset = self.request.GET.get("window", "")
        return preset if preset in DATE_WINDOWS else ""

    def _actor_options(self) -> list[dict[str, Any]]:
        """Only people who actually appear in the log.

        A select listing every user in the database is unusable at any real size, and
        most of those users have never opened the panel. The distinct set here is
        bounded by the log rather than by the user table.
        """
        selected = self._actor_id()
        rows = (
            VantageLogEntry.objects.exclude(actor__isnull=True)
            .values_list("actor_id", "actor_repr")
            .distinct()
            .order_by("actor_repr")
        )
        seen: dict[int, str] = {}
        for actor_id, label in rows:
            # One person can appear under several stored names if they were renamed.
            # The most recent spelling wins, which is the one their colleagues know.
            seen[actor_id] = label or str(actor_id)
        return [
            {"value": actor_id, "label": label, "selected": actor_id == selected}
            for actor_id, label in sorted(seen.items(), key=lambda item: item[1].lower())
        ]


def _row(entry: VantageLogEntry) -> dict[str, Any]:
    """One entry shaped for the template, with its link resolved here rather than there.

    ``reverse`` can raise, and a raised exception inside template variable resolution
    is a silent blank rather than an error — so the URL is built where a failure is
    visible.
    """
    return {
        "entry": entry,
        "url": _object_url(entry),
        "changes": entry.change_list,
        "model": (
            str(entry.content_type.model_class()._meta.verbose_name).capitalize()
            if entry.content_type_id and entry.content_type.model_class() is not None
            else (entry.content_type.model if entry.content_type_id else "")
        ),
    }


def _object_url(entry: VantageLogEntry) -> str | None:
    """A link to the record, when it is still there and still has a panel.

    Returns ``None`` for a deleted record rather than a dead link: the entry's stored
    ``object_repr`` is what stands in for it, and that is the reason the column exists.
    """
    from django.urls import NoReverseMatch, reverse

    if entry.action == VantageLogEntry.Action.DELETE or not entry.object_id:
        return None
    if not entry.content_type_id:
        return None
    model = entry.content_type.model_class()
    if model is None:
        return None
    opts = model._meta
    try:
        return reverse(
            f"vantage:{opts.app_label}-{opts.model_name}-detail", args=[entry.object_id]
        )
    except NoReverseMatch:
        return None


def _date(raw: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _day_start(value: dt.date) -> dt.datetime:
    from django.conf import settings as django_settings

    naive = dt.datetime.combine(value, dt.time.min)
    if django_settings.USE_TZ:
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return naive


def build_urls(site) -> list:
    """The one route the audit page needs, under the ``_/`` prefix :mod:`vantage.urls`
    already reserves for panel machinery so it can never collide with an app label."""
    from django.urls import path

    return [path("_/audit/", AuditLogView.as_view(site=site), name="audit")]
