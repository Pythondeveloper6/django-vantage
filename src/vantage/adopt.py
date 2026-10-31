"""The mapping report behind ``manage.py vantage_adopt``.

:mod:`vantage.contrib.django_admin` translates a ``ModelAdmin`` and lists what it left
behind as free text. That is enough for ``manage.py check``; it is not enough to plan a
migration of two hundred admins. This module turns the same translation into a
structured report — one row per attribute, each with one of three statuses — and into a
starter ``panel.py`` the project then owns.

The three statuses mean different things to the person reading them:

``translated``
    Carried across. Either a setting that has a direct Vantage counterpart, or one that
    needs no setting because Vantage does the job by default (the report says which).
``needs panel code``
    The behaviour exists in Vantage, but it lives in Python that a generator cannot
    safely rewrite: a column method, an action, a permission hook, a queryset hook. The
    generated ``panel.py`` leaves a ``TODO`` where the port goes.
``unsupported``
    Nothing in Vantage does this today. The report gives the reason, and the closest
    thing Vantage has when there is one.

Only the first two ever appear as code. Nothing here guesses: a ``panel.py`` that claims
more than the report does would be the migration-path equivalent of a silent failure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ImproperlyConfigured

from vantage.contrib import django_admin as adapter

__all__ = [
    "AttrReport",
    "ModelReport",
    "build_report",
    "render_app_module",
    "report_to_dict",
    "summarise",
]

TRANSLATED = "translated"
NEEDS_CODE = "needs panel code"
UNSUPPORTED = "unsupported"
STATUSES = (TRANSLATED, NEEDS_CODE, UNSUPPORTED)

#: ModelAdmin attribute -> the ModelPanel attribute it becomes.
DIRECT: dict[str, str] = {
    "list_display": "list_display",
    "list_display_links": "list_link",
    "list_filter": "list_filter",
    "search_fields": "list_search",
    "ordering": "ordering",
    "list_per_page": "list_per_page",
    "list_select_related": "list_select_related",
    "list_editable": "list_editable",
    "fields": "fields",
    "exclude": "exclude",
    "readonly_fields": "readonly_fields",
    "fieldsets": "layout",
    "formfield_overrides": "formfield_overrides",
    "date_hierarchy": "list_filter",
}

#: What each adapter-unmapped setting means for a migration, and what Vantage has instead.
#: ``(status, vantage_equivalent)``. Anything not listed here is ``unsupported`` with the
#: adapter's own explanation and no equivalent, which is the honest default.
KNOWN: dict[str, tuple[str, str]] = {
    "inlines": (NEEDS_CODE, "ModelPanel.inlines (declared by hand; see forms.md#editable-inlines)"),
    "raw_id_fields": (TRANSLATED, "built in: relations use a searching picker, no setting needed"),
    "autocomplete_fields": (TRANSLATED, "built in: the relation picker already searches"),
    "filter_horizontal": (TRANSLATED, "built in: many-to-many fields use the searching picker"),
    "filter_vertical": (TRANSLATED, "built in: many-to-many fields use the searching picker"),
    "preserve_filters": (TRANSLATED, "built in: filters are always preserved"),
    "actions_on_top": (TRANSLATED, "built in: the action bar follows the selection"),
    "actions_on_bottom": (TRANSLATED, "built in: the action bar follows the selection"),
    "actions_selection_counter": (TRANSLATED, "built in: the selection counter is always shown"),
    "radio_fields": (UNSUPPORTED, "ModelPanel.formfield_overrides with a RadioSelect widget"),
    "change_list_template": (UNSUPPORTED, "slots (vantage.slots) or VANTAGE['CUSTOM_CSS']"),
    "change_form_template": (UNSUPPORTED, "slots (vantage.slots) or VANTAGE['CUSTOM_CSS']"),
    "add_form_template": (UNSUPPORTED, "slots (vantage.slots) or VANTAGE['CUSTOM_CSS']"),
    "delete_confirmation_template": (UNSUPPORTED, "VANTAGE['CUSTOM_CSS']"),
    "object_history_template": (UNSUPPORTED, "VANTAGE['CUSTOM_CSS']"),
    "form": (UNSUPPORTED, "ModelPanel.fields / layout / formfield_overrides"),
    "get_urls": (NEEDS_CODE, "register a Page on the Vantage site"),
    "get_list_display": (NEEDS_CODE, "ModelPanel.get_list_display"),
    "get_fieldsets": (NEEDS_CODE, "ModelPanel.layout"),
    "get_actions": (NEEDS_CODE, "ModelPanel.actions"),
    "get_search_results": (NEEDS_CODE, "ModelPanel.get_queryset / list_search"),
    "get_form": (UNSUPPORTED, "ModelPanel.fields / layout / formfield_overrides"),
    "formfield_for_foreignkey": (UNSUPPORTED, "ModelPanel.get_queryset on the related panel"),
    "has_module_permission": (UNSUPPORTED, "model-level permissions gate the panel"),
    "Media": (UNSUPPORTED, "VANTAGE['CUSTOM_CSS']"),
}

#: Third-party admin base classes, by name, and what replaces them.
MIXINS: dict[str, tuple[str, str]] = {
    "ImportExportModelAdmin": (TRANSLATED, "built in: list export and the import wizard"),
    "ImportExportMixin": (TRANSLATED, "built in: list export and the import wizard"),
    "ExportMixin": (TRANSLATED, "built in: list export (lists.md#export)"),
    "ImportMixin": (TRANSLATED, "built in: the import wizard (lists.md#import)"),
    "ExportActionModelAdmin": (TRANSLATED, "built in: list export (lists.md#export)"),
    "SimpleHistoryAdmin": (TRANSLATED, "built in: per-record history with diffs (history.md)"),
    "VersionAdmin": (
        UNSUPPORTED,
        "history (history.md) records changes made through the panel; reversion's "
        "recover-deleted views have no equivalent, undo.md is the nearest",
    ),
    "MPTTModelAdmin": (TRANSLATED, "built in: tree models render parent-before-child"),
    "DraggableMPTTAdmin": (
        UNSUPPORTED,
        "trees render but drag-to-reorder does not exist",
    ),
    "GuardedModelAdmin": (
        NEEDS_CODE,
        "VANTAGE['PERMISSIONS'] = 'vantage.permissions.GuardianPermissions'",
    ),
    "SortableAdminMixin": (UNSUPPORTED, "no drag-to-reorder; sort by the order column"),
    "SortableAdminBase": (UNSUPPORTED, "no drag-to-reorder; sort by the order column"),
    "DjangoObjectActions": (NEEDS_CODE, "ModelPanel actions (@action)"),
    "ModelAdmin": (TRANSLATED, ""),
}

#: Module roots that never count as an "unknown mixin".
_BUILTIN_ROOTS = {"django", "builtins"}

_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LINE = 100


@dataclass(frozen=True)
class AttrReport:
    """One attribute of one ``ModelAdmin`` and what became of it."""

    attr: str
    status: str
    detail: str = ""
    equivalent: str = ""


@dataclass
class ModelReport:
    """Everything about one adopted model."""

    label: str
    app_label: str
    admin_class: str
    model: type
    items: list[AttrReport] = field(default_factory=list)
    adoption: Any = None
    admin: Any = None

    @property
    def counts(self) -> dict[str, int]:
        return {s: sum(1 for i in self.items if i.status == s) for s in STATUSES}


# ── building the report ─────────────────────────────────────────────────────


def build_report(admin_site: Any, app_label: str | None = None) -> list[ModelReport]:
    """Translate every admin on ``admin_site`` and report per model, sorted by label.

    The adapter records each translation in a module-level registry that the system
    check reads. A report is a read-only question, so that registry is put back as it
    was: running the command must not make ``manage.py check`` start warning about
    models the project never adopted.
    """
    saved = dict(adapter._adoptions)
    try:
        reports = []
        for model, admin_obj in sorted(
            admin_site._registry.items(), key=lambda pair: pair[0]._meta.label
        ):
            if app_label and model._meta.app_label != app_label:
                continue
            reports.append(_report_model(model, admin_obj))
        return reports
    finally:
        adapter._adoptions.clear()
        adapter._adoptions.update(saved)


def _report_model(model: type, admin_obj: Any) -> ModelReport:
    adoption = adapter.panel_from_admin(model, admin_obj)
    panel = adoption.panel_class
    own = vars(panel)
    report = ModelReport(
        label=model._meta.label,
        app_label=model._meta.app_label,
        admin_class=type(admin_obj).__name__,
        model=model,
        adoption=adoption,
        admin=admin_obj,
    )
    items = report.items

    for admin_attr, panel_attr in DIRECT.items():
        if not adapter._declared(admin_obj, admin_attr):
            continue
        carried = "get_list_filter" in own if admin_attr == "date_hierarchy" else panel_attr in own
        if carried:
            note = (
                "a date filter, not the drill-down control"
                if admin_attr == "date_hierarchy"
                else ""
            )
            items.append(AttrReport(admin_attr, TRANSLATED, note, f"ModelPanel.{panel_attr}"))
        else:
            items.append(
                AttrReport(
                    admin_attr,
                    TRANSLATED,
                    "nothing to carry; Vantage's inference covers it",
                    f"ModelPanel.{panel_attr}",
                )
            )

    for name in own.get("list_display", []):
        method = own.get(name)
        if not callable(method) or getattr(method, "vantage_str_column", False):
            continue
        items.append(
            AttrReport(
                f"list_display[{name}]",
                NEEDS_CODE,
                f"column method {name}() runs against the admin at runtime; port it",
                f"a method {name}(self, obj) on the ModelPanel",
            )
        )

    for entry in adapter._declared(admin_obj, "actions") or ():
        if entry == "delete_selected":
            continue
        name = getattr(entry, "__name__", None) or str(entry)
        items.append(
            AttrReport(
                f"actions[{name}]",
                NEEDS_CODE,
                "the admin action runs against the admin at runtime; port it",
                "a ModelPanel method decorated with @action",
            )
        )

    for verb in adapter.PERMISSION_VERBS:
        hook = f"has_{verb}_permission"
        if hook in own:
            items.append(
                AttrReport(
                    hook,
                    NEEDS_CODE,
                    "consulted before the superuser bypass, as in contrib.admin",
                    f"the same {hook}() on the ModelPanel",
                )
            )

    for name in adapter.DELEGATED_METHODS:
        if name in own:
            items.append(
                AttrReport(
                    f"{name}()",
                    NEEDS_CODE,
                    "adopt_admin_site() delegates to the admin; a standalone panel needs it ported",
                    f"ModelPanel.{name}",
                )
            )

    for text in adoption.unmapped:
        items.append(_classify_unmapped(text))

    items.extend(_mixin_items(admin_obj))
    return report


def _classify_unmapped(text: str) -> AttrReport:
    """One line of the adapter's free-text report as a structured row."""
    if text.startswith("list_filter:"):
        kept = "still filterable" in text
        return AttrReport(
            "list_filter",
            TRANSLATED if kept else UNSUPPORTED,
            text.removeprefix("list_filter: "),
            "" if kept else "a field filter or a queryset scope",
        )
    if text.startswith("readonly_fields:"):
        return AttrReport(
            "readonly_fields",
            NEEDS_CODE,
            text.removeprefix("readonly_fields: "),
            "a ModelPanel method or model property shown in the detail layout",
        )
    if text.startswith("actions:"):
        return AttrReport("actions", UNSUPPORTED, text.removeprefix("actions: "))
    if text.startswith("short_description"):
        return AttrReport("list_display", UNSUPPORTED, text, "name the method as the header")
    if text.startswith("list_display:"):
        return AttrReport("list_display", UNSUPPORTED, text.removeprefix("list_display: "))
    if text.startswith("Media"):
        status, equivalent = KNOWN["Media"]
        return AttrReport("Media", status, "extra CSS/JS on the admin", equivalent)

    name = re.split(r"\(\)? ?\(|\s\(", text, maxsplit=1)[0].strip()
    name = name.removesuffix("()").strip()
    reason = text[len(name) :].strip()
    reason = reason.removeprefix("()").strip().strip("()")
    if name in KNOWN:
        status, equivalent = KNOWN[name]
        return AttrReport(name, status, reason, equivalent)
    return AttrReport(name, UNSUPPORTED, reason)


def _mixin_items(admin_obj: Any) -> list[AttrReport]:
    """Third-party base classes: known ones map, unknown ones are flagged for review."""
    admin_cls = type(admin_obj)
    found: list[AttrReport] = []
    seen: set[str] = set()
    own_root = admin_cls.__module__.split(".")[0]
    for base in admin_cls.__mro__[1:]:
        root = base.__module__.split(".")[0]
        name = base.__name__
        if name in seen or name == "object":
            continue
        seen.add(name)
        if name in MIXINS and name != "ModelAdmin":
            status, equivalent = MIXINS[name]
            found.append(AttrReport(f"mixin {name}", status, "third-party admin base", equivalent))
        elif root not in _BUILTIN_ROOTS and root != own_root:
            found.append(
                AttrReport(
                    f"mixin {name}",
                    NEEDS_CODE,
                    f"unrecognised base class from {base.__module__}; review what it adds",
                )
            )
    return found


# ── serialising the report ──────────────────────────────────────────────────


def summarise(reports: list[ModelReport]) -> dict[str, int]:
    totals = dict.fromkeys(STATUSES, 0)
    for report in reports:
        for status, n in report.counts.items():
            totals[status] += n
    totals["models"] = len(reports)
    return totals


def report_to_dict(reports: list[ModelReport]) -> dict[str, Any]:
    return {
        "summary": summarise(reports),
        "models": [
            {
                "model": r.label,
                "admin": r.admin_class,
                "counts": r.counts,
                "attributes": [
                    {
                        "attribute": i.attr,
                        "status": i.status,
                        "detail": i.detail,
                        "vantage": i.equivalent,
                    }
                    for i in r.items
                ],
            }
            for r in reports
        ],
    }


def report_to_json(reports: list[ModelReport]) -> str:
    return json.dumps(report_to_dict(reports), indent=2, ensure_ascii=False)


def report_to_text(reports: list[ModelReport]) -> str:
    lines: list[str] = []
    for r in reports:
        lines.append(f"{r.label}  ({r.admin_class})")
        if not r.items:
            lines.append("  nothing declared; Vantage inference covers this model")
        for i in r.items:
            row = f"  {i.status:<16}  {i.attr}"
            if i.detail:
                row += f" - {i.detail}"
            if i.equivalent:
                row += f" [Vantage: {i.equivalent}]"
            lines.append(row)
        lines.append("")
    totals = summarise(reports)
    lines.append(
        f"{totals['models']} model(s): {totals[TRANSLATED]} translated, "
        f"{totals[NEEDS_CODE]} need panel code, {totals[UNSUPPORTED]} unsupported."
    )
    return "\n".join(lines)


# ── generating panel.py ─────────────────────────────────────────────────────


class _Unrenderable(Exception):
    """A value that has no honest source-code spelling."""


class _Seq:
    """A bracketed sequence that is broken one item per line only when it must be."""

    def __init__(self, open_: str, close: str, items: list[Any]) -> None:
        self.open, self.close, self.items = open_, close, items


class _Item:
    """``prefix`` + a value, for ``key: value`` and ``name=value`` entries."""

    def __init__(self, prefix: str, value: Any) -> None:
        self.prefix, self.value = prefix, value


def _flat(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, _Item):
        return node.prefix + _flat(node.value)
    return node.open + ", ".join(_flat(i) for i in node.items) + node.close


def _render(node: Any, col: int, indent: int, tail: int = 0) -> str:
    """Lay ``node`` out the way ``ruff format`` would: flat if it fits, else exploded."""
    flat = _flat(node)
    if col + len(flat) + tail <= _LINE or isinstance(node, str):
        return flat
    if isinstance(node, _Item):
        return node.prefix + _render(node.value, col + len(node.prefix), indent, tail)
    if not node.items:
        return flat
    inner = indent + 4
    lines = [node.open]
    for item in node.items:
        lines.append(" " * inner + _render(item, inner, inner, 1) + ",")
    lines.append(" " * indent + node.close)
    return "\n".join(lines)


class _Imports:
    """The names a generated module needs, collected as the code is rendered."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}  # name -> module

    def ref(self, cls: type) -> str:
        module = cls.__module__
        if module.startswith("django.forms"):
            module = "django.forms"
        elif module.startswith("django.db.models"):
            module = "django.db.models"
        name = cls.__name__
        if self.names.get(name, module) != module or "<locals>" in cls.__qualname__:
            raise _Unrenderable(name)
        self.names[name] = module
        return name


def _literal(value: Any, imports: _Imports) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return _Seq("[", "]", [_literal(v, imports) for v in value])
    if isinstance(value, dict):
        return _Seq(
            "{",
            "}",
            [
                _Item(_flat(_literal(k, imports)) + ": ", _literal(v, imports))
                for k, v in value.items()
            ],
        )
    if isinstance(value, type):
        return imports.ref(value)
    attrs = getattr(value, "attrs", None)
    if type(value).__module__.startswith("django.forms") and isinstance(attrs, dict):
        args = [_Item("attrs=", _literal(attrs, imports))] if attrs else []
        return _Seq(f"{imports.ref(type(value))}(", ")", args)
    raise _Unrenderable(type(value).__name__)


def _layout_node(node: Any) -> Any:
    from vantage.layout import Row, Section, Tab, Tabs

    def child(c: Any) -> Any:
        return _layout_node(c) if hasattr(c, "kind") else json.dumps(str(c), ensure_ascii=False)

    if isinstance(node, Row):
        return _Seq("Row(", ")", [child(c) for c in node.children])
    if isinstance(node, Section):
        args = [child(c) for c in node.children]
        if node.title:
            args.append(_Item("title=", json.dumps(str(node.title), ensure_ascii=False)))
        if node.description:
            args.append(
                _Item("description=", json.dumps(str(node.description), ensure_ascii=False))
            )
        if node.collapsed:
            args.append(_Item("collapsed=", "True"))
        return _Seq("Section(", ")", args)
    if isinstance(node, Tab):
        args = [json.dumps(str(node.label), ensure_ascii=False)] + [child(c) for c in node.children]
        return _Seq("Tab(", ")", args)
    if isinstance(node, Tabs):
        return _Seq("Tabs(", ")", [child(c) for c in node.children])
    raise _Unrenderable(type(node).__name__)


def _layout_names(node: Any) -> set[str]:
    return {type(node).__name__} | {
        n for c in getattr(node, "children", ()) if hasattr(c, "kind") for n in _layout_names(c)
    }


_SCALAR_ORDER = (
    "list_display",
    "list_link",
    "list_filter",
    "list_search",
    "ordering",
    "list_per_page",
    "list_select_related",
    "list_editable",
    "fields",
    "exclude",
    "readonly_fields",
)


def _render_panel(report: ModelReport, imports: _Imports, vantage_names: set[str]) -> str:
    panel = report.adoption.panel_class
    own = vars(panel)
    model_name = report.model.__name__
    body: list[str] = []  # attribute assignments, each already indented
    methods: list[str] = []  # method definitions, which follow the TODO comments
    todos: list[str] = []

    skipped_columns = [
        i.attr[len("list_display[") : -1]
        for i in report.items
        if i.attr.startswith("list_display[")
    ]
    str_columns = [
        name
        for name in own.get("list_display", [])
        if getattr(own.get(name), "vantage_str_column", False)
    ]

    for attr in _SCALAR_ORDER:
        if attr not in own:
            continue
        value = own[attr]
        if attr == "list_display":
            value = [c for c in value if c not in skipped_columns]
            if not value:
                continue
        if attr == "list_link" and value in skipped_columns:
            continue
        if attr == "list_editable":
            value = list(value)
        node = _literal(value, imports)
        body.append(f"    {attr} = " + _render(node, 4 + len(attr) + 3, 4))

    if "layout" in own:
        try:
            names = _layout_names(own["layout"])
            vantage_names.update(n for n in names if n in {"Row", "Section", "Tab", "Tabs"})
            node = _layout_node(own["layout"])
            body.append("    layout = " + _render(node, 4 + len("layout = "), 4))
        except _Unrenderable:
            todos.append("fieldsets: translate to `layout` (see forms.md)")

    if "formfield_overrides" in own:
        try:
            node = _literal(own["formfield_overrides"], imports)
            body.append("    formfield_overrides = " + _render(node, 4 + 22, 4))
        except _Unrenderable as exc:
            todos.append(
                f"formfield_overrides: copy the entries from {report.admin_class} "
                f"(could not write out {exc})"
            )

    date_field = adapter._declared(report.admin, "date_hierarchy")
    if date_field:
        quoted = json.dumps(str(date_field))
        methods.append(
            "\n    def get_list_filter(self, request=None):\n"
            "        filters = super().get_list_filter(request)\n"
            f"        return filters if {quoted} in filters else [*filters, {quoted}]"
        )

    for name in str_columns:
        methods.append(f"\n    def {name}(self, obj):\n        return str(obj)")

    for name in skipped_columns:
        todos.append(f"column {name}: port {report.admin_class}.{name}() as a method taking obj")
    for item in report.items:
        if item.attr.startswith("actions["):
            todos.append(f"action {item.attr[8:-1]}: port it with @action(...)")
        elif item.attr.startswith("has_") and item.status == NEEDS_CODE:
            sig = "request" if item.attr == "has_add_permission" else "request, obj=None"
            todos.append(f"{item.attr}(self, {sig}): port from {report.admin_class}")
        elif item.attr in ("get_queryset()", "save_model()"):
            todos.append(f"{item.attr[:-2]}: port from {report.admin_class}")
        elif item.attr == "inlines":
            todos.append("inlines: declare ModelPanel.inlines by hand (forms.md#editable-inlines)")
        elif item.status == UNSUPPORTED:
            todos.append(f"{item.attr}: unsupported ({item.equivalent or 'no equivalent'})")
        elif item.attr.startswith("mixin ") and item.status != TRANSLATED:
            todos.append(f"{item.attr}: {item.equivalent or item.detail}")

    lines = [f"@register({model_name})", f"class {model_name}Panel(ModelPanel):"]
    lines.extend(body)
    for todo in todos:
        lines.append(f"    # TODO(vantage_adopt) {todo}")
    lines.extend(methods)
    if not body and not methods:
        lines.append("    pass")
    return "\n".join(lines)


_HEADER = '''"""Vantage panels for the {app} app.

Generated by ``manage.py vantage_adopt`` from the ``ModelAdmin`` classes registered
with ``django.contrib.admin``. Every attribute below is one the adapter translates;
anything it could not carry over is a ``TODO(vantage_adopt)`` comment. Edit freely:
this file is yours, and it no longer reads ``admin.py``.
"""

'''


def render_app_module(app_label: str, reports: list[ModelReport]) -> str:
    """A complete, importable ``panel.py`` for the models of one app."""
    imports = _Imports()
    vantage_names: set[str] = set()
    classes = [_render_panel(r, imports, vantage_names) for r in reports]

    model_imports: dict[str, list[str]] = {}
    for r in reports:
        model_imports.setdefault(r.model.__module__, []).append(r.model.__name__)

    blocks: list[str] = []
    django_lines = []
    by_module: dict[str, list[str]] = {}
    for name, module in imports.names.items():
        by_module.setdefault(module, []).append(name)
    for module in sorted(by_module):
        django_lines.append(f"from {module} import {', '.join(sorted(by_module[module]))}")
    if django_lines:
        blocks.append("\n".join(django_lines))

    vantage = ", ".join(sorted({"ModelPanel", "register"} | set()))
    vantage_lines = [f"from vantage import {vantage}"]
    if vantage_names:
        vantage_lines.append(f"from vantage.layout import {', '.join(sorted(vantage_names))}")
    blocks.append("\n".join(vantage_lines))

    model_lines = []
    for module in sorted(model_imports):
        names = ", ".join(sorted(set(model_imports[module])))
        line = f"from {module} import {names}"
        if len(line) > _LINE:
            body = "".join(f"    {n},\n" for n in sorted(set(model_imports[module])))
            line = f"from {module} import (\n{body})"
        model_lines.append(line)
    blocks.append("\n".join(model_lines))

    return (
        _HEADER.format(app=app_label)
        + "\n\n".join(blocks)
        + "\n\n\n"
        + "\n\n\n".join(classes)
        + "\n"
    )


def check_label(app_label: str) -> str:
    """An app label that is safe to use as a path segment, or ``ImproperlyConfigured``."""
    if not _LABEL_RE.match(app_label):
        raise ImproperlyConfigured(f"Refusing to use {app_label!r} as a directory name.")
    return app_label
