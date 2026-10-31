"""The access review: who can see and change which model, and which guarded field.

The evidence an auditor asks for once a quarter ("show me everyone who can change
prices, and everyone who can see card numbers") and that a panel is the right place to
produce, because the panel is where those decisions are actually enforced.

**Asked, not re-derived.** Every answer comes from the configured permission resolver
— :func:`vantage.permissions.get_permissions` — through the exact calls a page makes:
``has_model_perm`` for the four verbs, ``field_access`` for field rules, and the panel's
``can_reveal`` for sensitive values. So a panel's own ``has_<verb>_permission`` hook,
read-only mode, a custom resolver, an authentication backend and an active break-glass
grant are all reflected, and the report cannot describe a rule the panel does not
apply. A resolver needs a request, so each subject gets a minimal one of its own.

**A role is reviewed as a person who holds only that role.** The subject is a
stand-in user whose permissions are exactly the role's, with the role's field rules
loaded for it — then the same resolver answers for it. Panel hooks that look at
something else about the user (an email domain, say) answer for that stand-in, which
is why the per-user report is the one to hand an auditor.

**Bounded cost.** Permissions are cached per user by Django's backends and field rules
per request by :mod:`vantage.fieldperms`, so the cost grows with the number of users,
not with users times models times fields. Field rows are produced only for fields that are
sensitive or that some role restricts — the fields an auditor asks about — and a model
appears for a subject only when the subject holds at least one verb on it.

What it does *not* show: row-level scoping (tenancy, object permissions), which decides
*which* records someone sees rather than whether they see the model at all.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.http import HttpRequest
from django.utils import timezone

__all__ = [
    "BY",
    "CSV_COLUMNS",
    "FORMATS",
    "FieldRow",
    "ModelAccess",
    "Report",
    "Subject",
    "build_report",
    "may_review",
    "to_csv",
    "to_json",
]

FORMATS = ("csv", "json")
BY = ("user", "role")
ACTIONS = ("view", "add", "change", "delete")

CSV_COLUMNS = (
    "subject_type",
    "subject",
    "superuser",
    "elevated_until",
    "model",
    "field",
    "view",
    "add",
    "change",
    "delete",
    "field_visible",
    "field_editable",
    "field_sensitive",
    "field_reveal",
)


def may_review(request) -> bool:
    """Superusers, unless ``VANTAGE["ACCESS_REVIEW_PERMISSION"]`` says otherwise."""
    from vantage.conf import vantage_settings

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    rule = vantage_settings.ACCESS_REVIEW_PERMISSION
    is_superuser = bool(getattr(user, "is_superuser", False))
    if rule is None:
        return is_superuser
    if callable(rule):
        return bool(rule(request))
    return is_superuser or user.has_perm(str(rule))


# ── the shape of the answer ─────────────────────────────────────────────────


@dataclass
class FieldRow:
    name: str
    label: str
    visible: bool
    editable: bool
    sensitive: bool
    #: ``None`` when the panel's ``can_reveal`` decides per record and cannot say.
    reveal: bool | None


@dataclass
class ModelAccess:
    model: str
    label: str
    view: bool
    add: bool
    change: bool
    delete: bool
    fields: list[FieldRow] = field(default_factory=list)


@dataclass
class Subject:
    kind: str
    id: Any
    name: str
    superuser: bool = False
    #: When the subject's active break-glass grants end, if they have any.
    elevated_until: datetime | None = None
    models: list[ModelAccess] = field(default_factory=list)


@dataclass
class Report:
    generated_at: datetime
    version: str
    by: str
    subjects: list[Subject]
    models: list[str]


# ── building it ─────────────────────────────────────────────────────────────


def build_report(site=None, *, by: str = "user", models: Iterable[str] | None = None) -> Report:
    """The whole report. ``models`` narrows it to ``app_label.model`` labels."""
    from vantage import __version__
    from vantage.sites import site as default_site

    if by not in BY:
        raise ValueError(f"by must be one of {BY}, not {by!r}")
    site = site if site is not None else default_site
    wanted = {m.lower() for m in models} if models else None
    panels = [p for p in site.panels() if wanted is None or p.model._meta.label_lower in wanted]
    panels.sort(key=lambda p: p.model._meta.label_lower)
    guarded = _guarded_fields(panels)
    subjects = _users() if by == "user" else _roles()
    for subject, request in subjects:
        for panel in panels:
            entry = _model_access(request, panel, guarded.get(panel.model._meta.label_lower, []))
            if entry is not None:
                subject.models.append(entry)
    return Report(
        generated_at=timezone.now(),
        version=__version__,
        by=by,
        subjects=[s for s, _r in subjects],
        models=[p.model._meta.label_lower for p in panels],
    )


def _guarded_fields(panels) -> dict[str, list[tuple[str, str, bool]]]:
    """Per model, the fields worth a row: sensitive, or restricted by some role.

    One query for the stored rules, whatever the number of models.
    """
    from django.contrib.contenttypes.models import ContentType

    from vantage.fieldperms import code_rules, rule_fields
    from vantage.models import FieldPermission

    stored: dict[int, set[str]] = {}
    for ct_id, name in FieldPermission.objects.values_list("content_type_id", "field_name"):
        stored.setdefault(ct_id, set()).add(name)
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for panel in panels:
        model = panel.model
        ct_id = ContentType.objects.get_for_model(model, for_concrete_model=False).pk
        sensitive = set(panel.get_sensitive_fields())
        restricted = set(stored.get(ct_id, set()))
        for fields in code_rules(panel).values():
            restricted |= set(fields)
        names = sensitive | restricted
        rows = []
        for model_field in rule_fields(model):
            if model_field.name in names:
                label = str(getattr(model_field, "verbose_name", model_field.name)).capitalize()
                rows.append((model_field.name, label, model_field.name in sensitive))
        out[model._meta.label_lower] = rows
    return out


def _request_for(user) -> HttpRequest:
    """The least a permission check needs: a user, an empty session, no client."""
    request = HttpRequest()
    request.method = "GET"
    request.user = user
    request.session = {}  # type: ignore[assignment]
    return request


def _users() -> list[tuple[Subject, HttpRequest]]:
    """Active staff accounts — the people who can sign in to the panel at all."""
    from vantage.breakglass import elevation

    user_model = get_user_model()
    names = {f.name for f in user_model._meta.concrete_fields}
    queryset = user_model._default_manager.all()
    if "is_active" in names:
        queryset = queryset.filter(is_active=True)
    if "is_staff" in names:
        queryset = queryset.filter(is_staff=True)
    out = []
    for user in queryset.order_by(user_model.USERNAME_FIELD):
        request = _request_for(user)
        out.append(
            (
                Subject(
                    kind="user",
                    id=user.pk,
                    name=user.get_username(),
                    superuser=bool(getattr(user, "is_superuser", False)),
                    elevated_until=elevation(request).expires_at,
                ),
                request,
            )
        )
    return out


class _RoleHolder:
    """Someone who holds exactly one role and nothing else, for the per-role report.

    Answers ``has_perm`` from the role's permissions, which is what Django's model
    backend would answer for a member with no other source of permissions. Never a
    superuser, never elevated (no primary key), always staff and active.
    """

    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = True
    is_superuser = False
    pk = None
    id = None

    def __init__(self, group: Group, permissions: frozenset[str]) -> None:
        self.group = group
        self._permissions = permissions

    def get_username(self) -> str:
        return self.group.name

    def has_perm(self, perm: str, obj=None) -> bool:
        return perm in self._permissions

    def has_perms(self, perms, obj=None) -> bool:
        return all(self.has_perm(p, obj) for p in perms)

    def get_all_permissions(self, obj=None) -> set[str]:
        return set(self._permissions)

    def has_module_perms(self, app_label: str) -> bool:
        return any(p.startswith(f"{app_label}.") for p in self._permissions)


def _roles() -> list[tuple[Subject, HttpRequest]]:
    """Every role, each with a stand-in member. Three queries however many roles."""
    from vantage import fieldperms
    from vantage.models import FieldPermission

    groups = list(Group.objects.order_by("name"))
    perms: dict[int, set[str]] = {g.pk: set() for g in groups}
    grants: dict[int, dict[int, dict[int, set[str]]]] = {g.pk: {} for g in groups}
    through = Group.permissions.through
    for gid, ct_id, app_label, codename in through.objects.values_list(
        "group_id",
        "permission__content_type_id",
        "permission__content_type__app_label",
        "permission__codename",
    ):
        perms[gid].add(f"{app_label}.{codename}")
        action = codename.partition("_")[0]
        grants[gid].setdefault(ct_id, {}).setdefault(gid, set()).add(action)
    stored: dict[int, dict[int, dict[int, dict[str, str]]]] = {g.pk: {} for g in groups}
    for ct_id, gid, name, level in FieldPermission.objects.values_list(
        "content_type_id", "group_id", "field_name", "level"
    ):
        stored[gid].setdefault(ct_id, {}).setdefault(gid, {})[name] = level
    out = []
    for group in groups:
        holder = _RoleHolder(group, frozenset(perms[group.pk]))
        request = _request_for(holder)
        # Pre-loaded so fieldperms answers for this role alone, without asking the
        # database which roles the stand-in "belongs to" — it has no row to ask about.
        rules = fieldperms._UserRules(
            user=holder,
            stored=stored[group.pk],
            names={group.pk: group.name},
            _grants=grants[group.pk],
        )
        setattr(request, fieldperms._RULES_ATTR, rules)
        out.append((Subject(kind="role", id=group.pk, name=group.name), request))
    return out


def _model_access(request, panel, guarded) -> ModelAccess | None:
    from vantage import fieldperms
    from vantage.permissions import get_permissions
    from vantage.sensitive import can_reveal

    resolver = get_permissions()
    model = panel.model
    verbs = {action: resolver.has_model_perm(request, model, action) for action in ACTIONS}
    if not any(verbs.values()):
        return None
    access = fieldperms.access(request, model)
    rows = []
    for name, label, sensitive in guarded:
        reveal: bool | None = False
        if sensitive and verbs["view"] and access.can_view(name):
            try:
                reveal = bool(can_reveal(request, panel, None, name))
            except Exception:
                # A ``can_reveal`` hook that reads the record cannot answer without one:
                # the honest entry is "decided per record", not a guess either way.
                reveal = None
        rows.append(
            FieldRow(
                name=name,
                label=label,
                visible=bool(verbs["view"] and access.can_view(name)),
                editable=bool((verbs["add"] or verbs["change"]) and access.can_change(name)),
                sensitive=sensitive,
                reveal=reveal if sensitive else False,
            )
        )
    return ModelAccess(
        model=model._meta.label_lower,
        label=str(panel.get_label_plural()),
        fields=rows,
        **verbs,
    )


# ── output ──────────────────────────────────────────────────────────────────


def _yes(value: bool | None) -> str:
    if value is None:
        return "per-record"
    return "yes" if value else "no"


def rows(report: Report) -> Iterable[list[str]]:
    """Flat rows in :data:`CSV_COLUMNS` order: one per model, one per guarded field."""
    for subject in report.subjects:
        head = [
            subject.kind,
            subject.name,
            _yes(subject.superuser),
            subject.elevated_until.isoformat(timespec="seconds") if subject.elevated_until else "",
        ]
        for entry in subject.models:
            verbs = [_yes(entry.view), _yes(entry.add), _yes(entry.change), _yes(entry.delete)]
            yield [*head, entry.model, "", *verbs, "", "", "", ""]
            for f in entry.fields:
                yield [
                    *head,
                    entry.model,
                    f.name,
                    *verbs,
                    _yes(f.visible),
                    _yes(f.editable),
                    _yes(f.sensitive),
                    _yes(f.reveal) if f.sensitive else "",
                ]


def to_csv(report: Report) -> str:
    """CSV with a comment-free header row; every cell armoured against formulas.

    A username or role name is typed by a person, and ``=HYPERLINK(...)`` as a role
    name would otherwise run in the spreadsheet of whoever opens the evidence.
    """
    from vantage.importer import armour

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows(report):
        writer.writerow([armour(str(cell)) for cell in row])
    return buffer.getvalue()


def to_dict(report: Report) -> dict:
    data = asdict(report)
    data["generated_at"] = report.generated_at.isoformat(timespec="seconds")
    for subject in data["subjects"]:
        until = subject["elevated_until"]
        subject["elevated_until"] = until.isoformat(timespec="seconds") if until else None
    return data


def to_json(report: Report) -> str:
    return json.dumps(to_dict(report), indent=2, default=str)
