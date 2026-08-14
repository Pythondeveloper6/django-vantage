"""Create, edit and delete — the views that make the panel writable.

Everything about *what* a form contains lives in :mod:`vantage.widgets`; this module is
only about the request cycle around it. That split is deliberate: field selection,
sectioning and widget inference are pure functions that a test can assert on a table,
while these classes are the part that touches permissions, the database and the session.

Two rules shape almost every decision below.

**Never trust the absence of an input.** A readonly field is not rendered as a control
*and* is not in the form's field list at all, so a hand-crafted POST that includes it is
ignored by ``construct_instance`` rather than merely unrendered. Hiding a control is a
UI affordance; removing the field is the security boundary.

**A row you may not see does not exist.** Every object lookup goes through
:meth:`~vantage.permissions.PanelPermissions.filter_queryset` before any object-level
check runs, so an out-of-scope record answers 404. Answering 403 would confirm that
record 12 exists and is merely off-limits, which is the leak the scoping was for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db import router, transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from vantage import inlines
from vantage.views import PanelView
from vantage.widgets import FieldKind, build_form_class, classify, infer_form_fields, layout

__all__ = [
    "CascadeGroup",
    "CascadeSummary",
    "CreateView",
    "DeleteView",
    "UpdateView",
    "collect_cascade",
    "form_editable_names",
    "form_field_names",
    "get_record",
    "object_denied_message",
    "save_form",
]

#: Kinds whose control wants the full width of the grid rather than one column.
WIDE_KINDS = frozenset(
    {
        FieldKind.LONGTEXT,
        FieldKind.RICHTEXT,
        FieldKind.JSON,
        FieldKind.BINARY,
        FieldKind.RELATION_MANY,
    }
)


def _control_class(widget: forms.Widget) -> str:
    """The stylesheet hook for a control, read off the widget the spec already built.

    Taken from the widget instance rather than re-derived from the field so a panel that
    overrides a widget still gets styled, and so the class can never disagree with what
    was actually rendered.
    """
    if isinstance(widget, forms.CheckboxInput):
        return "v-checkbox"
    if isinstance(widget, forms.Textarea):
        return "v-textarea"
    if isinstance(widget, forms.SelectMultiple):
        return "v-select v-select-multiple"
    if isinstance(widget, forms.Select):
        return "v-select"
    if isinstance(widget, forms.FileInput):
        return "v-file"
    return "v-input"


def _model_field(model, name: str):
    """The model field behind a form field name, or None for a form-only field."""
    try:
        return model._meta.get_field(name)
    except FieldDoesNotExist:
        return None


# ── the write path, without a view around it ────────────────────────────────
#
# The four functions below were methods on the classes further down until the JSON
# API needed them. They are here rather than duplicated there because each one is a
# rule rather than a convenience: which names reach the form is the security boundary
# this module's docstring describes, and how a save is wrapped decides whether the
# audit entry and the m2m write happen at all. A second copy of either would be a
# second answer, and only one of the two would get fixed.


def form_field_names(panel) -> list[str]:
    """Every name the form page shows, editable or not, in layout order.

    Readonly names are appended when inference did not already pick them up: the
    canonical example is ``readonly_fields = ["created_at"]`` on an ``auto_now_add``
    column, which is not an editable field at all and would otherwise vanish from a
    page whose whole purpose was to display it.
    """
    names = list(panel.fields) if panel.fields else infer_form_fields(panel.model)
    for name in panel.readonly_fields or ():
        if name not in names:
            names.append(name)
    excluded = set(panel.exclude or ())
    return [name for name in names if name not in excluded]


def form_editable_names(panel) -> list[str]:
    """The names a write may actually set. Everything else is not in the form at all."""
    readonly = set(panel.readonly_fields or ())
    return [name for name in form_field_names(panel) if name not in readonly]


def save_form(request, panel, form: forms.ModelForm, *, change: bool, inline_groups=()):
    """Persist, handing control to the panel's ``save_model`` hook when it has one.

    ``commit=False`` first so the hook sees the populated-but-unsaved instance — the
    point of the hook is to set what the form cannot, such as the acting user. The m2m
    write has to follow the row insert regardless of who performed it, so it is done
    here rather than being one more thing every hook has to remember.

    ``inline_groups`` are child formsets already validated by
    :func:`vantage.inlines.all_valid`. They are written *inside this transaction*, and
    that is the whole reason they are a parameter here rather than a second call after
    this one returns: a parent that committed while its children failed is data loss
    wearing a success message. Callers with no inlines — the JSON API among them — pass
    nothing and this behaves exactly as it did.
    """
    from vantage.tenancy import enforce as enforce_tenant

    hook = getattr(panel, "save_model", None)
    with transaction.atomic(using=router.db_for_write(panel.model)):
        obj = form.save(commit=False)
        # Before the hook, so the hook sees a complete object. Stamps the tenant on a
        # create and refuses a write that would move the record to another one —
        # scoping the read cannot catch either. A no-op unless tenancy is on.
        enforce_tenant(request, obj)
        if callable(hook):
            hook(request, obj, form, change)
        else:
            obj.save()
        form.save_m2m()
        inlines.save(request, obj, inline_groups)
    # Outside the transaction on purpose: catching a logging error *inside* an atomic
    # block still marks it for rollback, so a swallowed exception here would destroy
    # the write it was recording.
    from vantage import audit

    audit.log_form_save(request, obj, form, change)
    inlines.log(request, inline_groups)
    return obj


#: One sentence per verb, for the reason given on ``PanelView.DENIED_MESSAGES``.
OBJECT_DENIED_MESSAGES = {
    "view": _("You do not have permission to view this %(model)s."),
    "add": _("You do not have permission to add this %(model)s."),
    "change": _("You do not have permission to change this %(model)s."),
    "delete": _("You do not have permission to delete this %(model)s."),
}


def object_denied_message(panel, action: str) -> str:
    """Why this verb was refused on this row, in a sentence a person can read."""
    model = panel.get_label().lower()
    message = OBJECT_DENIED_MESSAGES.get(action)
    if message is None:
        return _("You do not have permission to %(action)s this %(model)s.") % {
            "action": action,
            "model": model,
        }
    return message % {"model": model}


def get_record(request, panel, permissions, pk, action: str):
    """One row, scoped then authorised. The order is the whole point.

    ``filter_queryset`` runs with ``"view"`` rather than with the action being
    attempted, because scoping answers "does this row exist for you?" and that question
    has one answer per user, not one per verb. Only once the row is admissible does the
    object-level check decide whether this particular verb is allowed on it — and *that*
    failure is a 403, because at that point the user already knows the record exists.
    """
    queryset = permissions.filter_queryset(request, panel.get_queryset(request), "view")
    obj = get_object_or_404(queryset, pk=pk)
    if not permissions.has_object_perm(request, panel.model, action, obj):
        raise PermissionDenied(object_denied_message(panel, action))
    return obj


# ── the shared create/edit machinery ────────────────────────────────────────


class PanelFormView(PanelView):
    """Common ground between create and edit: build a form, render it, save it."""

    template_name = "vantage/form.html"

    #: True on edit. Passed straight through to the panel's ``save_model`` hook, which
    #: is the signature ``contrib.admin`` established and users already know.
    change = False

    object = None

    # ── which fields ────────────────────────────────────────────────────────

    def get_field_names(self) -> list[str]:
        """Every name the page shows, editable or not, in layout order."""
        return form_field_names(self.panel)

    def get_editable_field_names(self) -> list[str]:
        """The names this page may actually write. See :func:`form_editable_names`."""
        return form_editable_names(self.panel)

    # ── the form ────────────────────────────────────────────────────────────

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"instance": self.object}
        if self.request.method == "POST":
            kwargs.update(data=self.request.POST, files=self.request.FILES)
        return kwargs

    def get_form(self) -> forms.ModelForm:
        form_class = build_form_class(self.panel, self.get_editable_field_names())
        form = form_class(**self.get_form_kwargs())
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", _control_class(field.widget))
        return form

    def get_object(self):
        return None

    # ── request handling ────────────────────────────────────────────────────

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        groups = inlines.bind(self, form)
        return self.render_to_response(self.get_context_data(form=form, inlines=groups))

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        # Both halves are validated before either verdict is acted on, and neither call
        # is allowed to short-circuit the other: a page that reported the parent's errors
        # and not its children's would be fixed, resubmitted, and refused again for
        # reasons it declined to mention the first time.
        form_ok = form.is_valid()
        groups = inlines.bind(self, form)
        if not inlines.all_valid(groups) or not form_ok:
            return self.render_to_response(self.get_context_data(form=form, inlines=groups))
        obj = self.save_form(form, groups)
        messages.success(request, self.get_success_message(obj))
        return HttpResponseRedirect(self.get_success_url(obj))

    def save_form(self, form: forms.ModelForm, inline_groups=()):
        """Persist through :func:`save_form`, which the JSON API writes through too."""
        return save_form(
            self.request, self.panel, form, change=self.change, inline_groups=inline_groups
        )

    # ── presentation ────────────────────────────────────────────────────────

    def get_sections(self, form: forms.ModelForm) -> list[dict[str, Any]]:
        """The layout, resolved into things a template can render without branching much.

        Sections come from :func:`~vantage.widgets.layout` over the *full* name list, so
        a readonly field keeps its position among the inputs instead of being exiled to
        a trailing block. A readonly field with nothing to show — every one of them on a
        create form — drops out, and a section left empty by that drops out with it.
        """
        entries_by_section = []
        for section in layout(self.panel.model, self.get_field_names()):
            entries = [self.get_field_entry(name, form) for name in section.fields]
            entries = [entry for entry in entries if entry is not None]
            if entries:
                entries_by_section.append(
                    {"key": section.key, "title": section.title, "fields": entries}
                )
        return entries_by_section

    def get_field_entry(self, name: str, form: forms.ModelForm) -> dict[str, Any] | None:
        from vantage.render import render_cell

        model_field = _model_field(self.panel.model, name)
        kind = classify(model_field) if model_field is not None else FieldKind.UNKNOWN

        if name in form.fields:
            return {
                "name": name,
                "kind": str(kind),
                "wide": kind in WIDE_KINDS,
                "readonly": False,
                "bound": form[name],
            }

        if self.object is None or self.object.pk is None:
            return None  # a readonly field on a record that does not exist yet
        return {
            "name": name,
            "kind": str(kind),
            "wide": kind in WIDE_KINDS,
            "readonly": True,
            "label": str(getattr(model_field, "verbose_name", name)).capitalize(),
            "value": render_cell(self.panel, self.object, name),
            "help_text": str(getattr(model_field, "help_text", "") or ""),
        }

    def get_success_url(self, obj) -> str:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
            return reverse(self.panel.list_url_name)

    def get_success_message(self, obj) -> str:
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            panel=self.panel,
            object=self.object,
            sections=self.get_sections(context["form"]),
            cancel_url=self.get_cancel_url(),
            submit_label=self.submit_label,
        )
        return context

    def get_cancel_url(self) -> str:
        return reverse(self.panel.list_url_name)


class CreateView(PanelFormView):
    """A blank form for one new record."""

    required_action = "add"
    change = False
    submit_label = _("Create")

    @property
    def page_title(self) -> str:
        return _("New %(model)s") % {"model": self.panel.get_label().lower()}

    def get_breadcrumbs(self):
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (self.page_title, None),
        ]

    def get_success_message(self, obj) -> str:
        return _("Created %(model)s “%(object)s”.") % {
            "model": self.panel.get_label().lower(),
            "object": obj,
        }


class RecordView(PanelView):
    """Base for the views that act on one existing record."""

    object = None

    def get_object(self):
        """Scope first, then authorise — see :func:`get_record`, which does both."""
        return get_record(
            self.request,
            self.panel,
            self.permissions,
            self.kwargs["pk"],
            self.required_action,
        )

    #: Kept as a class attribute because a subclass may have reworded it. The module
    #: level dict is the one :func:`object_denied_message` reads.
    OBJECT_DENIED_MESSAGES = OBJECT_DENIED_MESSAGES

    def _denied_message(self):
        return object_denied_message(self.panel, self.required_action)

    def get_detail_url(self, obj) -> str | None:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
            return None


class UpdateView(RecordView, PanelFormView):
    """The same form as :class:`CreateView`, bound to an existing record."""

    required_action = "change"
    change = True
    submit_label = _("Save changes")

    @property
    def page_title(self) -> str:
        if self.object:
            return _("Edit %(object)s") % {"object": self.object}
        return _("Edit")

    def get_breadcrumbs(self):
        detail = self.get_detail_url(self.object) if self.object else None
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), detail),
            (_("Edit"), None),
        ]

    def get_cancel_url(self) -> str:
        return self.get_detail_url(self.object) or reverse(self.panel.list_url_name)

    def get_success_message(self, obj) -> str:
        return _("Saved changes to %(model)s “%(object)s”.") % {
            "model": self.panel.get_label().lower(),
            "object": obj,
        }


# ── deletion ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CascadeGroup:
    """One model's worth of collateral damage."""

    label: str
    count: int


@dataclass(frozen=True)
class CascadeSummary:
    """What a delete would actually destroy, in the terms a person would ask it in."""

    #: Rows that go with the record, by model. Excludes the record itself.
    groups: tuple[CascadeGroup, ...] = ()
    #: Rows that survive but lose a reference — ``on_delete=SET_NULL``.
    cleared: tuple[CascadeGroup, ...] = ()
    #: Total related rows destroyed. What goes in the confirm button's label.
    total: int = 0
    #: Set when a ``PROTECT``/``RESTRICT`` relation makes the delete impossible.
    blocked_by: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_by is not None


def collect_cascade(obj) -> CascadeSummary:
    """Summarise everything ``obj.delete()`` would take with it.

    Uses the same :class:`~django.db.models.deletion.Collector` the delete itself will
    use, so the preview cannot drift from the behaviour it is predicting — a preview
    computed from a separate walk of the relation graph is a second implementation of
    cascade semantics, and the two will disagree the first time someone adds a
    ``SET_DEFAULT``.

    Both of the Collector's row sets are counted. ``data`` holds instances Django loaded
    into memory; ``fast_deletes`` holds querysets it intends to delete with a single
    statement and therefore never loads. Reporting only the first understates the damage
    for exactly the common case — a plain cascading foreign key with no signal
    receivers — which is the surprise this page exists to prevent.
    """
    model = type(obj)
    collector = Collector(using=router.db_for_write(model, instance=obj), origin=obj)
    try:
        collector.collect([obj])
    except (ProtectedError, RestrictedError) as exc:
        return CascadeSummary(blocked_by=_blocked_message(model, exc))

    counts: dict[Any, int] = {}
    for related_model, instances in collector.data.items():
        counts[related_model] = counts.get(related_model, 0) + len(instances)
    for queryset in collector.fast_deletes:
        counts[queryset.model] = counts.get(queryset.model, 0) + queryset.count()

    # The record itself is not collateral. Its model can still appear with a positive
    # count through a self-referential cascade, so subtract rather than drop.
    counts[model] = counts.get(model, 0) - 1

    groups = tuple(
        CascadeGroup(label=_plural(related_model), count=count)
        for related_model, count in sorted(counts.items(), key=lambda item: -item[1])
        if count > 0
    )
    return CascadeSummary(
        groups=groups,
        cleared=_cleared_groups(collector),
        total=sum(group.count for group in groups),
    )


def _cleared_groups(collector: Collector) -> tuple[CascadeGroup, ...]:
    """Rows that survive with a field blanked, from the collector's ``SET_NULL`` plan.

    Reported separately from the destroyed rows because the two are different kinds of
    bad news, and collapsing them into one number would make a harmless nulled optional
    reference look like a deletion.
    """
    counts: dict[str, int] = {}
    for (field, value), instances in collector.field_updates.items():
        if value is not None:
            continue
        label = _("%(model)s — %(field)s cleared") % {
            "model": _plural(field.model),
            "field": field.verbose_name,
        }
        counts[label] = counts.get(label, 0) + sum(_length(item) for item in instances)
    return tuple(
        CascadeGroup(label=label, count=count) for label, count in counts.items() if count
    )


def _length(instances) -> int:
    """Collector batches are sets, lists or querysets depending on how they were found."""
    try:
        return len(instances)
    except TypeError:  # pragma: no cover - defensive; querysets support len()
        return instances.count()


def _plural(model) -> str:
    return str(model._meta.verbose_name_plural).capitalize()


def _blocked_message(model, exc: Exception) -> str:
    blockers = getattr(exc, "protected_objects", None) or getattr(exc, "restricted_objects", ())
    names = sorted({_plural(type(item)) for item in blockers})
    listed = ", ".join(names) if names else str(_("other records"))
    return _(
        "This %(model)s cannot be deleted while %(blockers)s still reference it. "
        "Remove or reassign those first."
    ) % {"model": str(model._meta.verbose_name).lower(), "blockers": listed}


class DeleteView(RecordView):
    """Confirm, showing the blast radius, then delete."""

    template_name = "vantage/confirm_delete.html"
    required_action = "delete"

    @property
    def page_title(self) -> str:
        if self.object:
            return _("Delete %(object)s") % {"object": self.object}
        return _("Delete")

    def get_breadcrumbs(self):
        detail = self.get_detail_url(self.object) if self.object else None
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), detail),
            (_("Delete"), None),
        ]

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        cascade = collect_cascade(self.object)
        if cascade.is_blocked:
            # Re-render rather than raise: the reason is the useful part, and a bare 409
            # would lose it. 409 rather than 200 so a scripted caller can tell.
            messages.error(request, cascade.blocked_by)
            return self.render_to_response(self.get_context_data(cascade=cascade), status=409)

        label = str(self.object)
        # Before the delete: Model.delete() blanks the pk, so an entry written
        # afterwards would snapshot a record with no identity.
        from vantage import audit

        audit.log_delete(request, self.object)
        with transaction.atomic(using=router.db_for_write(self.panel.model)):
            self.object.delete()
        messages.success(request, self.get_success_message(label, cascade))
        return HttpResponseRedirect(reverse(self.panel.list_url_name))

    def get_success_message(self, label: str, cascade: CascadeSummary) -> str:
        name = self.panel.get_label().lower()
        if not cascade.total:
            return _("Deleted %(model)s “%(object)s”.") % {"model": name, "object": label}
        return ngettext(
            "Deleted %(model)s “%(object)s” and %(count)s related record.",
            "Deleted %(model)s “%(object)s” and %(count)s related records.",
            cascade.total,
        ) % {"model": name, "object": label, "count": cascade.total}

    def get_context_data(self, **kwargs):
        kwargs.setdefault("cascade", collect_cascade(self.object))
        context = super().get_context_data(**kwargs)
        cascade = context["cascade"]
        context.update(
            panel=self.panel,
            object=self.object,
            confirm_label=self.get_confirm_label(cascade),
            cancel_url=self.get_detail_url(self.object) or reverse(self.panel.list_url_name),
        )
        return context

    def get_confirm_label(self, cascade: CascadeSummary) -> str:
        """The count goes in the button, not only in the list above it.

        A confirmation whose button just says "Delete" is a confirmation people click
        without reading; putting the number on the control being pressed is the last
        chance to notice that this order is taking twelve other rows with it.
        """
        name = self.panel.get_label().lower()
        if not cascade.total:
            return _("Delete %(model)s") % {"model": name}
        return ngettext(
            "Delete %(model)s and %(count)s related record",
            "Delete %(model)s and %(count)s related records",
            cascade.total,
        ) % {"model": name, "count": cascade.total}
