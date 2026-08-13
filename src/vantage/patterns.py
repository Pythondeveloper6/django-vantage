"""Recognising the modelling conventions a project already follows.

Django has no framework-level notion of a tree, a soft delete or a publishable record,
but the community has settled on a small vocabulary for each, and a panel that
recognises that vocabulary can offer the matching affordance — a tree view, a trash bin
with restore, a timeline, drag-to-reorder, publish/unpublish — with no configuration at
all. That is the last piece of the zero-config thesis.

Every function here is pure in the same sense as :mod:`vantage.inference`: model in,
description out. No queries, so a detector never learns how many rows exist or what is
in them; no request; no clock. Optional libraries are soft-detected with
``apps.is_installed`` rather than ``try: import``, because a package being importable
says nothing about whether *this project* uses it, and importing an optional dependency
is a side effect a pure function has no business having.

The bar for a detection is deliberately higher than for column inference, because here
a false positive costs more than a miss: a model wrongly treated as soft-deleted *hides
rows*, and one wrongly treated as a tree renders nonsense. So every detector wants a
name the convention dictates **and** a field type that makes the convention work, and
every detector documents what it declines.

Overriding is uniform across all seven. A boolean panel attribute switches a detector
off (``tree = False``); a ``*_field`` attribute names the field to use instead of
inferring one (``soft_delete_field = "archived_at"``). Detectors accept either a panel
or a bare model, so the whole module is table-testable without a panel in sight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import Q

from vantage.inference import TRAILING_NAMES, infer_list_link
from vantage.introspection import concrete_fields, relation_attr_name, reverse_relations

__all__ = [
    "ModelPatterns",
    "MoneyField",
    "OwnershipPattern",
    "PublishablePattern",
    "SoftDeletePattern",
    "SortablePattern",
    "TimelinePattern",
    "TreePattern",
    "detect_all",
    "detect_money",
    "detect_ownership",
    "detect_publishable",
    "detect_soft_delete",
    "detect_sortable",
    "detect_timeline",
    "detect_tree",
]

# ── the vocabulary ──────────────────────────────────────────────────────────

#: Self-referential FK names that mean "my parent in a hierarchy". A self-FK called
#: anything else — ``duplicate_of``, ``replied_to``, ``original``, ``merged_into`` — is
#: a link between two records, not a tree, and drawing it as one produces nonsense.
PARENT_NAMES = ("parent", "parent_id")

#: django-mptt's bookkeeping columns. All four, or it is not an mptt model.
MPTT_FIELDS = ("lft", "rght", "tree_id", "level")

#: django-treebeard, materialised path (``MP_Node``) — the common case.
TREEBEARD_MP_FIELDS = ("path", "depth", "numchild")

#: django-treebeard, nested set (``NS_Node``). ``rgt`` rather than mptt's ``rght`` is
#: the only thing separating the two shapes, so the spelling is load-bearing.
TREEBEARD_NS_FIELDS = ("lft", "rgt", "tree_id", "depth")

#: django-treebeard, adjacency list (``AL_Node``): a parent FK plus a sibling order.
TREEBEARD_AL_FIELD = "sib_order"

#: Soft-delete markers in priority order. Type decides how each is read, so ``deleted``
#: is a flag when it is a boolean and a timestamp when it is a date.
SOFT_DELETE_NAMES = ("deleted_at", "date_deleted", "deleted_on", "is_deleted", "deleted")

#: Audit timestamps that will do for a timeline when the model has no date of its own.
#: ``TRAILING_NAMES`` in :mod:`vantage.inference` already encodes the domain/audit split;
#: this is the half of it that records a *creation*.
TIMELINE_AUDIT_NAMES = ("created_at", "created", "date_created", "timestamp", "date_joined")

#: Timestamps that move. Grouping rows by one answers a question nobody asked: the
#: buckets change every time a row is touched.
TIMELINE_MUTATING_NAMES = tuple(n for n in TRAILING_NAMES if n not in TIMELINE_AUDIT_NAMES)

#: The ``last_login`` / ``last_seen`` family. Not ``auto_now``, but rewritten just as
#: often, and never the date the row is *about*.
TIMELINE_MUTATING_PREFIX = "last_"

#: Integer columns that hold a manual position, in priority order.
POSITION_NAMES = ("position", "sort_order", "display_order", "sequence", "order", "sort")

#: Fields whose choices are worth reading for a publication state.
STATUS_NAMES = ("status", "state", "publication_status", "visibility")

#: Choice values (or labels) that mean the record is visible to the public.
PUBLISHED_TOKENS = ("published", "publish", "public", "live")

#: ...and the ones that mean it is not. Absent from a model's choices, "not published"
#: is used instead, which is correct for any two-state workflow.
DRAFT_TOKENS = ("draft", "unpublished", "private", "hidden")

#: Boolean and timestamp spellings of the same idea.
PUBLISHED_FLAG_NAMES = ("is_published", "published", "is_public", "is_live")
PUBLISHED_DATE_NAMES = ("published_at", "date_published", "publish_date", "published_on")

#: FK names that mean "the user this row belongs to", in priority order. This is the
#: hook a multi-tenant scope attaches to, so the ordering matters: ``owner`` beats
#: ``created_by``, because the second records history and the first records tenancy.
OWNER_NAMES = ("owner", "user", "created_by", "author", "creator", "added_by")

#: A money word must *end* the field name. Anchoring the suffix is what keeps
#: ``total_weight`` and ``price_rank`` out while letting ``unit_price``,
#: ``total_amount`` and ``shipping_fee`` in.
MONEY_NAME = re.compile(
    r"(^|_)(price|total|subtotal|amount|cost|fee|balance|revenue)$",
    re.IGNORECASE,
)


# ── descriptions ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreePattern:
    """A hierarchy the panel can render as an expandable tree.

    ``ordering`` sorts siblings and roots alike; ``roots`` selects the top level.
    Together they are enough to render a tree without knowing which library, if any,
    produced the columns.
    """

    library: str  # "self_fk" | "mptt" | "treebeard"
    parent_field: str | None
    children_accessor: str | None
    ordering: tuple[str, ...]
    roots: Q
    depth_field: str | None


@dataclass(frozen=True)
class SoftDeletePattern:
    """Rows that are hidden rather than destroyed.

    When ``manager_hides_trashed`` is set, the model's default manager already filters
    the trashed rows out, so a trash view has to go through ``_base_manager`` to see
    them at all. That is the single most important thing this detection reports.
    """

    field: str
    kind: str  # "timestamp" | "boolean"
    live: Q
    trashed: Q
    manager_hides_trashed: bool


@dataclass(frozen=True)
class TimelinePattern:
    """A date to group rows by, per day, week or month."""

    field: str
    kind: str  # "datetime" | "date"
    is_audit: bool
    nullable: bool


@dataclass(frozen=True)
class SortablePattern:
    """A manual position, so the list can offer drag-to-reorder.

    ``scope`` names the fields a position is unique *within* — chapters ordered within
    a book. Reordering across a scope boundary is meaningless, so a UI must respect it.
    """

    field: str
    library: str | None  # "ordered_model" | None
    scope: tuple[str, ...]


@dataclass(frozen=True)
class PublishablePattern:
    """A two-state publication workflow the panel can offer buttons for.

    For ``kind == "timestamp"`` both values are ``None`` and must not be assigned:
    publishing means writing the current time, and a pure detector cannot read the
    clock without freezing it at import. The caller supplies ``timezone.now()``.
    """

    field: str
    kind: str  # "status" | "boolean" | "timestamp"
    published: Q
    unpublished: Q
    publish_value: Any
    unpublish_value: Any


@dataclass(frozen=True)
class OwnershipPattern:
    """The FK a per-user or per-tenant scope would filter on."""

    field: str
    user_model: type[models.Model]
    nullable: bool


@dataclass(frozen=True)
class MoneyField:
    """A currency column. ``decimal_places`` is what the formatter needs."""

    name: str
    decimal_places: int


@dataclass(frozen=True)
class ModelPatterns:
    """Every detection for one model."""

    model: type[models.Model]
    tree: TreePattern | None
    soft_delete: SoftDeletePattern | None
    timeline: TimelinePattern | None
    sortable: SortablePattern | None
    publishable: PublishablePattern | None
    ownership: OwnershipPattern | None
    money: tuple[MoneyField, ...]

    @property
    def detected(self) -> tuple[str, ...]:
        """The names of the patterns that fired. Useful in a scaffold or a debug page."""
        names = ("tree", "soft_delete", "timeline", "sortable", "publishable", "ownership")
        found = [name for name in names if getattr(self, name) is not None]
        if self.money:
            found.append("money")
        return tuple(found)


# ── shared plumbing ─────────────────────────────────────────────────────────


def _split(target: Any) -> tuple[type[models.Model], Any]:
    """Accept a panel or a bare model.

    Tested with ``issubclass`` rather than ``getattr(target, "model")`` because a model
    with a field called ``model`` would otherwise hand back its own descriptor.
    """
    if isinstance(target, type) and issubclass(target, models.Model):
        return target, None
    return target.model, target


def _off(panel: Any, attribute: str) -> bool:
    return panel is not None and getattr(panel, attribute, None) is False


def _forced(panel: Any, attribute: str) -> bool:
    return panel is not None and getattr(panel, attribute, None) is True


def _declared(panel: Any, attribute: str) -> Any:
    return None if panel is None else getattr(panel, attribute, None)


def _by_name(model: type[models.Model]) -> dict[str, models.Field]:
    return {f.name: f for f in concrete_fields(model)}


def _installed(app_label: str) -> bool:
    """Whether an optional library is part of *this project*, not merely importable."""
    return apps.is_installed(app_label)


def _has_all(names: set[str], required: tuple[str, ...]) -> bool:
    return all(name in names for name in required)


def _choice_pairs(field: models.Field) -> list[tuple[Any, Any]]:
    """Flatten grouped choices, which nest a list of pairs under a group label."""
    pairs: list[tuple[Any, Any]] = []
    for value, label in field.choices or ():
        if isinstance(label, list | tuple):
            pairs.extend(label)
        else:
            pairs.append((value, label))
    return pairs


def _matches_token(pair: tuple[Any, Any], tokens: tuple[str, ...]) -> bool:
    """Whole-value comparison on both halves of a choice.

    Exact rather than substring, so ``unpublished`` never reads as ``published`` and a
    status called ``republish_pending`` is left alone.
    """
    return any(str(half).strip().lower() in tokens for half in pair)


# ── 1. tree ─────────────────────────────────────────────────────────────────


def _self_referential_fks(model: type[models.Model]) -> list[models.Field]:
    """Forward FKs pointing back at this model.

    ``related_model is None`` is the GenericForeignKey case — it reports
    ``many_to_one = True`` and has no target at all, and assuming otherwise is the most
    common crash in third-party admin replacements. ``issubclass`` rather than identity
    so that a self-FK declared on an abstract or multi-table parent still counts.
    """
    found = []
    for field in concrete_fields(model):
        if not (field.is_relation and field.many_to_one):
            continue
        related = field.related_model
        if related is None or not issubclass(model, related):
            continue
        found.append(field)
    return found


def _parent_field(model: type[models.Model], declared: str | None, forced: bool):
    candidates = _self_referential_fks(model)

    if declared:
        return next((f for f in candidates if declared in (f.name, f.attname)), None)

    if forced:
        # ``tree = True`` is the developer asserting the hierarchy, so the name rule is
        # waived — but only when there is exactly one self-FK to mean. Two, and which
        # one is the parent is a coin flip, so ``tree_parent_field`` has to say.
        return candidates[0] if len(candidates) == 1 else None

    return next(
        (f for f in candidates if (f.name in PARENT_NAMES or f.attname in PARENT_NAMES) and f.null),
        None,
    )


def _children_accessor(model: type[models.Model], parent) -> str | None:
    """The attribute holding a node's children.

    The relation is a ``ManyToOneRel``, whose ``one_to_many`` flag is the ``True`` one:
    the flags describe traversal *from this model*, not the class name.
    """
    for rel in reverse_relations(model):
        if rel.one_to_many and getattr(rel, "field", None) is parent:
            return relation_attr_name(rel)
    return None


def _sibling_ordering(model: type[models.Model]) -> tuple[str, ...]:
    """``Meta.ordering`` is the developer's own statement of intent and wins.

    Otherwise siblings sort by their label, because a tree is read alphabetically far
    more often than it is read newest-first.
    """
    if model._meta.ordering:
        return tuple(model._meta.ordering)
    return (infer_list_link(model),)


def detect_tree(target: Any) -> TreePattern | None:
    """A hierarchy: plain self-FK, django-mptt, or django-treebeard.

    Declined for any self-FK not named ``parent``. A ``ForeignKey("self")`` is a common
    way to record a duplicate, a revision or a reply, and rendering one of those as an
    expandable tree is worse than showing a flat list. Set ``tree_parent_field`` to name
    an unconventionally-spelled parent, or ``tree = True`` when the model has exactly one
    self-FK and it is the parent.

    Also declined when the conventional ``parent`` is not nullable: without a null
    parent the tree has no roots, so the view would render nothing.
    """
    model, panel = _split(target)
    if _off(panel, "tree"):
        return None

    parent = _parent_field(model, _declared(panel, "tree_parent_field"), _forced(panel, "tree"))
    names = {f.name for f in concrete_fields(model)}
    parent_name = parent.name if parent else None
    accessor = _children_accessor(model, parent) if parent else None

    if parent and _installed("mptt") and _has_all(names, MPTT_FIELDS):
        # tree_id then lft is a full pre-order traversal: parents immediately followed
        # by their descendants, which is the order a tree renders in.
        return TreePattern(
            library="mptt",
            parent_field=parent_name,
            children_accessor=accessor,
            ordering=("tree_id", "lft"),
            roots=Q(**{f"{parent_name}__isnull": True}),
            depth_field="level",
        )

    if _installed("treebeard"):
        if _has_all(names, TREEBEARD_MP_FIELDS):
            # The materialised path sorts the whole tree in one column, and treebeard
            # numbers depth from 1 rather than 0.
            return TreePattern(
                library="treebeard",
                parent_field=parent_name,
                children_accessor=accessor,
                ordering=("path",),
                roots=Q(depth=1),
                depth_field="depth",
            )
        if _has_all(names, TREEBEARD_NS_FIELDS):
            return TreePattern(
                library="treebeard",
                parent_field=parent_name,
                children_accessor=accessor,
                ordering=("tree_id", "lft"),
                roots=Q(depth=1),
                depth_field="depth",
            )
        if parent and TREEBEARD_AL_FIELD in names:
            return TreePattern(
                library="treebeard",
                parent_field=parent_name,
                children_accessor=accessor,
                ordering=(TREEBEARD_AL_FIELD,),
                roots=Q(**{f"{parent_name}__isnull": True}),
                depth_field=None,
            )

    if parent:
        return TreePattern(
            library="self_fk",
            parent_field=parent_name,
            children_accessor=accessor,
            ordering=_sibling_ordering(model),
            roots=Q(**{f"{parent_name}__isnull": True}),
            depth_field=None,
        )

    return None


# ── 2. soft delete ──────────────────────────────────────────────────────────


def _manager_hides_trashed(model: type[models.Model]) -> bool:
    """Whether the default manager already filters the trashed rows out.

    ``_base_manager`` is a plain ``Manager`` unless ``Meta.base_manager_name`` says
    otherwise, so a differing class that overrides ``get_queryset`` is the tell. A
    custom manager that only adds helper methods inherits ``get_queryset`` unchanged
    and correctly reads as not filtering.
    """
    default = model._default_manager
    base = model._base_manager
    if type(default) is type(base):
        return False
    return type(default).get_queryset is not type(base).get_queryset


def detect_soft_delete(target: Any) -> SoftDeletePattern | None:
    """Rows hidden rather than destroyed, and whether a manager already hides them.

    The field name is only half the evidence; the type carries the rest. A ``deleted``
    that is a ``CharField`` is a status string whose values this module cannot know, so
    it is declined rather than guessed at — filtering on the wrong one would hide live
    rows, which is the worst thing this module could do.

    ``archived_at`` and friends are declined too: archiving is a domain state that
    happens to sound like deletion, and a panel must not put a user's records in a trash
    bin on the strength of a synonym. Name it with ``soft_delete_field`` to opt in.
    """
    model, panel = _split(target)
    if _off(panel, "soft_delete"):
        return None

    by_name = _by_name(model)
    declared = _declared(panel, "soft_delete_field")
    if declared:
        candidates = [declared] if declared in by_name else []
    else:
        candidates = [name for name in SOFT_DELETE_NAMES if name in by_name]

    for name in candidates:
        field = by_name[name]
        if isinstance(field, models.BooleanField):
            # ``~Q(flag=True)`` rather than ``Q(flag=False)`` so that a nullable flag
            # does not quietly drop every row that was never marked either way.
            return SoftDeletePattern(
                field=name,
                kind="boolean",
                live=~Q(**{name: True}),
                trashed=Q(**{name: True}),
                manager_hides_trashed=_manager_hides_trashed(model),
            )
        if isinstance(field, models.DateTimeField | models.DateField) and field.null:
            return SoftDeletePattern(
                field=name,
                kind="timestamp",
                live=Q(**{f"{name}__isnull": True}),
                trashed=Q(**{f"{name}__isnull": False}),
                manager_hides_trashed=_manager_hides_trashed(model),
            )

    return None


# ── 3. timeline ─────────────────────────────────────────────────────────────


def detect_timeline(target: Any) -> TimelinePattern | None:
    """The date to group rows by.

    Prefers a domain date — ``published_at``, ``due_date`` — over an audit timestamp,
    the same preference ``infer_list_filter`` makes, because "posts per week" means the
    week they were published in.

    Declines every timestamp that moves: ``auto_now`` fields, the ``updated``/
    ``modified`` names and the ``last_*`` family. A bucket that changes whenever a row is
    edited describes edit activity, not the data. Soft-delete timestamps are declined for
    the same reason they are not domain dates.
    """
    model, panel = _split(target)
    if _off(panel, "timeline"):
        return None

    by_name = _by_name(model)
    declared = _declared(panel, "timeline_field")
    if declared:
        field = by_name.get(declared)
        if field is None or not isinstance(field, models.DateField):
            return None
        return _timeline(field, is_audit=field.name in TIMELINE_AUDIT_NAMES)

    candidates = [
        f
        for f in concrete_fields(model)
        if isinstance(f, models.DateField)
        and not getattr(f, "auto_now", False)
        and f.name not in TIMELINE_MUTATING_NAMES
        and not f.name.startswith(TIMELINE_MUTATING_PREFIX)
        and f.name not in SOFT_DELETE_NAMES
    ]
    if not candidates:
        return None

    domain = next((f for f in candidates if f.name not in TIMELINE_AUDIT_NAMES), None)
    if domain is not None:
        return _timeline(domain, is_audit=False)

    for name in TIMELINE_AUDIT_NAMES:
        field = next((f for f in candidates if f.name == name), None)
        if field is not None:
            return _timeline(field, is_audit=True)
    return None


def _timeline(field: models.Field, *, is_audit: bool) -> TimelinePattern:
    return TimelinePattern(
        field=field.name,
        kind="datetime" if isinstance(field, models.DateTimeField) else "date",
        is_audit=is_audit,
        nullable=bool(field.null),
    )


# ── 4. sortable ─────────────────────────────────────────────────────────────


def _ordered_model_scope(model: type[models.Model]) -> tuple[str, ...]:
    scope = getattr(model, "order_with_respect_to", None)
    if not scope:
        return ()
    return (scope,) if isinstance(scope, str) else tuple(scope)


def detect_sortable(target: Any) -> SortablePattern | None:
    """A manual position column, so the list can offer drag-to-reorder.

    An integer is required, which is what separates a ``position`` from the very common
    ``order`` foreign key pointing at an ``Order`` model — the near-miss that makes this
    detector worth writing carefully. A choice field is declined too: its integers are
    an enumeration, not a sequence, and dragging a row would rewrite its meaning.
    """
    model, panel = _split(target)
    if _off(panel, "sortable"):
        return None

    by_name = _by_name(model)
    declared = _declared(panel, "sortable_field")
    if declared:
        field = by_name.get(declared)
        return SortablePattern(field=declared, library=None, scope=()) if field else None

    # The base class is read by name rather than imported, for the same reason the
    # library is soft-detected: an optional dependency must not be imported to answer
    # a question about a model that may have nothing to do with it.
    if _installed("ordered_model") and any(
        base.__name__ in ("OrderedModel", "OrderedModelBase") for base in model.__mro__
    ):
        name = getattr(model, "order_field_name", None) or "order"
        if name in by_name:
            return SortablePattern(
                field=name, library="ordered_model", scope=_ordered_model_scope(model)
            )

    # Django's own convention. ``Meta.order_with_respect_to`` synthesises ``_order``,
    # which is a position column by definition and needs no name heuristic at all.
    with_respect_to = model._meta.order_with_respect_to
    if with_respect_to is not None and "_order" in by_name:
        scope = getattr(with_respect_to, "name", with_respect_to)
        return SortablePattern(field="_order", library=None, scope=(str(scope),))

    for name in POSITION_NAMES:
        field = by_name.get(name)
        if (
            field is not None
            and isinstance(field, models.IntegerField)
            and not field.choices
            and not field.primary_key
        ):
            return SortablePattern(field=name, library=None, scope=())

    return None


# ── 5. publishable ──────────────────────────────────────────────────────────


def _publishable_status(field: models.Field) -> PublishablePattern | None:
    pairs = _choice_pairs(field)
    published = next((p for p in pairs if _matches_token(p, PUBLISHED_TOKENS)), None)
    if published is None:
        return None

    draft = next((p for p in pairs if _matches_token(p, DRAFT_TOKENS)), None)
    name = field.name
    return PublishablePattern(
        field=name,
        kind="status",
        published=Q(**{name: published[0]}),
        # Not-published rather than is-draft: a workflow with a review or scheduled
        # state has more than two values, and everything that is not live is unpublished.
        unpublished=~Q(**{name: published[0]}),
        publish_value=published[0],
        unpublish_value=draft[0] if draft else None,
    )


def detect_publishable(target: Any) -> PublishablePattern | None:
    """A publication state the panel can offer publish/unpublish for.

    A status field wins over a boolean, which wins over a timestamp: when a model has
    both ``status`` and ``published_at``, the status is the authority and the timestamp
    is a record of when it last changed.

    Choice values are matched whole, never as substrings, so ``unpublished`` cannot read
    as ``published``. A status whose choices are a delivery workflow — pending, paid,
    shipped — is declined, because there is no value in it that means "visible".

    Scheduled publishing is deliberately not modelled. ``published_at <= now`` is the
    right predicate for a site, but a detector may not read the clock, and a stale
    ``now`` baked in at import would be worse than leaving it to the caller.
    """
    model, panel = _split(target)
    if _off(panel, "publishable"):
        return None

    by_name = _by_name(model)
    declared = _declared(panel, "publishable_field")
    names = [declared] if declared else [*STATUS_NAMES, *PUBLISHED_FLAG_NAMES]

    for name in names:
        field = by_name.get(name)
        if field is None:
            continue
        if field.choices:
            found = _publishable_status(field)
            if found:
                return found
        if isinstance(field, models.BooleanField):
            return PublishablePattern(
                field=name,
                kind="boolean",
                published=Q(**{name: True}),
                unpublished=~Q(**{name: True}),
                publish_value=True,
                unpublish_value=False,
            )

    for name in [declared] if declared else PUBLISHED_DATE_NAMES:
        field = by_name.get(name)
        # A non-nullable publication date makes every row permanently published, so
        # there is nothing for an unpublish button to do.
        if isinstance(field, models.DateTimeField | models.DateField) and field.null:
            return PublishablePattern(
                field=name,
                kind="timestamp",
                published=Q(**{f"{name}__isnull": False}),
                unpublished=Q(**{f"{name}__isnull": True}),
                publish_value=None,
                unpublish_value=None,
            )

    return None


# ── 6. ownership ────────────────────────────────────────────────────────────


def _user_model() -> type[models.Model] | None:
    """The project's user model, or ``None`` when there isn't one to compare against."""
    from django.contrib.auth import get_user_model

    try:
        return get_user_model()
    except (ImproperlyConfigured, LookupError):  # pragma: no cover
        return None


def detect_ownership(target: Any) -> OwnershipPattern | None:
    """The FK a per-user or per-tenant scope would filter on.

    The target must be ``AUTH_USER_MODEL`` itself. A ``Post.author`` pointing at an
    ``Author`` profile model is not an ownership hook however suggestive the name is —
    the panel cannot scope rows to the request's user through a model the request's user
    is not. Name the field with ``owner_field`` if the hop is indirect.
    """
    model, panel = _split(target)
    if _off(panel, "ownership"):
        return None

    user_model = _user_model()
    if user_model is None:
        return None

    by_name = _by_name(model)
    declared = _declared(panel, "owner_field")
    names = [declared] if declared else OWNER_NAMES

    for name in names:
        field = by_name.get(name)
        if field is None or not field.is_relation:
            continue
        if not (field.many_to_one or field.one_to_one):
            continue
        if field.related_model is not user_model:
            continue
        return OwnershipPattern(field=name, user_model=user_model, nullable=bool(field.null))

    return None


# ── 7. money ────────────────────────────────────────────────────────────────


def detect_money(target: Any) -> tuple[MoneyField, ...]:
    """Currency columns, for formatting and for a smarter summary bar.

    ``DecimalField`` only. Money in a float is a bug, and treating one as currency would
    dress up a rounding error as a total. The currency itself is never guessed: there is
    nothing on the model that says which one, and a wrong symbol is a worse lie than no
    symbol.
    """
    model, panel = _split(target)
    if _off(panel, "money"):
        return ()

    by_name = _by_name(model)
    declared = _declared(panel, "money_fields")
    names = list(declared) if declared else [f.name for f in concrete_fields(model)]

    found = []
    for name in names:
        field = by_name.get(name)
        if not isinstance(field, models.DecimalField):
            continue
        if not declared and not MONEY_NAME.search(name):
            continue
        found.append(MoneyField(name=name, decimal_places=field.decimal_places or 0))
    return tuple(found)


# ── everything at once ──────────────────────────────────────────────────────


def detect_all(target: Any) -> ModelPatterns:
    """Every detection for one model, from a panel or a bare model."""
    model, _ = _split(target)
    return ModelPatterns(
        model=model,
        tree=detect_tree(target),
        soft_delete=detect_soft_delete(target),
        timeline=detect_timeline(target),
        sortable=detect_sortable(target),
        publishable=detect_publishable(target),
        ownership=detect_ownership(target),
        money=detect_money(target),
    )
