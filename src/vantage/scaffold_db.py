"""Scaffolding from an existing database, and from a starter preset.

Two jobs, both aimed at the first hour with a database that already exists:

``--from-db``
    Run Django's own ``inspectdb`` (so every backend quirk it already handles stays
    handled), then write the matching read-only panels. The models are ``managed =
    False`` — inspectdb's own output — so ``migrate`` never touches the tables, and the
    panels refuse add, change and delete, so nothing here can write to them either.
    Opening the data up to edits is a later, deliberate step.

``--preset``
    A sensible dashboard and panel grouping for a CRM, a shop or a SaaS back office.
    A preset is written against *roles* (``contact``, ``order``, ``subscription``) not
    model names, because a project's models are never called what a template guesses.
    Roles are matched to models by name, ``--map role=Model`` overrides the guess, and a
    role nothing matches simply contributes no card: the output never references a model
    that does not exist.
"""

from __future__ import annotations

import ast
import fnmatch
import json
from dataclasses import dataclass, field
from io import StringIO

from django.core.management import call_command
from django.db import connections

from vantage.adopt import _Item, _render, _Seq

__all__ = [
    "PRESETS",
    "ModelInfo",
    "inspect_models",
    "models_from_app",
    "models_from_source",
    "parse_mapping",
    "render_dashboard",
    "render_readonly_panels",
]


# ── inspectdb ───────────────────────────────────────────────────────────────


def inspect_models(
    database: str = "default",
    tables: list[str] | None = None,
    exclude: list[str] | None = None,
    include_views: bool = False,
) -> str:
    """The ``models.py`` source ``inspectdb`` writes, for the tables that match.

    ``tables`` and ``exclude`` are shell-style patterns (``legacy_*``). inspectdb's own
    positional arguments only take exact names, so the filtering goes through its
    ``table_name_filter`` option instead, which sees every table once.
    """
    if database not in connections:
        raise ValueError(f"Unknown database alias {database!r}.")
    wanted = tables or []
    unwanted = exclude or []

    def keep(name: str) -> bool:
        if wanted and not any(fnmatch.fnmatchcase(name, p) for p in wanted):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in unwanted)

    out = StringIO()
    call_command(
        "inspectdb",
        database=database,
        include_views=include_views,
        table_name_filter=keep,
        stdout=out,
    )
    return out.getvalue()


# ── describing models, from source or from classes ──────────────────────────


@dataclass
class ModelInfo:
    """What the dashboard generator needs to know about one model."""

    name: str
    module: str
    #: field name -> kind: ``text``, ``int``, ``decimal``, ``bool``, ``date``,
    #: ``datetime``, ``relation`` or ``other``.
    fields: dict[str, str] = field(default_factory=dict)


_KINDS = {
    "CharField": "text",
    "TextField": "text",
    "SlugField": "text",
    "EmailField": "text",
    "IntegerField": "int",
    "BigIntegerField": "int",
    "SmallIntegerField": "int",
    "PositiveIntegerField": "int",
    "AutoField": "int",
    "BigAutoField": "int",
    "DecimalField": "decimal",
    "FloatField": "decimal",
    "BooleanField": "bool",
    "DateField": "date",
    "DateTimeField": "datetime",
    "ForeignKey": "relation",
    "OneToOneField": "relation",
}


def models_from_source(source: str, module: str) -> list[ModelInfo]:
    """Read the model classes out of generated source without importing it.

    Importing it would need an installed app to attach the models to, and the whole
    point of ``--from-db`` is that the app may not exist yet.
    """
    infos = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        info = ModelInfo(node.name, module)
        for stmt in node.body:
            if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
                continue
            func = stmt.value.func
            kind = _KINDS.get(func.attr if isinstance(func, ast.Attribute) else "", "other")
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    info.fields[target.id] = kind
        infos.append(info)
    return infos


def models_from_app(app_label: str) -> list[ModelInfo]:
    from django.apps import apps as django_apps

    infos = []
    for model in sorted(
        django_apps.get_app_config(app_label).get_models(), key=lambda m: m.__name__
    ):
        info = ModelInfo(model.__name__, model.__module__)
        for f in model._meta.concrete_fields:
            kind = "other"
            for cls in type(f).__mro__:
                if cls.__name__ in _KINDS:
                    kind = _KINDS[cls.__name__]
                    break
            info.fields[f.name] = kind
        infos.append(info)
    return infos


# ── presets ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Role:
    aliases: tuple[str, ...]
    icon: str
    group: str


@dataclass(frozen=True)
class Preset:
    title: str
    roles: dict[str, Role]
    #: ``(kind, role, label)``; kinds are count, sum, breakdown, timeseries, latest.
    cards: tuple[tuple[str, str, str], ...]


MONEY_HINTS = ("total", "amount", "value", "price", "mrr", "revenue")
GROUP_HINTS = ("status", "stage", "plan", "state", "type", "category")
DATE_HINTS = ("created", "placed", "ordered", "signed", "opened", "date", "at")

PRESETS: dict[str, Preset] = {
    "crm": Preset(
        "Sales overview",
        {
            "contact": Role(("contact", "lead", "person"), "users", "Sales"),
            "company": Role(
                ("company", "organisation", "organization", "account"), "building", "Sales"
            ),
            "deal": Role(("deal", "opportunity"), "briefcase", "Sales"),
        },
        (
            ("count", "contact", "Contacts"),
            ("count", "company", "Companies"),
            ("count", "deal", "Deals"),
            ("sum", "deal", "Pipeline value"),
            ("breakdown", "deal", "Deals by stage"),
            ("timeseries", "contact", "New contacts"),
            ("latest", "deal", "Latest deals"),
        ),
    ),
    "ecommerce": Preset(
        "Shop overview",
        {
            "customer": Role(("customer", "buyer", "client"), "users", "Customers"),
            "product": Role(("product", "item", "sku"), "package", "Catalogue"),
            "order": Role(("order", "sale", "purchase"), "shopping-cart", "Sales"),
        },
        (
            ("sum", "order", "Revenue"),
            ("count", "order", "Orders"),
            ("count", "customer", "Customers"),
            ("count", "product", "Products"),
            ("timeseries", "order", "Orders over time"),
            ("breakdown", "order", "Orders by status"),
            ("latest", "order", "Latest orders"),
        ),
    ),
    "saas": Preset(
        "Business overview",
        {
            "account": Role(
                ("account", "organisation", "organization", "tenant", "workspace"),
                "building",
                "Customers",
            ),
            "user": Role(("user", "member", "seat"), "users", "Customers"),
            "subscription": Role(("subscription", "plan", "contract"), "credit-card", "Billing"),
        },
        (
            ("count", "account", "Accounts"),
            ("count", "user", "Users"),
            ("count", "subscription", "Subscriptions"),
            ("sum", "subscription", "Recurring revenue"),
            ("breakdown", "subscription", "Subscriptions by plan"),
            ("timeseries", "account", "New accounts"),
            ("latest", "subscription", "Latest subscriptions"),
        ),
    ),
}


def parse_mapping(pairs: list[str]) -> dict[str, str]:
    """``["deal=Opportunity"]`` -> ``{"deal": "Opportunity"}``, or ``ValueError``."""
    mapping = {}
    for pair in pairs or []:
        role, sep, model = pair.partition("=")
        if not sep or not role.strip() or not model.strip():
            raise ValueError(f"--map expects role=Model, got {pair!r}.")
        mapping[role.strip()] = model.strip()
    return mapping


def resolve_roles(
    preset: Preset, models: list[ModelInfo], mapping: dict[str, str]
) -> dict[str, ModelInfo]:
    """Which model plays each role. An explicit mapping wins; otherwise match by name."""
    unknown = set(mapping) - set(preset.roles)
    if unknown:
        raise ValueError(
            f"Unknown role(s) {', '.join(sorted(unknown))}; this preset has "
            f"{', '.join(sorted(preset.roles))}."
        )
    by_name = {m.name: m for m in models}
    resolved: dict[str, ModelInfo] = {}
    for role, spec in preset.roles.items():
        if role in mapping:
            if mapping[role] not in by_name:
                raise ValueError(
                    f"--map {role}={mapping[role]}: no such model among the scaffolded ones."
                )
            resolved[role] = by_name[mapping[role]]
            continue
        for alias in spec.aliases:
            match = next((m for m in models if m.name.lower().endswith(alias)), None)
            if match:
                resolved[role] = match
                break
    return resolved


def _pick(info: ModelInfo, kinds: tuple[str, ...], hints: tuple[str, ...]) -> str | None:
    candidates = [name for name, kind in info.fields.items() if kind in kinds]
    for hint in hints:
        for name in candidates:
            if hint in name.lower():
                return name
    return candidates[0] if candidates and not hints else None


def _date_field(info: ModelInfo) -> str | None:
    return _pick(info, ("datetime", "date"), DATE_HINTS) or _pick(info, ("datetime", "date"), ())


def _q(value: str) -> str:
    return json.dumps(value)


def render_dashboard(preset_name: str, models: list[ModelInfo], mapping: dict[str, str]) -> str:
    """A ``dashboard.py`` for the preset, over the models that fill its roles."""
    preset = PRESETS[preset_name]
    roles = resolve_roles(preset, models, mapping)

    cards: list[str] = []
    used: dict[str, ModelInfo] = {}
    skipped: list[str] = []
    for kind, role, label in preset.cards:
        info = roles.get(role)
        date = _date_field(info) if info else None
        if info is None:
            skipped.append(f"{label} (needs a model for the {role!r} role)")
            continue
        qs = f"{info.name}.objects.all()"
        node: _Seq | None = None
        if kind == "count":
            args = [_q(label), qs] + ([f"trend={_q(date)}"] if date else [])
            node = _Seq("StatCard(", ")", args)
        elif kind == "sum":
            money = _pick(info, ("decimal",), MONEY_HINTS) or _pick(info, ("decimal",), ())
            if money:
                args = [_q(label), qs, f"aggregate=Sum({_q(money)})", '''format="currency"''']
                if date:
                    args.append(f"trend={_q(date)}")
                node = _Seq("StatCard(", ")", args)
        elif kind == "breakdown":
            group = _pick(info, ("text",), GROUP_HINTS)
            if group:
                chart = _Seq(
                    "Chart.breakdown(",
                    ")",
                    [_q(label), f"group_by={_q(group)}", 'value=Count("pk")', 'kind="donut"'],
                )
                node = _Seq("ChartCard(", ")", [_q(label), qs, _Item("chart=", chart)])
        elif kind == "timeseries" and date:
            chart = _Seq(
                "Chart.timeseries(", ")", [_q(label), f"date={_q(date)}", 'value=Count("pk")']
            )
            node = _Seq("ChartCard(", ")", [_q(label), qs, _Item("chart=", chart)])
        elif kind == "latest":
            order = f'order_by=["-{date}"]' if date else None
            args = [_q(label), qs, "limit=5"] + ([order] if order else [])
            node = _Seq("TableCard(", ")", args)
        if node is None:
            skipped.append(f"{label} (no suitable field on {info.name})")
            continue
        used[info.name] = info
        cards.append(_render(node, 12, 12, 1))

    modules: dict[str, list[str]] = {}
    for info in used.values():
        modules.setdefault(info.module, []).append(info.name)
    aggregates = sorted(n for n in ("Count", "Sum") if any(f"{n}(" in c for c in cards))
    card_names = sorted({c.split("(", 1)[0] for c in cards})
    lines = [
        f'"""The {preset_name} starter dashboard, written by ``manage.py vantage_scaffold``.',
        "",
        "Point ``VANTAGE['DASHBOARD']`` at ``StarterDashboard`` (as a dotted path) to use it.",
        "Roles with no matching model contributed no card; see the comments at the end.",
        '"""',
        "",
    ]
    if aggregates:
        lines.append(f"from django.db.models import {', '.join(aggregates)}")
        lines.append("")
    vantage_lines = []
    if any(c.startswith("ChartCard") for c in cards):
        vantage_lines.append("from vantage import Chart")
    if card_names:
        vantage_lines.append(
            f"from vantage.dashboards import {', '.join(['Dashboard', *card_names])}"
        )
    else:
        vantage_lines.append("from vantage.dashboards import Dashboard")
    lines.extend(vantage_lines)
    lines.append("")
    for module in sorted(modules):
        lines.append(f"from {module} import {', '.join(sorted(modules[module]))}")
    lines += ["", "", "class StarterDashboard(Dashboard):", f"    title = {_q(preset.title)}", ""]
    lines.append("    def get_widgets(self, request):")
    if cards:
        lines.append("        return [")
        lines.extend(f"            {c}," for c in cards)
        lines.append("        ]")
    else:
        lines.append("        return []")
    for item in skipped:
        lines.append(f"\n# Skipped: {item}. Use --map role=Model to name one.")
    return "\n".join(lines) + "\n"


def render_readonly_panels(
    models: list[ModelInfo],
    module: str,
    app_label: str,
    preset_name: str | None = None,
    mapping: dict[str, str] | None = None,
) -> str:
    """A ``panel.py`` of read-only panels, optionally grouped and iconed by a preset."""
    decor: dict[str, Role] = {}
    if preset_name:
        preset = PRESETS[preset_name]
        decor = {
            m.name: preset.roles[r] for r, m in resolve_roles(preset, models, mapping or {}).items()
        }

    classes = []
    for info in models:
        lines = [f"@register({info.name})", f"class {info.name}Panel(ModelPanel):"]
        role = decor.get(info.name)
        if role:
            lines.append(f"    icon = {_q(role.icon)}")
            lines.append(f"    group = {_q(role.group)}")
        lines += [
            "",
            "    def has_add_permission(self, request, obj=None):",
            "        return False",
            "",
            "    def has_change_permission(self, request, obj=None):",
            "        return False",
            "",
            "    def has_delete_permission(self, request, obj=None):",
            "        return False",
        ]
        classes.append("\n".join(lines))

    names = ", ".join(sorted(m.name for m in models))
    header = (
        f'"""Read-only Vantage panels for the {app_label} app, from an existing database.\n\n'
        "Generated by ``manage.py vantage_scaffold --from-db``. Columns, filters and search are\n"
        "inferred from the models, exactly as for any other panel. Every write is refused; delete\n"
        "the three ``has_*_permission`` methods on a panel to open that table up to edits.\n"
        '"""\n\n'
        "from vantage import ModelPanel, register\n\n"
        f"from {module} import {names}\n"
    )
    if not models:
        return header + "\n# No tables matched.\n"
    return header + "\n\n" + "\n\n\n".join(classes) + "\n"
