"""Editable child records on the parent's own form — ``inlines = [OrderItemInline]``.

Not to be confused with :mod:`vantage.inline` (singular), which is inline *cell* editing
on the list page. That module edits one field of many rows of the model you are looking
at; this one edits many rows of a *different* model while you look at their parent. They
share a word and nothing else.

An ``Order`` whose ``OrderItem``s can only be reached through a second page is not an
order form, it is a receipt. Every decision below follows from that: the children are
part of the parent's edit, so they are validated with it, saved with it, and rolled back
with it.

Four rules shape the module.

**One transaction, or none.** A parent that saved while its children failed validation is
data loss with a success message on top — the user is told the edit landed, and the half
that did not land is the half they were actually working on. Validation of every formset
happens before anything is written, and the write itself runs inside the parent's own
``transaction.atomic(using=router.db_for_write(...))``. There is exactly one such block,
in :meth:`~vantage.formviews.PanelFormView.save_form`, and the inlines join it rather
than opening a second one.

**Permissions are answered against the child model.** ``change_order`` is not
``delete_orderitem``. Every inline resolves its own four verbs against its own model,
through the same :class:`~vantage.permissions.PanelPermissions` the rest of the panel
uses, so read-only mode, ``has_<action>_permission`` hooks and row scoping all apply.
Missing ``add`` removes the extra forms *and* sets ``edit_only``, so a hand-written POST
cannot create what the page refused to offer; missing ``delete`` removes the checkbox
*and* skips the deletions on the way in. Hiding a control is a UI affordance; refusing
the operation server-side is the security boundary.

**The query count does not follow the row count.** Two things would otherwise make it:
the child rows' ``__str__``, which conventionally touches their own foreign keys, and —
the subtler one — every ``ModelChoiceField`` in every row. ``ModelChoiceField.__deepcopy__``
deliberately re-clones its queryset per form instance to force a fresh iterator, so
twenty rows with a ``product`` select is twenty identical ``SELECT * FROM product``
queries. :func:`_share_choices` evaluates each distinct field's options exactly once and
hands the same list to every row.

**An error belongs to a row.** Django's formsets collect errors per form; a template that
pours them into one list at the top hands the user a heap of "This field is required" with
no way to tell which of eleven lines is short. Errors render in the offending cell, and a
row that has any is marked so it can be found without reading.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from django import forms
from django.core.exceptions import ImproperlyConfigured
from django.forms.models import BaseInlineFormSet, inlineformset_factory
from django.utils.text import capfirst

from vantage.introspection import (
    concrete_fields,
    is_auto_created_through,
    m2m_fields,
    resolve_path,
    through_model,
)
from vantage.optimizer import plan_related
from vantage.permissions import get_permissions
from vantage.widgets import (
    RELATION_SEARCH_THRESHOLD,
    FieldKind,
    _formfield_callback,
    classify,
    infer_form_fields,
)

__all__ = [
    "STACKED",
    "TABULAR",
    "BoundInline",
    "Inline",
    "InlineRow",
    "StackedInline",
    "TabularInline",
    "all_valid",
    "bind",
    "log",
    "resolve_fk",
    "save",
]

#: The two layouts. A grid for children that fit on a line, a stack of full forms for
#: children that do not. Nothing else is offered: a third layout would be a third set of
#: templates to keep accessible, and every child record seen so far is one or the other.
TABULAR = "tabular"
STACKED = "stacked"

#: Above this many columns a table stops being a table and becomes a horizontal scroll
#: bar. Six columns of inputs is already dense at 1280px; seven is unreadable.
TABULAR_FIELD_LIMIT = 6

#: Kinds that cannot honestly live in a table cell whatever the column count. A textarea
#: in a grid cell is three visible words of a paragraph, and a file input in one is a
#: button with its filename cut off.
STACKED_KINDS: frozenset[FieldKind] = frozenset(
    {
        FieldKind.LONGTEXT,
        FieldKind.RICHTEXT,
        FieldKind.JSON,
        FieldKind.BINARY,
        FieldKind.FILE,
        FieldKind.IMAGE,
        FieldKind.RELATION_MANY,
    }
)

#: The hard ceiling on forms one POST may carry, independent of ``max_num``.
#: ``BaseFormSet`` defaults ``absolute_max`` to ``max_num + 1000``, so leaving it implicit
#: on an inline with no ``max_num`` lets a crafted ``TOTAL_FORMS`` build a thousand forms
#: before anything rejects it. Declared here so the number is a decision rather than a
#: side effect of a default.
ABSOLUTE_MAX = 500


# ── the declarative API ─────────────────────────────────────────────────────


class Inline:
    """One child model editable on its parent's form.

    Declared and attached exactly like the rest of the panel API::

        class OrderItemInline(Inline):
            model = OrderItem
            fields = ["product", "quantity", "unit_price"]
            extra = 1

        @register(Order)
        class OrderPanel(ModelPanel):
            inlines = [OrderItemInline]

    Every attribute that can be inferred defaults to ``None`` and is resolved by its own
    ``get_*`` method, which is :class:`~vantage.panels.ModelPanel`'s rule restated: setting
    ``fields`` overrides the field list and nothing else, and the layout stays inferred.
    """

    #: The child model. Required — there is nothing to infer it from.
    model: type[Any] | None = None

    #: Which foreign key on the child points at the parent. Only needed when the child
    #: has more than one, which is exactly when guessing would be a data-loss bug.
    fk_name: str | None = None

    #: ``None`` infers from the child model, minus the parent link.
    fields: list[str] | None = None
    exclude: list[str] | None = None

    #: Blank rows offered on load. One, not three: ``contrib.admin``'s default fills the
    #: page with empty forms nobody asked for, and the Add button is right there.
    extra: int = 1
    min_num: int = 0
    max_num: int | None = None

    #: Whether existing rows may be removed. Gated by the ``delete`` permission on the
    #: *child* model regardless — this only turns it off further.
    can_delete: bool = True

    #: ``None`` infers from the field count and the field kinds.
    layout: str | None = None

    #: ``None`` takes the child model's ``verbose_name`` / ``verbose_name_plural``.
    label: str | None = None
    label_plural: str | None = None
    description: str | None = None

    #: ``None`` uses the child model's ``Meta.ordering``.
    ordering: list[str] | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        name = getattr(self.model, "__name__", "?")
        return f"<{type(self).__name__} for {name}>"

    # ── identity ────────────────────────────────────────────────────────────

    def get_label(self) -> str:
        return self.label or capfirst(str(self.model._meta.verbose_name))

    def get_label_plural(self) -> str:
        return self.label_plural or capfirst(str(self.model._meta.verbose_name_plural))

    # ── resolved configuration ──────────────────────────────────────────────

    def get_fields(self, parent_model) -> list[str]:
        """The columns this inline edits, with the parent link removed.

        The parent foreign key is dropped whether or not it was declared: Django replaces
        it with a hidden ``InlineForeignKeyField`` anyway, so leaving it in the list only
        produces an empty column with a header on it.
        """
        names = list(self.fields) if self.fields else infer_form_fields(self.model)
        excluded = set(self.exclude or ())
        excluded.add(resolve_fk(parent_model, self.model, self.fk_name).name)
        return [name for name in names if name not in excluded]

    def get_layout(self, parent_model) -> str:
        """``"tabular"`` or ``"stacked"``, inferred from what the fields actually are.

        Inferred rather than defaulted because the right answer is a property of the child
        model and the user already told us what that is. Three short columns want a grid;
        a body ``TextField`` wants a form. Declaring ``layout`` overrides both tests.
        """
        if self.layout:
            return self.layout
        names = self.get_fields(parent_model)
        if len(names) > TABULAR_FIELD_LIMIT:
            return STACKED
        if any(self._kind(name) in STACKED_KINDS for name in names):
            return STACKED
        return TABULAR

    def _kind(self, name: str) -> FieldKind:
        hops = resolve_path(self.model, name)
        return classify(hops[-1].field) if hops else FieldKind.UNKNOWN

    def get_queryset(self, request, parent_model):
        """The child rows, with the joins their rendering will need already planned.

        Two sources. The declared field paths go through :func:`~vantage.optimizer.plan_related`,
        the same function the list page uses. Every *other* forward relation is joined too,
        which the list page does not do — because a row heading here is ``str(child)`` and
        the overwhelmingly common ``__str__`` reads the row's own foreign keys. One join is
        cheaper than one query per row, and this is the promise the module is asserting.
        """
        queryset = self.model._default_manager.all()
        select, prefetch = plan_related(self.model, self.get_fields(parent_model))
        select = set(select) | {
            field.name
            for field in concrete_fields(self.model)
            if field.is_relation and (field.many_to_one or field.one_to_one)
        }
        if select:
            queryset = queryset.select_related(*sorted(select))
        if prefetch:
            queryset = queryset.prefetch_related(*sorted(prefetch))
        ordering = self.ordering or self.model._meta.ordering
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset

    # ── permissions, answered against the child model ───────────────────────

    def allows(self, request, action: str, obj=None) -> bool:
        """Whether ``request.user`` may perform ``action`` on this inline's child model.

        An inline may answer for itself with ``has_<action>_permission(request, obj=None)``,
        the same four hook names :class:`~vantage.panels.ModelPanel` uses, and its answer
        wins outright — including over the superuser bypass, for the same reason it does
        there: an inline that says nobody may delete these rows means nobody.

        Otherwise the question goes to the configured resolver *with the child model*, so
        ``delete_orderitem`` is what gets checked and a panel-wide ``change_order`` grants
        nothing here. ``obj`` is only ever passed where N is one — a row actually being
        written — never once per rendered row.
        """
        hook = getattr(self, f"has_{action}_permission", None)
        if hook is not None:
            return bool(hook(request, obj))
        permissions = get_permissions()
        if obj is None:
            return permissions.has_model_perm(request, self.model, action)
        return permissions.has_object_perm(request, self.model, action, obj)

    def is_through(self, parent_model) -> bool:
        """Whether the child is the join table of a many-to-many declared on the parent."""
        return any(through_model(field) is self.model for field in m2m_fields(parent_model))

    def is_generated_through(self, parent_model) -> bool:
        """Whether that join table was generated by Django rather than written by hand.

        Answered through :func:`~vantage.introspection.is_auto_created_through`, which is a
        **truthiness** test on ``Meta.auto_created``: Django stores the *owning model class*
        there on a generated through model, never ``True``. Writing ``is True`` here — the
        obvious spelling — reports every generated join table as hand-written; writing
        ``is not True`` reports every ordinary child model as generated. Both end in the
        same place, which is why the answer lives behind a named function instead of being
        re-spelled at each call site.

        The distinction is real and not cosmetic. A generated table has nothing on it but
        the two links, so its inline is a one-column picker and ``fields`` inference is
        exact. A hand-written one carries the extra columns that made someone write it, and
        those are the whole reason it is being inlined instead of rendered as a multi-select.
        """
        return self.is_through(parent_model) and is_auto_created_through(self.model)


class TabularInline(Inline):
    """An :class:`Inline` pinned to the grid layout."""

    layout = TABULAR


class StackedInline(Inline):
    """An :class:`Inline` pinned to the stacked layout."""

    layout = STACKED


# ── resolving the parent link ───────────────────────────────────────────────


def resolve_fk(parent_model, child_model, fk_name: str | None = None):
    """The foreign key on ``child_model`` that points at ``parent_model``.

    Django's own ``_get_foreign_key`` answers this and is what
    ``inlineformset_factory`` uses; the resolved name is handed back to it so Django
    stays the authority. This exists to *fail well*: the ambiguous case — two foreign
    keys to the same parent — is a silent data-loss bug if guessed, and the message a
    user needs is the list of names they could have written.
    """
    candidates = [
        field
        for field in concrete_fields(child_model)
        if field.is_relation
        and (field.many_to_one or field.one_to_one)
        and field.remote_field.model is not None
        and issubclass(parent_model, field.remote_field.model)
    ]
    if fk_name is not None:
        for field in candidates:
            if field.name == fk_name:
                return field
        raise ImproperlyConfigured(
            f"{child_model.__name__}.{fk_name} is not a foreign key to {parent_model.__name__}."
        )
    if not candidates:
        raise ImproperlyConfigured(
            f"{child_model.__name__} has no foreign key to {parent_model.__name__}, "
            f"so it cannot be inlined on it."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(field.name for field in candidates))
        raise ImproperlyConfigured(
            f"{child_model.__name__} has more than one foreign key to "
            f"{parent_model.__name__} ({names}). Set fk_name on the inline to say "
            f"which one is the parent link."
        )
    return candidates[0]


# ── what a template renders ─────────────────────────────────────────────────


@dataclass(frozen=True)
class InlineRow:
    """One child row, pre-resolved so the template does no lookups.

    Cells come out in column order rather than form order, which is the whole reason this
    exists: a tabular layout has to guarantee that cell three is under header three, and a
    template that iterates ``form.visible_fields`` cannot promise that once a subclass
    reorders anything.
    """

    form: forms.ModelForm
    cells: tuple[Any, ...]
    hidden: tuple[Any, ...]
    delete: Any | None
    #: True for a blank row that does not correspond to a saved record.
    is_extra: bool
    #: ``str(instance)``, or empty for a row that does not exist yet.
    label: str

    @property
    def errors(self) -> bool:
        return bool(self.form.errors)

    @property
    def non_field_errors(self):
        return self.form.non_field_errors()


@dataclass
class BoundInline:
    """One inline, resolved against one request: the formset plus every yes/no answer."""

    inline: Inline
    formset: BaseInlineFormSet
    layout: str
    columns: tuple[dict[str, Any], ...]
    rows: tuple[InlineRow, ...]
    empty_row: InlineRow | None
    can_add: bool
    can_delete: bool
    #: True when the user may see these rows but not change them. The controls render
    #: disabled *and* every field carries ``disabled=True``, which makes Django ignore
    #: submitted data for it — so a forged POST is inert rather than merely unstyled.
    readonly: bool

    #: ``(instance, pk)`` for every row :func:`save` destroyed, captured before the pk was
    #: blanked so :func:`log` can still name them.
    deleted: list[tuple[Any, Any]] = dataclass_field(default_factory=list)

    @property
    def prefix(self) -> str:
        return self.formset.prefix

    @property
    def label(self) -> str:
        return self.inline.get_label()

    @property
    def label_plural(self) -> str:
        return self.inline.get_label_plural()

    @property
    def description(self) -> str | None:
        return self.inline.description

    @property
    def is_tabular(self) -> bool:
        return self.layout == TABULAR

    @property
    def max_num(self) -> int | None:
        return self.inline.max_num

    @property
    def colspan(self) -> int:
        """Every column in the tabular header, so a row's error banner spans all of it."""
        return len(self.columns) + (1 if (self.can_delete or self.can_add) else 0)

    @property
    def error_count(self) -> int:
        return sum(1 for row in self.rows if row.errors)


# ── binding ─────────────────────────────────────────────────────────────────


def bind(view, form: forms.ModelForm | None = None) -> list[BoundInline]:
    """Every inline the panel declares, resolved for this request.

    Called on both GET and POST from :class:`~vantage.formviews.PanelFormView`. The parent
    instance comes from ``form.instance`` rather than from ``view.object`` so a create page
    works unchanged: the instance is unsaved and has no pk, ``BaseInlineFormSet`` answers
    with an empty queryset, and the real parent is attached at save time.
    """
    panel = getattr(view, "panel", None)
    declared = list(getattr(panel, "inlines", None) or ())
    if not declared:
        return []

    request = view.request
    instance = form.instance if form is not None else view.object
    data = files = None
    if request.method in ("POST", "PUT"):
        data, files = request.POST, request.FILES

    bound: list[BoundInline] = []
    for entry in declared:
        inline = entry() if isinstance(entry, type) else entry
        resolved = _bind_one(inline, panel.model, request, instance, data, files)
        if resolved is not None:
            bound.append(resolved)
    return bound


def _bind_one(inline: Inline, parent_model, request, instance, data, files):
    if inline.model is None:
        raise ImproperlyConfigured(f"{type(inline).__name__} must set model.")
    if not inline.allows(request, "view"):
        # Not "rendered empty": an inline the user may not read should leave no trace of
        # the relation on the page at all.
        return None

    may_change = inline.allows(request, "change")
    may_add = may_change and inline.allows(request, "add")
    may_delete = may_change and inline.can_delete and inline.allows(request, "delete")
    readonly = not may_change

    from vantage import fieldperms

    # The child model's own field rules: a hidden field is no column, and a read-only
    # one is a disabled field, which Django fills from the stored value whatever the
    # POST says — the same server-side refusal as leaving it out, kept on the page.
    rules = fieldperms.access(request, inline.model)
    names = [name for name in inline.get_fields(parent_model) if name not in rules.hidden]
    formset_class = _formset_class(
        inline,
        parent_model,
        # A copy: before Django 6.0, BaseInlineFormSet appends the parent link to
        # Meta.fields in place, which would put it back into `names` as a column.
        fields=list(names),
        extra=0 if (readonly or not may_add) else max(int(inline.extra), 0),
        can_delete=may_delete,
        edit_only=not may_add,
    )
    formset = formset_class(
        data=None if readonly else data,
        files=None if readonly else files,
        instance=instance,
        queryset=inline.get_queryset(request, parent_model),
    )

    # Built once and reused. ``BaseFormSet.empty_form`` is a property that constructs a
    # fresh form on every access, so reading it three times would produce three objects —
    # and the one the choice sharing fixed up would not be the one rendered into the
    # <template>, leaving every added row with an empty relation picker.
    empty_form = formset.empty_form

    _share_choices([*formset.forms, empty_form])
    if rules.readonly:
        for child_form in [*formset.forms, empty_form]:
            for name in rules.readonly:
                if name in child_form.fields:
                    child_form.fields[name].disabled = True
    if readonly:
        _disable(formset)

    initial = formset.initial_form_count()
    return BoundInline(
        inline=inline,
        formset=formset,
        layout=inline.get_layout(parent_model),
        columns=tuple(_columns(empty_form, names)),
        rows=tuple(
            _row(row_form, names, extra=index >= initial)
            for index, row_form in enumerate(formset.forms)
        ),
        empty_row=_row(empty_form, names, extra=True) if may_add else None,
        can_add=may_add,
        can_delete=may_delete,
        readonly=readonly,
    )


def _formset_class(inline: Inline, parent_model, *, fields, extra, can_delete, edit_only):
    """The ``inlineformset_factory`` call, with the parameters that are easy to forget.

    ``formfield_callback`` is passed explicitly rather than inherited from a form class
    built by :func:`~vantage.widgets.build_form_class`. ``modelform_factory`` puts
    ``formfield_callback`` into the new class's attrs with the value it was given, and
    ``ModelFormMetaclass`` pops that key before it looks for an inherited one — so a
    callback carried on the base form's ``Meta`` is dropped, silently, and every control
    reverts to Django's 2005-era defaults. Passing it here is the path that cannot lose it.

    ``can_delete_extra=False`` because a blank row nobody has typed into does not need a
    "delete" checkbox; the browser removes it from the DOM. ``absolute_max`` is explicit
    for the reason given on :data:`ABSOLUTE_MAX`.
    """
    max_num = inline.max_num
    return inlineformset_factory(
        parent_model,
        inline.model,
        fk_name=resolve_fk(parent_model, inline.model, inline.fk_name).name,
        fields=fields,
        extra=extra,
        min_num=max(int(inline.min_num), 0),
        max_num=max_num,
        validate_max=max_num is not None,
        validate_min=inline.min_num > 0,
        absolute_max=ABSOLUTE_MAX if max_num is None else min(max_num + 100, ABSOLUTE_MAX),
        can_delete=can_delete,
        can_delete_extra=False,
        edit_only=edit_only,
        formfield_callback=_formfield_callback,
    )


def _columns(reference, names: list[str]):
    """Header cells, taken off the empty form so a formset with no rows still has them."""
    for name in names:
        field = reference.fields.get(name)
        if field is None:
            continue
        yield {
            "name": name,
            "label": str(field.label or name),
            "required": bool(field.required),
            "help_text": str(field.help_text or ""),
        }


def _row(form, names: list[str], *, extra: bool) -> InlineRow:
    cells = tuple(form[name] for name in names if name in form.fields)
    delete = form["DELETE"] if "DELETE" in form.fields else None
    label = "" if extra or form.instance.pk is None else str(form.instance)
    return InlineRow(
        form=form,
        cells=cells,
        hidden=tuple(form.hidden_fields()),
        delete=delete,
        is_extra=extra,
        label=label,
    )


def _share_choices(forms_to_share) -> None:
    """Resolve each relation field's options once and hand every row the same list.

    This is the difference between an inline that costs a constant number of queries and
    one that costs a query per row per relation. ``ModelChoiceField.__deepcopy__`` does
    ``result.queryset = self.queryset.all()`` — a deliberate re-clone, so that a fresh
    ``ModelChoiceIterator`` gets built and Django ticket #11183 stays fixed — and
    ``_set_queryset`` calls ``.all()`` again on assignment. Between them there is no way
    to hand two form instances a queryset that shares a result cache, so twenty rows with
    a ``product`` select is twenty identical ``SELECT * FROM product``.

    The options are rebuilt here from a **bounded** probe rather than by materialising the
    queryset, because an inline may point at a table with fifty thousand rows in it and
    "one query instead of twenty" is no comfort if that one query is fifty thousand rows.
    Above the threshold the field is left exactly as it was: :class:`~vantage.widgets.SearchSelect`
    then truncates it per row and marks it up for the combobox, which is the behaviour a
    relation that size actually needs, at one bounded query per row.

    Below the threshold the reconstruction is exactly what ``ModelChoiceIterator`` would
    have yielded — the empty label, then ``iterator.choice(obj)`` per row — and
    ``SearchSelect`` reaches the same "not searchable" conclusion it would have reached
    itself, because that is what being under the threshold means.

    Validation is untouched either way. ``to_python`` still resolves the submitted value
    through ``self.queryset``; a rendered option list has never been what authorises a
    value, so shortening one cannot widen what is accepted.
    """
    shared: dict[str, list | None] = {}
    for form in forms_to_share:
        for name, field in form.fields.items():
            if not isinstance(field, forms.ModelChoiceField):
                continue
            if name not in shared:
                shared[name] = _bounded_choices(field)
            options = shared[name]
            if options is not None:
                field.choices = options


def _bounded_choices(field) -> list | None:
    """The field's options as a plain list, or ``None`` if there are too many to share."""
    iterator = field.choices
    queryset = getattr(iterator, "queryset", None)
    if queryset is None:  # pragma: no cover - a panel supplied a plain choice list
        return None
    rows = list(queryset[: RELATION_SEARCH_THRESHOLD + 1])
    if len(rows) > RELATION_SEARCH_THRESHOLD:
        return None
    options: list = []
    if getattr(field, "empty_label", None) is not None:
        options.append(("", field.empty_label))
    options.extend(iterator.choice(obj) for obj in rows)
    return options


def _disable(formset) -> None:
    """Make a view-only inline inert, not merely uninviting.

    ``Field.disabled`` renders the control disabled *and* makes ``BoundField.value()``
    ignore submitted data in favour of the initial value, so the field cannot be written
    even if the POST carries it. The formset is never bound or saved for a readonly inline
    either; this is the second lock.
    """
    for form in formset.forms:
        for field in form.fields.values():
            field.disabled = True


# ── validation and saving ───────────────────────────────────────────────────


def all_valid(groups: list[BoundInline]) -> bool:
    """Whether every editable inline validated.

    The list comprehension is not an accident: ``all()`` over a generator stops at the
    first failure, and the formsets it never touched would render with no errors on them.
    The user would fix the one complaint shown, resubmit, and be handed the next one.
    """
    results = [group.formset.is_valid() for group in groups if not group.readonly]
    return all(results)


def save(request, parent, groups: list[BoundInline]) -> None:
    """Write every inline's changes. Must be called inside the parent's transaction.

    Deliberately not opening its own ``atomic`` block: the point is that the children and
    the parent share one, so a child that fails here takes the parent's row with it.
    :meth:`~vantage.formviews.PanelFormView.save_form` owns that block and calls this
    from inside it.

    ``commit=False`` first so each write can be checked against the row it is about to
    touch. That is the only place an object-level permission is consulted — once per row
    actually being written, never once per row rendered — and it short-circuits to the
    model-level answer entirely when no backend can answer an object-level question.
    """
    for group in groups:
        if group.readonly:
            continue
        formset = group.formset
        # Reattached rather than assumed: on a create page the formset was built against
        # an unsaved instance, and ``BaseInlineFormSet.save_new`` reads ``self.instance``
        # to set the foreign key on every new child.
        formset.instance = parent

        # ``save(commit=False)`` reports which rows are new and which changed, and — the
        # part that matters here — leaves the deletions un-run so they can be checked.
        # New rows are collected before the deletions so an ``add`` that a permission
        # refuses aborts before anything has been destroyed.
        instances = formset.save(commit=False)

        for obj in instances:
            action = "change" if obj.pk else "add"
            if not group.inline.allows(request, action, obj):
                raise _denied(group, action)
            obj.save()
        formset.save_m2m()

        if not group.can_delete:
            if formset.deleted_objects:  # pragma: no cover - no DELETE field is rendered
                raise _denied(group, "delete")
            continue

        for obj in formset.deleted_objects:
            if not group.inline.allows(request, "delete", obj):
                raise _denied(group, "delete")
            # Stashed before the delete because ``Model.delete()`` blanks the pk, and an
            # audit entry written afterwards would describe a record with no identity —
            # the same reason ``DeleteView`` logs before it deletes. The entry itself is
            # written by :func:`log`, after this transaction commits.
            group.deleted.append((obj, obj.pk))
            obj.delete()


def _denied(group: BoundInline, action: str):
    from django.core.exceptions import PermissionDenied

    return PermissionDenied(f"You do not have permission to {action} this {group.label.lower()}.")


def log(request, groups: list[BoundInline]) -> None:
    """Audit every child write. Called *after* the transaction commits, never inside it.

    An exception raised by the audit layer inside an atomic block marks that block for
    rollback even when it is caught, so logging a write from inside the transaction is a
    way to destroy the write being recorded. Same rule, same reason, as the parent's own
    entry in :meth:`~vantage.formviews.PanelFormView.save_form`.
    """
    from vantage import audit

    for group in groups:
        if group.readonly:
            continue
        formset = group.formset
        saved = getattr(formset, "saved_forms", ())
        initial = formset.initial_form_count()
        for index, form in enumerate(formset.forms):
            if form in saved:
                audit.log_form_save(request, form.instance, form, index < initial)
        for obj, pk in group.deleted:
            # The pk was blanked by ``Model.delete()``. Put it back on the in-memory
            # instance — which exists only to be described — so the entry names a record
            # rather than a ``None``.
            obj.pk = pk
            audit.log_delete(request, obj)
