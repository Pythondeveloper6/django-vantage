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
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext, ngettext_lazy

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
    #: ``"sql"`` for an action that is one ``queryset.update()`` or ``.delete()``;
    #: ``"instances"`` for one that loads and saves each row. Declared rather than
    #: inferred because ``queryset.update()`` bypasses ``save()``, signals and
    #: ``auto_now``, and that must never be a silent choice. It is also how the job
    #: layer costs the work: an SQL action over 40,000 rows still belongs in the
    #: request; an instance action over 1,001 does not.
    bulk_strategy: str = "instances"
    keywords: list[str] = field(default_factory=list)

    def needs_confirmation(self) -> bool:
        return self.destructive or self.confirm is not None

    def confirmation_text(self, count: int) -> str:
        if self.confirm:
            return self.confirm.format(count=count)
        return ngettext_lazy(
            "%(label)s will be applied to %(count)d record. This cannot be undone.",
            "%(label)s will be applied to %(count)d records. This cannot be undone.",
            "count",
        ) % {"label": self.label, "count": count}

    def run(self, panel, request, queryset) -> ActionResult:
        outcome = self.func(panel, request, queryset)
        if isinstance(outcome, ActionResult):
            return outcome
        if outcome is None:
            return ActionResult(_("%(label)s completed.") % {"label": self.label})
        return ActionResult(str(outcome))


def action(
    label: str | None = None,
    *,
    icon: str | None = None,
    permission: str | None = "change",
    destructive: bool = False,
    confirm: str | None = None,
    max_rows: int | None = None,
    bulk_strategy: str = "instances",
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
            "bulk_strategy": bulk_strategy,
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

    # Only when the panel offers nothing of its own. An author who wrote actions has
    # thought about which ones belong, and silently adding two more to their list is
    # the kind of helpfulness that reads as a bug.
    if not resolved:
        resolved.extend(_from_patterns(panel))

    if request is None:
        return resolved

    permissions = get_permissions()
    return [
        act
        for act in resolved
        if act.permission is None
        or permissions.has_model_perm(request, panel.model, act.permission)
    ]


def _from_patterns(panel) -> list[Action]:
    """Actions a detected convention implies, for panels that declare none.

    A model with an ``is_published`` flag almost certainly wants publish and
    unpublish, and making the author write two identical methods on every such model
    is the boilerplate this package exists to remove.
    """
    pattern = panel.get_patterns().publishable
    if pattern is None:
        return []

    def _set(value_attr: str):
        publishing = value_attr == "publish_value"

        def run(self, request, queryset):
            from django.utils import timezone

            value = getattr(pattern, value_attr)
            # A timestamp pattern has no fixed value: publishing means "now", and a
            # pure detector must not read the clock, so the caller supplies it.
            if pattern.kind == "timestamp" and publishing:
                value = timezone.now()
            updated = queryset.update(**{pattern.field: value})
            # Two whole sentences rather than one assembled from a verb and a count.
            # "Published" is not a word a translator can place: it is the start of a
            # sentence in English and the end of one in plenty of other languages.
            if publishing:
                return ngettext(
                    "Published %(count)s record.",
                    "Published %(count)s records.",
                    updated,
                ) % {"count": updated}
            return ngettext(
                "Unpublished %(count)s record.",
                "Unpublished %(count)s records.",
                updated,
            ) % {"count": updated}

        return run

    built = []
    for name, label, value_attr in (
        ("publish", _("Publish"), "publish_value"),
        ("unpublish", _("Unpublish"), "unpublish_value"),
    ):
        built.append(
            Action(
                func=_set(value_attr),
                name=name,
                # The button's own wording, so it is translated rather than derived
                # from the Python identifier by ``str.capitalize``.
                label=label,
                icon="check" if name == "publish" else "x",
                permission="change",
                bulk_strategy="sql",
            )
        )
    return built


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
            "bulk_strategy": "instances",
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
        """Say plainly what scope was resolved, so it can never shrink silently.

        Three whole phrases rather than a noun glued to a clause. This text is read
        back to the user immediately after a destructive action has run, which makes it
        the last place in the package where a translator should be handed fragments and
        asked to guess how they will be joined.
        """
        if self.select_all and self.excluded:
            # Pluralised on the matching count, which is the number the phrase is
            # about; the exclusion count is reported as a bare figure.
            return ngettext(
                "all %(count)s matching record, %(excluded)s excluded",
                "all %(count)s matching records, %(excluded)s excluded",
                count,
            ) % {"count": count, "excluded": len(self.excluded)}
        if self.select_all:
            return ngettext(
                "all %(count)s matching record",
                "all %(count)s matching records",
                count,
            ) % {"count": count}
        return ngettext(
            "%(count)s selected record",
            "%(count)s selected records",
            count,
        ) % {"count": count}
