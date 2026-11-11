"""A machine-readable description of the whole admin, for people and coding agents.

``manage.py vantage_describe`` is a thin shell over :func:`describe_site`. It answers
"what does this admin look like right now?" from the registered panels and the model
metadata alone:

* **No row is ever read.** Every value comes from ``Model._meta``, the panel class and
  the settings. Sensitive fields are listed by name with a ``masked`` flag and nothing
  else; the description carries no default, no example value and no count.
* **Output is deterministic.** Everything is sorted, so two runs on the same code are
  byte-identical and a description can be diffed or snapshot-tested.
* **A broken model does not hide the others.** A model whose introspection raises is
  reported in ``errors`` and left out of ``models``.

The JSON shape is documented in ``docs/for-coding-agents.md``.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings as django_settings
from django.urls import NoReverseMatch, reverse

from vantage import __version__

__all__ = ["SCHEMA_VERSION", "describe_model", "describe_site", "render_markdown"]

#: Bumped when a key is removed or changes meaning; adding a key does not bump it.
SCHEMA_VERSION = 1

#: Settings whose name matches are never printed, whatever their value.
_SECRET_NAME = re.compile(r"secret|token|key|password|passwd|dsn", re.IGNORECASE)


def _is_secret_name(name: str) -> bool:
    from vantage.sensitive import name_is_sensitive

    return bool(_SECRET_NAME.search(str(name))) or name_is_sensitive(str(name))


def _jsonable(value: Any) -> Any:
    """Reduce a setting to plain JSON data, dropping secret-looking dict keys."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {
            str(k): _jsonable(v)
            for k, v in sorted(value.items(), key=str)
            if not _is_secret_name(k)
        }
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted((_jsonable(v) for v in value), key=str)
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None)
    if module and name:
        return f"{module}.{name}"
    return str(value)


def _dotted(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


# ── one model ───────────────────────────────────────────────────────────────


def _field_entry(field, sensitive: frozenset[str]) -> dict[str, Any]:
    from vantage.widgets import classify

    entry: dict[str, Any] = {
        "name": field.name,
        "kind": str(classify(field)),
        "required": bool(getattr(field, "editable", True) and not field.blank),
        "unique": bool(getattr(field, "unique", False)),
        "nullable": bool(getattr(field, "null", False)),
        "help_text": str(getattr(field, "help_text", "") or ""),
        "max_length": getattr(field, "max_length", None),
        "relation": None,
        "choices": None,
        "sensitive": field.name in sensitive,
    }
    related = getattr(field, "related_model", None)
    if getattr(field, "is_relation", False) and related is not None:
        entry["relation"] = related._meta.label_lower
    choices = getattr(field, "flatchoices", None) if getattr(field, "choices", None) else None
    if choices:
        entry["choices"] = [{"value": value, "label": str(label)} for value, label in choices]
    return entry


def _declared_attributes(panel) -> list[str]:
    from vantage.panels import ModelPanel

    known = set(ModelPanel.__annotations__)
    names: set[str] = set()
    for klass in type(panel).__mro__:
        if klass is ModelPanel or klass is object:
            continue
        names.update(name for name in vars(klass) if name in known)
    return sorted(names)


def _action_entry(act) -> dict[str, Any]:
    return {
        "name": act.name,
        "label": str(act.label),
        "bulk": True,
        "destructive": bool(act.destructive),
        "permission": act.permission,
    }


def _name_of(entry: Any) -> str:
    return str(getattr(entry, "name", None) or entry)


def _inline_entry(inline) -> dict[str, Any]:
    model = getattr(inline, "model", None)
    return {
        "class": _dotted(inline) if isinstance(inline, type) else _dotted(type(inline)),
        "model": model._meta.label_lower if model is not None else None,
    }


def _api_url(panel) -> str | None:
    try:
        return reverse(
            "vantage:api-list",
            kwargs={"app_label": panel.opts.app_label, "model_name": panel.opts.model_name},
        )
    except NoReverseMatch:
        return None


def describe_model(panel) -> dict[str, Any]:
    """Everything the description says about one registered model."""
    from vantage import export, inference, tenancy
    from vantage.actions import resolve_actions
    from vantage.fieldperms import code_rules
    from vantage.panels import ModelPanel
    from vantage.safety import model_read_only

    model = panel.model
    opts = model._meta
    sensitive = panel.get_sensitive_fields()
    fields = [
        *(f for f in opts.get_fields() if getattr(f, "concrete", False)),
        *opts.many_to_many,
    ]
    seen: set[str] = set()
    entries = []
    for field in fields:
        if field.name not in seen:
            seen.add(field.name)
            entries.append(_field_entry(field, sensitive))

    soft = panel.get_patterns().soft_delete
    scope = tenancy.scope_for(model)
    read_only = model_read_only(model)
    inferred = type(panel) is ModelPanel
    rules = code_rules(panel) or {}

    described: dict[str, Any] = {
        "model": opts.label_lower,
        "verbose_name": str(opts.verbose_name),
        "verbose_name_plural": str(opts.verbose_name_plural),
        "panel": {
            "inferred": inferred,
            "hand_written": not inferred,
            "class": _dotted(type(panel)),
            "declared_attributes": _declared_attributes(panel),
        },
        "columns": list(panel.get_list_display()),
        "list_link": panel.get_list_link(),
        "filters": list(panel.get_list_filter()),
        "search_fields": list(panel.get_list_search()),
        "ordering": list(panel.get_ordering()),
        "fields": sorted(entries, key=lambda e: e["name"]),
        "flags": {
            "read_only": read_only is not None,
            "editable_when_read_only": sorted(read_only.editable) if read_only else [],
            "soft_delete_field": soft.field if soft else None,
            "tenant_scoped": scope.is_scoped,
            "tenant_path": scope.path,
        },
        "sensitive_fields": [{"name": name, "masked": True} for name in sorted(sensitive)],
        "actions": sorted(
            (_action_entry(a) for a in resolve_actions(panel)), key=lambda a: a["name"]
        ),
        "row_actions": sorted(_name_of(a) for a in (panel.row_actions or [])),
        "approval_required_fields": list(panel.get_requires_approval()),
        "field_permissions": {
            field: dict(sorted(by_role.items())) for field, by_role in sorted(rules.items())
        },
        "inlines": sorted(
            (_inline_entry(i) for i in (panel.inlines or [])), key=lambda i: i["class"]
        ),
        "api_url": _api_url(panel),
        "export_formats": list(export.FORMATS),
    }
    if inferred:
        described["inferred_defaults"] = _suggested_attributes(inference.infer_all(model))
    return described


def _suggested_attributes(inferred: dict[str, Any]) -> list[str]:
    """``ModelPanel`` attribute assignments that reproduce the inferred behaviour."""
    return [f"{name} = {inferred[name]!r}" for name in sorted(inferred)]


# ── the site ────────────────────────────────────────────────────────────────


def _changed_settings() -> dict[str, Any]:
    from vantage.conf import DEFAULTS

    raw = getattr(django_settings, "VANTAGE", None) or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): _jsonable(value)
        for name, value in sorted(raw.items(), key=lambda item: str(item[0]))
        if not _is_secret_name(name) and DEFAULTS.get(name, object()) != value
    }


def _pages(site) -> list[dict[str, Any]]:
    out = []
    for page in site.pages():
        permission = getattr(page, "permission", None)
        out.append(
            {
                "slug": page.get_slug(),
                "title": str(page.get_title()),
                "group": str(page.group) if getattr(page, "group", None) else None,
                "path": page.get_path(),
                "class": _dotted(page),
                "permission": permission
                if isinstance(permission, str) or permission is None
                else "callable",
            }
        )
    return sorted(out, key=lambda p: p["slug"])


def describe_site(
    site=None, *, model: str | None = None, undeclared: bool = False
) -> dict[str, Any]:
    """The full description as plain data. ``model`` is ``"app_label.model"``."""
    if site is None:
        from vantage import site as default_site

        site = default_site

    from vantage.panels import ModelPanel

    wanted = model.lower() if model else None
    models: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    groups: set[str] = set()
    for panel in site.panels():
        label = panel.opts.label_lower
        try:
            groups.add(str(panel.get_group()))
        except Exception:  # Reported per model below if it matters there too.
            pass
        if wanted and label != wanted:
            continue
        if undeclared and type(panel) is not ModelPanel:
            continue
        try:
            models.append(describe_model(panel))
        except Exception as exc:  # One broken model must not hide the rest.
            errors.append({"model": label, "error": f"{type(exc).__name__}: {exc}"})

    pages = _pages(site)
    groups.update(p["group"] for p in pages if p["group"])
    dashboard = getattr(django_settings, "VANTAGE", {}) or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "vantage_version": __version__,
        "site": {
            "settings_changed": _changed_settings(),
            "pages": pages,
            "dashboard": _jsonable(dashboard.get("DASHBOARD")) or "default",
            "nav_groups": sorted(groups),
        },
        "models": sorted(models, key=lambda m: m["model"]),
        "errors": sorted(errors, key=lambda e: e["model"]),
    }


# ── markdown ────────────────────────────────────────────────────────────────


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(description: dict[str, Any]) -> str:
    """The same description as a Markdown document."""
    lines = [
        f"# Vantage admin description (schema {description['schema_version']})",
        "",
        f"- Vantage version: {description['vantage_version']}",
    ]
    site = description["site"]
    lines.append(f"- Dashboard: {site['dashboard']}")
    lines.append(f"- Navigation groups: {', '.join(site['nav_groups']) or 'none'}")
    if site["settings_changed"]:
        lines += ["", "## Settings that differ from the defaults", ""]
        lines += [f"- `{k}` = `{v!r}`" for k, v in site["settings_changed"].items()]
    if site["pages"]:
        lines += ["", "## Pages", ""]
        lines += [f"- `{p['slug']}`: {p['title']} (`{p['class']}`)" for p in site["pages"]]

    for item in description["models"]:
        panel = item["panel"]
        kind = "inferred" if panel["inferred"] else "hand-written"
        lines += ["", f"## {item['model']}", ""]
        lines.append(f"- Names: {item['verbose_name']} / {item['verbose_name_plural']}")
        lines.append(f"- Panel: {kind} (`{panel['class']}`)")
        if panel["declared_attributes"]:
            lines.append(f"- Declared attributes: {', '.join(panel['declared_attributes'])}")
        lines.append(f"- Columns: {', '.join(item['columns'])}")
        lines.append(f"- Filters: {', '.join(item['filters']) or 'none'}")
        lines.append(f"- Search: {', '.join(item['search_fields']) or 'none'}")
        lines.append(f"- Ordering: {', '.join(item['ordering']) or 'none'}")
        flags = item["flags"]
        lines.append(
            f"- Flags: read_only={flags['read_only']}, "
            f"soft_delete_field={flags['soft_delete_field']}, "
            f"tenant_scoped={flags['tenant_scoped']}"
        )
        if item["sensitive_fields"]:
            names = ", ".join(f"{s['name']} (masked)" for s in item["sensitive_fields"])
            lines.append(f"- Sensitive fields: {names}")
        if item["actions"]:
            lines.append(f"- Actions: {', '.join(a['name'] for a in item['actions'])}")
        if item["approval_required_fields"]:
            lines.append(f"- Approval required: {', '.join(item['approval_required_fields'])}")
        if item["inlines"]:
            lines.append(f"- Inlines: {', '.join(i['class'] for i in item['inlines'])}")
        lines.append(f"- API: {item['api_url']}")
        lines.append(f"- Exports: {', '.join(item['export_formats'])}")
        lines += [
            "",
            "| Field | Kind | Required | Relation | Choices | Max length | Help |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for f in item["fields"]:
            choices = ", ".join(str(c["value"]) for c in f["choices"]) if f["choices"] else ""
            lines.append(
                f"| {_cell(f['name'])} | {f['kind']} | {'yes' if f['required'] else 'no'} "
                f"| {_cell(f['relation'])} | {_cell(choices)} | {_cell(f['max_length'])} "
                f"| {_cell(f['help_text'])} |"
            )
        if "inferred_defaults" in item:
            lines += ["", "Inferred defaults, as `ModelPanel` attributes:", "", "```python"]
            lines += item["inferred_defaults"]
            lines.append("```")

    if description["errors"]:
        lines += ["", "## Errors", ""]
        lines += [f"- `{e['model']}`: {e['error']}" for e in description["errors"]]
    return "\n".join(lines) + "\n"
