"""The calendar: a month grid of records placed by one date field.

**The query is bounded by the visible range.** Only rows whose date falls inside the
weeks on screen are fetched — six weeks at most — and never more than
:attr:`CalendarMode.max_events` of those. A calendar that loaded every dated record and
then picked out this month's would be the one list mode whose cost grows with the
table.

**Days are the viewer's days.** A ``DateTimeField`` stores an instant, and "which square
does it go in" depends on where you are standing: 23:30 UTC on the 3rd is the 4th in
Tokyo. The range is built from local midnights in the active time zone and each event
is bucketed by its local date, so an event never appears on the wrong day, and an
event near midnight is never fetched for one month and drawn in another.

**The week starts where the locale says.** ``FIRST_DAY_OF_WEEK`` from Django's format
localisation, so the grid starts on Sunday in the United States and on Monday nearly
everywhere else without anyone configuring it.
"""

from __future__ import annotations

import calendar as pycalendar
from datetime import date, datetime, time, timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.dates import WEEKDAYS, WEEKDAYS_ABBR
from django.utils.formats import date_format as localized_date
from django.utils.formats import get_format, time_format
from django.utils.translation import gettext_lazy as _

from vantage.listviews.base import ListMode, detail_url
from vantage.listviews.kanban import local_field
from vantage.sensitive import safe_str

__all__ = ["CalendarMode", "month_grid", "parse_month"]


def parse_month(raw: str | None, today: date) -> date:
    """``"2026-10"`` → the 1st of October 2026. Anything unparseable is this month."""
    try:
        year, month = (int(part) for part in (raw or "").split("-", 1))
        return date(year, month, 1)
    except (TypeError, ValueError):
        return today.replace(day=1)


def month_grid(first: date, first_weekday: int) -> tuple[date, date]:
    """The first and last day on screen: whole weeks covering the month.

    ``first_weekday`` uses Django's convention (0 is Sunday); Python's ``weekday()``
    has Monday as 0, hence the shift.
    """
    start_weekday = (first_weekday - 1) % 7
    start = first - timedelta(days=(first.weekday() - start_weekday) % 7)
    last = first.replace(day=pycalendar.monthrange(first.year, first.month)[1])
    end = last + timedelta(days=(start_weekday - last.weekday() - 1) % 7)
    return start, end


def _shift(first: date, months: int) -> date:
    index = first.year * 12 + first.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


class CalendarMode(ListMode):
    name = "calendar"
    label = _("Calendar")
    icon = "calendar"
    template = "vantage/list_views/calendar.html"
    field_label = _("Dates from")

    #: The most records one month will fetch. Past it the page says it was truncated.
    max_events = 500
    #: Events drawn in a day's square before the rest fold into "N more".
    per_day = 3

    @classmethod
    def supports(cls, panel) -> bool:
        return isinstance(local_field(panel.model, panel.get_calendar_field()), models.DateField)

    def field_choices(self) -> list[str]:
        return [
            name
            for name in self.panel.get_calendar_fields(self.request)
            if isinstance(local_field(self.panel.model, name), models.DateField)
        ]

    @property
    def field(self) -> models.DateField:
        return local_field(self.panel.model, self.field_name)

    def bounds(self, start: date, end: date):
        """The query's half-open range, as local midnights for a ``DateTimeField``."""
        if isinstance(self.field, models.DateTimeField):
            lower = datetime.combine(start, time.min)
            upper = datetime.combine(end + timedelta(days=1), time.min)
            if settings.USE_TZ:
                zone = timezone.get_current_timezone()
                lower, upper = timezone.make_aware(lower, zone), timezone.make_aware(upper, zone)
            return lower, upper
        return start, end + timedelta(days=1)

    def local_day(self, value) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if timezone.is_aware(value):
                value = timezone.localtime(value)
            return value.date()
        return value

    def context(self, request, queryset, *, permissions) -> dict[str, Any]:

        field = self.field
        today = timezone.localdate()
        first = parse_month(request.GET.get("month") if request else None, today)
        first_weekday = int(get_format("FIRST_DAY_OF_WEEK") or 0)
        start, end = month_grid(first, first_weekday)
        lower, upper = self.bounds(start, end)

        rows = list(
            queryset.filter(**{f"{field.name}__gte": lower, f"{field.name}__lt": upper}).order_by(
                field.name, "pk"
            )[: self.max_events + 1]
        )
        truncated = len(rows) > self.max_events
        rows = rows[: self.max_events]

        by_day: dict[date, list[dict]] = {}
        timed = isinstance(field, models.DateTimeField)
        for obj in rows:
            value = getattr(obj, field.name)
            day = self.local_day(value)
            if day is None:
                continue
            moment = timezone.localtime(value) if timed and timezone.is_aware(value) else value
            by_day.setdefault(day, []).append(
                {
                    "obj": obj,
                    "pk": obj.pk,
                    "title": safe_str(obj),
                    "url": detail_url(self.panel, obj),
                    "time": time_format(moment) if timed else "",
                    "iso": moment.isoformat(),
                }
            )

        weeks = []
        cursor = start
        while cursor <= end:
            week = []
            for _offset in range(7):
                events = by_day.get(cursor, [])
                week.append(
                    {
                        "date": cursor,
                        "iso": cursor.isoformat(),
                        "label": localized_date(cursor, "DATE_FORMAT"),
                        "in_month": cursor.month == first.month,
                        "is_today": cursor == today,
                        "events": events[: self.per_day],
                        "overflow": events[self.per_day :],
                        "count": len(events),
                    }
                )
                cursor += timedelta(days=1)
            weeks.append(week)

        python_start = (first_weekday - 1) % 7
        order = [(python_start + offset) % 7 for offset in range(7)]
        base = self.own_url(request)
        joiner = "&" if "?" in base and not base.endswith("?") else ""
        return {
            "calendar_field": field.name,
            "calendar_field_label": str(field.verbose_name).capitalize(),
            "calendar_title": date_format(first, get_format("YEAR_MONTH_FORMAT")),
            "calendar_weekdays": [
                {"short": WEEKDAYS_ABBR[index], "long": WEEKDAYS[index]} for index in order
            ],
            "calendar_weeks": weeks,
            "calendar_prev_url": f"{base}{joiner}month={_shift(first, -1):%Y-%m}",
            "calendar_next_url": f"{base}{joiner}month={_shift(first, 1):%Y-%m}",
            "calendar_today_url": base,
            "calendar_is_current": first == today.replace(day=1),
            "calendar_count": len(rows),
            "calendar_truncated": truncated,
            "calendar_max": self.max_events,
            "view_field": self.field_picker(request),
        }
