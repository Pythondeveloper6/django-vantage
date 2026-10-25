"""The single facade over Django's ``Model._meta``.

Nothing else in Vantage touches ``_meta`` directly. Django documents only three
members of that API as public — ``get_field()``, ``get_fields()`` and ``pk_fields`` —
and marks ``Options.fields``, ``.concrete_fields``, ``.many_to_many`` and
``.related_objects`` as *"Private API intended only to be used by Django itself."*
``django.contrib.admin`` uses them freely because it ships in the same tarball as the
ORM; a third-party package does not have that licence. Concentrating the access here
means one file to fix if Django's internals move.

Three facts this module exists to encode correctly, each a documented trap:

* ``ManyToOneRel.many_to_one`` is ``False`` and ``.one_to_many`` is ``True``. The flags
  describe the relation *as traversed from the current model*, not the class name.
  Django's own source calls this "unfortunate".
* ``GenericForeignKey`` has ``many_to_one = True`` but ``related_model is None``.
  Assuming otherwise is the most common crash in third-party admin replacements.
* An auto-created M2M through model has ``Meta.auto_created`` set to the *owning model
  class*, not ``True``.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Field, Model
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields.reverse_related import ForeignObjectRel

__all__ = [
    "PathHop",
    "concrete_fields",
    "displayable_fields",
    "editable_fields",
    "generic_foreign_keys",
    "generic_relations",
    "is_auto_created_through",
    "m2m_fields",
    "relation_attr_name",
    "relation_query_name",
    "resolve_path",
    "reverse_relations",
    "through_model",
]


# ── field enumeration ───────────────────────────────────────────────────────


def concrete_fields(model: type[Model]) -> list[Field]:
    """Forward, database-backed, non-M2M fields, including inherited ones."""
    return [
        f
        for f in model._meta.get_fields()
        if isinstance(f, Field) and f.concrete and not f.many_to_many
    ]


def editable_fields(model: type[Model]) -> list[Field]:
    """Concrete fields a user may actually type into."""
    return [f for f in concrete_fields(model) if f.editable and not f.auto_created]


def displayable_fields(model: type[Model]) -> list[Field]:
    """Concrete fields worth considering for a list column.

    Includes non-editable fields such as ``auto_now_add`` timestamps, which are
    valuable to *show* even though they cannot be typed into.
    """
    return [f for f in concrete_fields(model) if not f.primary_key or f.name == "id"]


# ── relations ───────────────────────────────────────────────────────────────


def reverse_relations(
    model: type[Model], *, include_hidden: bool = False
) -> list[ForeignObjectRel]:
    """Relations pointing *at* this model: reverse FK, reverse O2O, reverse M2M."""
    return [
        f
        for f in model._meta.get_fields(include_hidden=include_hidden)
        if isinstance(f, ForeignObjectRel)
    ]


def m2m_fields(model: type[Model]) -> list[Field]:
    """Forward many-to-many fields declared on this model."""
    return [
        f
        for f in model._meta.get_fields()
        if f.is_relation and f.many_to_many and not isinstance(f, ForeignObjectRel)
    ]


def through_model(m2m_field: Field) -> type[Model]:
    return m2m_field.remote_field.through


def is_auto_created_through(through: type[Model]) -> bool:
    """Whether Django generated this through model rather than the user writing it.

    ``Meta.auto_created`` holds the *owning model class* on generated through models,
    so this must be a truthiness test and never an ``is True`` comparison.

    The distinction decides the widget: an auto-created through model can be edited
    with a multi-select, because ``.add()`` / ``.set()`` work. An explicit through
    model carrying extra fields must be rendered as a nested inline instead.
    """
    return bool(through._meta.auto_created)


def generic_foreign_keys(model: type[Model]) -> list[GenericForeignKey]:
    """GenericForeignKeys, which live in ``private_fields`` rather than ``fields``."""
    return [f for f in model._meta.private_fields if isinstance(f, GenericForeignKey)]


def generic_relations(model: type[Model]) -> list[GenericRelation]:
    return [f for f in model._meta.get_fields() if isinstance(f, GenericRelation)]


def relation_query_name(rel: ForeignObjectRel) -> str:
    """The name to use in ``filter()`` / ``annotate()`` paths."""
    return rel.name


def relation_attr_name(rel: ForeignObjectRel) -> str | None:
    """The name to use with ``getattr()``.

    Differs from :func:`relation_query_name` when ``related_query_name`` is set
    explicitly, and is ``None`` for a symmetrical self-referential M2M, which has no
    reverse accessor at all.
    """
    return rel.accessor_name


# ── path resolution ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathHop:
    """One segment of a ``customer__country__name`` style path."""

    name: str
    field: object
    model: type[Model]

    @property
    def is_relation(self) -> bool:
        return bool(getattr(self.field, "is_relation", False))

    @property
    def is_multivalued(self) -> bool:
        """Whether traversing this hop can yield more than one row.

        True for reverse FK, M2M and generic relations — the hops that force
        ``prefetch_related`` rather than ``select_related``.
        """
        field = self.field
        return bool(getattr(field, "one_to_many", False) or getattr(field, "many_to_many", False))

    @property
    def is_joinable(self) -> bool:
        """Whether this hop can be followed with a SQL join.

        Forward FK and both directions of a one-to-one qualify. Note a *reverse*
        one-to-one is joinable despite ``concrete`` being False — the one non-concrete
        relation Django can ``select_related``.
        """
        field = self.field
        if isinstance(field, GenericForeignKey):
            return False
        return (
            bool(getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False))
            and not self.is_multivalued
        )


def resolve_path(model: type[Model], path: str) -> list[PathHop]:
    """Resolve a ``__``-separated field path into its hops.

    Stops cleanly at the first segment that is not a model field, so a path ending in
    a lookup (``created_at__gte``) resolves its field portion and ignores the rest.
    Returns an empty list when the very first segment does not resolve.
    """
    hops: list[PathHop] = []
    current = model
    for part in path.split(LOOKUP_SEP):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist:
            break
        hops.append(PathHop(name=part, field=field, model=current))
        related = getattr(field, "related_model", None)
        if related is None:
            break
        current = related
    return hops
