"""Adopting an existing ``django.contrib.admin`` configuration.

This is the migration path. A project with two hundred models and four years of
``admin.py`` will not rewrite it to try a new panel, so Vantage reads what is already
there::

    # project/panel.py
    from vantage import site
    from vantage.contrib.django_admin import adopt_admin_site

    adopt_admin_site(site)

Every model registered with ``contrib.admin`` now has a Vantage panel carrying its
columns, filters, search fields, ordering, page size, form fields, actions and
permission methods. Nothing in ``admin.py`` changes, and both panels keep working
side by side — which matters, because the honest way to evaluate a replacement is to
run it next to the thing it replaces.

**What this module refuses to do is pretend.** ``ModelAdmin`` has a large surface and
some of it has no equivalent here — inlines, ``raw_id_fields``,
custom ``get_urls``, media, and the view-override hooks. Those are *reported*, per
model, through :func:`check_admin_adapter`, so ``manage.py check`` lists exactly what
did not come across. A silent partial translation is worse than no translation: it
looks finished, and the missing half is discovered by a user who needed it.

Only what the admin actually declared is translated. An attribute left at its
``ModelAdmin`` default stays *unset* on the panel, so Vantage's inference still runs —
adopting an admin that only set ``search_fields`` should not cost you inferred columns,
filters and widgets everywhere else.

``contrib.admin`` stays an optional dependency: nothing here is imported by the core,
and :func:`adopt_admin_site` soft-detects with ``apps.is_installed`` rather than
assuming.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.apps import apps as django_apps
from django.core.checks import Error, Warning
from django.core.exceptions import ImproperlyConfigured

from vantage.actions import Action, ActionResult
from vantage.introspection import resolve_path
from vantage.panels import ModelPanel

__all__ = [
    "Adoption",
    "admin_installed",
    "adopt_admin_site",
    "adoptions",
    "check_admin_adapter",
    "from_admin",
    "panel_from_admin",
]

logger = logging.getLogger("vantage.contrib.admin")


# ── what does not come across ───────────────────────────────────────────────
#
# Declared as data rather than discovered, because the point of the list is that a
# human wrote down what each thing would have meant. "inlines" reported as a bare
# attribute name tells a reader nothing about what they have to do next.

#: Attributes whose presence means real configuration was left behind.
UNMAPPED_ATTRS: dict[str, str] = {
    "inlines": "inline formsets — edit the related model from its own page, or add it "
    "as a related tab",
    "raw_id_fields": "raw id inputs — Vantage uses a searching select for relations",
    "autocomplete_fields": "autocomplete selects — Vantage's relation widget already "
    "searches, so this is usually a no-op",
    "filter_horizontal": "the horizontal m2m widget",
    "filter_vertical": "the vertical m2m widget",
    "prepopulated_fields": "JavaScript slug prepopulation",
    "radio_fields": "radio inputs in place of a select",
    "form": "a custom ModelForm — Vantage builds its own form from the panel",
    "add_form_template": "a custom add template",
    "change_form_template": "a custom change template",
    "change_list_template": "a custom changelist template",
    "delete_confirmation_template": "a custom delete template",
    "object_history_template": "a custom history template",
    "paginator": "a custom paginator class",
    "sortable_by": "a restricted sortable column set",
    "save_as": "the 'save as new' button",
    "save_on_top": "the duplicated top submit row",
    "view_on_site": "the 'view on site' link",
    "empty_value_display": "a custom empty-value marker",
    "show_full_result_count": "the unfiltered total in the result count",
    "list_max_show_all": "the 'show all' ceiling",
    "preserve_filters": "filter preservation across save — Vantage always preserves it",
    "actions_on_top": "action-bar placement — Vantage's bar is fixed to the selection",
    "actions_on_bottom": "action-bar placement — Vantage's bar is fixed to the selection",
    "actions_selection_counter": "the selection counter — Vantage always shows one",
}

#: Methods whose override means behaviour lives in Python that this cannot read.
UNMAPPED_METHODS: dict[str, str] = {
    "get_urls": "extra URLs hung off the admin — register a Page on the Vantage site",
    "get_form": "a custom form",
    "get_fieldsets": "fieldsets computed per request — only the declared ones are read",
    "get_list_display": "columns computed per request — override the panel's own "
    "get_list_display instead",
    "get_search_results": "custom search",
    "get_actions": "actions computed per request — only the declared ones are read",
    "get_inline_instances": "inlines computed per request",
    "formfield_for_foreignkey": "per-field relation querysets",
    "formfield_for_manytomany": "per-field m2m querysets",
    "formfield_for_choice_field": "per-field choice overrides",
    "lookup_allowed": "a custom filter allowlist",
    "changelist_view": "a custom list view",
    "change_view": "a custom change view",
    "add_view": "a custom add view",
    "delete_view": "a custom delete view",
    "history_view": "a custom history view",
    "response_add": "a custom post-add response",
    "response_change": "a custom post-change response",
    "render_change_form": "custom change-form rendering",
    "save_related": "custom related-object saving",
    "has_module_permission": "app-level visibility — Vantage gates on the model",
}

#: Unmapped methods that *restrict* what someone may see or choose. Reported as an
#: error (``vantage.E041``), not a warning: the panel would offer more than the admin
#: did. Handle the restriction on the panel (or in a ``PanelPermissions`` subclass),
#: then silence the check with ``SILENCED_SYSTEM_CHECKS = ["vantage.E041"]``.
RESTRICTIVE_METHODS: frozenset[str] = frozenset(
    {
        "formfield_for_foreignkey",
        "formfield_for_manytomany",
        "formfield_for_choice_field",
        "get_form",
        "has_module_permission",
    }
)

#: Methods that *do* come across, because the signature is the same on both sides.
DELEGATED_METHODS: tuple[str, ...] = ("get_queryset", "save_model")

#: The four permission methods. ``add`` is listed separately below because its
#: signature has no ``obj``.
PERMISSION_VERBS: tuple[str, ...] = ("view", "add", "change", "delete")


@dataclass(frozen=True)
class Adoption:
    """One model's translation: the panel that came out, and what did not."""

    model: type
    admin_class: type
    panel_class: type[ModelPanel]
    unmapped: tuple[str, ...] = ()
    #: The restrictive hooks among ``unmapped`` (see :data:`RESTRICTIVE_METHODS`).
    refused: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.model._meta.label


#: Every adoption made so far, so the system check can report on all of them. Keyed by
#: model label rather than by model, so re-adopting replaces rather than accumulates.
_adoptions: dict[str, Adoption] = {}


def adoptions() -> list[Adoption]:
    return [_adoptions[key] for key in sorted(_adoptions)]


def reset() -> None:
    """Forget every adoption. For tests."""
    _adoptions.clear()


# ── detection ───────────────────────────────────────────────────────────────


def admin_installed() -> bool:
    """Whether ``django.contrib.admin`` is in ``INSTALLED_APPS``.

    ``is_installed`` rather than an import guard: the module imports perfectly well in
    a project that does not use it, and importing it to find out would create the
    dependency this function exists to avoid.
    """
    return django_apps.is_installed("django.contrib.admin")


# ── translation ─────────────────────────────────────────────────────────────


def panel_from_admin(
    model: type,
    admin: Any,
    *,
    base: type[ModelPanel] = ModelPanel,
    name: str | None = None,
) -> Adoption:
    """Build a :class:`~vantage.panels.ModelPanel` subclass from a ``ModelAdmin``.

    ``admin`` may be the class or an instance. An instance is preferable and is what
    :func:`adopt_admin_site` passes, because a registered admin has already been given
    its model and its site, and its overridden methods can then simply be called.
    """
    admin_obj = _as_instance(admin, model)
    attrs, unmapped = _translate(model, admin_obj)
    date_field = attrs.pop("_date_hierarchy", None)
    panel_class = type(name or f"{model.__name__}AdminPanel", (base,), attrs)
    if date_field:
        _install_date_filter(panel_class, date_field)
    adoption = Adoption(
        model=model,
        admin_class=type(admin_obj),
        panel_class=panel_class,
        unmapped=tuple(unmapped),
        refused=tuple(sorted(name for name in RESTRICTIVE_METHODS if _overrides(admin_obj, name))),
    )
    _adoptions[adoption.label] = adoption
    return adoption


def from_admin(admin: Any, model: type | None = None, *, name: str | None = None):
    """Class decorator translating one ``ModelAdmin`` onto one panel::

        @register(Post)
        @from_admin(PostAdmin)
        class PostPanel(ModelPanel):
            icon = "file-text"      # anything declared here wins

    Attributes the decorated class sets itself are never overwritten. That is the whole
    ergonomic point: adopt the admin wholesale, then correct the two things you always
    disliked about it, without forking the translation.

    ``model`` is only needed when the ``ModelAdmin`` is not registered with
    ``contrib.admin`` — a registered one already knows which model it is for.
    """

    def wrap(panel_class: type[ModelPanel]) -> type[ModelPanel]:
        target = model or getattr(panel_class, "model", None) or _model_for_admin(admin)
        adoption = panel_from_admin(target, admin, base=ModelPanel, name=name)
        for key, value in vars(adoption.panel_class).items():
            if key.startswith("__") or key in panel_class.__dict__:
                continue
            setattr(panel_class, key, value)
        # The adoption record has to name the class the project will actually see, or
        # the check output points at a class nobody wrote.
        _adoptions[adoption.label] = Adoption(
            model=target,
            admin_class=adoption.admin_class,
            panel_class=panel_class,
            unmapped=adoption.unmapped,
            refused=adoption.refused,
        )
        return panel_class

    return wrap


def adopt_admin_site(site=None, admin_site: Any = None, *, replace: bool = False) -> list[Adoption]:
    """Register a Vantage panel for every model registered with ``contrib.admin``.

    Models already registered with Vantage are skipped unless ``replace=True``: a
    hand-written ``ModelPanel`` is a later, more deliberate statement than the
    ``admin.py`` it was written to replace, and silently overwriting it would make
    adoption a one-way door.

    With no ``admin_site`` this uses ``django.contrib.admin.site``, and returns an
    empty list — loudly, in the log — when ``contrib.admin`` is not installed. Passing
    an ``AdminSite`` explicitly skips the detection, which is what makes the adapter
    testable and usable with a custom admin site.
    """
    from vantage.sites import site as default_site

    site = site or default_site

    if admin_site is None:
        if not admin_installed():
            logger.warning(
                "vantage.contrib.django_admin.adopt_admin_site() was called but "
                "'django.contrib.admin' is not in INSTALLED_APPS; nothing was adopted."
            )
            return []
        from django.contrib import admin as contrib_admin

        admin_site = contrib_admin.site

    adopted: list[Adoption] = []
    # Sorted so the panels register in a stable order whatever the admin's dict order,
    # which keeps the nav from reshuffling between deploys.
    registry = sorted(admin_site._registry.items(), key=lambda pair: pair[0]._meta.label)
    for model, admin_obj in registry:
        if site.is_registered(model):
            if not replace:
                continue
            site.unregister(model)
        adoption = panel_from_admin(model, admin_obj)
        site.register(model, adoption.panel_class)
        adopted.append(adoption)
    return adopted


def _translate(model: type, admin_obj: Any) -> tuple[dict[str, Any], list[str]]:
    """The whole translation. Returns the panel's class attributes and what was left."""
    attrs: dict[str, Any] = {"_admin": admin_obj}
    unmapped: list[str] = []

    columns, column_methods, link = _translate_display(model, admin_obj, unmapped)
    attrs.update(column_methods)
    if columns:
        attrs["list_display"] = columns
    if link:
        attrs["list_link"] = link

    filters = _translate_list_filter(model, admin_obj, unmapped)
    if filters:
        attrs["list_filter"] = filters

    if search := _declared(admin_obj, "search_fields"):
        attrs["list_search"] = [str(entry) for entry in search]

    if ordering := _declared(admin_obj, "ordering"):
        attrs["ordering"] = list(ordering)

    if per_page := _declared(admin_obj, "list_per_page"):
        attrs["list_per_page"] = int(per_page)

    if (related := _declared(admin_obj, "list_select_related")) is not None:
        # True means "the admin worked it out"; Vantage works it out too, and from
        # what the page actually renders, so the answer is at least as good.
        attrs["list_select_related"] = None if related is True else list(related)

    if editable := _declared(admin_obj, "list_editable"):
        attrs["list_editable"] = [name for name in editable if name != link]

    _translate_form(model, admin_obj, attrs, unmapped)
    _translate_formfield_overrides(admin_obj, attrs)
    _translate_date_hierarchy(admin_obj, attrs)
    _translate_actions(admin_obj, attrs, unmapped)
    _translate_permissions(admin_obj, attrs)
    _translate_delegates(admin_obj, attrs)

    unmapped.extend(_unmapped_attrs(admin_obj))
    unmapped.extend(_unmapped_methods(admin_obj))
    return attrs, unmapped


# ── columns ─────────────────────────────────────────────────────────────────


def _translate_display(model, admin_obj, unmapped) -> tuple[list[str], dict, str | None]:
    """``list_display`` plus whatever methods it needs carried across.

    An admin column is often a method on the ``ModelAdmin``, and the panel is a
    different object, so the method is wrapped rather than copied: it keeps running
    against the admin instance it was written for, with whatever else that instance
    holds.
    """
    declared = _declared(admin_obj, "list_display")
    methods: dict[str, Any] = {}
    if not declared:
        return [], methods, _translate_link(model, admin_obj, None, methods)

    columns: list[str] = []
    for entry in declared:
        if entry == "action_checkbox":
            continue  # Vantage draws its own selection column.
        name = _column_name(model, entry, methods, admin_obj, unmapped)
        if name:
            columns.append(name)

    return columns, methods, _translate_link(model, admin_obj, columns, methods)


def _column_name(model, entry, methods: dict, admin_obj, unmapped) -> str | None:
    if callable(entry):
        # A bare function taking the object. Vantage calls panel methods as
        # ``method(obj)``, so a one-line binding is the whole adaptation.
        name = getattr(entry, "__name__", None)
        if not name:
            unmapped.append("list_display: an anonymous callable column")
            return None
        methods[name] = _bind_display(entry, takes_admin=False)
        _note_short_description(entry, name, unmapped)
        return name

    name = str(entry)
    if name == "__str__":
        return _str_column(model, methods)
    if resolve_path(model, name):
        return name

    attribute = getattr(type(admin_obj), name, None)
    if callable(attribute):
        methods[name] = _bind_display(attribute, takes_admin=True)
        _note_short_description(attribute, name, unmapped)
        return name

    # A model attribute or property. Vantage resolves those itself.
    return name


def _bind_display(func, *, takes_admin: bool):
    def column(self, obj):
        return func(self._admin, obj) if takes_admin else func(obj)

    column.__name__ = getattr(func, "__name__", "column")
    return column


def _str_column(model, methods: dict) -> str:
    """``"__str__"`` as a real column name.

    ``getattr(panel, "__str__")`` is the panel's own ``__str__``, which takes no
    argument, so the name cannot be passed through unchanged. The model name is used
    instead, because it is what the column header should say anyway.

    The collision checks are not paranoia. A model called ``Order`` would otherwise
    define a column named ``order`` — which is also ``ModelPanel.order``, the nav sort
    weight — and the panel would appear to work right up until the sidebar tried to
    sort a bound method.
    """
    stem = model._meta.model_name
    for candidate in (stem, f"{stem}_display", f"{stem}_str"):
        if resolve_path(model, candidate) or hasattr(ModelPanel, candidate):
            continue
        if candidate in methods:
            return candidate
        name = candidate
        break
    else:  # pragma: no cover - three collisions on one model
        name = f"{stem}_str_column"

    def column(self, obj):
        return str(obj)

    # A marker, so ``vantage_adopt`` can write this column out as ``str(obj)`` instead
    # of treating it as a method it cannot read.
    column.vantage_str_column = True  # type: ignore[attr-defined]
    methods[name] = column
    return name


def _note_short_description(func, name: str, unmapped: list[str]) -> None:
    description = getattr(func, "short_description", None)
    if description:
        unmapped.append(
            f"short_description {description!r} on the {name!r} column — the header "
            f"falls back to the method name"
        )


def _translate_link(model, admin_obj, columns, methods) -> str | None:
    links = _declared(admin_obj, "list_display_links")
    if not links:
        return None
    first = links[0]
    name = _str_column(model, methods) if first == "__str__" else str(first)
    # E014: the linked column has to be one that is actually displayed.
    if columns and name not in columns:
        return None
    return name


# ── filters, form, dates ────────────────────────────────────────────────────


def _translate_list_filter(model, admin_obj, unmapped) -> list[str]:
    declared = _declared(admin_obj, "list_filter")
    if not declared:
        return []

    kept: list[str] = []
    for entry in declared:
        if isinstance(entry, str) and resolve_path(model, entry):
            kept.append(entry)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            path, filter_class = entry
            # The field still filters; only the custom UI class is lost.
            if isinstance(path, str) and resolve_path(model, path):
                kept.append(path)
            unmapped.append(
                f"list_filter: the {_name_of(filter_class)} filter class on {path!r} — "
                f"the field is still filterable, with Vantage's own control"
            )
        else:
            unmapped.append(
                f"list_filter: the custom filter {_name_of(entry)} — a SimpleListFilter "
                f"has no Vantage equivalent; express it as a field or a queryset scope"
            )
    return kept


def _translate_form(model, admin_obj, attrs: dict, unmapped: list) -> None:
    """``fields``, ``exclude``, ``readonly_fields`` and ``fieldsets``."""
    if fields := _declared(admin_obj, "fields"):
        attrs["fields"] = _flatten(fields)

    if exclude := _declared(admin_obj, "exclude"):
        attrs["exclude"] = list(exclude)

    if readonly := _declared(admin_obj, "readonly_fields"):
        attrs["readonly_fields"] = [str(name) for name in readonly]
        for name in readonly:
            if not resolve_path(model, str(name)):
                unmapped.append(
                    f"readonly_fields: {str(name)!r} is a computed value, not a field — "
                    f"it is excluded from the form but not displayed"
                )

    if fieldsets := _declared(admin_obj, "fieldsets"):
        attrs["layout"] = _layout_from_fieldsets(fieldsets)

    _translate_dynamic_form(model, admin_obj, attrs)


#: Per-request editability hooks, carried across as field rules rather than dropped.
DYNAMIC_FORM_METHODS: tuple[str, ...] = ("get_readonly_fields", "get_exclude", "get_fields")


def _translate_dynamic_form(model, admin_obj, attrs: dict) -> None:
    """``get_readonly_fields``/``get_exclude``/``get_fields`` become read-only field rules.

    These decide, per request, what a person may edit — a restriction, so silently
    dropping them would widen access on adoption. They are answered through the
    panel's ``get_field_access(request)`` hook, which every write path already obeys
    (forms, inline editing, bulk edit, import, the API). Asked with ``obj=None``: the
    rules are per model, not per row. A field the admin's form leaves out becomes
    read-only rather than hidden, so nothing the admin displayed disappears.
    """
    overridden = [name for name in DYNAMIC_FORM_METHODS if _overrides(admin_obj, name)]
    if not overridden:
        return
    from vantage.fieldperms import FieldAccess, rule_fields

    known = [f.name for f in rule_fields(model)]

    def get_field_access(self, request):
        admin = self._admin
        locked: set[str] = set()
        if "get_readonly_fields" in overridden:
            locked |= {str(n) for n in admin.get_readonly_fields(request) or ()}
        if "get_exclude" in overridden:
            locked |= {str(n) for n in admin.get_exclude(request) or ()}
        if "get_fields" in overridden:
            offered = set(_flatten(admin.get_fields(request) or ()))
            locked |= {name for name in known if name not in offered}
        return FieldAccess(readonly=frozenset(n for n in locked if n in known))

    attrs["get_field_access"] = get_field_access


def _translate_formfield_overrides(admin_obj, attrs: dict) -> None:
    """``formfield_overrides`` maps one-to-one onto ``ModelPanel.formfield_overrides``.

    Read from the *class*, not the instance: ``ModelAdmin.__init__`` merges contrib.admin's
    own widget defaults (the admin date picker, the admin file input) into the instance
    dict, and carrying those across would swap Vantage's controls for admin-styled ones.
    """
    declared = getattr(type(admin_obj), "formfield_overrides", None)
    base = getattr(_admin_base(), "formfield_overrides", None)
    if declared and declared != base:
        attrs["formfield_overrides"] = dict(declared)


def _layout_from_fieldsets(fieldsets):
    """Fieldsets are already Vantage's layout DSL, spelled differently.

    ``(name, {"fields": (...), "classes": (...)})`` is a ``Section``, a nested tuple
    inside ``fields`` is a ``Row``, and ``classes=("collapse",)`` is ``collapsed=True``.
    Translating it keeps the form's shape, which is usually the part of an admin that
    somebody actually spent time on.
    """
    from vantage.layout import Row, Section

    sections = []
    for title, options in fieldsets:
        children: list[Any] = []
        for entry in options.get("fields", ()):
            if isinstance(entry, (list, tuple)):
                children.append(Row(*entry))
            else:
                children.append(entry)
        sections.append(
            Section(
                *children,
                title=str(title) if title else None,
                description=options.get("description"),
                collapsed="collapse" in (options.get("classes") or ()),
            )
        )
    return sections[0] if len(sections) == 1 else Section(*sections)


def _translate_date_hierarchy(admin_obj, attrs: dict) -> None:
    """The date drill-down becomes a date filter.

    Not the same control, and the report says so — but the capability it exists for
    (narrow this list to a period) is one Vantage's date filter already has.
    """
    date_field = _declared(admin_obj, "date_hierarchy")
    if date_field:
        attrs["_date_hierarchy"] = str(date_field)


def _install_date_filter(panel_class, date_field: str) -> None:
    """Add the date field to the panel's filters without freezing the others.

    Through ``get_list_filter`` rather than by writing ``list_filter``, so a panel that
    inherited its filters from inference keeps inferring them — the one rule that must
    never be broken by a partial override.
    """

    def get_list_filter(self, request=None):
        filters = super(panel_class, self).get_list_filter(request)
        return filters if date_field in filters else [*filters, date_field]

    panel_class.get_list_filter = get_list_filter


# ── actions ─────────────────────────────────────────────────────────────────


def _translate_actions(admin_obj, attrs: dict, unmapped: list) -> None:
    declared = _declared(admin_obj, "actions")
    if not declared:
        return

    built: list[Action] = []
    for entry in declared:
        if entry == "delete_selected":
            # Vantage ships its own bulk delete, with a confirmation step and a row cap.
            continue
        action = _action_from_admin(admin_obj, entry, unmapped)
        if action is not None:
            built.append(action)
    if built:
        attrs["actions"] = built


def _action_from_admin(admin_obj, entry, unmapped) -> Action | None:
    """One admin action as a Vantage :class:`~vantage.actions.Action`.

    The signatures line up almost exactly — ``(modeladmin, request, queryset)`` against
    ``(panel, request, queryset)`` — so the admin instance is passed through as the
    first argument rather than the panel. An action calling ``modeladmin.message_user``
    then still works, which is what most of them do.
    """
    if callable(entry):
        func, bound = entry, False
        name = getattr(entry, "__name__", "action")
    else:
        name = str(entry)
        func = getattr(admin_obj, name, None)
        bound = True
        if func is None:
            unmapped.append(f"actions: {name!r} is not a method on the admin")
            return None

    label = str(getattr(func, "short_description", None) or name.replace("_", " ").capitalize())
    permission = _action_permission(func, name, unmapped)

    def run(panel, request, queryset):
        outcome = func(request, queryset) if bound else func(admin_obj, request, queryset)
        return _action_result(label, outcome)

    run.__name__ = name
    return Action(func=run, name=name, label=label, permission=permission)


def _action_permission(func, name: str, unmapped: list) -> str | None:
    """``@admin.action(permissions=[...])`` maps only for the four standard verbs.

    A custom permission name means the admin has a ``has_<name>_permission`` method,
    which is a hook Vantage does not call. Downgrading to "change" silently would grant
    the action to people the admin denied it to, so it is reported instead.
    """
    allowed = getattr(func, "allowed_permissions", None)
    if not allowed:
        return "change"
    first = str(allowed[0])
    if first in PERMISSION_VERBS:
        return first
    unmapped.append(
        f"actions: the custom permission {first!r} on {name!r} — the action requires "
        f"'change' instead"
    )
    return "change"


def _action_result(label: str, outcome) -> ActionResult:
    """Admin actions may return an ``HttpResponse``; Vantage actions return a message."""
    from django.http import HttpResponse

    if isinstance(outcome, ActionResult):
        return outcome
    if isinstance(outcome, HttpResponse):
        location = outcome.headers.get("Location") if hasattr(outcome, "headers") else None
        if location:
            return ActionResult(f"{label} completed.", redirect=location)
        return ActionResult(
            f"{label} returned a page, which Vantage cannot show from an action.",
            level="warning",
        )
    if outcome is None:
        # The admin convention: the action already called message_user.
        return ActionResult(f"{label} completed.")
    return ActionResult(str(outcome))


# ── permissions and delegated hooks ─────────────────────────────────────────


def _translate_permissions(admin_obj, attrs: dict) -> None:
    """``has_*_permission`` becomes the same method on the panel.

    :meth:`vantage.permissions.PanelPermissions.panel_override` consults these before
    the superuser bypass, matching ``contrib.admin``: an admin that denies deletion
    denies it to everyone, and adoption must not quietly widen that.
    """
    for verb in PERMISSION_VERBS:
        hook = f"has_{verb}_permission"
        if not _overrides(admin_obj, hook):
            continue
        attrs[hook] = _bind_permission(hook, takes_obj=verb != "add")


def _bind_permission(hook: str, *, takes_obj: bool):
    def check(self, request, obj=None):
        method = getattr(self._admin, hook)
        return bool(method(request, obj) if takes_obj else method(request))

    check.__name__ = hook
    return check


def _translate_delegates(admin_obj, attrs: dict) -> None:
    """Hooks whose signature is identical on both sides, so they can just be called."""
    for name in DELEGATED_METHODS:
        if _overrides(admin_obj, name):
            attrs[name] = _bind_delegate(name)


def _bind_delegate(name: str):
    def delegate(self, *args, **kwargs):
        return getattr(self._admin, name)(*args, **kwargs)

    delegate.__name__ = name
    return delegate


# ── the honest report ───────────────────────────────────────────────────────


def _unmapped_attrs(admin_obj) -> list[str]:
    return [f"{attr} ({why})" for attr, why in UNMAPPED_ATTRS.items() if _declared(admin_obj, attr)]


def _unmapped_methods(admin_obj) -> list[str]:
    found = [
        f"{name}() ({why})" for name, why in UNMAPPED_METHODS.items() if _overrides(admin_obj, name)
    ]
    if _has_media(admin_obj):
        found.append("Media (extra CSS/JS — add it with VANTAGE['CUSTOM_CSS'])")
    return found


def check_admin_adapter(app_configs, **kwargs) -> list:
    """List, per model, exactly what the adapter could not translate.

    A warning rather than an error: the panel works, and the project chose adoption
    knowing it is a translation. But it is reported every time ``manage.py check``
    runs, because the alternative — finding out from a user that the inlines are gone —
    is how a migration path loses trust.
    """
    labels = None
    if app_configs is not None:
        labels = {config.label for config in app_configs}

    messages = []
    for adoption in adoptions():
        if labels is not None and adoption.model._meta.app_label not in labels:
            continue
        if adoption.refused:
            messages.append(
                Error(
                    f"{adoption.admin_class.__name__} restricts access in a way the "
                    f"adapter cannot carry across to {adoption.label}: "
                    f"{', '.join(f'{name}()' for name in adoption.refused)}.",
                    hint=(
                        "Without it the panel offers more than the admin did. Restrict the "
                        "same thing on the panel (or in a PanelPermissions subclass), then "
                        'add "vantage.E041" to SILENCED_SYSTEM_CHECKS.'
                    ),
                    obj=adoption.panel_class,
                    id="vantage.E041",
                )
            )
        if not adoption.unmapped:
            continue
        listed = "\n".join(f"  · {item}" for item in adoption.unmapped)
        messages.append(
            Warning(
                f"{adoption.admin_class.__name__} was adopted for {adoption.label}, but "
                f"{len(adoption.unmapped)} setting(s) could not be translated.",
                hint=f"Not carried across:\n{listed}",
                obj=adoption.panel_class,
                id="vantage.W040",
            )
        )
    return messages


# ── small helpers ───────────────────────────────────────────────────────────

_MISSING = object()


def _admin_base():
    from django.contrib.admin import ModelAdmin

    return ModelAdmin


def _declared(admin_obj, attr: str):
    """The value of ``attr`` if the admin actually set it, else ``None``.

    Comparing against the ``ModelAdmin`` default is what keeps inference alive. Every
    admin has ``list_display = ("__str__",)``; copying that onto a panel would replace
    a table of inferred columns with a single column of ``str(obj)``, on every model,
    for every project that adopted — the worst possible first impression.

    Declaration is a question about the *class*, and asking the instance gets it wrong:
    ``ModelAdmin.__init__`` merges the global widget defaults into
    ``self.formfield_overrides``, so every admin alive looks like it customised them.
    The value still comes from the instance, so anything ``__init__`` computed is kept.
    """
    value = getattr(admin_obj, attr, _MISSING)
    if value is _MISSING:
        return None
    declared = getattr(type(admin_obj), attr, _MISSING)
    default = getattr(_admin_base(), attr, _MISSING)
    if _same(declared, default):
        return None
    return value


def _same(value, default) -> bool:
    if isinstance(value, (list, tuple)) and isinstance(default, (list, tuple)):
        return tuple(value) == tuple(default)
    try:
        return bool(value == default)
    except Exception:  # pragma: no cover - exotic __eq__
        return value is default


def _overrides(admin_obj, name: str) -> bool:
    """Whether the admin class replaced a ``ModelAdmin`` method."""
    mine = getattr(type(admin_obj), name, None)
    theirs = getattr(_admin_base(), name, None)
    return mine is not None and mine is not theirs


def _has_media(admin_obj) -> bool:
    return getattr(type(admin_obj), "Media", None) is not None


def _as_instance(admin: Any, model: type):
    """A ``ModelAdmin`` instance to read from — the class alone is not enough.

    No ``is_installed`` gate here, unlike :func:`adopt_admin_site`: the caller has
    handed over a ``ModelAdmin`` subclass, so ``contrib.admin`` is importable by
    construction. Only *discovering* the default admin site needs the app installed.
    """
    if not isinstance(admin, type):
        return admin
    from django.contrib.admin import AdminSite

    return admin(model, AdminSite())


def _model_for_admin(admin: Any) -> type:
    """Which model a ``ModelAdmin`` class configures.

    Only a *registered* admin knows: the class itself carries no model, which is why
    the answer has to come from the registry — and why the error, when it cannot, says
    exactly what to type instead.
    """
    if not isinstance(admin, type):
        return admin.model
    if admin_installed():
        from django.contrib import admin as contrib_admin

        for model, registered in contrib_admin.site._registry.items():
            if type(registered) is admin:
                return model
    raise ImproperlyConfigured(
        f"{admin.__name__} is not registered with django.contrib.admin, so the model it "
        f"configures cannot be worked out. Pass it: @from_admin({admin.__name__}, MyModel)."
    )


def _flatten(entries) -> list[str]:
    """``("title", ("a", "b"))`` -> ``["title", "a", "b"]``."""
    flat: list[str] = []
    for entry in entries:
        if isinstance(entry, (list, tuple)):
            flat.extend(str(item) for item in entry)
        else:
            flat.append(str(entry))
    return flat


def _name_of(value) -> str:
    return getattr(value, "__name__", None) or repr(value)
