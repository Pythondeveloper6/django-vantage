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

from vantage.views import PanelView
from vantage.widgets import FieldKind, build_form_class, classify, infer_form_fields, layout

__all__ = [
    "CascadeGroup",
    "CascadeSummary",
    "CreateView",
    "DeleteView",
    "UpdateView",
    "collect_cascade",
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
        """Every name the page shows, editable or not, in layout order.

        Readonly names are appended when inference did not already pick them up: the
        canonical example is ``readonly_fields = ["created_at"]`` on an ``auto_now_add``
        column, which is not an editable field at all and would otherwise vanish from a
        page whose whole purpose was to display it.
        """
        panel = self.panel
        names = list(panel.fields) if panel.fields else infer_form_fields(panel.model)
        for name in panel.readonly_fields or ():
            if name not in names:
                names.append(name)
        excluded = set(panel.exclude or ())
        return [name for name in names if name not in excluded]

    def get_editable_field_names(self) -> list[str]:
        readonly = set(self.panel.readonly_fields or ())
        return [name for name in self.get_field_names() if name not in readonly]

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
        return self.render_to_response(self.get_context_data(form=self.get_form()))

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        obj = self.save_form(form)
        messages.success(request, self.get_success_message(obj))
        return HttpResponseRedirect(self.get_success_url(obj))

    def save_form(self, form: forms.ModelForm):
        """Persist, handing control to the panel's ``save_model`` hook when it has one.

        ``commit=False`` first so the hook sees the populated-but-unsaved instance — the
        point of the hook is to set what the form cannot, such as the acting user. The
        m2m write has to follow the row insert regardless of who performed it, so it is
        done here rather than being one more thing every hook has to remember.
        """
        hook = getattr(self.panel, "save_model", None)
        with transaction.atomic(using=router.db_for_write(self.panel.model)):
            obj = form.save(commit=False)
            if callable(hook):
                hook(self.request, obj, form, self.change)
            else:
                obj.save()
            form.save_m2m()
        # Outside the transaction on purpose: catching a logging error *inside* an
        # atomic block still marks it for rollback, so a swallowed exception here
        # would destroy the write it was recording.
        from vantage import audit

        audit.log_form_save(self.request, obj, form, self.change)
        return obj

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
    submit_label = "Create"

    @property
    def page_title(self) -> str:
        return f"New {self.panel.get_label().lower()}"

    def get_breadcrumbs(self):
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (self.page_title, None),
        ]

    def get_success_message(self, obj) -> str:
        return f"Created {self.panel.get_label().lower()} “{obj}”."


class RecordView(PanelView):
    """Base for the views that act on one existing record."""

    object = None

    def get_object(self):
        """Scope first, then authorise. The order is the whole point.

        ``filter_queryset`` runs with ``"view"`` rather than with the action being
        attempted, because scoping answers "does this row exist for you?" and that
        question has one answer per user, not one per verb. Only once the row is
        admissible does the object-level check decide whether this particular verb is
        allowed on it — and *that* failure is a 403, because at that point the user
        already knows the record exists.
        """
        queryset = self.permissions.filter_queryset(
            self.request, self.panel.get_queryset(self.request), "view"
        )
        obj = get_object_or_404(queryset, pk=self.kwargs["pk"])
        if not self.permissions.has_object_perm(
            self.request, self.panel.model, self.required_action, obj
        ):
            raise PermissionDenied(
                f"You do not have permission to {self.required_action} this "
                f"{self.panel.get_label().lower()}."
            )
        return obj

    def get_detail_url(self, obj) -> str | None:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
            return None


class UpdateView(RecordView, PanelFormView):
    """The same form as :class:`CreateView`, bound to an existing record."""

    required_action = "change"
    change = True
    submit_label = "Save changes"

    @property
    def page_title(self) -> str:
        return f"Edit {self.object}" if self.object else "Edit"

    def get_breadcrumbs(self):
        detail = self.get_detail_url(self.object) if self.object else None
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), detail),
            ("Edit", None),
        ]

    def get_cancel_url(self) -> str:
        return self.get_detail_url(self.object) or reverse(self.panel.list_url_name)

    def get_success_message(self, obj) -> str:
        return f"Saved changes to {self.panel.get_label().lower()} “{obj}”."


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
        label = f"{_plural(field.model)} — {field.verbose_name} cleared"
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
    listed = ", ".join(names) if names else "other records"
    return (
        f"This {str(model._meta.verbose_name).lower()} cannot be deleted while "
        f"{listed} still reference it. Remove or reassign those first."
    )


class DeleteView(RecordView):
    """Confirm, showing the blast radius, then delete."""

    template_name = "vantage/confirm_delete.html"
    required_action = "delete"

    @property
    def page_title(self) -> str:
        return f"Delete {self.object}" if self.object else "Delete"

    def get_breadcrumbs(self):
        detail = self.get_detail_url(self.object) if self.object else None
        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), detail),
            ("Delete", None),
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
            return f"Deleted {name} “{label}”."
        plural = "" if cascade.total == 1 else "s"
        return f"Deleted {name} “{label}” and {cascade.total} related record{plural}."

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
            return f"Delete {name}"
        plural = "" if cascade.total == 1 else "s"
        return f"Delete {name} and {cascade.total} related record{plural}"
