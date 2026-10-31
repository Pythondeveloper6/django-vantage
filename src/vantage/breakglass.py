"""Break glass: time-boxed elevated access, granted with a reason and audited end to end.

Auditors dislike standing admin-wide privileges, and they are right to: an account that
can do everything all year is an account whose compromise costs everything. The usual
answer is a smaller standing set plus a way to hold more *for minutes*, with a stated
reason, a second person's approval and a trail. That is what this module is.

Four decisions shape it.

**Active is computed, never maintained.** A grant is a row with a window. Whether it
confers anything is decided at the moment a permission is checked, from the clock — so
an expired or revoked grant gives nothing the instant it expires or is revoked, whether
or not any job ran. Nothing is added to group membership, which is the design that
leaves people elevated when the cron that was meant to remove them did not run.

**One integration point.** :class:`~vantage.permissions.PanelPermissions` asks
:func:`holds` when the standing answer is no, for model-level and object-level checks,
and :func:`widen` for field rules. Every screen, the JSON API, actions, exports and a
background job's re-check already go through that resolver, so all of them honour an
elevation and its expiry without knowing this module exists. The answer is cached on
the request and nowhere else: the next request asks the database again.

**Elevation only widens, and never to superuser.** A grant adds permission strings, and
a role grant adds the role's vote in field rules (see :mod:`vantage.fieldperms`). It
cannot narrow anything a person already had, it cannot set ``is_superuser``, and it is
*not* counted when deciding what someone may grant: an elevated person cannot use
their elevation to hand out more of it, nor to edit roles.

**Granting follows the roles screen's rule.** Superusers only by default
(``VANTAGE["BREAK_GLASS_PERMISSION"]`` widens it), and a granter who is not a
superuser may only grant permissions they hold themselves. A person may *request*
elevation for themselves; someone else approves it unless
``VANTAGE["BREAK_GLASS_SELF_SERVICE"]`` lets authorised granters activate their own —
that switch is the one deliberate exception to the reach rule, because elevating an
on-call engineer beyond their standing access is the whole point of it, and the reason,
the cap and the audit trail are what bound it.

Every request, grant, activation, first use of each permission, expiry and revocation
writes an entry to the audit log. Activation and expiry are recorded by whichever check
first observes them, exactly once, so they too need no scheduled job.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

__all__ = [
    "EMPTY",
    "MIN_REASON_LENGTH",
    "Elevation",
    "approve",
    "banner",
    "clean_minutes",
    "clean_permissions",
    "clean_reason",
    "elevation",
    "forget",
    "grant",
    "holds",
    "max_minutes",
    "may_approve",
    "may_grant",
    "observe_all",
    "request_elevation",
    "revoke",
    "self_service",
    "widen",
]

#: The shortest reason accepted. Long enough that "fix" or "asdf" does not pass,
#: short enough that nobody pads it to get through an outage.
MIN_REASON_LENGTH = 10

_CACHE_ATTR = "_vantage_elevation"
_RULES_ATTR = "_vantage_elevated_field_rules"
_USED_ATTR = "_vantage_elevation_used"

ACTIVE = "active"
EXPIRED = "expired"


# ── settings ────────────────────────────────────────────────────────────────


def max_minutes() -> int:
    from vantage.conf import vantage_settings

    try:
        value = int(vantage_settings.BREAK_GLASS_MAX_MINUTES)
    except (TypeError, ValueError):
        value = 60
    return max(1, value)


def self_service() -> bool:
    from vantage.conf import vantage_settings

    return bool(vantage_settings.BREAK_GLASS_SELF_SERVICE)


def may_grant(request) -> bool:
    """Superusers, unless ``VANTAGE["BREAK_GLASS_PERMISSION"]`` says otherwise.

    The same shape as :func:`vantage.roles.may_manage_roles`. ``user.has_perm`` is the
    *standing* answer — an elevation never counts towards granting another.
    """
    from vantage.conf import vantage_settings

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    rule = vantage_settings.BREAK_GLASS_PERMISSION
    is_superuser = bool(getattr(user, "is_superuser", False))
    if rule is None:
        return is_superuser
    if callable(rule):
        return bool(rule(request))
    return is_superuser or user.has_perm(str(rule))


# ── what someone holds right now ────────────────────────────────────────────


@dataclass(frozen=True)
class Elevation:
    """One person's active grants, as the permission layer needs them. Falsy when none."""

    grants: tuple[Any, ...] = ()
    #: ``"app_label.codename"`` strings, the shape ``user.has_perm`` takes.
    permissions: frozenset[str] = frozenset()
    #: Roles held through a grant, for field rules.
    roles: tuple[int, ...] = ()
    #: Which grant supplies each permission, for the "used" audit entry.
    sources: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.grants)

    @property
    def expires_at(self) -> datetime | None:
        """When the last of these grants ends."""
        ends = [g.expires_at for g in self.grants if g.expires_at is not None]
        return max(ends) if ends else None


EMPTY = Elevation()


def elevation(request) -> Elevation:
    """The signed-in person's active grants, computed once per request.

    Superusers have none — they hold everything already, and asking would cost every
    superuser page a query for nothing. Cached on the request keyed by the user, because
    a background job swaps ``request.user`` for a reloaded actor.
    """
    if request is None:
        return EMPTY
    user = getattr(request, "user", None)
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "is_superuser", False)
        or getattr(user, "pk", None) is None
    ):
        return EMPTY
    cached = getattr(request, _CACHE_ATTR, None)
    if cached is not None and cached[0] == user.pk:
        return cached[1]
    answer = _load(user)
    try:
        setattr(request, _CACHE_ATTR, (user.pk, answer))
    except AttributeError:  # pragma: no cover - an exotic request-like object
        pass
    return answer


def forget(request) -> None:
    """Drop what this request cached, after a grant changed mid-request."""
    for attr in (_CACHE_ATTR, _RULES_ATTR, _USED_ATTR):
        if hasattr(request, attr):
            delattr(request, attr)


def _load(user) -> Elevation:
    try:
        return _query(user)
    except RuntimeError as exc:
        # The bargain vantage.fieldperms strikes: pytest-django blocks the database for
        # tests that did not ask for it, and that is not a reason to fail a page.
        if "Database access not allowed" not in str(exc):
            raise
        return EMPTY


def _query(user) -> Elevation:
    from vantage.models import ElevatedAccess

    now = timezone.now()
    # Active grants, plus ones that have expired but whose expiry is not logged yet —
    # the same query records the expiry, so no job has to.
    rows = list(
        ElevatedAccess.objects.filter(
            user_id=user.pk, granted_at__isnull=False, revoked_at__isnull=True
        )
        .filter(Q(expires_at__gt=now) | ~Q(logged_state=EXPIRED))
        .select_related("role")
    )
    if not rows:
        return EMPTY
    _observe(rows, now)
    active = tuple(g for g in rows if _is_active(g, now))
    return _build(active)


def _is_active(grant, now: datetime) -> bool:
    return (
        grant.granted_at is not None
        and grant.revoked_at is None
        and grant.starts_at is not None
        and grant.expires_at is not None
        and grant.starts_at <= now < grant.expires_at
    )


def _build(active: tuple[Any, ...]) -> Elevation:
    if not active:
        return EMPTY
    sources: dict[str, list[int]] = {}
    roles = tuple(sorted({g.role_id for g in active if g.role_id is not None}))
    by_role: dict[int, set[str]] = {}
    if roles:
        through = Group.permissions.through
        for gid, app_label, codename in through.objects.filter(group_id__in=roles).values_list(
            "group_id", "permission__content_type__app_label", "permission__codename"
        ):
            by_role.setdefault(gid, set()).add(f"{app_label}.{codename}")
    for grant in active:
        labels = set(grant.permissions or [])
        if grant.role_id is not None:
            labels |= by_role.get(grant.role_id, set())
        for label in labels:
            sources.setdefault(label, []).append(grant.pk)
    return Elevation(
        grants=active,
        permissions=frozenset(sources),
        roles=roles,
        sources={k: tuple(v) for k, v in sources.items()},
    )


def holds(request, perm: str) -> bool:
    """Whether an active grant gives this request's user ``perm``.

    Asked by the permission layer only after the standing answer was no, so a person
    with no grants pays for it once per request, on their first refusal. The first time
    a grant actually decides an allow for a permission, that use is audited.
    """
    answer = elevation(request)
    if not answer or perm not in answer.permissions:
        return False
    _note_use(request, answer, perm)
    return True


def _note_use(request, answer: Elevation, perm: str) -> None:
    from vantage.models import ElevatedAccess

    seen = getattr(request, _USED_ATTR, None)
    if seen is None:
        seen = set()
        try:
            setattr(request, _USED_ATTR, seen)
        except AttributeError:  # pragma: no cover
            pass
    if perm in seen:
        return
    seen.add(perm)
    by_pk = {g.pk: g for g in answer.grants}
    for pk in answer.sources.get(perm, ()):
        grant = by_pk[pk]
        if perm in (grant.used or []):
            continue
        grant.used = [*(grant.used or []), perm]
        ElevatedAccess.objects.filter(pk=pk).update(used=grant.used)
        _audit(
            request,
            grant,
            "break-glass.used",
            {gettext("Permission used"): {"to": perm}},
        )


def widen(request, user, model, answer):
    """Field restrictions with the elevated roles' votes added — never narrower.

    The roles held through a grant vote in field rules exactly as memberships do. The
    result is intersected with the standing answer, so adding a role can only lift a
    restriction: a grant meant to help someone must not hide what they could already see.
    """
    from vantage import fieldperms

    if not answer:
        return answer
    elevated = elevation(request)
    if not elevated.roles:
        return answer
    rules = getattr(request, _RULES_ATTR, None)
    if rules is None or rules.user is not user or rules.extra != elevated.roles:
        rules = fieldperms._load(user, extra=elevated.roles)
        try:
            setattr(request, _RULES_ATTR, rules)
        except AttributeError:  # pragma: no cover
            pass
    other = fieldperms.resolve(user, model, rules=rules)
    hidden = answer.hidden & other.hidden
    readonly = (answer.locked & other.locked) - hidden
    return fieldperms.FieldAccess(hidden=frozenset(hidden), readonly=frozenset(readonly))


# ── the lifecycle entries nobody has to schedule ────────────────────────────


def _observe(grants: Iterable[Any], now: datetime | None = None) -> None:
    """Write the activation and expiry entries that are due, each exactly once.

    The conditional ``UPDATE`` is the lock: two requests observing the same expiry at
    the same moment both try to move ``logged_state``, one of them changes a row, and
    only that one writes the entry.
    """
    from vantage.models import ElevatedAccess

    now = now or timezone.now()
    for grant in grants:
        if grant.granted_at is None or grant.revoked_at is not None:
            continue
        if grant.starts_at is None or grant.expires_at is None:
            continue
        state = grant.logged_state
        if state == "" and grant.starts_at <= now:
            if ElevatedAccess.objects.filter(pk=grant.pk, logged_state="").update(
                logged_state=ACTIVE
            ):
                grant.logged_state = state = ACTIVE
                _audit(
                    None,
                    grant,
                    "break-glass.activated",
                    {gettext("Active from"): {"to": _stamp(grant.starts_at)}},
                )
        if state == ACTIVE and grant.expires_at <= now:
            if ElevatedAccess.objects.filter(pk=grant.pk, logged_state=ACTIVE).update(
                logged_state=EXPIRED
            ):
                grant.logged_state = EXPIRED
                _audit(
                    None,
                    grant,
                    "break-glass.expired",
                    {gettext("Expired at"): {"to": _stamp(grant.expires_at)}},
                )


def observe_all() -> int:
    """Record every due activation and expiry, for the list screen and the command.

    Never required for correctness — the permission check does not depend on it — but
    it lets the audit log show an expiry for someone who never came back after it.
    """
    from vantage.models import ElevatedAccess

    now = timezone.now()
    due = list(
        ElevatedAccess.objects.filter(granted_at__isnull=False, revoked_at__isnull=True).filter(
            Q(logged_state="", starts_at__lte=now) | Q(logged_state=ACTIVE, expires_at__lte=now)
        )
    )
    _observe(due, now)
    return len(due)


def _stamp(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else ""


def _audit(request, grant, action_name: str, changes: dict) -> None:
    from vantage.audit import record
    from vantage.models import VantageLogEntry

    record(
        request,
        VantageLogEntry.Action.ACTION,
        obj=grant,
        changes=changes,
        action_name=action_name,
        object_repr=str(grant),
    )


# ── validation ──────────────────────────────────────────────────────────────


def clean_reason(value: Any) -> str:
    reason = " ".join(str(value or "").split())
    if not reason:
        raise ValidationError(_("A reason is required."))
    if len(reason) < MIN_REASON_LENGTH:
        raise ValidationError(
            _("Say why in at least %(count)d characters.") % {"count": MIN_REASON_LENGTH}
        )
    return reason


def clean_minutes(value: Any) -> int:
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(_("Enter the duration as a whole number of minutes.")) from None
    if minutes < 1:
        raise ValidationError(_("The duration must be at least one minute."))
    limit = max_minutes()
    if minutes > limit:
        raise ValidationError(
            _("Elevated access lasts at most %(limit)d minutes.") % {"limit": limit}
        )
    return minutes


def clean_permissions(values: Iterable[str]) -> list[str]:
    """``"app_label.codename"`` strings that name real permissions, de-duplicated.

    Unknown ones are an error rather than being dropped: a grant is reviewed by a person
    who reads what it says, and it must say exactly what it confers.
    """
    labels: list[str] = []
    for value in values:
        for part in str(value or "").replace("\n", ",").split(","):
            part = part.strip()
            if part and part not in labels:
                labels.append(part)
    if not labels:
        return []
    wanted = {tuple(label.split(".", 1)) for label in labels if label.count(".") == 1}
    known: set[str] = set()
    if wanted:
        condition = Q()
        for app_label, codename in wanted:
            condition |= Q(content_type__app_label=app_label, codename=codename)
        known = {
            f"{a}.{c}"
            for a, c in Permission.objects.filter(condition).values_list(
                "content_type__app_label", "codename"
            )
        }
    unknown = [label for label in labels if label not in known]
    if unknown:
        raise ValidationError(_("Unknown permission: %(names)s.") % {"names": ", ".join(unknown)})
    return labels


def _covered(actor, role: Group | None, permissions: list[str]) -> bool:
    """Whether ``actor``'s *standing* permissions include everything being granted."""
    from vantage.roles import reach_for

    reach = reach_for(actor)
    if reach.everything:
        return True
    if any(label not in reach.held for label in permissions):
        return False
    if role is not None:
        return reach.covers_all(role.permissions.select_related("content_type"))
    return True


def _check_target(user) -> None:
    if user is None or getattr(user, "pk", None) is None:
        raise ValidationError(_("Choose who the access is for."))
    if not getattr(user, "is_active", False):
        raise ValidationError(_("That account is disabled."))
    if getattr(user, "is_superuser", False):
        raise ValidationError(_("A superuser already holds every permission."))


def _check_what(role: Group | None, permissions: list[str]) -> None:
    if role is None and not permissions:
        raise ValidationError(_("Choose a role or at least one permission to grant."))


# ── who may approve what ────────────────────────────────────────────────────


def may_approve(request, grant) -> bool:
    """Whether this request's user may approve ``grant`` (a pending request)."""
    if grant.granted_at is not None or grant.revoked_at is not None:
        return False
    if not may_grant(request):
        return False
    actor = request.user
    if grant.user_id == actor.pk:
        return self_service()
    return _covered(actor, grant.role, list(grant.permissions or []))


def may_revoke(request, grant) -> bool:
    """Granters may revoke anything; everyone may drop or withdraw their own."""
    if grant.revoked_at is not None:
        return False
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return grant.user_id == user.pk or may_grant(request)


# ── operations ──────────────────────────────────────────────────────────────


def _repr(user) -> str:
    return user.get_username()[:150] if user is not None else ""


def _what(grant) -> dict:
    changes: dict[str, dict[str, str]] = {
        gettext("User"): {"to": grant.user_repr},
        gettext("Reason"): {"to": grant.reason},
        gettext("Minutes"): {"to": str(grant.minutes)},
    }
    if grant.role_repr:
        changes[gettext("Role")] = {"to": grant.role_repr}
    if grant.permissions:
        changes[gettext("Permissions")] = {"to": ", ".join(grant.permissions)}
    return changes


def _activate(grant, actor, *, starts_at: datetime | None = None) -> None:
    now = timezone.now()
    grant.granted_by = actor
    grant.granted_by_repr = _repr(actor)
    grant.granted_at = now
    grant.starts_at = max(starts_at or now, now)
    # Clamped as well as validated: the cap may have been lowered between a request
    # and its approval, and the cap in force when it is granted is the one that counts.
    grant.expires_at = grant.starts_at + timedelta(minutes=min(grant.minutes, max_minutes()))


def request_elevation(
    request,
    *,
    role: Group | None = None,
    permissions: Iterable[str] = (),
    minutes: Any,
    reason: Any,
):
    """The signed-in person asks for elevation for themselves.

    Pending until someone allowed to grant approves it — or active at once, when
    ``BREAK_GLASS_SELF_SERVICE`` is on and the person may grant.
    """
    from vantage.models import ElevatedAccess

    actor = request.user
    labels = clean_permissions(permissions)
    _check_what(role, labels)
    _check_target(actor)
    grant = ElevatedAccess(
        user=actor,
        user_repr=_repr(actor),
        role=role,
        role_repr=role.name[:150] if role is not None else "",
        permissions=labels,
        reason=clean_reason(reason),
        minutes=clean_minutes(minutes),
        requested_by=actor,
        requested_by_repr=_repr(actor),
    )
    activate = self_service() and may_grant(request)
    if activate:
        _activate(grant, actor)
    grant.save()
    _audit(request, grant, "break-glass.requested", _what(grant))
    if activate:
        _audit(request, grant, "break-glass.granted", _what(grant))
        _observe([grant])
    forget(request)
    return grant


def grant(
    request,
    *,
    user,
    role: Group | None = None,
    permissions: Iterable[str] = (),
    minutes: Any,
    reason: Any,
    starts_at: datetime | None = None,
    enforce: bool = True,
):
    """Grant elevation to ``user`` outright. Raises ``PermissionDenied`` or ``ValidationError``.

    ``enforce=False`` skips the *who may grant* checks, for ``manage.py
    vantage_break_glass`` — shell access is already beyond anything the panel can
    restrict. The reason, the target and the duration cap are checked either way.
    """
    from vantage.models import ElevatedAccess

    actor = getattr(request, "user", None) if request is not None else None
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    labels = clean_permissions(permissions)
    _check_what(role, labels)
    _check_target(user)
    if enforce:
        if not may_grant(request):
            raise PermissionDenied(gettext("You may not grant elevated access."))
        if user.pk == actor.pk and not self_service():
            raise PermissionDenied(gettext("Someone else has to approve elevated access for you."))
        if not (user.pk == actor.pk and self_service()) and not _covered(actor, role, labels):
            raise PermissionDenied(gettext("You can only grant permissions you hold yourself."))
    grant_row = ElevatedAccess(
        user=user,
        user_repr=_repr(user),
        role=role,
        role_repr=role.name[:150] if role is not None else "",
        permissions=labels,
        reason=clean_reason(reason),
        minutes=clean_minutes(minutes),
        requested_by=actor,
        requested_by_repr=_repr(actor),
    )
    _activate(grant_row, actor, starts_at=starts_at)
    grant_row.save()
    _audit(request, grant_row, "break-glass.granted", _what(grant_row))
    _observe([grant_row])
    return grant_row


def approve(request, grant_row):
    """Approve a pending request, starting its window now."""
    if not may_approve(request, grant_row):
        raise PermissionDenied(gettext("You may not approve this request."))
    if grant_row.role_id is None and grant_row.role_repr:
        raise ValidationError(_("The role this request names has been deleted."))
    _activate(grant_row, request.user)
    grant_row.save(
        update_fields=["granted_by", "granted_by_repr", "granted_at", "starts_at", "expires_at"]
    )
    _audit(request, grant_row, "break-glass.granted", _what(grant_row))
    _observe([grant_row])
    return grant_row


def revoke(request, grant_row, *, reason: str = "", enforce: bool = True):
    """End a grant now, or turn down a request. The entry says which."""
    if enforce and not may_revoke(request, grant_row):
        raise PermissionDenied(gettext("You may not revoke this grant."))
    if grant_row.revoked_at is not None:
        return grant_row
    actor = getattr(request, "user", None) if request is not None else None
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    now = timezone.now()
    was = grant_row.status
    grant_row.revoked_at = now
    grant_row.revoked_by = actor
    grant_row.revoked_by_repr = _repr(actor)
    grant_row.revoke_reason = " ".join(str(reason or "").split())
    grant_row.save(update_fields=["revoked_at", "revoked_by", "revoked_by_repr", "revoke_reason"])
    own = actor is not None and actor.pk == grant_row.user_id
    if was == "pending":
        name = "break-glass.withdrawn" if own else "break-glass.rejected"
    elif was == "expired":
        name = "break-glass.closed"
    else:
        name = "break-glass.dropped" if own else "break-glass.revoked"
    changes = {gettext("Revoked at"): {"to": _stamp(now)}}
    if grant_row.revoke_reason:
        changes[gettext("Reason")] = {"to": grant_row.revoke_reason}
    _audit(request, grant_row, name, changes)
    if request is not None:
        forget(request)
    return grant_row


# ── the banner ──────────────────────────────────────────────────────────────


def banner(request) -> dict | None:
    """What the always-visible banner says, or ``None`` when not elevated."""
    answer = elevation(request)
    if not answer:
        return None
    now = timezone.now()
    items = []
    for g in sorted(answer.grants, key=lambda g: g.expires_at):
        left = max(0, math.ceil((g.expires_at - now).total_seconds() / 60))
        items.append(
            {
                "pk": g.pk,
                "label": g.role_repr or ", ".join(g.permissions or []),
                "expires_at": g.expires_at,
                "minutes_left": left,
            }
        )
    return {"grants": items, "expires_at": answer.expires_at}
