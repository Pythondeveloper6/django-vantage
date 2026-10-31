"""Three things people do to records every day: copy one, change many, act on one.

Everything here is the *decision* half of those three features — which values a copy
should start from, which fields a bulk edit is allowed to offer, which buttons a row
gets. :mod:`vantage.recordviews` is the request half. The split is the same one
:mod:`vantage.widgets` and :mod:`vantage.formviews` already make, and for the same
reason: the rules below are the part worth asserting in a table test, and none of them
needs a request to be true.

Three rules shape the module, one per feature.

**A duplicate is a draft, never an insert.** Copying a record produces a *form*, filled
in and unsaved, that the person reviews before it becomes a row. That is not timidity:
a copy is the one create that is guaranteed to violate every unique constraint on the
model unless something intervenes, and the only place a person can fix a slug is
before the save, not in the traceback afterwards. So :func:`duplicate_plan` resolves
every unique field to a value nothing currently holds — "Copy of X", or a cleared slug
with a note saying so — and reports what it did in prose the form can show.

**A blank box means "leave this alone".** Bulk edit's whole value is that distinction,
and the only way to make it impossible to get wrong is to make an unticked field
*absent from the form*, not merely empty in it. That is the rule
:mod:`vantage.formviews` states for readonly fields — hiding a control is an
affordance, removing the field is the boundary — applied to the same problem from the
other side. A hand-written POST carrying ``body=`` for a field nobody ticked writes
nothing, because ``construct_instance`` never sees a field it was not given.

**A row action is an action.** ``@action`` already carries a label, an icon, a
permission verb, a destructive flag and a confirmation string. A second decoration
syntax for the same five facts would be two places to look and one place to forget, so
:func:`resolve_row_actions` reads the existing metadata and adds only what a single-row
button needs that a bulk one does not: a URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from django.db import models, router, transaction
from django.db.models import F
from django.db.models.fields.reverse_related import ForeignObjectRel
from django.db.models.signals import post_save, pre_save
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from vantage.concurrency import StaleWrite, claim, token_for, version_column
from vantage.introspection import (
    concrete_fields,
    is_auto_created_through,
    m2m_fields,
    relation_attr_name,
    through_model,
)
from vantage.widgets import FieldKind, build_form_class, classify, infer_form_fields

__all__ = [
    "BULK_EDIT_ENABLE",
    "BULK_EDIT_INSTANCE_LIMIT",
    "BULK_EDIT_PREFIX",
    "BuiltinRowAction",
    "BulkEditOutcome",
    "ChildGroup",
    "DuplicatePlan",
    "RowAction",
    "apply_bulk_edit",
    "bulk_edit_fields",
    "bulk_edit_form_class",
    "bulk_edit_payload",
    "bulk_edit_strategy",
    "copy_children",
    "copyable_children",
    "duplicate_plan",
    "panel_slot",
    "render_duplicate_notes",
    "render_row_actions",
    "resolve_row_actions",
    "row_actions_for",
    "run_bulk_edit",
    "ticked_fields",
]


# ── duplicating a record ────────────────────────────────────────────────────

#: How many suffixed candidates to try before giving up and clearing the field. A
#: model with "Copy of X (2)" through "(50)" already has a naming problem this feature
#: is not going to solve, and an unbounded loop here is one long table scan per copy.
UNIQUE_ATTEMPTS = 50


@dataclass(frozen=True)
class ChildGroup:
    """One reverse relation a copy could take with it. Never taken by default."""

    #: The attribute the children are reached through — ``post.comments``.
    accessor: str
    label: str
    count: int
    model: type[models.Model]
    #: The name of the foreign key on the child that points back at the source.
    field_name: str


@dataclass(frozen=True)
class DuplicatePlan:
    """Everything the duplicate form needs, and the sentences it has to show.

    ``notes`` is not decoration. A copy silently sharing an uploaded file with its
    original, or arriving with an empty slug, is a surprise the person discovers later
    — and "later" for a shared file means the day they delete one of the two records
    and the other one loses its image.
    """

    initial: dict[str, Any] = dataclass_field(default_factory=dict)
    notes: tuple[str, ...] = ()
    children: tuple[ChildGroup, ...] = ()


def duplicate_plan(panel, source, field_names: list[str] | None = None) -> DuplicatePlan:
    """Initial values for a copy of ``source``, with every collision already resolved.

    ``field_names`` is the form's own field list, so the plan can never offer a value
    for a control the form does not render — which is what would happen if this walked
    the model instead. Auto fields are excluded for free by that: they are not form
    fields, because :func:`~vantage.widgets.infer_form_fields` already refuses to
    offer an auto primary key, an ``auto_now`` timestamp or anything ``editable=False``.
    """
    model = panel.model
    names = list(field_names) if field_names is not None else infer_form_fields(model)
    initial: dict[str, Any] = {}
    notes: list[str] = []

    soft_delete = panel.get_patterns().soft_delete
    m2m_names = {f.name for f in m2m_fields(model)}
    secret = panel.get_sensitive_fields()

    for name in names:
        if name in secret:
            # Never copied. Two records sharing an API key is a security incident
            # waiting for whoever revokes one of them, and the duplicate form would
            # have to carry the value in the page to copy it at all.
            try:
                label = _label(model._meta.get_field(name))
            except Exception:
                label = name
            notes.append(f"{label}: not copied, because it is a sensitive value.")
            continue
        try:
            field = model._meta.get_field(name)
        except Exception:  # a form-only field a panel added has nothing to copy
            continue

        if name in m2m_names:
            if not is_auto_created_through(through_model(field)):
                continue
            related = list(getattr(source, name).values_list("pk", flat=True))
            if related:
                initial[name] = related
                notes.append(
                    f"{_label(field)}: {len(related)} "
                    f"{'link' if len(related) == 1 else 'links'} copied."
                )
            continue

        if soft_delete is not None and name == soft_delete.field:
            # A copy of an archived record is a live record. Carrying the tombstone
            # over would create a row that exists only in the trash, which nobody
            # pressing "Duplicate" has ever meant.
            initial[name] = None if soft_delete.kind == "timestamp" else False
            continue

        kind = classify(field)

        if kind in (FieldKind.FILE, FieldKind.IMAGE):
            stored = getattr(source, name, None)
            if getattr(stored, "name", None):
                initial[name] = stored
                notes.append(
                    f"{_label(field)}: the copy points at the same file "
                    f"({stored.name}). The file itself was not duplicated, so "
                    f"replacing it here replaces it for this record only."
                )
            continue

        value = getattr(source, getattr(field, "attname", name), None)

        if getattr(field, "unique", False) and not field.primary_key:
            value, note = _unique_value(model, field, value)
            if note:
                notes.append(note)

        initial[name] = value

    notes.extend(_composite_unique_notes(model, initial))
    return DuplicatePlan(
        initial=initial, notes=tuple(notes), children=tuple(_child_groups(model, source))
    )


def _unique_value(model, field, value) -> tuple[Any, str | None]:
    """A value for a unique field that no row currently holds, and why it changed.

    The candidates are tried against the database *now*, at form-build time, which is
    the whole point: a duplicate that fails on a unique constraint after the person
    has pressed Save has wasted their work and told them nothing they could have known
    beforehand. The form still runs Django's own unique validation on submit, so a row
    that appears in between is caught as a field error rather than an ``IntegrityError``
    — belt and braces, in that order.
    """
    label = _label(field)
    if value in (None, ""):
        return value, None

    kind = classify(field)
    if kind is FieldKind.SLUG:
        if field.blank:
            return None, (
                f"{label} was cleared — a slug has to be unique, and the right one "
                f"depends on what you call this copy."
            )
        candidate = _first_free(model, field, _slug_candidates(str(value), field))
        if candidate is not None:
            return candidate, f"{label} was changed to “{candidate}” to keep it unique."
    elif kind in (FieldKind.EMAIL, FieldKind.URL, FieldKind.UUID, FieldKind.IP):
        # Text-shaped, but "Copy of ada@example.com" is not an address and would fail
        # the field's own validator on save. A unique identifier with a format has no
        # "copy of": the honest move is to ask, which is what a cleared required field
        # does. Falls through to the clearing branch below.
        pass
    elif isinstance(field, models.CharField | models.TextField) and not field.choices:
        candidate = _first_free(model, field, _text_candidates(str(value), field))
        if candidate is not None:
            return candidate, f"{label} was changed to “{candidate}” to keep it unique."

    # Nothing sensible to invent — a unique integer, date or UUID has no "copy of".
    # Clearing it is the honest answer: the form then asks for a value, which is a
    # question, where a pre-filled duplicate would have been a trap.
    if field.blank or field.null:
        return None, f"{label} was cleared — it has to be unique."
    return None, f"{label} has to be unique. Enter a new one before saving."


def _slug_candidates(value: str, field):
    limit = field.max_length or 255
    yield _fit(f"{value}-copy", limit)
    for index in range(2, UNIQUE_ATTEMPTS + 1):
        yield _fit(f"{value}-copy-{index}", limit)


def _text_candidates(value: str, field):
    limit = field.max_length or 255
    yield _fit(f"Copy of {value}", limit)
    for index in range(2, UNIQUE_ATTEMPTS + 1):
        yield _fit(f"Copy of {value} ({index})", limit, tail=f" ({index})")


def _fit(value: str, limit: int, tail: str = "") -> str:
    """Truncate to ``limit`` while keeping ``tail`` — the part that makes it unique."""
    if len(value) <= limit:
        return value
    if tail and len(tail) < limit:
        return value[: limit - len(tail)] + tail
    return value[:limit]


def _first_free(model, field, candidates) -> Any | None:
    """The first candidate no row holds. ``_base_manager`` because the constraint is
    on the table: a soft-deleted row still occupies its slug."""
    for candidate in candidates:
        if not model._base_manager.filter(**{field.name: candidate}).exists():
            return candidate
    return None


def _composite_unique_notes(model, initial: dict[str, Any]) -> list[str]:
    """Warn about a ``unique_together`` the proposed copy would land on.

    Not resolved automatically, deliberately: which of three columns to vary is a
    judgement about the data, and guessing produces a record whose meaning quietly
    changed. Naming the group is what lets the person make that call in one edit
    rather than one failed save.
    """
    notes: list[str] = []
    for group in _unique_groups(model):
        lookup = {}
        for name in group:
            if name not in initial:
                break
            lookup[name] = initial[name]
        else:
            if any(value not in (None, "") for value in lookup.values()) and (
                model._base_manager.filter(**lookup).exists()
            ):
                labels = ", ".join(_label(model._meta.get_field(n)) for n in group)
                notes.append(
                    f"{labels} must be unique together, and this combination already "
                    f"exists. Change one of them before saving."
                )
    return notes


def _unique_groups(model) -> list[tuple[str, ...]]:
    groups = [tuple(group) for group in (model._meta.unique_together or ())]
    for constraint in model._meta.constraints:
        fields = getattr(constraint, "fields", None)
        if fields and getattr(constraint, "condition", None) is None:
            groups.append(tuple(fields))
    return groups


def _child_groups(model, source):
    """Reverse relations worth offering to copy, with a live count for each."""
    for rel in model._meta.get_fields():
        if not isinstance(rel, ForeignObjectRel) or rel.many_to_many or rel.one_to_one:
            continue
        accessor = relation_attr_name(rel)
        if accessor is None:
            continue
        manager = getattr(source, accessor, None)
        if manager is None:
            continue
        count = manager.count()
        if not count:
            continue
        yield ChildGroup(
            accessor=accessor,
            label=str(rel.related_model._meta.verbose_name_plural).capitalize(),
            count=count,
            model=rel.related_model,
            field_name=rel.field.name,
        )


def copy_children(request, source, target, groups) -> dict[str, int]:
    """Clone the chosen reverse children onto ``target``. Returns counts by accessor.

    Each child is re-created rather than moved — the original keeps its own — and its
    unique fields go through the same resolution the parent's did, because a child
    carrying a unique reference is exactly as capable of failing the insert.
    """
    from vantage import audit, fieldperms
    from vantage.permissions import get_permissions
    from vantage.sensitive import sensitive_names
    from vantage.tenancy import enforce as enforce_tenant

    permissions = get_permissions()
    copied: dict[str, int] = {}
    for group in copyable_children(request, groups):
        child_model = group.model
        # Never copied, like the parent's own: a sensitive value would end up in two
        # rows, and a hidden one would be written by someone who may not see it.
        skip = set(sensitive_names(child_model)) | set(
            fieldperms.access(request, child_model).hidden
        )
        rows = permissions.filter_queryset(request, getattr(source, group.accessor).all(), "view")
        made = 0
        with transaction.atomic(using=router.db_for_write(child_model)):
            for child in rows:
                clone = _clone_child(
                    child,
                    group.field_name,
                    target,
                    skip=skip,
                    before_save=enforce_tenant,
                    request=request,
                )
                made += 1
        # Outside the transaction, as everywhere else in the package: an audit write
        # that fails inside an atomic block marks it for rollback and destroys the
        # write it was recording.
        for clone in _recent(child_model, target, group.field_name, made):
            audit.record(request, "create", obj=clone, changes=audit.snapshot(clone))
        copied[group.accessor] = made
    return copied


def copyable_children(request, groups):
    """The child groups this user may copy: view *and* add on the child model.

    Copying a child is creating a row of the child model. Judged only by the parent's
    ``add``, it would let someone create — and, through the copy, read — records of a
    model they hold no permission on at all.
    """
    from vantage.permissions import get_permissions

    permissions = get_permissions()
    return [
        group
        for group in groups
        if permissions.has_model_perm(request, group.model, "view")
        and permissions.has_model_perm(request, group.model, "add")
    ]


def _clone_child(
    child, field_name: str, parent, *, skip=frozenset(), before_save=None, request=None
):
    clone = child.__class__()
    for field in concrete_fields(type(child)):
        if field.primary_key:
            continue
        if field.name == field_name:
            setattr(clone, field.attname, parent.pk)
            continue
        if not field.concrete or field.auto_created:
            continue
        if field.name in skip:
            # Left at the field's own default rather than copied.
            continue
        value = getattr(child, field.attname, None)
        if getattr(field, "unique", False):
            value, _ = _unique_value(type(child), field, value)
        setattr(clone, field.attname, value)
    if before_save is not None:
        before_save(request, clone)
    clone.save()
    for field in m2m_fields(type(child)):
        if is_auto_created_through(through_model(field)):
            getattr(clone, field.name).set(getattr(child, field.name).all())
    return clone


def _recent(child_model, parent, field_name: str, limit: int):
    if not limit:
        return []
    return list(child_model._base_manager.filter(**{field_name: parent.pk}).order_by("-pk")[:limit])


# ── bulk edit ───────────────────────────────────────────────────────────────

#: The form prefix every bulk-edit control carries. A prefix rather than bare field
#: names because the same POST also carries ``selected``, ``select_all`` and
#: ``excluded``, and a model with a field called ``selected`` must not silently
#: collide with the selection machinery.
BULK_EDIT_PREFIX = "bulk"

#: The multi-valued checkbox naming which fields to write. Its *contents* are the
#: security boundary: the form is constructed from exactly these names, so a field
#: nobody ticked is not a field the form has.
BULK_EDIT_ENABLE = "_bulk_enable"


def bulk_edit_fields(panel, request=None) -> list[str]:
    """The fields this panel will offer to set on many records at once.

    Three exclusions, each of which would otherwise be a guaranteed failure rather
    than a choice:

    * **unique fields** — one value across N rows violates the constraint by
      definition, so offering a slug here is offering a button that cannot work;
    * **file and image fields** — "upload one file and attach it to forty records"
      is not what anybody means by bulk edit, and the storage semantics of the
      alternative (forty rows sharing one path) are a trap;
    * **readonly and excluded names** — the panel already said those are not editable,
      and a bulk screen is not a way around a panel's own configuration. With a
      ``request``, that includes the fields this person may only read or not see.
    """
    readonly = set(panel.readonly_fields or ())
    excluded = set(panel.exclude or ())
    names = list(panel.fields) if panel.fields else infer_form_fields(panel.model)

    offered: list[str] = []
    secret = panel.get_sensitive_fields()
    for name in names:
        if name in readonly or name in excluded or name in secret:
            # A secret set on forty rows at once is forty rows sharing one secret.
            continue
        try:
            field = panel.model._meta.get_field(name)
        except Exception:
            continue
        if getattr(field, "unique", False) or field.primary_key:
            continue
        if classify(field) in (FieldKind.FILE, FieldKind.IMAGE):
            continue
        offered.append(name)
    if request is None:
        return offered
    from vantage.fieldperms import editable_fields

    return editable_fields(request, panel, offered)


def ticked_fields(post, available: list[str]) -> list[str]:
    """The fields the person asked to change, intersected with what is on offer.

    Intersected, not trusted. The ticked list arrives from the browser, and a name
    that is not in ``available`` is either a stale form or someone trying their luck;
    both get the same answer, which is that the field is not in the form.
    """
    asked = set(post.getlist(BULK_EDIT_ENABLE))
    return [name for name in available if name in asked]


def bulk_edit_form_class(panel, names: list[str]):
    """A ``ModelForm`` over exactly the ticked fields — the whole safety property.

    Reuses :func:`~vantage.widgets.build_form_class` so a bulk edit renders the same
    inferred control as the single-record form. Nothing here re-derives a widget: two
    answers to "how is a decimal edited" is how a bulk screen ends up rejecting the
    value the detail page accepted.
    """
    return build_form_class(panel, list(names))


def bulk_edit_payload(post, names: list[str]) -> dict[str, list[str]]:
    """The raw submitted values for the ticked fields, ready to be stored on a job.

    Raw strings rather than cleaned Python, because the worker re-validates. Storing
    ``cleaned_data`` would mean a second serialisation format for every field type and
    a second place for a decimal to lose a digit; storing what the browser sent means
    the job runs the same form over the same input the request would have.
    """
    wanted = {f"{BULK_EDIT_PREFIX}-{name}" for name in names}
    data = {key: post.getlist(key) for key in post if key in wanted}
    data[BULK_EDIT_ENABLE] = list(names)
    return data


def apply_bulk_edit(request, panel, obj, data, names: list[str], *, on_saved=None):
    """Write the ticked fields onto one record. Returns the bound form.

    One record at a time and through a real ``ModelForm``, not ``queryset.update()``.
    That costs a query per row and buys everything an ``update()`` skips: per-record
    validation, ``save()``, signals, ``auto_now``, m2m writes, the panel's own
    ``save_model`` hook, and an audit diff that says what each row actually changed.
    :mod:`vantage.jobs` is what makes that affordable at scale, and
    ``bulk_strategy="instances"`` is how it knows to.
    """
    form_class = bulk_edit_form_class(panel, names)
    form = form_class(data=data, instance=obj, prefix=BULK_EDIT_PREFIX)
    if not form.is_valid():
        return form

    from vantage import approvals

    hook = getattr(panel, "save_model", None)
    with transaction.atomic(using=router.db_for_write(panel.model)):
        instance = form.save(commit=False)
        # run_bulk_edit refuses a guarded field before it gets here. Held back rather
        # than trusted, so a caller that skipped that check still writes nothing that
        # nobody approved.
        pending = approvals.withhold(request, panel, instance, form)
        if callable(hook):
            hook(request, instance, form, True)
        else:
            instance.save()
        form.save_m2m()
        if on_saved is not None:
            # Inside the transaction: the undo records what *this* save stored.
            on_saved(instance)

    from vantage import audit

    audit.log_form_save(request, instance, form, True)
    approvals.announce(request, instance, pending)
    return form


# ── running a bulk edit ─────────────────────────────────────────────────────

#: How many rows the slow path will touch inside a request. Derived from the same cost
#: model :mod:`vantage.jobs` routes by — ``COST_PER_ROW["instances"]`` is 2ms and
#: ``DEFAULT_THRESHOLD_SECONDS`` is two seconds — so the ceiling here is exactly the
#: point at which that module would have moved the work off the request. Refusing is
#: the honest answer rather than a gateway timeout half way through a write.
BULK_EDIT_INSTANCE_LIMIT = 1000


@dataclass(frozen=True)
class BulkEditOutcome:
    """What a bulk edit did, in the terms the person who pressed the button asked in.

    ``strategy`` and ``reason`` are reported rather than kept internal on purpose. One
    ``UPDATE`` and N ``save()`` calls are not the same operation — the second runs
    signals, ``auto_now`` and ``full_clean`` and the first does not — and a screen that
    silently picks between them has made a semantic choice on the user's behalf without
    telling them which. §11 already refuses to let ``bulk_strategy`` be inferred for an
    action; the same rule applies to the one edit the panel performs itself.
    """

    #: ``"sql"`` for a single ``UPDATE``, ``"instances"`` for a save per row.
    strategy: str
    #: Why the slow path was taken. Empty for ``"sql"``.
    reason: str = ""
    #: How many rows the predicate resolved to when the edit ran.
    matched: int = 0
    updated: int = 0
    #: Rows a concurrent edit had already moved. Never silently folded into ``updated``.
    skipped: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()
    #: Set when nothing ran at all: too many rows for the slow path.
    refused: str | None = None

    @property
    def message(self) -> str:
        if self.refused:
            return self.refused
        how = "in one query" if self.strategy == "sql" else f"one at a time, because {self.reason}"
        noun = "record" if self.updated == 1 else "records"
        parts = [f"Updated {self.updated} {noun} {how}."]
        if self.skipped:
            were = "was" if self.skipped == 1 else "were"
            parts.append(
                f"{self.skipped} {were} changed by someone else after you made this "
                f"selection and {'was' if self.skipped == 1 else 'were'} left alone."
            )
        if self.failed:
            parts.append(f"{self.failed} could not be saved.")
        return " ".join(parts)

    @property
    def level(self) -> str:
        if self.refused or self.failed:
            return "error"
        if self.skipped:
            return "warning"
        return "success"


def bulk_edit_strategy(panel, names: list[str]) -> tuple[str, str]:
    """``("sql", "")`` when one ``UPDATE`` is faithful, or ``("instances", why)``.

    ``queryset.update()`` is the right answer for the overwhelmingly common bulk edit —
    set a status on forty thousand rows — and it is one statement whatever the row
    count. It is also a different operation from a save: it does not run ``save()``, it
    does not fire ``pre_save``/``post_save``, it cannot write the far side of a
    many-to-many, and it does not advance an ``auto_now`` column. So the fast path is
    taken only where none of those matters, and every reason for declining is a sentence
    the outcome can show rather than a silent downgrade.

    Detected rather than declared, unlike an action's ``bulk_strategy``: an action is a
    method somebody wrote and can annotate, while this edit is generated from the panel's
    own field list and there is nowhere for the author to put the declaration.
    """
    model = panel.model
    if callable(getattr(panel, "save_model", None)):
        return "instances", (
            f"{type(panel).__name__} defines save_model(), which only a save() calls"
        )
    # ``model`` is already the class, so ``type(model)`` would be ModelBase — the
    # metaclass, which has no save() at all. The question is whether this model
    # overrides Model.save, because a queryset update() would skip that override.
    if model.save is not models.Model.save:
        return "instances", f"{model.__name__} overrides save()"
    for name in names:
        try:
            field = model._meta.get_field(name)
        except Exception:  # pragma: no cover - ticked names are already intersected
            continue
        if field.many_to_many:
            return "instances", f"{_label(field)} is a many-to-many relation, written after the row"
    # ``has_listeners`` covers receivers connected without a sender too, which is the
    # conservative reading: a project-wide audit receiver is exactly the thing an
    # ``UPDATE`` would skip.
    if pre_save.has_listeners(model) or post_save.has_listeners(model):
        return "instances", "a pre_save or post_save receiver is connected to this model"
    return "sql", ""


def run_bulk_edit(request, panel, queryset, form, names: list[str], *, since=None, capture=None):
    """Write the ticked fields across ``queryset``. Returns a :class:`BulkEditOutcome`.

    ``form`` is a *validated* bulk form — one form for the whole edit, because the values
    are the same for every row and validating them N times would be N times the work for
    one answer. ``queryset`` is the selection already resolved through the list page's own
    predicate, so "all 40,000 matching" arrives here as a queryset and never as ids.

    ``since`` is what stops this from being a silent overwrite. :mod:`vantage.concurrency`
    makes the ``WHERE`` clause the check for one row; a bulk edit needs the same property
    for a set, and gets it the same way — the version column is compared *inside* the
    statement, so rows that moved between the selection and the write fall out of it and
    are reported rather than stomped. Two halves:

    * rows whose timestamp is newer than ``since`` are excluded by the ``UPDATE`` itself;
    * the version column is *advanced* by the same statement, so a change form somebody
      has open, holding the old token, now fails its own compare-and-set instead of
      quietly putting the row back.

    That second half is the one an ``UPDATE`` would otherwise get wrong: it does not run
    ``pre_save``, so an ``auto_now`` column would keep its old value and every optimistic
    lock in flight would still appear valid.
    """
    model = panel.model
    strategy, reason = bulk_edit_strategy(panel, names)
    column = version_column(model)

    matched = queryset.count()
    from vantage.approvals import refusal

    refused = refusal(request, panel, names)
    if refused:
        # Refused rather than queued per row: see vantage.approvals.refusal. Before
        # anything is written, so nothing is applied that nobody approved.
        return BulkEditOutcome(strategy=strategy, reason=reason, matched=matched, refused=refused)
    if strategy == "instances" and matched > BULK_EDIT_INSTANCE_LIMIT:
        return BulkEditOutcome(
            strategy=strategy,
            reason=reason,
            matched=matched,
            refused=(
                f"{matched} records is too many to edit one at a time, and this edit "
                f"has to be: {reason}. Narrow the selection to "
                f"{BULK_EDIT_INSTANCE_LIMIT} or fewer, or use a bulk action."
            ),
        )

    guarded = _guard_by_version(queryset, column, since)
    if capture is not None:
        # Restored the way the edit was written: one UPDATE, or a save() per row.
        capture.strategy = strategy
    if strategy == "sql":
        return _bulk_edit_sql(request, panel, guarded, form, names, column, matched, capture)
    return _bulk_edit_instances(request, panel, guarded, form, names, matched, reason, capture)


def _guard_by_version(queryset, column, since):
    """Exclude rows a newer write already touched. The comparison is in the query.

    Only a timestamp column can answer "has this moved since I looked", so a counter
    model is not filtered here — it is still protected, because the counter is advanced
    by the write itself and that is what invalidates anybody else's token.
    """
    if column is None or column.is_counter or since is None:
        return queryset
    return queryset.filter(**{f"{column.name}__lte": since})


def _bulk_edit_sql(request, panel, queryset, form, names, column, matched: int, capture=None):
    values = {name: form.cleaned_data[name] for name in names}
    if column is not None:
        # Advancing the lock is part of the same statement, not a second write: a
        # separate UPDATE would leave a window in which the row has the new value and
        # the old token, which is precisely the state an optimistic lock exists to
        # make impossible.
        values[column.name] = F(column.name) + 1 if column.is_counter else timezone.now()

    with transaction.atomic(using=router.db_for_write(panel.model)):
        # ``order_by()`` because a bulk edit inherits the list page's sort and an
        # ORDER BY on an UPDATE is at best ignored and at worst rejected.
        if capture is not None:
            # Pinned to the rows it locked and recorded, so what is undone later is
            # exactly what this statement changed.
            queryset = capture.lock(queryset)
        updated = queryset.order_by().update(**values)
        if capture is not None:
            capture.read_after()

    from vantage import audit

    audit.record(
        request,
        "action",
        model=panel.model,
        action_name="bulk_edit",
        object_repr=f"{updated} record{'' if updated == 1 else 's'}",
        changes={name: {"to": _loggable(value)} for name, value in form.cleaned_data.items()},
    )
    return BulkEditOutcome(
        strategy="sql",
        matched=matched,
        updated=updated,
        skipped=max(matched - updated, 0),
    )


def _loggable(value):
    from vantage.audit import _jsonify

    return _jsonify(value)


def _bulk_edit_instances(
    request, panel, queryset, form, names, matched: int, reason: str, capture=None
):
    """One ``save()`` per row, each taking the row's lock first.

    The per-row :func:`~vantage.concurrency.claim` is not redundant with the ``since``
    filter above it: the filter closes the window between the selection and this run,
    and the claim closes the window between *this* ``SELECT`` and *this* ``save()``. A
    ``ModelForm.save()`` writes every column, so a row that moved inside that window
    would otherwise be reverted wholesale by an edit that meant to touch one field.
    """
    # The submitted data verbatim, not ``cleaned_data``. Re-binding a real ``ModelForm``
    # per row is what buys the per-record validation, the ``save()`` and the audit diff
    # this path exists for; handing it cleaned values would mean a second coercion step
    # and a second place for a decimal to lose a digit.
    data = form.data

    updated = skipped = failed = 0
    errors: list[str] = []
    for obj in queryset.order_by("pk").iterator():
        try:
            claim(obj, token_for(obj))
        except StaleWrite:
            skipped += 1
            continue
        if capture is not None:
            capture.note(obj)
        bound = apply_bulk_edit(
            request,
            panel,
            obj,
            data,
            names,
            on_saved=capture.read_row if capture is not None else None,
        )
        if bound.errors:
            if capture is not None:
                capture.forget(obj)
            failed += 1
            if len(errors) < 20:
                errors.append(f"{obj}: {'; '.join(_flatten_errors(bound))}")
        else:
            updated += 1

    return BulkEditOutcome(
        strategy="instances",
        reason=reason,
        matched=matched,
        updated=updated,
        skipped=skipped + max(matched - updated - failed - skipped, 0),
        failed=failed,
        errors=tuple(errors),
    )


def _flatten_errors(form) -> list[str]:
    return [f"{name}: {' '.join(messages)}" for name, messages in form.errors.items()]


# ── row actions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BuiltinRowAction:
    """One of the four buttons every record already has a page for."""

    name: str
    label: str
    icon: str
    route: str
    permission: str
    destructive: bool = False


#: The built-ins, by the name a panel writes in ``row_actions``. They are links, not
#: posts, because each one already has a page that does the job properly — including
#: delete, whose page shows the cascade before it happens. Re-implementing that as a
#: one-click POST would be a strictly worse delete.
BUILTIN_ROW_ACTIONS: dict[str, BuiltinRowAction] = {
    "view": BuiltinRowAction("view", "View", "file-text", "detail", "view"),
    "edit": BuiltinRowAction("edit", "Edit", "pencil", "change", "change"),
    "duplicate": BuiltinRowAction("duplicate", "Duplicate", "plus", "duplicate", "add"),
    "delete": BuiltinRowAction("delete", "Delete", "trash", "delete", "delete", True),
}
#: Aliases, so a panel may spell them the way the permission verbs are spelled.
BUILTIN_ALIASES = {"detail": "view", "change": "edit", "copy": "duplicate"}


@dataclass(frozen=True)
class RowAction:
    """One button on one row, resolved down to what the template needs.

    Deliberately not an :class:`~vantage.actions.Action`: an Action is a callable over
    a queryset, and half of these are links to pages that already exist. The metadata
    is *read off* the Action rather than restated, which is the part that matters —
    ``@action(label=…, icon=…, permission=…, destructive=…, confirm=…)`` stays the one
    place those five facts are written down.
    """

    name: str
    label: str
    icon: str | None
    #: Set for a link. ``None`` for an action that posts.
    url: str | None
    #: Set for an action that posts. ``None`` for a link.
    post_url: str | None
    destructive: bool = False
    confirm: str | None = None

    @property
    def is_link(self) -> bool:
        return self.url is not None

    @property
    def confirm_text(self) -> str:
        return self.confirm or (
            f"{self.label} this record. This cannot be undone."
            if self.destructive
            else f"{self.label} this record?"
        )


def resolve_row_actions(panel, request) -> list[tuple[Any, str | None]]:
    """What ``panel.row_actions`` names, filtered by what this user may do.

    Returns ``(spec, permission)`` pairs where ``spec`` is either a
    :class:`BuiltinRowAction` or an :class:`~vantage.actions.Action`. Resolved once per
    page rather than once per row: ``has_model_perm`` is a cheap call but a hundred of
    them on a hundred-row page is the exact N+1 :mod:`vantage.permissions` exists to
    prevent, and the answer cannot differ between two rows of the same model.

    "Once per page" is enforced here rather than asked of the caller, because the caller
    is a template slot that fires once per ``<td>`` and has nowhere to keep an answer.
    The memo hangs off the request, which is the only object in play whose lifetime is
    exactly one page — a module-level cache would outlive the permission grant it
    recorded, which is a security bug rather than a stale render.

    Object-level checks are deliberately not done here. They *can* differ between rows,
    so they belong where N is one: :class:`~vantage.recordviews.RowActionView` runs
    exactly one before it executes anything.
    """
    from vantage.permissions import get_permissions

    declared = panel.row_actions
    if not declared:
        return []

    memo = _row_action_memo(request)
    if memo is not None and panel.slug in memo:
        return memo[panel.slug]

    permissions = get_permissions() if request is not None else None
    resolved: list[tuple[Any, str | None]] = []
    for entry in declared:
        spec, permission = _row_spec(panel, entry)
        if spec is None:
            continue
        if permissions is not None and permission is not None:
            if not permissions.has_model_perm(request, panel.model, permission):
                continue
        resolved.append((spec, permission))

    if memo is not None:
        memo[panel.slug] = resolved
    return resolved


def _row_action_memo(request) -> dict | None:
    """The per-request store, or ``None`` when there is nowhere to keep one."""
    if request is None:
        return None
    memo = getattr(request, "_vantage_row_actions", None)
    if memo is None:
        memo = {}
        try:
            request._vantage_row_actions = memo
        except AttributeError:  # pragma: no cover - a request-like with __slots__
            return None
    return memo


def _row_spec(panel, entry) -> tuple[Any, str | None]:
    from vantage.actions import Action, _build

    if isinstance(entry, str):
        key = BUILTIN_ALIASES.get(entry, entry)
        builtin = BUILTIN_ROW_ACTIONS.get(key)
        if builtin is not None:
            return builtin, builtin.permission
    if isinstance(entry, Action):
        return entry, entry.permission
    built = _build(panel, entry)
    if built is None:
        return None, None
    return built, built.permission


def restore_action_for(panel, request, obj) -> RowAction | None:
    """The Restore button, for a row that is currently in the trash.

    Not declared in ``row_actions`` and deliberately not declarable. In the trash it is
    the *only* thing anyone wants to do, and a trash whose restore button had to be
    switched on first would be a trash people lose records in. Every other built-in is
    opt-in because every other built-in is optional.

    A POST, not a link, for the reason the rest of this module keeps repeating: it
    mutates, and a GET that mutates is a URL a crawler or a prefetcher will fire.
    """
    pattern = panel.get_patterns().soft_delete
    if pattern is None:
        return None

    value = getattr(obj, pattern.field, None)
    trashed = bool(value) if pattern.kind == "boolean" else value is not None
    if not trashed:
        return None

    # Memoised per request, like resolve_row_actions and for the same reason: the
    # answer cannot differ between two rows of the same model, and asking once per row
    # is the N+1 the permissions layer exists to prevent.
    memo = _row_action_memo(request)
    key = f"_restore:{panel.slug}"
    if memo is not None and key in memo:
        allowed = memo[key]
    else:
        from vantage.permissions import get_permissions

        allowed = request is None or get_permissions().has_model_perm(
            request, panel.model, "delete"
        )
        if memo is not None:
            memo[key] = allowed
    if not allowed:
        return None

    url = _reverse(f"vantage:{panel.slug}-restore", obj.pk)
    if url is None:
        return None
    # The page's own filters ride along, exactly as they do for a declared action:
    # restoring row nine of page two of the trash should come back to page two of the
    # trash, not to the unfiltered first page of the live list.
    query = _query_string(request)
    if query:
        url = f"{url}?{query}"
    return RowAction(
        name="restore",
        label=str(_("Restore")),
        icon="rotate-ccw",
        url=None,
        post_url=url,
        destructive=False,
    )


def row_actions_for(panel, request, obj) -> list[RowAction]:
    """The buttons for one row, with their URLs filled in.

    A built-in whose route is not mounted drops out silently rather than raising: the
    package's own URLs are built from the registry, but a project is free to mount a
    subset, and a ``NoReverseMatch`` from inside a table cell is a 500 on a page that
    was only trying to draw a button.
    """
    restore = restore_action_for(panel, request, obj)
    resolved = resolve_row_actions(panel, request)
    if not resolved:
        # Restore still shows: it is the trash's own button, not one of the declared
        # ones, so a panel that declares no row actions must still be able to undo.
        return [restore] if restore is not None else []

    post_url = _reverse(f"vantage:{panel.slug}-row-action", obj.pk)
    if post_url is not None:
        # The page's own filters ride along, so running an action from row nine of a
        # filtered, sorted, paginated list comes back to row nine of the same list
        # rather than to the unfiltered first page.
        query = _query_string(request)
        if query:
            post_url = f"{post_url}?{query}"
    # First, so the primary thing to do with a trashed row is the leftmost button.
    buttons: list[RowAction] = [restore] if restore is not None else []
    for spec, _permission in resolved:
        if isinstance(spec, BuiltinRowAction):
            url = _reverse(f"vantage:{panel.slug}-{spec.route}", obj.pk)
            if url is None:
                continue
            buttons.append(
                RowAction(
                    name=spec.name,
                    label=spec.label,
                    icon=spec.icon,
                    url=url,
                    post_url=None,
                    destructive=spec.destructive,
                )
            )
            continue
        if post_url is None:
            continue
        buttons.append(
            RowAction(
                name=spec.name,
                label=spec.label,
                icon=spec.icon,
                url=None,
                post_url=post_url,
                destructive=spec.destructive,
                confirm=spec.confirm,
            )
        )
    return buttons


def _reverse(name: str, *args) -> str | None:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _query_string(request) -> str:
    get = getattr(request, "GET", None) if request is not None else None
    return get.urlencode() if get is not None else ""


def render_row_actions(panel, context: dict | None) -> Any:
    """The HTML for one row's buttons, for the ``row_actions`` slot.

    Wired through :func:`panel_slot` rather than through a template tag, so the
    existing slot — which already renders a trailing column, already passes
    ``object=row.obj``, and which a project may already be contributing to — stays the
    one seam. Two mechanisms drawing into the same cell is how the column ends up with
    two of everything.
    """
    if context is None:
        # No context at all: the most that can be known is whether this panel declares
        # any, and an unanswerable question is not a reason to skip the column.
        return bool(panel.row_actions)

    obj = context.get("object")
    if obj is None or getattr(obj, "pk", None) is None:
        # A context but no row: this is ``slots.is_filled`` asking whether the column
        # should exist, not a request to draw a cell. The trash answers yes even for a
        # panel that declares nothing, because every row in it gets a Restore button —
        # and the column has to exist before the buttons can go in it.
        showing_trash = bool(context.get("showing_trash"))
        has_trash = panel.get_patterns().soft_delete is not None
        return bool(panel.row_actions) or (showing_trash and has_trash)
    buttons = row_actions_for(panel, context.get("request"), obj)
    if not buttons:
        return None
    return mark_safe(  # noqa: S308 - rendered by the template engine, which escapes
        render_to_string(
            "vantage/partials/row_actions.html",
            {"row_actions": buttons, "object": obj, "panel": panel},
            request=context.get("request"),
        )
    )


def render_duplicate_notes(context: dict | None) -> Any:
    """The "here is what the copy could not bring with it" block, above the form.

    Rendered through the ``before_form`` slot rather than by editing ``form.html``,
    because the duplicate page *is* the create form — the same template, the same
    layout, the same widgets — and forking it to add one card would fork it for every
    future change too. Nothing renders when the context carries no notes, which is every
    other page that template serves.

    Built with ``format_html`` rather than a template of its own: it is four elements
    reusing the alert classes ``form.html`` already ships, and :mod:`vantage.slots`
    documents exactly this shape for a fragment contributed from Python.
    """
    if not context:
        return None
    notes = context.get("duplicate_notes")
    if not notes:
        return None
    source = context.get("duplicate_source")
    lede = (
        f"This is a copy of “{source}”. Check it over before saving."
        if source is not None
        else "This is a copy. Check it over before saving."
    )
    return format_html(
        '<div class="v-form-alert" data-tone="info" role="note">'
        '<p class="v-form-alert-title">{}</p><ul>{}</ul></div>',
        lede,
        format_html_join("", "<li>{}</li>", ((note,) for note in notes)),
    )


def panel_slot(panel, name: str, context: dict | None = None) -> Any:
    """``ModelPanel.slot`` for the slots Vantage itself fills. See the panels.py diff.

    Lives here rather than on the panel so that a panel author who overrides ``slot``
    and forgets to call ``super()`` loses only their own contribution — and so this
    file, not ``panels.py``, is where the behaviour is read and tested.
    """
    if name == "row_actions":
        return render_row_actions(panel, context)
    if name == "before_form":
        return render_duplicate_notes(context)
    return None


# ── shared helpers ──────────────────────────────────────────────────────────


def _label(field) -> str:
    verbose = getattr(field, "verbose_name", None)
    if verbose:
        return str(verbose).capitalize()
    return str(getattr(field, "name", "")).replace("_", " ").capitalize()


def editable_field_names(panel) -> list[str]:
    """The form field names for this panel, mirroring ``PanelFormView``.

    Duplicated from the view rather than imported from it because importing a view
    into the model layer would invert the dependency the module split exists to keep;
    the list itself comes from :func:`~vantage.widgets.infer_form_fields` in both
    places, so the two cannot disagree about what a form contains.
    """
    names = list(panel.fields) if panel.fields else infer_form_fields(panel.model)
    readonly = set(panel.readonly_fields or ())
    excluded = set(panel.exclude or ())
    return [n for n in names if n not in readonly and n not in excluded]
