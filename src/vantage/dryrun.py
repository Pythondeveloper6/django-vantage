"""Dry runs for the JSON API: say what a write would do, and write nothing.

``?dry_run=1`` (or ``X-Vantage-Dry-Run: 1``) on a create, update, delete or bulk-action
request returns what *would* happen. The rule that shapes this module: a dry run is
**validation only**. It runs the same permission, scope, field and form checks the real
write runs, then stops before anything is saved. It does not open a transaction and roll
it back, because a rolled-back save has already fired signals, webhooks and emails.

So a dry run cannot predict what only a real write finds out: a database constraint the
form does not know about, what a ``save_model`` hook or a signal receiver does, or a
change somebody else makes in between. ``docs/agent-writes.md`` lists them.
"""

from __future__ import annotations

from typing import Any

from vantage.audit import _form_sensitive, _jsonify

__all__ = ["cascade_payload", "form_diff", "payload"]


def payload(
    would: str,
    *,
    valid: bool = True,
    errors: dict[str, Any] | None = None,
    diff: list[dict[str, Any]] | None = None,
    cascade: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The one response shape every dry run has."""
    return {
        "dry_run": True,
        "would": would,
        "valid": valid,
        "errors": errors or {},
        "diff": diff or [],
        "cascade": cascade or {},
        **extra,
    }


def form_diff(form, *, creating: bool) -> list[dict[str, Any]]:
    """``[{"field", "from", "to"}]`` for a bound, valid form.

    Values are rendered as the audit log renders them. A sensitive field is reported as
    ``{"field", "changed": true, "masked": true}`` with no value on either side.
    """
    sensitive = _form_sensitive(form)
    out: list[dict[str, Any]] = []
    for name in form.fields:
        after = _jsonify(form.cleaned_data.get(name))
        if creating:
            if after in (None, "", []):
                continue
            before = None
        else:
            if name not in getattr(form, "changed_data", ()):
                continue
            before = _jsonify(form.initial.get(name))
            if before == after:
                continue
        if name in sensitive:
            out.append({"field": name, "changed": True, "masked": True})
        else:
            out.append({"field": name, "from": before, "to": after})
    return out


def cascade_payload(summary) -> dict[str, Any]:
    """The delete preview tree, counts per model, as the HTML delete page shows it."""
    return {
        "total": summary.total,
        "groups": [{"label": str(g.label), "count": g.count} for g in summary.groups],
        "cleared": [{"label": str(g.label), "count": g.count} for g in summary.cleared],
        "blocked_by": str(summary.blocked_by) if summary.blocked_by else None,
        "unavailable": str(summary.unavailable) if summary.unavailable else None,
    }
