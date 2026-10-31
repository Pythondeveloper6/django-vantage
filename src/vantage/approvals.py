"""Approval workflows: fields whose changes wait for a second pair of eyes.

One line on a panel turns it on::

    @register(Product)
    class ProductPanel(ModelPanel):
        requires_approval = ["price", "status"]

Anyone may still edit a product. When someone who cannot approve changes ``price``,
every other field they changed is saved as usual and the new price becomes a
:class:`~vantage.models.ChangeRequest`: who asked, when, the value they saw, the value
they want and an optional note. The record keeps its old price until someone holding
``shop.approve_product`` (or a superuser) approves it.

Four decisions shape the module.

**Held back inside the save, not in front of it.** :func:`withhold` runs inside the
transaction every write path already opens — :func:`vantage.formviews.save_form`, the
inline grid, the Kanban move and the importer — after the form has been validated and
applied to the instance, and before the row is written. It puts the guarded fields back
to what the database holds and records the difference. There is no second form and no
second validation: the requested value has already passed the same ``ModelForm`` it
will pass again when it is approved. A write path that forgot to call it would save the
change outright, so the paths that cannot hold a change back one record at a time — the
bulk edit, the publish action — refuse instead. See :func:`refusal`.

**Approving is an ordinary save.** :func:`approve` binds the requested values to the
panel's own form and saves through ``save_form``: validation, ``save_model``, tenancy,
the audit entry and the ``updated`` webhook all happen exactly as for an edit, at the
moment the change is actually made rather than when it was asked for.

**Stale is a refusal, not a merge.** Each request carries the value its author saw.
Approving compares it, inside the save's transaction, with the value the record holds
now; if somebody has changed the field since, the request no longer means what it said,
and the approver is told so instead of the newer value being silently overwritten. They
reject it, and the author asks again from the record as it now is.

**Nobody approves their own request.** Superusers included. A superuser's own edits never
need approval in the first place, so the rule only bites someone who was given the
approve permission after asking — and "I asked, then I said yes" is exactly what this
feature exists to rule out.

**Deciding needs write access to every field asked about.** With
:mod:`vantage.fieldperms`, an approver may be someone a role hides a field from, or lets
only read it. Approving *is* changing the field, so a request that touches one such
field is not theirs to decide: it is not in their queue or their count, and both buttons
are refused. A request is applied whole or not at all — never with the fields this
approver may not write quietly dropped. Where they can see the request at all, a field
hidden from them is shown as "a field you cannot see": not its values, not its name.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from django.db import models, router, transaction
from django.utils import timezone
from django.utils.text import get_text_list
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _

__all__ = [
    "NOTE_FIELD",
    "OUT_OF_REACH",
    "PERMISSION_VERB",
    "AlreadyDecided",
    "ApprovalOutcome",
    "Stale",
    "approvable_fields",
    "approve",
    "ensure_permissions",
    "field_label",
    "guarded_for",
    "is_own",
    "may_approve",
    "out_of_reach",
    "pending_for",
    "permission_name",
    "redact_action_name",
    "refusal",
    "reject",
    "rows",
    "stale_fields",
    "unseen_fields",
    "withdraw",
    "withhold",
]

logger = logging.getLogger("vantage.approvals")

#: The verb in the permission codename: ``shop.approve_product``. Django's own naming
#: (``get_permission_codename``), so it reads like the four permissions next to it in
#: the roles matrix, and a panel can answer for itself with ``has_approve_permission``.
PERMISSION_VERB = "approve"

#: The form field the edit page posts the requester's note in. Underscored so it cannot
#: collide with a model field of the same name.
NOTE_FIELD = "_approval_note"

#: Longest note or rejection reason kept. A note is a sentence, not an attachment.
MAX_NOTE_CHARS = 2000


class Stale(Exception):
    """A field has changed since the request was made; nothing was written."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


class AlreadyDecided(Exception):
    """Somebody else approved, rejected or withdrew it first."""


# ── which fields ────────────────────────────────────────────────────────────


def approvable_fields(panel) -> tuple[str, ...]:
    """``requires_approval``, keeping only names a change can be held back for.

    A held change is a column value put back before the row is written, so it has to be
    a concrete column the form writes. A many-to-many is written after the row and a
    file is stored as it is saved; neither can wait. ``manage.py check`` names every
    entry dropped here (``vantage.E105`` to ``E108``).
    """
    declared = getattr(panel, "requires_approval", None)
    if not declared or isinstance(declared, str):
        return ()
    from vantage.formviews import form_editable_names

    editable = set(form_editable_names(panel))
    kept: list[str] = []
    for name in declared:
        model_field = _concrete(panel.model, name)
        if model_field is None or name not in editable or name in kept:
            continue
        kept.append(name)
    return tuple(kept)


def _concrete(model, name) -> models.Field | None:
    """The model field ``name``, if a held change can be made to it."""
    try:
        model_field = model._meta.get_field(str(name))
    except Exception:
        return None
    if not getattr(model_field, "concrete", False) or model_field.many_to_many:
        return None
    if isinstance(model_field, models.FileField):
        return None
    return model_field


def field_label(model, name: str) -> str:
    try:
        return str(model._meta.get_field(name).verbose_name).capitalize()
    except Exception:
        return name


def _labels(model, names) -> str:
    """``"Price and Status"``, translated."""
    return str(get_text_list([field_label(model, name) for name in names], _("and")))


# ── who may approve ─────────────────────────────────────────────────────────


def permission_name(panel) -> str:
    """``"shop.approve_product"``."""
    from vantage.permissions import get_permissions

    return get_permissions().perm_name(panel.model, PERMISSION_VERB)


def may_approve(request, panel, obj=None) -> bool:
    """Whether this user's changes to the panel's guarded fields apply directly.

    The approve permission *and* change on the record: approving writes it. Read-only
    mode refuses this as it refuses every other write. A panel can answer for itself
    with ``has_approve_permission(request, obj=None)``, consulted the way
    :meth:`~vantage.permissions.PanelPermissions.panel_override` consults the others.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    from vantage.permissions import get_permissions
    from vantage.safety import read_only_reason

    if read_only_reason() is not None:
        return False
    permissions = get_permissions()
    if obj is None:
        return permissions.has_model_perm(
            request, panel.model, PERMISSION_VERB
        ) and permissions.has_model_perm(request, panel.model, "change")
    return permissions.has_object_perm(
        request, panel.model, PERMISSION_VERB, obj
    ) and permissions.has_object_perm(request, panel.model, "change", obj)


def guarded_for(request, panel, obj=None) -> tuple[str, ...]:
    """The fields whose changes this user cannot make directly. Empty when none.

    Empty without a signed-in user, too: a write with no person behind it — a
    management command calling the importer — has nobody to attribute a request to and
    nobody to tell, and it is code the project wrote, not somebody using the panel.
    """
    names = approvable_fields(panel)
    if not names:
        return ()
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ()
    if may_approve(request, panel, obj):
        return ()
    return names


def out_of_reach(request, panel, change_request) -> list[str]:
    """The fields in ``change_request`` this person may not write: hidden, or read-only.

    Not empty means they cannot decide it. See the module docstring: approving writes
    the field, so it needs the access an edit would.
    """
    from vantage.fieldperms import access

    locked = access(request, panel).locked
    return [name for name in change_request.field_names if name in locked]


def refusal(request, panel, names) -> str | None:
    """Why a bulk write that sets ``names`` is refused, or ``None`` if it may run.

    For the paths that write many rows in one go. Holding one change back is one
    request; holding back forty thousand would bury the queue the feature depends on,
    and a single ``UPDATE`` cannot be held back at all. So they refuse, by name, rather
    than either flooding the queue or writing what nobody approved.
    """
    blocked = [name for name in guarded_for(request, panel) if name in set(names)]
    if not blocked:
        return None
    return gettext(
        "%(fields)s cannot be changed in bulk: a change to it waits for approval, which "
        "is asked for one record at a time. Change it from each record's own page, or "
        "ask someone who can approve it."
    ) % {"fields": _labels(panel.model, blocked)}


def ensure_permissions(sender=None, using: str = "default", site=None, **kwargs) -> None:
    """Create ``<app>.approve_<model>`` for every panel that declares ``requires_approval``.

    After ``migrate``, like :func:`vantage.sensitive.ensure_reveal_permission`, so a
    project's own models need no ``Meta.permissions`` and no migration of their own.
    On the model's own content type (the proxy's, for a proxy), so it sits in the
    model's row of the roles matrix beside view, add, change and delete.
    """
    from django.contrib.auth import get_permission_codename
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    if site is None:
        from vantage.sites import all_sites
        from vantage.sites import site as default_site

        sites = [default_site, *[s for s in all_sites if s is not default_site]]
    else:
        sites = [site]
    seen: set[Any] = set()
    try:
        for current in sites:
            for panel in current.panels():
                if panel.model in seen or not approvable_fields(panel):
                    continue
                seen.add(panel.model)
                opts = panel.model._meta
                content_type = ContentType.objects.db_manager(using).get_for_model(
                    panel.model, for_concrete_model=False
                )
                Permission.objects.db_manager(using).get_or_create(
                    codename=get_permission_codename(PERMISSION_VERB, opts),
                    content_type=content_type,
                    defaults={"name": f"Can approve changes to {opts.verbose_name_raw}"},
                )
    except Exception:  # pragma: no cover - a database without the auth tables
        logger.exception("Could not create the approve permissions.")


# ── holding a change back ───────────────────────────────────────────────────


def serial(model_field, value) -> Any:
    """A field value as JSON, the way ``Field.value_to_string`` writes it.

    ``None`` stays ``None``: ``value_to_string`` turns it into the string ``"None"`` for
    most field types, which a form would then try to parse as a number.
    """
    if value is None:
        return None
    holder = model_field.model.__new__(model_field.model)
    holder.__dict__[model_field.attname] = value
    text = model_field.value_to_string(holder)
    if text is None or isinstance(text, str | int | float | bool | list | dict):
        return text
    return str(text)


def _stored(model, pk, names: list[str], using=None) -> dict[str, Any]:
    """The guarded columns as the database holds them now, by field name."""
    attnames = {name: model._meta.get_field(name).attname for name in names}
    manager = model._base_manager.db_manager(using) if using else model._base_manager
    row = manager.filter(pk=pk).values(*attnames.values()).first() or {}
    return {name: row.get(attname) for name, attname in attnames.items()}


def _display(panel, obj, name: str) -> str | None:
    """What a person reads for ``obj.<name>``. ``None`` for a sensitive field."""
    if name in panel.get_sensitive_fields():
        return None
    model_field = obj._meta.get_field(name)
    if model_field.choices:
        value = getattr(obj, model_field.attname)
        return str(dict(model_field.flatchoices).get(value, "" if value is None else value))
    value = getattr(obj, name, None)
    if isinstance(value, bool):
        return gettext("Yes") if value else gettext("No")
    from vantage.audit import _jsonify

    shown = _jsonify(value)
    if shown is None:
        return ""
    return shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False)


def withhold(request, panel, obj, form):
    """Put guarded fields back and record what was asked for. Inside the save's transaction.

    Called by every per-record write path after ``form.save(commit=False)`` and before
    the row is written. Returns the new :class:`~vantage.models.ChangeRequest`, or
    ``None`` when nothing needed approval — a create, a user who may approve, or an edit
    that left the guarded fields alone.

    When ``form.vantage_approving`` names a request, this is that request being applied:
    the stale check and the claim run here instead, so both are inside the transaction
    that writes the values.

    The withheld names are stamped on the form as ``vantage_withheld``, which
    :func:`vantage.audit.diff_from_form` reads so the edit's own audit entry — and the
    ``updated`` webhook it fires — does not report a change that has not happened.
    """
    approving = getattr(form, "vantage_approving", None)
    if approving is not None:
        _claim(request, panel, obj, approving)
        return None
    if obj.pk is None or obj._state.adding:
        # A new record has no approved value to protect. The add permission is the
        # gate for creating one; see docs/approvals.md.
        return None
    names = [name for name in guarded_for(request, panel, obj) if name in form.fields]
    if not names:
        return None

    using = router.db_for_write(panel.model, instance=obj)
    before = _stored(panel.model, obj.pk, names, using)
    changes: dict[str, dict[str, Any]] = {}
    for name in names:
        model_field = panel.model._meta.get_field(name)
        old = serial(model_field, before[name])
        new = serial(model_field, getattr(obj, model_field.attname))
        if old == new:
            continue
        to_display = _display(panel, obj, name)
        # Back to what the database holds. For a foreign key, setting the id also drops
        # the cached related object, so nothing downstream reads the requested one.
        setattr(obj, model_field.attname, before[name])
        changes[name] = {
            "from": old,
            "to": new,
            "from_display": _display(panel, obj, name),
            "to_display": to_display,
        }
    if not changes:
        return None

    from vantage.models import ChangeRequest
    from vantage.safety import real_user
    from vantage.sensitive import safe_str

    actor = real_user(request)
    change_request = ChangeRequest.objects.db_manager(using).create(
        resource_key=panel.model._meta.label_lower,
        object_id=str(obj.pk),
        object_repr=safe_str(obj)[:200],
        changes=changes,
        note=str(getattr(form, "vantage_approval_note", "") or "").strip()[:MAX_NOTE_CHARS],
        requested_by=actor,
        requested_by_repr=actor.get_username()[:150],
    )
    form.vantage_withheld = frozenset(changes)
    form.vantage_pending = change_request
    return change_request


def announce(request, obj, change_request) -> None:
    """The audit entry for a new request. Outside the transaction, like every entry.

    An ``action`` entry with no diff. The values live on the request; putting them in
    the entry as a ``from``/``to`` diff would have the record's history replay a change
    that was never made.
    """
    if change_request is None:
        return
    from vantage import audit

    audit.record(
        request,
        "action",
        obj=obj,
        action_name=_action_name("requested approval of", change_request),
        changes={},
    )
    _notify(request, type(obj), change_request, "approval.requested")


#: The audit verb of a decision, mapped to its notification event.
_DECISION_EVENTS = {
    "approved change to": "approval.approved",
    "rejected change to": "approval.rejected",
    "withdrew request to change": "approval.withdrawn",
}


def _notify(request, model, change_request, event: str) -> None:
    """Tell the channels that asked (``VANTAGE["NOTIFICATIONS"]``). Never raises.

    Names the record by its stored safe label and the change by a field *count*: the
    people reading a chat channel may not be allowed to know which fields a record has,
    let alone their values.
    """
    from django.urls import NoReverseMatch, reverse

    from vantage.notifications import notify

    if model is None:
        return
    count = len(change_request.field_names)
    title = {
        "approval.requested": _("Approval requested"),
        "approval.approved": _("Change approved"),
        "approval.rejected": _("Change rejected"),
        "approval.withdrawn": _("Approval request withdrawn"),
    }[event]
    link = None
    try:
        path = reverse(
            f"vantage:{model._meta.app_label}-{model._meta.model_name}-detail",
            args=[change_request.object_id],
        )
        link = request.build_absolute_uri(path) if request is not None else path
    except (NoReverseMatch, Exception):
        link = None
    notify(
        event,
        title=f"{title}: {str(model._meta.verbose_name).capitalize()} "
        f"{change_request.object_repr or change_request.object_id} (#{change_request.pk})",
        lines=[str(_("%(count)s field(s) affected.") % {"count": count})],
        link=link,
        model=model,
    )


def _action_name(verb: str, change_request) -> str:
    """``"requested approval of price, status (#12)"``. Names, not labels: it is a log."""
    return f"{verb} {', '.join(change_request.field_names)} (#{change_request.pk})"


#: The verbs :func:`_action_name` is called with, for :func:`redact_action_name`.
_LOGGED_VERBS = (
    "requested approval of",
    "approved change to",
    "rejected change to",
    "withdrew request to change",
)


def redact_action_name(name: str, hidden) -> str:
    """An audit entry's ``action_name`` with the field names in ``hidden`` taken out.

    The entries above name the fields of a request, and the log is read by people some
    of those fields are hidden from; :func:`vantage.fieldperms.hide_entry_fields` calls
    this for them. Anything that is not one of these entries comes back unchanged.
    """
    if not name or not hidden:
        return name
    for verb in _LOGGED_VERBS:
        prefix = f"{verb} "
        if not name.startswith(prefix) or not name.endswith(")"):
            continue
        listed, sep, tail = name.removeprefix(prefix).rpartition(" (#")
        if not sep:
            return name
        names = listed.split(", ")
        kept = [field_name for field_name in names if field_name not in hidden]
        unseen = len(names) - len(kept)
        if not unseen:
            return name
        # Plain English like the rest of the entry: it is a log line, stored untranslated.
        kept.append("1 hidden field" if unseen == 1 else f"{unseen} hidden fields")
        return f"{verb} {', '.join(kept)} (#{tail}"
    return name


def pending_message(panel, change_request) -> str:
    """What the person who made the change is told."""
    return gettext(
        "Your change to %(fields)s was sent for approval. It is not saved until someone "
        "who can approve it does."
    ) % {"fields": _labels(panel.model, change_request.field_names)}


# ── applying one ────────────────────────────────────────────────────────────


def stale_fields(model, change_request, using=None) -> list[str]:
    """The requested fields whose value has moved since the request was made."""
    names = [name for name in change_request.field_names if _concrete(model, name) is not None]
    current = _stored(model, change_request.object_id, names, using)
    stale = []
    for name in names:
        model_field = model._meta.get_field(name)
        if serial(model_field, current[name]) != change_request.changes[name].get("from"):
            stale.append(name)
    return stale


def _claim(request, panel, obj, change_request) -> None:
    """Inside the approving save: refuse a stale request, then mark it approved.

    The status moves with a conditional ``UPDATE``, so two approvers pressing at once
    produce one write and one :class:`AlreadyDecided`, never two saves.
    """
    from vantage.models import ChangeRequest
    from vantage.safety import real_user

    using = router.db_for_write(panel.model, instance=obj)
    stale = stale_fields(panel.model, change_request, using)
    if stale:
        raise Stale(stale)
    actor = real_user(request)
    claimed = (
        ChangeRequest.objects.db_manager(using)
        .filter(pk=change_request.pk, status=ChangeRequest.Status.PENDING)
        .update(
            status=ChangeRequest.Status.APPROVED,
            decided_by=actor,
            decided_by_repr=actor.get_username()[:150],
            decided_at=timezone.now(),
        )
    )
    if not claimed:
        raise AlreadyDecided()


#: Why an approver cannot decide a request. Names no field: one of them may be hidden.
OUT_OF_REACH = _(
    "This request changes a field you cannot change, so you cannot approve or reject "
    "it. Someone who can change every field in it has to decide it."
)


@dataclass
class ApprovalOutcome:
    """What pressing Approve did."""

    ok: bool
    message: str
    #: Set when the record moved since the request: the fields that did.
    stale: list[str] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)


def is_own(request, change_request) -> bool:
    from vantage.safety import real_user

    mine = {getattr(request.user, "pk", None), getattr(real_user(request), "pk", None)}
    mine.discard(None)
    return change_request.requested_by_id is not None and change_request.requested_by_id in mine


def may_decide(request, panel, obj, change_request) -> bool:
    """Approve or reject: the approve permission, on this record, not your own, and
    write access to every field it asks about (see :func:`out_of_reach`)."""
    return (
        change_request.is_pending
        and obj is not None
        and may_approve(request, panel, obj)
        and not is_own(request, change_request)
        and not out_of_reach(request, panel, change_request)
    )


def may_withdraw(request, change_request) -> bool:
    from vantage.safety import read_only_reason

    return (
        change_request.is_pending and read_only_reason() is None and is_own(request, change_request)
    )


def approve(request, panel, obj, change_request) -> ApprovalOutcome:
    """Apply a pending request through the panel's own form and ``save_form``."""
    from vantage.formviews import save_form
    from vantage.inline import form_errors, json_to_form_data
    from vantage.widgets import build_form_class

    if out_of_reach(request, panel, change_request):
        # Refused whole. The form below is built from the request's names, not from
        # what this approver may write, so nothing would be dropped — but applying a
        # change to a field they may not change is exactly what their role rules out.
        return ApprovalOutcome(False, str(OUT_OF_REACH))
    names = [name for name in change_request.field_names if _concrete(panel.model, name)]
    if not names:
        return ApprovalOutcome(False, gettext("None of these fields can be changed any more."))
    form_class = build_form_class(panel, names)
    values = {name: change_request.changes[name].get("to") for name in names}
    form = form_class(data=json_to_form_data(form_class, values), instance=obj)
    if not form.is_valid():
        # A choice removed, a unique value taken since: the request cannot be applied
        # as made. Nothing is written and the request stays pending, to be rejected.
        return ApprovalOutcome(
            False,
            gettext("The requested values are no longer valid, so nothing was saved."),
            errors=form_errors(form),
        )
    form.vantage_approving = change_request
    try:
        save_form(
            request,
            panel,
            form,
            change=True,
            action_name=_action_name("approved change to", change_request),
        )
    except Stale as exc:
        return ApprovalOutcome(
            False,
            gettext(
                "%(fields)s has changed since this was requested, so it was not applied. "
                "Compare the values again: reject it, and ask for a new request if the "
                "change is still wanted."
            )
            % {"fields": _labels(panel.model, exc.names)},
            stale=exc.names,
        )
    except AlreadyDecided:
        return ApprovalOutcome(False, gettext("Someone else has already decided this request."))
    change_request.refresh_from_db()
    _notify(request, panel.model, change_request, "approval.approved")
    return ApprovalOutcome(
        True,
        gettext("Approved. %(fields)s is now saved.") % {"fields": _labels(panel.model, names)},
    )


def _close(request, change_request, status: str, *, reason: str = "") -> bool:
    """Reject or withdraw: one conditional ``UPDATE``, so a race has one winner."""
    from vantage.models import ChangeRequest
    from vantage.safety import real_user

    actor = real_user(request)
    with transaction.atomic(using=router.db_for_write(ChangeRequest)):
        closed = ChangeRequest.objects.filter(
            pk=change_request.pk, status=ChangeRequest.Status.PENDING
        ).update(
            status=status,
            decided_by=actor,
            decided_by_repr=actor.get_username()[:150],
            decided_at=timezone.now(),
            reason=reason[:MAX_NOTE_CHARS],
        )
    if closed:
        change_request.refresh_from_db()
    return bool(closed)


def reject(request, obj, change_request, reason: str) -> bool:
    """Close a request without applying it. The reason is required by the caller."""
    closed = _close(request, change_request, "rejected", reason=reason.strip())
    if closed:
        _log(request, obj, change_request, "rejected change to")
    return closed


def withdraw(request, obj, change_request) -> bool:
    """The requester takes their own request back."""
    closed = _close(request, change_request, "withdrawn")
    if closed:
        _log(request, obj, change_request, "withdrew request to change")
    return closed


def _log(request, obj, change_request, verb: str) -> None:
    from vantage import audit

    event = _DECISION_EVENTS.get(verb)
    if event is not None:
        model = type(obj) if obj is not None else _model_for(change_request.resource_key)
        _notify(request, model, change_request, event)
    target = obj
    if target is None:
        # The record has gone; the entry still names the model and the id it was about.
        model = _model_for(change_request.resource_key)
        audit.record(
            request,
            "action",
            model=model,
            action_name=_action_name(verb, change_request),
            object_repr=change_request.object_repr,
            changes={},
        )
        return
    audit.record(
        request, "action", obj=target, action_name=_action_name(verb, change_request), changes={}
    )


# ── reading them back ───────────────────────────────────────────────────────


def _model_for(resource_key: str):
    from django.apps import apps as django_apps

    try:
        return django_apps.get_model(resource_key)
    except (LookupError, ValueError):
        return None


def pending_for(request, panel, obj) -> list:
    """The pending requests on one record, oldest first, for the banner."""
    if obj is None or obj.pk is None:
        return []
    from vantage.models import ChangeRequest

    return list(
        ChangeRequest.objects.filter(
            resource_key=panel.model._meta.label_lower,
            object_id=str(obj.pk),
            status=ChangeRequest.Status.PENDING,
        ).order_by("requested_at", "pk")
    )


def unseen_fields(request, panel, change_request) -> list[str]:
    """The fields in ``change_request`` hidden from this person. See :func:`rows`."""
    if request is None:
        return []
    from vantage.fieldperms import hidden_names

    hidden = hidden_names(request, panel)
    return [name for name in change_request.field_names if name in hidden]


def rows(panel, change_request, obj=None, request=None) -> list[dict[str, Any]]:
    """One row per field for a template: label, what it is now, what was asked, masked.

    A sensitive field is masked on every side, always. Approving it is still possible —
    an approver who may reveal values does so on the record itself, where the reveal
    is logged — but a queue is read by more people than the records in it.

    With a ``request``, fields hidden from that person (:mod:`vantage.fieldperms`) are
    not rows at all: one closing row says how many there are, with no name and no
    value, so the reader knows the request is more than what they see.
    """
    from vantage.sensitive import MASK

    secret = panel.get_sensitive_fields()
    unseen = set(unseen_fields(request, panel, change_request))
    stale = set(stale_fields(panel.model, change_request)) if obj is not None else set()
    out = []
    for name in change_request.field_names:
        if name in unseen:
            continue
        change = change_request.changes.get(name) or {}
        masked = name in secret or change.get("to_display") is None
        current = None
        if obj is not None and not masked and _concrete(panel.model, name) is not None:
            current = _display(panel, obj, name)
        out.append(
            {
                "name": name,
                "label": field_label(panel.model, name),
                "masked": masked,
                "from": MASK if masked else change.get("from_display", ""),
                "to": MASK if masked else change.get("to_display", ""),
                "now": MASK if masked else current,
                "stale": name in stale,
            }
        )
    if unseen:
        out.append(
            {
                "name": "",
                "label": ngettext(
                    "%(count)s field you cannot see",
                    "%(count)s fields you cannot see",
                    len(unseen),
                )
                % {"count": len(unseen)},
                "hidden": True,
                "masked": True,
                "from": "",
                "to": "",
                "now": None,
                "stale": False,
            }
        )
    return out
