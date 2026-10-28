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
from dataclasses import dataclass, field, replace
from typing import Any

from django.db import router, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext, ngettext_lazy

__all__ = [
    "Action",
    "ActionResult",
    "Selection",
    "action",
    "delete_selected",
    "resolve_actions",
]


@dataclass(frozen=True)
class ActionResult:
    """What an action reports back. Returning a bare string is also fine."""

    message: str
    level: str = "success"  # success | info | warning | error
    redirect: str | None = None
    #: For an action declared with ``@action(undo=…)``: anything JSON-serialisable the
    #: undo hook will need, such as the ids it touched and what they were. Returning
    #: ``None`` (the default) means this run offers no undo.
    undo_state: Any = None
    #: A ready :class:`vantage.undo.UndoPlan`. Set by the built-in actions and by
    #: ``undo_fields``; a custom action normally uses ``undo_state`` instead.
    undo: Any = None


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
    #: Fields this action writes, for an automatic undo: their values are recorded
    #: before and after the run, and Undo restores the "before" — only on rows that
    #: still hold the "after". The simplest correct undo for an update-style action.
    undo_fields: list[str] | None = None
    #: A hook for anything ``undo_fields`` cannot express: a panel method name or a
    #: callable ``(panel, request, state)``, called with the action's ``undo_state``
    #: inside a transaction. Raise :class:`vantage.undo.UndoConflict` to refuse.
    undo: Callable | str | None = None

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
        from vantage import undo

        capture = None
        # Counted before the run: an action that moves rows out of the page's filter
        # would otherwise describe itself as having touched none of them.
        hooked = self.undo is not None and undo.armed(request)
        count = queryset.count() if hooked else 0
        if self.undo_fields and undo.armed(request):
            capture = undo.FieldCapture(panel.model, self.undo_fields, strategy=self.bulk_strategy)
        if capture is None or not capture.active:
            result = self._normalise(self.func(panel, request, queryset))
        else:
            # One transaction, so the rows recorded, the rows the action wrote and the
            # values read back afterwards are the same rows in the same state.
            with transaction.atomic(using=capture.db):
                pinned = capture.lock(queryset)
                result = self._normalise(self.func(panel, request, pinned))
                capture.read_after()
            if result.undo is None and result.level != "error":
                result = replace(
                    result,
                    undo=capture.plan(
                        panel, verb=self.permission or "view", what="action", label=self.label
                    ),
                )
        if hooked and result.undo is None and result.undo_state is not None:
            if result.level != "error":
                summary, done = undo.describe(panel, count, "action", label=str(self.label))
                result = replace(
                    result,
                    undo=undo.UndoPlan(
                        model=panel.model,
                        verb=self.permission or "view",
                        summary=summary,
                        done=done,
                        steps=[{"kind": "custom", "action": self.name, "state": result.undo_state}],
                    ),
                )
        return result

    def _normalise(self, outcome) -> ActionResult:
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
    undo_fields: list[str] | None = None,
    undo: Callable | str | None = None,
):
    """Mark a panel method as an action.

    Used bare or called: ``@action`` and ``@action("Mark as paid")`` both work, because
    forgetting the parentheses is the mistake everyone makes once.

    An action offers Undo in its toast only when it says how: ``undo_fields=["status"]``
    for the common update-style action, or ``undo="undo_mark_paid"`` naming a panel
    method that receives the run's ``ActionResult.undo_state``. Nothing is inferred —
    an action is arbitrary code, and guessing its inverse is how undo loses data.
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
            "undo_fields": list(undo_fields) if undo_fields else None,
            "undo": undo,
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
    from vantage.approvals import guarded_for

    # An action that writes a field needing approval — the built-in Publish on a
    # guarded ``status``, or anything declaring it in ``undo_fields`` — is one
    # ``UPDATE`` over the selection and cannot be held back row by row. So it is not
    # offered to someone whose change would need approving; see vantage.approvals.
    held = set(guarded_for(request, panel))
    return [
        act
        for act in resolved
        if (
            act.permission is None
            or permissions.has_model_perm(request, panel.model, act.permission)
        )
        and not held.intersection(act.undo_fields or ())
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
                # Exactly reversible: one column, written by one UPDATE.
                undo_fields=[pattern.field],
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
        # A built-in, reachable by name only when the panel has nothing of its own
        # called that — a panel method always wins.
        return BUILTIN_ACTIONS.get(str(entry))

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
            "undo_fields": None,
            "undo": None,
        }
    return Action(func=func, name=getattr(func, "__name__", str(entry)), **meta)


# ── built-in actions ────────────────────────────────────────────────────────


def _delete_selected(panel, request, queryset) -> ActionResult:
    """Delete the selection: into the trash where the model has one, for good otherwise.

    The same two outcomes the single-record Delete page has, so the bulk bar cannot
    destroy what the per-row button would only have trashed. Rows already in the trash
    are destroyed — that is what Delete means inside a trash. Everything runs in one
    transaction: a ``PROTECT`` reference on one row leaves every row as it was.
    """
    from django.db.models.deletion import ProtectedError, RestrictedError

    from vantage import undo

    model = panel.model
    pattern = panel.get_patterns().soft_delete
    db = router.db_for_write(model)
    wants_undo = undo.armed(request)
    if pattern is not None:
        live = list(queryset.filter(pattern.live).order_by().values_list("pk", flat=True))
        dead = list(queryset.filter(pattern.trashed).order_by().values_list("pk", flat=True))
    else:
        live, dead = [], list(queryset.order_by().values_list("pk", flat=True))

    steps: list[dict] = []
    reversible = True
    trashed = destroyed = related = 0
    try:
        with transaction.atomic(using=db):
            if live:
                trashed, step = undo.soft_delete(
                    panel, model._base_manager.using(db).filter(pk__in=live), capture=wants_undo
                )
                if step is None:
                    reversible = False
                else:
                    steps.append(step)
            if dead:
                outcome = undo.delete(panel, _deletable(model, db, dead), request=request)
                destroyed = outcome.per_model.get(model._meta.label, 0)
                related = max(outcome.total - destroyed, 0)
                if outcome.step is None:
                    reversible = False
                else:
                    steps.append(outcome.step)
    except (ProtectedError, RestrictedError) as exc:
        blockers = sorted({str(obj._meta.verbose_name_plural) for obj in exc.args[1]})
        return ActionResult(
            _("Nothing was deleted: %(blockers)s still depend on some of these records.")
            % {"blockers": ", ".join(blockers)},
            level="error",
        )

    one, many = str(panel.get_label()).lower(), str(panel.get_label_plural()).lower()
    parts = []
    if trashed:
        parts.append(
            ngettext(
                "Moved %(count)s %(model)s to the trash.",
                "Moved %(count)s %(models)s to the trash.",
                trashed,
            )
            % {"count": trashed, "model": one, "models": many}
        )
    if destroyed and related:
        parts.append(
            ngettext(
                "Deleted %(count)s %(model)s and %(related)s related records.",
                "Deleted %(count)s %(models)s and %(related)s related records.",
                destroyed,
            )
            % {"count": destroyed, "model": one, "models": many, "related": related}
        )
    elif destroyed:
        parts.append(
            ngettext("Deleted %(count)s %(model)s.", "Deleted %(count)s %(models)s.", destroyed)
            % {"count": destroyed, "model": one, "models": many}
        )
    message = " ".join(parts) or _("Nothing was deleted.")

    plan = None
    if wants_undo and reversible and steps:
        summary, done = undo.describe(panel, trashed + destroyed, "delete")
        plan = undo.UndoPlan(model=model, verb="delete", summary=summary, done=done, steps=steps)
    return ActionResult(message, undo=plan)


def _deletable(model, db, pks):
    """The queryset a hard delete runs on.

    The default manager's own queryset class when it overrides ``delete()`` — the
    project's delete has to be the one that runs — and the base manager otherwise, so a
    default manager that hides trashed rows cannot hide the very rows being purged.
    """
    default = model._default_manager.db_manager(db).all()
    if type(default).delete is not QuerySet.delete:
        return default.filter(pk__in=pks)
    return model._base_manager.using(db).filter(pk__in=pks)


#: ``actions = ["delete_selected", …]`` on a panel. Opt-in, like Django admin's
#: action of the same name is not: a bulk delete appearing on every list of every
#: project the day it upgrades is a change nobody asked for.
delete_selected = Action(
    func=_delete_selected,
    name="delete_selected",
    label=_("Delete"),
    icon="trash",
    permission="delete",
    destructive=True,
    bulk_strategy="instances",
    keywords=["remove", "trash", "destroy"],
)

BUILTIN_ACTIONS: dict[str, Action] = {"delete_selected": delete_selected}


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
