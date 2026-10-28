"""Data quality: a read-only health check of the *rows* in every registered model.

``vantage.health`` answers "is the deployment sound?". This answers the question that
comes a month later: "is the data any good?" - the nullable column nobody fills in, the
``email`` that was never made unique and now is not, the foreign key with
``db_constraint=False`` pointing at a deleted row, the ``status`` that holds a value
that left the ``choices`` two releases ago.

Four rules shaped it.

*Every check is a small class on a registry.* ``@register_check`` is the whole
extension API, so a project (or a plugin) adds "every invoice has lines" next to the
built-ins instead of forking them. A check yields ``Finding`` objects and nothing else.

*Cost is bounded, and the bound is visible.* A check never scans a table of unknown
size. Above ``SAMPLE_SIZE`` rows it reads the newest rows by primary key (an index
range, one cheap query to find the cutoff) and the report says "sample"; above
``SKIP_ABOVE`` it skips the data checks and says "skipped". Each check has a time
budget, enforced between queries and, on PostgreSQL, as a ``statement_timeout``. The
result is cached, and only the "re-run" button (or the management command) pays again.

*No value ever leaves the database.* A finding carries a count, a field name and a
link, never a cell. That is stronger than masking sensitive-looking columns: there is
nothing to mask, so a report cannot become the way around the masking.

*The same scoping as the list page.* The queryset is ``panel.get_queryset`` passed
through the permission resolver, which is also where tenancy scoping lives, so a
tenant-scoped user audits their tenant's rows and nobody else's. The cache key hashes
the compiled SQL for the same reason ``vantage.scale`` does: the SQL *is* the scope.

Every finding that the filter system can express links to the list page pre-filtered to
the offending rows. Where it cannot (duplicates, orphans, out-of-choices values) the
finding has no link rather than a wrong one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.core.cache import cache
from django.core.exceptions import EmptyResultSet
from django.db import DatabaseError, models, transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Q, Sum
from django.db.models.functions import Cast, Lower
from django.http import QueryDict
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from vantage.conf import DATA_QUALITY_DEFAULTS, vantage_settings
from vantage.introspection import concrete_fields, generic_foreign_keys

__all__ = [
    "BudgetExceeded",
    "Check",
    "CheckRun",
    "Context",
    "Finding",
    "ModelReport",
    "audit_model",
    "config",
    "get_checks",
    "register_check",
    "run_audit",
    "unregister_check",
]

logger = logging.getLogger("vantage.dataquality")

SEVERITIES = ("info", "warn", "fail")
_CACHE_VERSION = "v1"

#: How many distinct content types a generic foreign key is followed into.
MAX_GENERIC_TYPES = 25
#: ``__in`` with more values than this is a poor query; such a field is skipped.
MAX_CHOICES = 500
#: Fields per aggregate, so one statement never carries hundreds of ``CASE WHEN``.
AGGREGATE_CHUNK = 25


def config() -> dict[str, Any]:
    """The resolved ``VANTAGE["DATA_QUALITY"]`` dict, defaults filled in."""
    value = vantage_settings.DATA_QUALITY
    return {**DATA_QUALITY_DEFAULTS, **(value or {})}


# ── the report vocabulary ───────────────────────────────────────────────────


@dataclass
class Finding:
    """One thing a check found. Counts and names only; never a value from a row."""

    check: str
    severity: str
    message: str
    field: str = ""
    count: int | None = None
    #: Rows the count was taken over. ``None`` when a rate is meaningless.
    scanned: int | None = None
    #: The list page, pre-filtered to the offending rows, when the filter system can say so.
    url: str | None = None
    #: The count came from a sample rather than the whole table.
    sampled: bool = False

    @property
    def rate(self) -> float | None:
        if self.count is None or not self.scanned:
            return None
        return self.count / self.scanned

    @property
    def percent(self) -> str | None:
        rate = self.rate
        if rate is None:
            return None
        pct = rate * 100
        return f"{pct:.0f}%" if pct >= 10 or pct == 0 else f"{pct:.1f}%"


@dataclass
class CheckRun:
    """How one check went on one model: it ran, was skipped, timed out, or failed."""

    name: str
    label: str
    status: str  # ok | skipped | timeout | error
    reason: str = ""
    elapsed: float = 0.0


@dataclass
class ModelReport:
    model: str  # label_lower
    label: str
    url: str | None
    rows: int | None
    #: ``exact`` | ``capped`` (a floor) | ``estimated`` | ``unknown``
    rows_kind: str
    rows_display: str
    sampled: bool = False
    skipped: bool = False
    scanned: int | None = None
    findings: list[Finding] = field(default_factory=list)
    checks: list[CheckRun] = field(default_factory=list)
    ran_at: dt.datetime = field(default_factory=timezone.now)
    elapsed: float = 0.0
    error: str = ""

    @property
    def issues(self) -> list[Finding]:
        return [f for f in self.findings if f.severity != "info"]

    @property
    def worst(self) -> Finding | None:
        """The most serious finding, the biggest first among equals."""
        ranked = sorted(
            self.issues,
            key=lambda f: (SEVERITIES.index(f.severity), f.count or 0),
            reverse=True,
        )
        return ranked[0] if ranked else None

    @property
    def score(self) -> int | None:
        """0-100. A failure costs 25, a warning 8; notes cost nothing.

        Per finding rather than per row on purpose: a thousand duplicate emails and
        two are the same kind of problem, and the count is on the finding for anyone
        who wants the size of it. ``None`` when nothing could be checked.
        """
        if self.error or (self.skipped and not self.findings):
            return None
        penalty = sum(25 if f.severity == "fail" else 8 for f in self.issues)
        return max(0, 100 - penalty)

    @property
    def tone(self) -> str:
        score = self.score
        if score is None:
            return "neutral"
        return "success" if score >= 90 else "warning" if score >= 70 else "destructive"

    @property
    def partial(self) -> bool:
        return any(c.status in {"timeout", "error"} for c in self.checks)


# ── the registry ────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """A check ran out of time. Findings already yielded are kept."""


class Check:
    """Base class for a check. Subclass, name it, implement :meth:`run`, register it.

    ``run`` is a generator over :class:`Finding`; yielding as it goes is what lets a
    check that times out still report what it found before the clock ran out.
    """

    name: str = ""
    label: str = ""
    description: str = ""
    #: Whether the check reads row data. A check that does not (empty table) still runs
    #: on a model whose data checks were skipped for size.
    scans: bool = True

    def applies(self, panel) -> bool:
        return True

    def run(self, ctx: Context) -> Iterator[Finding]:
        raise NotImplementedError


_registry: dict[str, Check] = {}


def register_check(cls: type[Check]) -> type[Check]:
    """Class decorator adding a check. Re-registering a name replaces it."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a name.")
    _registry[cls.name] = cls()
    return cls


def unregister_check(name: str) -> None:
    _registry.pop(name, None)


def get_checks() -> list[Check]:
    return list(_registry.values())


# ── what a check sees ───────────────────────────────────────────────────────


class Context:
    """Everything a check needs about one model, and the helpers that keep it honest."""

    def __init__(self, panel, request, queryset, *, rows, scan, sampled, scanned, cfg, budget):
        self.panel = panel
        self.model = panel.model
        self.request = request
        #: The scoped queryset. Counting uses this; reading data uses ``scan``.
        self.queryset = queryset
        self.rows = rows
        #: The bounded queryset every data check reads from, ordering stripped.
        self.scan = scan
        self.sampled = sampled
        self.scanned = scanned
        self.cfg = cfg
        self._deadline = time.monotonic() + budget
        self._filters: set[str] | None = None

    def tick(self) -> None:
        """Call between queries. Raises once this check's time is up."""
        if time.monotonic() > self._deadline:
            raise BudgetExceeded

    def reset_clock(self, budget: float) -> None:
        self._deadline = time.monotonic() + budget

    def finding(self, check: str, severity: str, message: str, **kwargs) -> Finding:
        kwargs.setdefault("sampled", self.sampled)
        return Finding(check, severity, message, **kwargs)

    def filter_url(self, path: str, value: str) -> str | None:
        """The list page filtered to ``path=value``, if the filter system can say it.

        Built by asking the real filter whether it accepts the value, so a link is
        only offered where the list page will honour it. A path the panel does not
        filter on, or a value the filter would drop, yields no link; an unfiltered
        list page would claim a result it did not produce.
        """
        from django.urls import NoReverseMatch, reverse

        from vantage.filters import build_filter

        if self._filters is None:
            self._filters = {str(p) for p in self.panel.get_list_filter(self.request)}
        if path not in self._filters:
            return None
        probe = QueryDict(mutable=True)
        probe[path] = value
        accepted = build_filter(self.model, path, probe)
        if accepted is None or accepted.values != [value]:
            return None
        try:
            base = reverse(self.panel.list_url_name)
        except NoReverseMatch:  # pragma: no cover
            return None
        from urllib.parse import urlencode

        return f"{base}?{urlencode({path: value})}"

    def aggregate(self, expressions: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Aggregate over ``scan`` in chunks, ticking the clock between statements."""
        items = list(expressions.items())
        for start in range(0, len(items), AGGREGATE_CHUNK):
            self.tick()
            yield self.scan.aggregate(**dict(items[start : start + AGGREGATE_CHUNK]))


# ── the built-in checks ─────────────────────────────────────────────────────

_TEXTLIKE = (models.CharField, models.TextField, models.FileField)
_SOFT_DELETE_NAME = re.compile(r"^(deleted|archived|trashed|removed)", re.I)


def _empty_q(f) -> Q:
    q = Q(**{f"{f.name}__isnull": True})
    if isinstance(f, _TEXTLIKE):
        q |= Q(**{f.name: ""})
    return q


@register_check
class NullsCheck(Check):
    name = "nulls"
    label = "Empty values"
    description = "Share of rows where a nullable or blankable field is empty."

    def run(self, ctx):
        candidates = [
            f
            for f in concrete_fields(ctx.model)
            if not f.primary_key
            and not f.auto_created
            and (f.null or (f.blank and isinstance(f, _TEXTLIKE)))
            and not _SOFT_DELETE_NAME.match(f.name)
        ]
        if not candidates or not ctx.scanned:
            return
        warn_rate = float(ctx.cfg["NULL_WARN_RATE"])
        by_name = {f.name: f for f in candidates}
        exprs = {f"n_{f.name}": Count("pk", filter=_empty_q(f)) for f in candidates}
        for chunk in ctx.aggregate(exprs):
            for key, count in chunk.items():
                if not count:
                    continue
                f = by_name[key[2:]]
                severity = "warn" if count / ctx.scanned >= warn_rate else "info"
                # A "null" link means isnull. For a text field the count also includes
                # "" which the filter cannot express, so only offer it where the two
                # agree.
                url = None if isinstance(f, _TEXTLIKE) else ctx.filter_url(f.name, "null")
                yield ctx.finding(
                    self.name,
                    severity,
                    _("Empty in %(count)s of %(rows)s rows")
                    % {"count": f"{count:,}", "rows": f"{ctx.scanned:,}"},
                    field=f.name,
                    count=count,
                    scanned=ctx.scanned,
                    url=url,
                )


@register_check
class DuplicatesCheck(Check):
    name = "duplicates"
    label = "Duplicate candidates"
    description = (
        "Fields that look like they should be unique (email, slug, sku...) but are not constrained."
    )

    #: Names that say "identifier". Exact names only: ``code`` and ``number`` appear in
    #: ``zip_code`` and ``floor_number``, and a check that cries wolf gets switched off.
    NAMES = frozenset(
        {
            "email",
            "e_mail",
            "email_address",
            "slug",
            "sku",
            "code",
            "username",
            "user_name",
            "handle",
            "isbn",
            "barcode",
            "serial",
            "serial_number",
            "number",
            "phone",
            "uuid",
            "reference",
            "ref",
        }
    )
    SUFFIXES = ("_email", "_slug", "_sku", "_username", "_isbn", "_barcode")

    def candidates(self, ctx) -> list:
        extra = {str(n) for n in ctx.cfg["DUPLICATE_FIELDS"]}
        meta = ctx.model._meta
        constrained: set[str] = set()
        for group in meta.unique_together:
            constrained.update(group)
        for c in meta.constraints:
            if isinstance(c, models.UniqueConstraint) and c.fields:
                # A multi-column or conditional constraint makes duplicates legitimate
                # in some scope, so the field is not "should be globally unique".
                constrained.update(c.fields)
        found = []
        for f in concrete_fields(ctx.model):
            if f.primary_key or f.unique or f.is_relation or f.name in constrained:
                continue
            if not isinstance(f, models.CharField | models.UUIDField):
                continue
            name = f.name.lower()
            if (
                name in self.NAMES
                or name.endswith(self.SUFFIXES)
                or f.name in extra
                or f"{ctx.model._meta.label_lower}.{f.name}" in extra
            ):
                found.append(f)
        return found

    def run(self, ctx):
        if not ctx.scanned:
            return
        for f in self.candidates(ctx):
            ctx.tick()
            key = Lower(f.name) if isinstance(f, models.CharField) else F(f.name)
            rows = ctx.scan.filter(**{f"{f.name}__isnull": False})
            if isinstance(f, models.CharField):
                rows = rows.exclude(**{f.name: ""})
            dup = rows.annotate(_dq_key=key).values("_dq_key").annotate(_dq_n=Count("pk"))
            dup = dup.filter(_dq_n__gt=1).order_by()
            groups = dup.count()
            if not groups:
                continue
            ctx.tick()
            affected = dup.aggregate(total=Sum("_dq_n"))["total"] or 0
            yield ctx.finding(
                self.name,
                "warn",
                ngettext(
                    "%(groups)s value is shared by %(rows)s rows",
                    "%(groups)s values are each shared by several rows (%(rows)s rows in all)",
                    groups,
                )
                % {"groups": f"{groups:,}", "rows": f"{affected:,}"},
                field=f.name,
                count=affected,
                scanned=ctx.scanned,
            )


@register_check
class OrphansCheck(Check):
    name = "orphans"
    label = "Orphaned references"
    description = (
        "Foreign keys without a database constraint, and generic foreign keys, "
        "that point at rows which no longer exist."
    )

    def applies(self, panel) -> bool:
        return bool(self._foreign_keys(panel.model) or generic_foreign_keys(panel.model))

    @staticmethod
    def _foreign_keys(model) -> list:
        return [
            f
            for f in concrete_fields(model)
            if f.is_relation
            and (f.many_to_one or f.one_to_one)
            and f.related_model is not None
            and not f.db_constraint
        ]

    def run(self, ctx):
        if not ctx.scanned:
            return
        for f in self._foreign_keys(ctx.model):
            ctx.tick()
            target = f.related_model._base_manager.filter(
                **{f.target_field.attname: OuterRef(f.attname)}
            )
            count = (
                ctx.scan.filter(**{f"{f.attname}__isnull": False})
                .annotate(_dq_ok=Exists(target))
                .filter(_dq_ok=False)
                .count()
            )
            if count:
                yield self._finding(ctx, f.name, count)
        for gfk in generic_foreign_keys(ctx.model):
            ctx.tick()
            count = self._generic_orphans(ctx, gfk)
            if count:
                yield self._finding(ctx, gfk.name, count)

    def _finding(self, ctx, name, count):
        return ctx.finding(
            self.name,
            "fail",
            _("%(count)s rows point at a row that does not exist") % {"count": f"{count:,}"},
            field=name,
            count=count,
            scanned=ctx.scanned,
        )

    def _generic_orphans(self, ctx, gfk) -> int:
        from django.contrib.contenttypes.models import ContentType

        meta = ctx.model._meta
        ct_attname = meta.get_field(gfk.ct_field).attname
        base = ctx.scan.filter(**{f"{ct_attname}__isnull": False, f"{gfk.fk_field}__isnull": False})
        types = list(
            base.values(ct_attname).annotate(_dq_n=Count("pk")).order_by()[:MAX_GENERIC_TYPES]
        )
        known = ContentType.objects.in_bulk([t[ct_attname] for t in types])
        orphans = 0
        for entry in types:
            ctx.tick()
            content_type = known.get(entry[ct_attname])
            target = content_type.model_class() if content_type else None
            if target is None:
                # The content type row, or the model it names, is gone.
                orphans += entry["_dq_n"]
                continue
            pk = target._meta.pk
            pk = pk.target_field if pk.is_relation else pk
            exists = Exists(
                target._base_manager.filter(
                    pk=Cast(OuterRef(gfk.fk_field), output_field=pk.clone())
                )
            )
            orphans += (
                base.filter(**{ct_attname: entry[ct_attname]})
                .annotate(_dq_ok=exists)
                .filter(_dq_ok=False)
                .count()
            )
        return orphans


@register_check
class ChoicesCheck(Check):
    name = "choices"
    label = "Values outside choices"
    description = "Rows whose value is not one of the field's declared choices."

    def run(self, ctx):
        fields = [f for f in concrete_fields(ctx.model) if f.choices and not f.is_relation]
        if not fields or not ctx.scanned:
            return
        by_name: dict[str, Any] = {}
        exprs = {}
        for f in fields:
            valid = [value for value, _label in f.flatchoices]
            if not valid or len(valid) > MAX_CHOICES:
                continue
            outside = Q(**{f"{f.name}__isnull": False}) & ~Q(**{f"{f.name}__in": valid})
            if f.blank and isinstance(f, models.CharField):
                outside &= ~Q(**{f.name: ""})
            by_name[f.name] = f
            exprs[f"c_{f.name}"] = Count("pk", filter=outside)
        for chunk in ctx.aggregate(exprs):
            for key, count in chunk.items():
                if count:
                    yield ctx.finding(
                        self.name,
                        "warn",
                        _("%(count)s rows hold a value that is not a current choice")
                        % {"count": f"{count:,}"},
                        field=key[2:],
                        count=count,
                        scanned=ctx.scanned,
                    )


_CREATED_LIKE = re.compile(
    r"(created|inserted|added|registered|joined|signed_up|updated|modified|last_login)", re.I
)
EARLIEST_PLAUSIBLE = dt.date(1900, 1, 1)


@register_check
class DatesCheck(Check):
    name = "dates"
    label = "Suspicious dates"
    description = "Created-like timestamps in the future, and any date before 1900."

    def run(self, ctx):
        from vantage.filters import _day_start

        fields = [f for f in concrete_fields(ctx.model) if isinstance(f, models.DateField)]
        if not fields or not ctx.scanned:
            return
        today = timezone.localdate()
        # Two days out, not one: a timestamp from a server a few hours ahead is clock
        # skew, not corruption, and the list-page link below is whole days.
        future_start = today + dt.timedelta(days=2)
        is_dt = {f.name: isinstance(f, models.DateTimeField) for f in fields}

        def bound(f, day):
            return _day_start(day) if is_dt[f.name] else day

        exprs = {}
        for f in fields:
            exprs[f"p_{f.name}"] = Count(
                "pk", filter=Q(**{f"{f.name}__lt": bound(f, EARLIEST_PLAUSIBLE)})
            )
            if _CREATED_LIKE.search(f.name):
                exprs[f"f_{f.name}"] = Count(
                    "pk", filter=Q(**{f"{f.name}__gte": bound(f, future_start)})
                )
        for chunk in ctx.aggregate(exprs):
            for key, count in chunk.items():
                if not count:
                    continue
                kind, name = key[0], key[2:]
                if kind == "f":
                    message = _("%(count)s rows are dated in the future")
                    url = ctx.filter_url(name, f"{future_start.isoformat()}..")
                else:
                    message = _("%(count)s rows are dated before 1900")
                    before = EARLIEST_PLAUSIBLE - dt.timedelta(days=1)
                    url = ctx.filter_url(name, f"..{before.isoformat()}")
                yield ctx.finding(
                    self.name,
                    "warn",
                    message % {"count": f"{count:,}"},
                    field=name,
                    count=count,
                    scanned=ctx.scanned,
                    url=url,
                )


@register_check
class EmptyTableCheck(Check):
    name = "empty"
    label = "Empty table"
    description = "A registered model with no rows."
    scans = False

    def run(self, ctx):
        if ctx.rows.value == 0 and ctx.rows.exact:
            yield ctx.finding(self.name, "info", _("There are no rows yet"), count=0)


@register_check
class StaleTableCheck(Check):
    name = "stale"
    label = "Stale table"
    description = "A table whose last-modified field has not moved in a long while."

    NAMES = (
        "updated_at",
        "modified_at",
        "updated",
        "modified",
        "last_modified",
        "last_updated",
        "updated_on",
        "modified_on",
    )

    def modified_field(self, model):
        fields = {f.name: f for f in concrete_fields(model) if isinstance(f, models.DateField)}
        for f in fields.values():
            if getattr(f, "auto_now", False):
                return f
        for name in self.NAMES:
            if name in fields:
                return fields[name]
        return None

    def applies(self, panel) -> bool:
        return self.modified_field(panel.model) is not None

    def run(self, ctx):
        f = self.modified_field(ctx.model)
        if f is None or not ctx.scanned:
            return
        ctx.tick()
        latest = ctx.scan.aggregate(latest=Max(f.name))["latest"]
        if latest is None:
            return
        today = timezone.localdate()
        last_day = timezone.localtime(latest).date() if isinstance(latest, dt.datetime) else latest
        age = (today - last_day).days
        limit = int(ctx.cfg["STALE_DAYS"])
        if age <= limit:
            return
        cutoff = today - dt.timedelta(days=limit)
        yield ctx.finding(
            self.name,
            "warn",
            _("Nothing has changed in %(days)s days (by %(field)s)")
            % {"days": f"{age:,}", "field": f.name},
            field=f.name,
            url=ctx.filter_url(f.name, f"..{cutoff.isoformat()}"),
        )


# ── running an audit ────────────────────────────────────────────────────────


def panel_opt_out(panel) -> tuple[bool, set[str]]:
    """``(skip entirely, check names to skip)`` from ``ModelPanel.data_quality``."""
    value = getattr(panel, "data_quality", None)
    if value is False:
        return True, set()
    if isinstance(value, list | tuple | set | frozenset):
        return False, {str(v) for v in value}
    return False, set()


def scoped_queryset(panel, request, permissions):
    """The rows this user may audit: the list page's queryset, minus its sorting."""
    qs = permissions.filter_queryset(request, panel.get_queryset(request), "view")
    patterns = panel.get_patterns()
    if patterns.soft_delete and not patterns.soft_delete.manager_hides_trashed:
        qs = qs.filter(patterns.soft_delete.live)
    return qs.order_by()


def _cache_key(panel, queryset, cfg, check_names) -> str:
    try:
        scope = str(queryset.query)
    except EmptyResultSet:
        scope = "empty"
    tuning = [
        cfg[k]
        for k in sorted(cfg)
        if k not in {"ENABLED", "PERMISSION", "PAGE_TIME_BUDGET", "CACHE_SECONDS"}
    ]
    digest = hashlib.sha256(
        f"{queryset.db}|{scope}|{tuning!r}|{check_names!r}".encode()
    ).hexdigest()
    return f"vantage.dq.{_CACHE_VERSION}.{panel.opts.label_lower}.{digest[:32]}"


def _sample(queryset, size: int):
    """The newest ``size`` rows by primary key, as a queryset; or ``None`` if unsupported.

    One bounded index lookup finds the cutoff key, then ``pk >= cutoff`` is an index
    range. A ``LIMIT`` inside an ``IN`` subquery is not portable (MySQL refuses it), and
    ``ORDER BY random()`` is a full scan, which is what sampling exists to avoid.
    """
    if queryset.query.is_sliced or queryset.query.combinator:
        return None
    pk = queryset.model._meta.pk
    if getattr(pk, "columns", None):  # a composite primary key has no single cutoff
        return None
    cutoff = list(queryset.order_by("-pk").values_list("pk", flat=True)[size - 1 : size])
    if not cutoff:
        return queryset
    return queryset.filter(pk__gte=cutoff[0])


def audit_model(panel, request, permissions, *, checks=None, cfg=None) -> ModelReport:
    """Run every applicable check on one model. Never raises for a data problem."""
    from vantage import scale

    cfg = cfg or config()
    checks = get_checks() if checks is None else checks
    started = time.monotonic()
    queryset = scoped_queryset(panel, request, permissions)
    report = ModelReport(
        model=panel.opts.label_lower,
        label=str(panel.get_label_plural()),
        url=None,
        rows=None,
        rows_kind="unknown",
        rows_display="?",
    )
    try:
        from django.urls import reverse

        report.url = reverse(panel.list_url_name)
    except Exception:  # pragma: no cover - a panel not mounted on this urlconf
        pass

    sample_size = max(int(cfg["SAMPLE_SIZE"]), 1)
    budget = float(cfg["CHECK_TIME_BUDGET"])
    try:
        with transaction.atomic(using=queryset.db):
            rows = scale.count_rows(
                queryset, cap=sample_size, request=request, permissions=permissions
            )
    except DatabaseError as exc:
        report.error = type(exc).__name__
        report.elapsed = time.monotonic() - started
        return report
    report.rows = rows.value
    report.rows_display = rows.display
    report.rows_kind = "capped" if rows.capped else "estimated" if rows.estimated else "exact"

    skip_above = int(cfg["SKIP_ABOVE"])
    scan = queryset
    if rows.estimated and skip_above and rows.value > skip_above:
        report.skipped = True
        scan = None
    elif rows.capped or rows.estimated:
        scan = _sample(queryset, sample_size)
        report.sampled = True
        if scan is None:
            report.skipped = True
    report.scanned = (
        0 if scan is None else min(rows.value, sample_size) if report.sampled else rows.value
    )

    ctx = Context(
        panel,
        request,
        queryset,
        rows=rows,
        scan=scan,
        sampled=report.sampled,
        scanned=report.scanned,
        cfg=cfg,
        budget=budget,
    )
    excluded = set(cfg["CHECKS_EXCLUDE"]) | panel_opt_out(panel)[1]
    for check in checks:
        if check.name in excluded or not check.applies(panel):
            continue
        label = str(check.label or check.name)
        if check.scans and report.skipped:
            report.checks.append(
                CheckRun(
                    check.name, label, "skipped", _("Table too large to check or sample safely")
                )
            )
            continue
        report.checks.append(_run_check(check, label, ctx, budget, report))
    report.elapsed = time.monotonic() - started
    return report


def _run_check(check, label, ctx, budget, report) -> CheckRun:
    from vantage import scale

    ctx.reset_clock(budget)
    started = time.monotonic()
    status, reason = "ok", ""
    try:
        # A savepoint, so a statement the database rejects cannot poison an outer
        # transaction (PostgreSQL aborts it on any error), plus a server-side ceiling
        # where the backend has one.
        with (
            transaction.atomic(using=ctx.queryset.db),
            scale.statement_timeout(int(budget * 1000), using=ctx.queryset.db),
        ):
            for finding in check.run(ctx):
                report.findings.append(finding)
    except BudgetExceeded:
        status, reason = "timeout", _("Stopped at its time budget")
    except DatabaseError as exc:
        timed_out = "timeout" in str(exc).lower() or "canceling" in str(exc).lower()
        status = "timeout" if timed_out else "error"
        reason = _("Stopped at its time budget") if timed_out else type(exc).__name__
        logger.debug("data quality check %s failed", check.name, exc_info=True)
    except Exception as exc:
        # A third-party check that raises must not take the report with it.
        status, reason = "error", type(exc).__name__
        logger.warning("data quality check %s raised", check.name, exc_info=True)
    return CheckRun(check.name, label, status, str(reason), time.monotonic() - started)


@dataclass
class Audit:
    reports: list[ModelReport] = field(default_factory=list)
    #: ``(label_lower, label)`` for models not reached before the page budget ran out,
    #: or with nothing cached while ``cached_only`` was requested.
    pending: list[tuple[str, str]] = field(default_factory=list)

    @property
    def worst_issue_count(self) -> int:
        return sum(len(r.issues) for r in self.reports)


def _without_hidden(report: ModelReport, request) -> ModelReport:
    """``report`` minus the findings about fields hidden from this person."""
    from dataclasses import replace

    from django.apps import apps as django_apps

    from vantage.fieldperms import hidden_names

    try:
        model = django_apps.get_model(report.model)
    except (LookupError, ValueError):
        return report
    hidden = hidden_names(request, model)
    if not hidden or not any(f.field in hidden for f in report.findings):
        return report
    return replace(report, findings=[f for f in report.findings if f.field not in hidden])


def run_audit(
    site,
    request,
    *,
    labels: list[str] | None = None,
    force: bool = False,
    page_budget: float | None = None,
    use_cache: bool = True,
) -> Audit:
    """Audit every model this user may view, from cache where possible.

    ``force`` re-runs (the "re-run" button). ``page_budget`` caps total time spent
    running audits in this call, in seconds; models not reached are returned in
    ``pending`` and keep whatever the cache already held. ``None`` is unbounded, which
    is what the management command wants.
    """
    from vantage.permissions import get_permissions

    cfg = config()
    permissions = get_permissions()
    checks = get_checks()
    names = [c.name for c in checks]
    audit = Audit()
    deadline = None if page_budget is None else time.monotonic() + page_budget
    ttl = int(cfg["CACHE_SECONDS"])

    for panel in permissions.visible_panels(request, list(site.panels())):
        label = panel.opts.label_lower
        if labels is not None and label not in labels:
            continue
        if panel_opt_out(panel)[0]:
            continue
        queryset = scoped_queryset(panel, request, permissions)
        key = _cache_key(panel, queryset, cfg, names)
        cached = cache.get(key) if use_cache else None
        if cached is not None and not force:
            audit.reports.append(cached)
            continue
        if deadline is not None and time.monotonic() > deadline:
            if cached is not None:
                audit.reports.append(cached)
            audit.pending.append((label, str(panel.get_label_plural())))
            continue
        report = audit_model(panel, request, permissions, checks=checks, cfg=cfg)
        if use_cache and not report.error:
            cache.set(key, report, ttl)
        audit.reports.append(report)

    # After the cache, not inside it: the cache key is the rows, which two people with
    # different field rules share, and a finding about a field one of them cannot see
    # is a statement about its values ("12% empty", "3 duplicates").
    audit.reports = [_without_hidden(report, request) for report in audit.reports]

    audit.reports.sort(key=lambda r: ((r.score is None), r.score or 0, r.label.lower()))
    return audit
