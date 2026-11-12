"""History for changes made *outside* the panel. Opt-in, off by default.

The audit log answers "what did a person do through this admin". That is a deliberate
scope (see :mod:`vantage.audit`), and it leaves a gap: a ``shell`` session, a script, a
signal handler or the project's own views change rows the log never hears about. This
module closes that gap for the models a project names in
``VANTAGE["TRACK_EXTERNAL_CHANGES"]`` and for nothing else.

How it stays honest and cheap:

* **Receivers are connected once** (``dispatch_uid``-ed, from ``AppConfig.ready``) and
  return after one settings read and one set lookup when the sender is not tracked: no
  query, no allocation worth the name.
* **A panel write is never recorded twice.** The panel records its own writes with the
  actor and the form diff. Every route of the panel is wrapped by :func:`guard_patterns`,
  and a background job by :func:`panel_scope`, which set a context variable for the
  duration of the call; a signal received while it is set is the panel's business and is
  ignored here. The cost is a stated limit: a side effect the panel triggers on a
  *different* model (a signal handler that saves a second row) is not seen either.
* **It never breaks the caller's save.** Everything runs inside ``try/except`` and the
  entry is written in its own savepoint, so a database error here rolls back only the
  savepoint, not the host application's transaction.
* **Nothing it cannot vouch for is recorded.** Sensitive fields are noted as changed and
  never valued, files by name only, raw (fixture) loads and historical migration models
  are skipped, and the package's own tables are never tracked (no recursion).

What it cannot see is whatever Django does not announce: ``QuerySet.update()``,
``bulk_create()``, ``bulk_update()``, ``delete()`` on a queryset that is fast-deleted and
raw SQL send no ``post_save``, so they leave no entry and a later point-in-time view will
report the chain as broken rather than guess.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from django.apps import apps as django_apps
from django.conf import settings as django_settings
from django.db import models, router, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

__all__ = [
    "OUTSIDE_SUFFIX",
    "connect",
    "guard_patterns",
    "is_external",
    "panel_scope",
    "tracked_models",
]

logger = logging.getLogger("vantage.external")

#: Ends every ``action_name`` this module writes. The marker is in the name rather than
#: in a new column so the feature costs no migration; "Created / Updated / Deleted
#: outside the panel" reads as a sentence wherever ``action_name`` is already shown.
OUTSIDE_SUFFIX = " outside the panel"

#: Shown as the actor when nobody was signed in to a request we could see.
UNKNOWN_ACTOR = "system / unknown"

_panel_depth: ContextVar[int] = ContextVar("vantage_panel_write", default=0)
_recording: ContextVar[bool] = ContextVar("vantage_external_recording", default=False)

#: Attribute used to carry the pre-save row from ``pre_save`` to ``post_save``.
_PRIOR = "_vantage_external_prior"
_M2M_BEFORE: ContextVar[dict | None] = ContextVar("vantage_external_m2m", default=None)


def is_external(entry) -> bool:
    """Whether an audit entry was written for a change made outside the panel."""
    return bool(entry.action_name and entry.action_name.endswith(OUTSIDE_SUFFIX))


# ── "the panel is writing" ──────────────────────────────────────────────────


@contextmanager
def panel_scope():
    """Mark the block as the panel's own write path; outside-the-panel capture stands down."""
    token = _panel_depth.set(_panel_depth.get() + 1)
    try:
        yield
    finally:
        _panel_depth.reset(token)


def in_panel() -> bool:
    return _panel_depth.get() > 0


def _scoped(view: Callable) -> Callable:
    @wraps(view)
    def scoped(request, *args, **kwargs):
        with panel_scope():
            return view(request, *args, **kwargs)

    scoped.panel_scoped = True  # type: ignore[attr-defined]
    return scoped


def guard_patterns(patterns: list) -> list:
    """Wrap every route a site builds in :func:`panel_scope`, in place (idempotent)."""
    for pattern in patterns:
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            guard_patterns(nested)
            continue
        callback = getattr(pattern, "callback", None)
        if callback is None or getattr(callback, "panel_scoped", False):
            continue
        pattern.callback = _scoped(callback)
    return patterns


# ── which models ────────────────────────────────────────────────────────────


def _declared() -> Any:
    declared = getattr(django_settings, "VANTAGE", None)
    if not isinstance(declared, dict):
        return False
    return declared.get("TRACK_EXTERNAL_CHANGES", False)


def _webhooks_on() -> bool:
    declared = getattr(django_settings, "VANTAGE", None)
    return bool(isinstance(declared, dict) and declared.get("TRACK_EXTERNAL_WEBHOOKS", False))


def _is_tracked(model: type[models.Model]) -> bool:
    """Cheap by construction: one settings read, then a label compare."""
    value = _declared()
    if not value:
        return False
    meta = model._meta
    if meta.app_label == "vantage" or meta.abstract or meta.swapped:
        return False
    # A model created by a migration's historical app registry is not the project's
    # model: its writes are the migration's and must not be logged as someone's.
    if meta.apps is not django_apps:
        return False
    if value == "*":
        from vantage.sites import site

        return site.is_registered(model)
    labels = {str(item).lower() for item in value}
    return meta.label_lower in labels or meta.concrete_model._meta.label_lower in labels


def tracked_models() -> list[type[models.Model]]:
    """Every installed model currently tracked. For checks and tests; not on the hot path."""
    return [model for model in django_apps.get_models() if _is_tracked(model)]


# ── receivers ───────────────────────────────────────────────────────────────


def _active(sender, raw: bool = False) -> bool:
    if raw or in_panel() or _recording.get():
        return False
    return _is_tracked(sender)


def _on_pre_save(sender, instance, raw=False, using=None, update_fields=None, **kwargs):
    if not _active(sender, raw) or instance.pk is None or instance._state.adding:
        return
    try:
        prior = (
            sender._base_manager.using(using or router.db_for_read(sender))
            .filter(pk=instance.pk)
            .values()
            .first()
        )
        setattr(instance, _PRIOR, prior)
    except Exception:
        logger.exception("Could not read the prior row of %r.", instance)


def _on_post_save(sender, instance, created, raw=False, using=None, update_fields=None, **kwargs):
    if not _active(sender, raw):
        return
    prior = instance.__dict__.pop(_PRIOR, None)
    try:
        from vantage.models import VantageLogEntry

        if created:
            changes = _created_changes(instance)
            action = VantageLogEntry.Action.CREATE
            name = "Created"
        else:
            if prior is None:
                # The row was not there to compare with: say nothing rather than guess.
                return
            changes = _updated_changes(instance, prior, update_fields)
            if not changes:
                return
            action = VantageLogEntry.Action.UPDATE
            name = "Updated"
        _write(instance, action, name, changes, using)
    except Exception:
        logger.exception("Could not record an outside-the-panel save of %r.", instance)


def _on_post_delete(sender, instance, using=None, **kwargs):
    if not _active(sender):
        return
    try:
        from vantage.audit import snapshot
        from vantage.models import VantageLogEntry

        _write(instance, VantageLogEntry.Action.DELETE, "Deleted", snapshot(instance), using)
    except Exception:
        logger.exception("Could not record an outside-the-panel delete of %r.", instance)


def _on_pre_m2m(sender, instance, action, reverse=False, model=None, **kwargs):
    if reverse or not action.startswith("pre_") or not _active(type(instance)):
        return
    if instance.pk is None:
        return
    field = _m2m_field(instance, sender)
    if field is None:
        return
    try:
        before = _m2m_state(instance, field)
        _M2M_BEFORE.set({**(_M2M_BEFORE.get() or {}), (id(instance), field.name): before})
    except Exception:
        logger.exception("Could not read a many-to-many field of %r.", instance)


def _on_post_m2m(sender, instance, action, reverse=False, **kwargs):
    if reverse or not action.startswith("post_") or not _active(type(instance)):
        return
    field = _m2m_field(instance, sender)
    if field is None:
        return
    key = (id(instance), field.name)
    stash = _M2M_BEFORE.get() or {}
    if key not in stash:
        return
    try:
        before = stash[key]
        _M2M_BEFORE.set({k: v for k, v in stash.items() if k != key})
        after = _m2m_state(instance, field)
        if before == after:
            return
        from vantage.audit import _jsonify
        from vantage.models import VantageLogEntry
        from vantage.sensitive import sensitive_names

        if field.name in sensitive_names(instance):
            changes: dict = {field.name: {"redacted": True}}
        else:
            changes = {field.name: {"from": _jsonify(before), "to": _jsonify(after)}}
        _write(instance, VantageLogEntry.Action.UPDATE, "Updated", changes, None)
    except Exception:
        logger.exception("Could not record an outside-the-panel relation change.")


def _m2m_field(instance, through):
    for field in instance._meta.many_to_many:
        if field.remote_field.through is through:
            return field
    return None


def _m2m_state(instance, field) -> list[str]:
    from vantage.audit import MAX_VALUE_CHARS, _clip

    related = getattr(instance, field.name).all()[:200]
    return sorted(_clip(str(item), MAX_VALUE_CHARS) for item in related)


# ── building the diff ───────────────────────────────────────────────────────


def _fields(instance):
    from vantage.introspection import concrete_fields
    from vantage.sensitive import sensitive_names

    return concrete_fields(type(instance)), sensitive_names(instance)


def _value(instance, field) -> Any:
    from vantage.audit import _clip, _jsonify

    if isinstance(field, models.FileField):
        stored = getattr(instance, field.name, None)
        return _clip(getattr(stored, "name", None) or "", 500) or None
    return _jsonify(getattr(instance, field.attname, None))


def _created_changes(instance) -> dict[str, dict[str, Any]]:
    fields, sensitive = _fields(instance)
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field.name in sensitive:
            continue
        changes[field.name] = {"from": None, "to": _value(instance, field)}
    return changes


def _updated_changes(instance, prior: dict, update_fields) -> dict[str, dict[str, Any]]:
    from vantage.audit import _jsonify

    fields, sensitive = _fields(instance)
    only = None if update_fields is None else set(update_fields)
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        if only is not None and field.name not in only and field.attname not in only:
            continue
        if getattr(field, "auto_now", False):
            continue  # moves on every save; the panel's own diff never carries it either
        if field.attname not in instance.__dict__ or field.attname not in prior:
            continue  # deferred: not loaded, so not written and not comparable
        old, new = prior[field.attname], instance.__dict__[field.attname]
        if old == new:
            continue
        if field.name in sensitive:
            changes[field.name] = {"redacted": True}
            continue
        if isinstance(field, models.FileField):
            before = getattr(old, "name", old) or None
            after = _value(instance, field)
        else:
            before, after = _jsonify(old), _jsonify(new)
        if before == after:
            continue
        changes[field.name] = {"from": before, "to": after}
    return changes


# ── writing ─────────────────────────────────────────────────────────────────


def _ambient_request():
    """The request in flight, when the tenancy middleware published one; else ``None``."""
    from vantage import tenancy

    candidate = tenancy._ambient.get()
    if candidate is tenancy._UNSET or not hasattr(candidate, "user"):
        return None
    return candidate


def _write(instance, action, verb: str, changes: dict, using) -> None:
    from vantage import webhooks
    from vantage.audit import _entry_kwargs
    from vantage.conf import vantage_settings
    from vantage.models import VantageLogEntry

    request = _ambient_request()
    token = _recording.set(True)
    try:
        if vantage_settings.AUDIT_LOG:
            kwargs = _entry_kwargs(
                request,
                action,
                obj=instance,
                model=None,
                changes=changes,
                action_name=verb + OUTSIDE_SUFFIX,
                object_repr=None,
            )
            if not kwargs["actor_repr"]:
                kwargs["actor_repr"] = UNKNOWN_ACTOR
            alias = router.db_for_write(VantageLogEntry)
            # Its own savepoint: a database error here must not poison the host's
            # transaction, which is what "never break the save" means on PostgreSQL.
            with transaction.atomic(using=alias):
                VantageLogEntry.objects.using(alias).create(**kwargs)
            if _webhooks_on():
                webhooks.notify(request, action, obj=instance, changes=changes)
    finally:
        _recording.reset(token)


_RECEIVERS = (
    (pre_save, "_on_pre_save", "vantage_external_pre_save"),
    (post_save, "_on_post_save", "vantage_external_post_save"),
    (post_delete, "_on_post_delete", "vantage_external_post_delete"),
    (m2m_changed, "_on_pre_m2m", "vantage_external_pre_m2m"),
    (m2m_changed, "_on_post_m2m", "vantage_external_post_m2m"),
)


def disconnect() -> None:
    """Remove every receiver, including the per-model ones."""
    for signal, _name, uid in _RECEIVERS:
        # A receiver connected with a sender has a different lookup key, so remove by
        # uid for each sender we may have used as well as for none.
        signal.disconnect(dispatch_uid=uid)
        for model in django_apps.get_models():
            signal.disconnect(sender=model, dispatch_uid=uid)
            for field in model._meta.many_to_many:
                through = field.remote_field.through
                if through is not None:
                    signal.disconnect(sender=through, dispatch_uid=uid)


def connect() -> None:
    """Connect the receivers for what ``TRACK_EXTERNAL_CHANGES`` names, and only that.

    A receiver connected without a sender switches off Django's fast delete and
    Vantage's own single-UPDATE bulk paths for *every* model, so nothing is connected
    while tracking is off, and a list of models connects per model. Safe to call
    repeatedly: every receiver has a ``dispatch_uid``. The setting is read again when it
    changes (see :func:`reconnect_on_change`).
    """
    disconnect()
    value = _declared()
    if not value:
        return
    if value != "*" and (isinstance(value, str | bytes) or not hasattr(value, "__iter__")):
        return  # invalid; the settings validation reports it
    tracked = [None] if value == "*" else [m for m in django_apps.get_models() if _is_tracked(m)]
    for signal, name, uid in _RECEIVERS:
        receiver = globals()[name]
        if signal is m2m_changed:
            # m2m_changed is sent with the through model as sender.
            senders = (
                [None]
                if tracked == [None]
                else [
                    f.remote_field.through
                    for m in tracked
                    for f in m._meta.many_to_many
                    if f.remote_field.through is not None
                ]
            )
        else:
            senders = tracked
        for sender in senders:
            kwargs = {"sender": sender} if sender is not None else {}
            signal.connect(receiver, dispatch_uid=uid, weak=False, **kwargs)


def reconnect_on_change(setting, **kwargs) -> None:
    """``setting_changed`` handler so tests and runtime overrides see the new value."""
    if setting == "VANTAGE":
        connect()
