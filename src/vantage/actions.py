"""Actions: doing something to a record, or to a lot of them at once.

    @register(Order)
    class OrderPanel(ModelPanel):
        actions = ["mark_paid"]

        @action(label="Mark as paid", icon="check", permission="change")
        def mark_paid(self, request, queryset):
            return f"{queryset.update(status='paid')} orders marked paid."

The selection is transmitted as a **predicate plus an exclusion set**, never as a list
of primary keys. "Select all 40,000 matching" is the case that decides this design:
sending 40,000 ids in a POST body is slow, hits server limits, and Filament's
alternative — hydrating every selected model into memory — led it to ship a
``selectCurrentPageOnly()`` switch, which is disabling the feature rather than solving
it. A predicate is a few hundred bytes whatever the row count, and it stays a queryset
all the way down.

Two safety properties are non-negotiable, both learned from Django's own bug history.
``contrib.admin`` shipped a defect where ``select_across`` silently stayed off with
``actions_on_bottom = True``, so a destructive action applied to page one only. Silent
scope reduction on a destructive action is the worst failure available here, so the
resolved scope is always reported back to the user. And rows change between selection
and execution, so an action reports the count it actually affected rather than echoing
the count shown at selection time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import QuerySet

__all__ = ["Action", "ActionResult", "Selection", "action", "resolve_actions"]


@dataclass(frozen=True)
class ActionResult:
    """What an action reports back. Returning a bare string is also fine."""

    message: str
    level: str = "success"  # success | info | warning | error
    redirect: str | None = None


@dataclass
class Action:
    """A callable bound to a panel, with the metadata the UI needs."""

    func: Callable
    name: str
    label: str
    icon: str | None = None
    #: The permission verb this action needs — "change" by default, since most
    #: actions mutate. Set "delete" for destructive ones, or None to require only
    #: the view permission the page already checked.
    permission: str | None = "change"
    #: Destructive actions confirm before running, with the affected count in the
    #: button label. A bulk delete is fifty times the damage of a single one, so it
    #: confirms even where the single-row version would not.
    destructive: bool = False
    confirm: str | None = None
    #: Guard against a mis-click wiping a table. None means no ceiling.
    max_rows: int | None = None
    keywords: list[str] = field(default_factory=list)

    def needs_confirmation(self) -> bool:
        return self.destructive or self.confirm is not None

    def confirmation_text(self, count: int) -> str:
        if self.confirm:
            return self.confirm.format(count=count)
        noun = "record" if count == 1 else "records"
        return f"{self.label} will be applied to {count} {noun}. This cannot be undone."

    def run(self, panel, request, queryset) -> ActionResult:
        outcome = self.func(panel, request, queryset)
        if isinstance(outcome, ActionResult):
            return outcome
        if outcome is None:
            return ActionResult(f"{self.label} completed.")
        return ActionResult(str(outcome))


def action(
    label: str | None = None,
    *,
    icon: str | None = None,
    permission: str | None = "change",
    destructive: bool = False,
    confirm: str | None = None,
    max_rows: int | None = None,
    keywords: list[str] | None = None,
):
    """Mark a panel method as an action.

    Used bare or called: ``@action`` and ``@action("Mark as paid")`` both work, because
    forgetting the parentheses is the mistake everyone makes once.
    """

    def wrap(func: Callable) -> Callable:
        func.vantage_action = {  # type: ignore[attr-defined]
            "label": label or func.__name__.replace("_", " ").capitalize(),
            "icon": icon,
            "permission": permission,
            "destructive": destructive,
            "confirm": confirm,
            "max_rows": max_rows,
            "keywords": keywords or [],
        }
        return func

    if callable(label):  # used as a bare @action
        func, label = label, None
        return wrap(func)
    return wrap


def resolve_actions(panel, request=None) -> list[Action]:
    """Every action available on ``panel``, filtered by what the user may do."""
    from vantage.permissions import get_permissions

    declared = panel.actions
    if declared is None:
        declared = _discover(panel)

    resolved: list[Action] = []
    for entry in declared:
        built = _build(panel, entry)
        if built is not None:
            resolved.append(built)

    if request is None:
        return resolved

    permissions = get_permissions()
    return [
        act
        for act in resolved
        if act.permission is None
        or permissions.has_model_perm(request, panel.model, act.permission)
    ]


def _discover(panel) -> list[str]:
    """Any decorated method on the panel, when ``actions`` is not declared.

    Decorating a method is already an explicit statement of intent, so requiring it to
    be listed a second time would be pure ceremony.
    """
    return [
        name
        for name in dir(type(panel))
        if hasattr(getattr(type(panel), name, None), "vantage_action")
    ]


def _build(panel, entry: Any) -> Action | None:
    if isinstance(entry, Action):
        return entry

    func = entry if callable(entry) else getattr(type(panel), str(entry), None)
    if func is None:
        return None

    meta = getattr(func, "vantage_action", None)
    if meta is None:
        # An undecorated callable is still usable; it just takes every default.
        meta = {
            "label": getattr(func, "__name__", str(entry)).replace("_", " ").capitalize(),
            "icon": None,
            "permission": "change",
            "destructive": False,
            "confirm": None,
            "max_rows": None,
            "keywords": [],
        }
    return Action(func=func, name=getattr(func, "__name__", str(entry)), **meta)


# ── selection ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Selection:
    """Which rows an action applies to.

    ``select_all`` means "everything matching the current filters", carried as the
    request's own query string rather than as ids, minus anything the user unticked —
    selecting all and then removing a handful is the common case, not an edge one.
    """

    select_all: bool
    ids: list[str]
    excluded: list[str]

    @classmethod
    def from_request(cls, request) -> Selection:
        post = request.POST
        return cls(
            select_all=post.get("select_all") in ("1", "true", "on"),
            ids=post.getlist("selected"),
            excluded=post.getlist("excluded"),
        )

    @property
    def is_empty(self) -> bool:
        return not self.select_all and not self.ids

    def apply(self, queryset: QuerySet) -> QuerySet:
        """Narrow ``queryset`` — which must already have the page's filters applied."""
        if self.select_all:
            return queryset.exclude(pk__in=self.excluded) if self.excluded else queryset
        return queryset.filter(pk__in=self.ids)

    def describe(self, count: int) -> str:
        """Say plainly what scope was resolved, so it can never shrink silently."""
        noun = "record" if count == 1 else "records"
        if self.select_all:
            excluded = f", {len(self.excluded)} excluded" if self.excluded else ""
            return f"all {count} matching {noun}{excluded}"
        return f"{count} selected {noun}"
