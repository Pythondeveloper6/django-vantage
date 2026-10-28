"""Recording what someone did in the panel.

The scope is the whole design: **what a person did through this admin**, not what
happened to a row. That is a much smaller thing than row history, and the difference
is why ``django-simple-history`` is soft-detected rather than required. It creates a
history model *in the user's own app*, which means a migration in their code and
roughly double the write volume on every table it touches. Making that the price of
installing an admin panel is not a trade a panel gets to make for its user. When they
have chosen it, :func:`history_timeline` renders its timeline next to ours.

Three rules follow from that scope, and each of them rules something out.

**Never a ``post_save`` signal.** A receiver cannot see who was logged in — the actor
is the single most valuable column in this table and a signal simply does not have it.
It would also faithfully log the nightly import job and every ``shell`` session, which
is the row history we just declined to build, minus the actor, at the same cost.
Entries are written from the two places a person's intent is actually known: the form
views and the bulk-action runner.

**The diff never carries a secret and never carries a file.** A sensitive field — the
same answer :mod:`vantage.sensitive` gives every other surface, including a panel's
own ``sensitive_fields`` — is recorded as having changed and never with its value, and
a file field records the *name* it was given, never a byte of its contents. An audit
log is read by more people than the records it describes. The rule is applied twice:
when an entry is written, and again when one is displayed, so an entry written before a
field was declared sensitive does not keep printing it.

**Logging must never break the write it describes.** Every entry is written through
:func:`record`, which catches everything, logs it and returns ``None``. It is also
called *outside* the transaction that performed the write: catching a database error
inside an atomic block still marks that block for rollback, so a swallowed exception
in the wrong place would destroy the very write it was trying to record.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db import models

from vantage.conf import vantage_settings
from vantage.models import VantageLogEntry

__all__ = [
    "HISTORY_LIMIT",
    "diff_from_form",
    "history_context",
    "history_timeline",
    "log_action",
    "log_delete",
    "log_form_save",
    "may_view_audit",
    "record",
    "snapshot",
]

logger = logging.getLogger("vantage.audit")

#: How many entries the per-record panel shows before it links to the full log. A
#: detail page must not become a query over ten thousand rows because a record is old.
HISTORY_LIMIT = 20

#: Longest value the diff will keep. A 40 KB body pasted into a text field is not
#: evidence of anything, and a hundred of them make the audit page unopenable.
MAX_VALUE_CHARS = 500


# ── writing ─────────────────────────────────────────────────────────────────


def record(
    request,
    action: str,
    *,
    obj=None,
    model: type[models.Model] | None = None,
    changes: dict | None = None,
    action_name: str = "",
    object_repr: str | None = None,
) -> VantageLogEntry | None:
    """Write one entry. Returns it, or ``None`` if nothing was written.

    Never raises. A panel whose edit form 500s because the audit table is missing a
    column is worse than a panel with a gap in its audit trail, and the gap is the
    thing the operator can actually see and act on. The failure is logged at
    ``exception`` level so it is loud in the place where loudness is free.
    """
    entry = None
    try:
        if vantage_settings.AUDIT_LOG:
            entry = VantageLogEntry.objects.create(
                **_entry_kwargs(
                    request,
                    action,
                    obj=obj,
                    model=model,
                    changes=changes,
                    action_name=action_name,
                    object_repr=object_repr,
                )
            )
    except Exception:  # deliberately broad: see the docstring
        logger.exception("Could not write a Vantage audit entry for %r.", action)
    # The same call sites, the same moment. Webhooks fire whether or not the log is on,
    # and a failure in one is no reason to lose the other; `notify` never raises.
    from vantage import webhooks

    webhooks.notify(request, action, obj=obj, changes=changes)
    return entry


def _entry_kwargs(
    request,
    action: str,
    *,
    obj,
    model,
    changes,
    action_name: str,
    object_repr: str | None,
) -> dict[str, Any]:
    from vantage.safety import real_user

    # The person signed in, not the person being impersonated. Impersonation exists so
    # a superuser can see what someone else sees; letting it also rewrite the audit
    # trail would make it a way to act as someone else without a trace, which is the
    # exact failure the feature is narrowed to avoid.
    actor = real_user(request) if request is not None else None
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None

    target = model if model is not None else (type(obj) if obj is not None else None)
    return {
        "actor": actor,
        "actor_repr": _clip(actor.get_username() if actor else "", 150),
        "action": action,
        "action_name": _clip(action_name, 100),
        "content_type": _content_type(target),
        "object_id": "" if obj is None or obj.pk is None else _clip(str(obj.pk), 255),
        "object_repr": _clip(
            object_repr if object_repr is not None else ("" if obj is None else _repr(obj)), 200
        ),
        "changes": changes or {},
        "ip_address": _client_ip(request),
        "user_agent": _clip(_meta(request).get("HTTP_USER_AGENT", ""), 400),
    }


def _content_type(model) -> ContentType | None:
    if model is None:
        return None
    # Concrete, so a proxy model registered as a second panel does not fork one
    # record's history into two timelines that neither page shows in full.
    return ContentType.objects.get_for_model(model)


def log_form_save(
    request, obj, form, change: bool, *, action_name: str = ""
) -> VantageLogEntry | None:
    """The one call the create and edit views make. ``change`` is what they already know.

    An edit whose form changed nothing writes no entry: a log full of "opened the form
    and pressed Save" is a log nobody reads, and the entries that matter drown in it.

    ``action_name`` says where an edit came from when it was not typed — a restore from
    the record's history is an ordinary update with the same diff, named so the log
    can tell the two apart.
    """
    changes = diff_from_form(form)
    if change and not changes:
        return None
    action = VantageLogEntry.Action.UPDATE if change else VantageLogEntry.Action.CREATE
    return record(request, action, obj=obj, changes=changes, action_name=action_name)


def log_delete(request, obj, *, soft: bool = False) -> VantageLogEntry | None:
    """Called with the row still present, because a deleted row cannot be read.

    Django blanks ``obj.pk`` as part of the delete, so an entry written afterwards
    would have neither an object id nor a way to snapshot the fields — and a delete
    entry that cannot say *what* was deleted is the least useful row in the table.

    ``soft`` records the recoverable case as its own action. The snapshot is taken
    either way: a trashed row can still be read back, but the fields at the moment it
    was trashed are what someone restoring it will want to compare against.
    """
    action = VantageLogEntry.Action.TRASH if soft else VantageLogEntry.Action.DELETE
    return record(request, action, obj=obj, changes=snapshot(obj))


def log_restore(request, obj) -> VantageLogEntry | None:
    """A row brought back out of the trash."""
    return record(request, VantageLogEntry.Action.RESTORE, obj=obj, changes=snapshot(obj))


def log_action(request, model, action, count: int) -> VantageLogEntry | None:
    """One entry per bulk-action run, not one per row.

    Forty thousand entries for one click is a denial of service on your own audit
    page. The scope actually resolved is recorded instead — which is also the number
    the user was shown, so the log and the message they saw cannot disagree.
    """
    noun = "record" if count == 1 else "records"
    return record(
        request,
        VantageLogEntry.Action.ACTION,
        model=model,
        action_name=getattr(action, "name", str(action)),
        object_repr=f"{count} {noun}",
        changes={},
    )


# ── diffing ─────────────────────────────────────────────────────────────────


def diff_from_form(form) -> dict[str, dict[str, Any]]:
    """``{"status": {"from": "draft", "to": "paid"}}`` from a bound, valid form.

    Read off the form rather than by re-querying the row. ``ModelForm.initial`` holds
    the values as they were when the form was built, so the before-state costs no
    query and — more importantly — is the state the user was actually looking at when
    they decided to change it. ``changed_data`` is Django's own answer to "what
    moved", so the diff cannot disagree with what was saved.
    """
    sensitive = _form_sensitive(form)
    changes: dict[str, dict[str, Any]] = {}
    for name in getattr(form, "changed_data", ()):
        if name in sensitive:
            # Recorded as having changed, never with its value. "Someone rotated the
            # API token at 14:02" is the useful half; the token is the dangerous half.
            changes[name] = {"redacted": True}
            continue
        before = _jsonify(form.initial.get(name))
        after = _jsonify(form.cleaned_data.get(name))
        if before == after:
            # A widget can report a change the model does not see — an empty string
            # against None is the usual one. Recording it would fill the log with
            # edits that changed nothing.
            continue
        changes[name] = {"from": before, "to": after}
    return changes


def snapshot(obj) -> dict[str, dict[str, Any]]:
    """Every concrete field of ``obj`` as ``{"from": value}``, for a delete.

    A delete has no "to". Keeping the "from" side in the same shape as an update's
    diff means one template renders both, and means a deleted record can still be
    read a year later — which is the only reason anyone opens an audit log about a
    row that no longer exists.
    """
    from vantage.introspection import concrete_fields
    from vantage.sensitive import sensitive_names

    sensitive = sensitive_names(obj)
    values: dict[str, dict[str, Any]] = {}
    for field in concrete_fields(type(obj)):
        if field.name in sensitive:
            continue
        if isinstance(field, models.FileField):
            # The name, never the bytes: an audit row is not a place to copy a file to.
            stored = getattr(obj, field.name, None)
            values[field.name] = {"from": getattr(stored, "name", None) or None}
            continue
        values[field.name] = {"from": _jsonify(getattr(obj, field.attname, None))}
    return values


def _form_sensitive(form) -> frozenset[str]:
    """The sensitive names for a form: what build_form_class stamped on it, else inferred.

    A form built by :func:`vantage.widgets.build_form_class` carries its panel's answer.
    Anything else — a test's hand-built form, a third-party one — falls back to the
    model's answer, and then to the name vocabulary for fields the model does not have.
    """
    from vantage.sensitive import name_is_sensitive, sensitive_names

    stamped = getattr(form, "vantage_sensitive_fields", None)
    if stamped is not None:
        return frozenset(stamped)
    model = getattr(getattr(form, "_meta", None), "model", None)
    names = set(sensitive_names(model)) if model is not None else set()
    names.update(name for name in getattr(form, "fields", {}) if name_is_sensitive(name))
    return frozenset(names)


def _repr(obj) -> str:
    from vantage.sensitive import safe_str

    return safe_str(obj)


def _jsonify(value: Any) -> Any:
    """A JSON-storable stand-in for any field value.

    Deliberately lossy. The log is read by humans, and ``"Paid"`` is worth more to
    them than a foreign key's integer — while a file is reduced to its name because
    the alternative is storing the file twice.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return _clip(value, MAX_VALUE_CHARS) if isinstance(value, str) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, models.Model):
        return _clip(_repr(value), MAX_VALUE_CHARS)
    if hasattr(value, "name") and hasattr(value, "url"):  # FieldFile / UploadedFile
        return _clip(str(getattr(value, "name", "")), MAX_VALUE_CHARS)
    if hasattr(value, "all"):  # a manager or queryset, from an m2m field
        return sorted(_clip(str(item), MAX_VALUE_CHARS) for item in value.all())
    if isinstance(value, list | tuple | set | frozenset):
        return sorted((_jsonify(item) for item in value), key=repr)
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    return _clip(str(value), MAX_VALUE_CHARS)


# ── reading ─────────────────────────────────────────────────────────────────


def entries_for(obj, *, limit: int = HISTORY_LIMIT):
    """This record's entries, newest first."""
    if obj is None or obj.pk is None:
        return VantageLogEntry.objects.none()
    return (
        VantageLogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(type(obj)), object_id=str(obj.pk)
        )
        .select_related("actor")
        .order_by("-created_at", "-pk")[:limit]
    )


def history_timeline(obj) -> list[dict[str, Any]] | None:
    """django-simple-history's own timeline for ``obj``, or ``None`` when it is absent.

    Soft-detected with ``apps.is_installed`` rather than ``try: import``, because an
    importable package that is not in ``INSTALLED_APPS`` has no tables and no
    registered models — the import would succeed and the query would then fail. Having
    installed it is the user's statement that they want row history; this renders it
    beside ours rather than instead of it, because the two answer different questions:
    theirs is what the row looked like, ours is who touched it and from where.
    """
    if not django_apps.is_installed("simple_history"):
        return None
    manager = getattr(type(obj), "history", None) if obj is not None else None
    if manager is None or obj is None or obj.pk is None:
        return None
    try:
        records = list(obj.history.all()[:HISTORY_LIMIT])
    except Exception:  # a third-party timeline must never 500 a detail page
        logger.exception("Could not read the django-simple-history timeline for %r.", obj)
        return None
    return [
        {
            "date": getattr(item, "history_date", None),
            "type": item.get_history_type_display()
            if hasattr(item, "get_history_type_display")
            else "",
            "user": getattr(item, "history_user", None),
            "reason": getattr(item, "history_change_reason", "") or "",
        }
        for item in records
    ]


def history_context(request, obj) -> dict[str, Any]:
    """Context for ``vantage/partials/history.html``. One line from a detail view."""
    if not vantage_settings.AUDIT_LOG or not may_view_audit(request):
        # Not merely hidden: "who edited this record" is itself information about
        # colleagues, and a panel that leaks it to everyone will be asked not to.
        return {"history": None}
    return {
        "history": list(entries_for(obj)),
        "history_timeline": history_timeline(obj),
        "history_limit": HISTORY_LIMIT,
        # Resolved here rather than with ``{% url %}``: the audit route may not be
        # mounted, and a NoReverseMatch raised inside a template is a 500 on a detail
        # page that had nothing to do with the audit log.
        "audit_url": _audit_url(),
    }


def _audit_url() -> str | None:
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse("vantage:audit")
    except NoReverseMatch:
        return None


def may_view_audit(request) -> bool:
    """Superusers, or anyone holding ``vantage.view_vantagelogentry``.

    Superuser-only by default because the log spans every model in the panel: someone
    who may not see orders can still learn a great deal from a list of who has been
    editing them. Widening it is a deliberate grant of a real Django permission rather
    than a setting, so it shows up wherever permissions are already reviewed.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return bool(user.is_superuser) or user.has_perm("vantage.view_vantagelogentry")


# ── helpers ─────────────────────────────────────────────────────────────────


def _meta(request) -> dict:
    """``request.META``, tolerant of the bare objects tests and commands pass in."""
    return getattr(request, "META", None) or {}


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _client_ip(request) -> str | None:
    """``REMOTE_ADDR`` only, never ``X-Forwarded-For``.

    A forwarded header is client-supplied unless a proxy the deployment controls
    overwrites it, and this layer cannot know whether one does. An audit column that
    is trivially spoofable is worse than an empty one, because it will be quoted in an
    investigation as though it meant something. A project behind a trusted proxy
    should normalise ``REMOTE_ADDR`` in middleware, where the trust boundary is known.
    """
    raw = _meta(request).get("REMOTE_ADDR") or ""
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None
