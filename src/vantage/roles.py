"""Roles: Django's ``auth.Group``, managed as named bundles of permissions.

A group *is* a role in every project that uses them, so this module adds no model of
its own for the role — it is a set of careful operations over the tables Django already
has. The one table it adds hangs off the group: the role's field rules
(:class:`~vantage.models.FieldPermission`), edited in the same matrix, saved in the same
transaction and covered by the same conflict token as its permissions.
The views in :mod:`vantage.rolesviews` are thin; everything that decides who may do
what, and what actually changes, lives here so it can be tested without HTTP.

Four decisions shape it, and each rules out the obvious version.

**Escalation is the threat model, not vandalism.** A screen that edits permissions is
the one place in a panel where a user can hand themselves more power. Superusers only
by default (``VANTAGE["ROLES_PERMISSION"]`` widens it), and anyone who is *not* a
superuser works inside their :class:`Reach`: they may grant or revoke only permissions
they hold themselves, and may only rename, duplicate, delete or change the members of a
role whose every permission they hold. Without the second half, "add myself to Admins"
would be a one-click escalation that never touched a checkbox.

**The client is never trusted with a permission id, or a field rule.** Every id arriving from the
browser is checked against the catalogue loaded from the database and against the
actor's reach; a field rule must name a field of a model this panel registers, and the
actor must hold ``change`` on that model and be unrestricted on that field themselves.
Unknown ids, and ids the actor may not touch, are dropped rather than
rejected with an error: the honest outcome of a stale tab or a hand-edited form is
"nothing you were not allowed to do happened", and that is also what the audit log
records.

**Two admins editing one role conflict, they do not clobber.** ``auth.Group`` has no
version column for :mod:`vantage.concurrency` to borrow, so the token here is a
fingerprint of the role's name and permission set. The check runs *under a write lock*
taken by a no-op ``UPDATE`` of the group row: on Postgres and MySQL that is a row lock
for the rest of the transaction, and on SQLite it promotes the transaction to the
database write lock — either way nobody can change the role between the comparison and
the write. A ``SELECT … FOR UPDATE`` would have been the textbook choice, but SQLite
silently ignores it, and a lock that only exists on some backends is not a lock.

**The matrix costs the same at five models and five hundred.** The whole grid is built
from one query over ``Permission`` (with its content type) and one over the role's
permission ids; everything else — grouping by app, spotting the four standard verbs,
deciding which models this panel registers — is done in Python against the app
registry, which costs no queries at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import Count, F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

__all__ = [
    "ACTIONS",
    "MAX_NAME_LENGTH",
    "AppSection",
    "Catalogue",
    "Cell",
    "DeleteImpact",
    "EffectiveRow",
    "FieldCell",
    "FieldRestriction",
    "ModelRow",
    "PermissionChange",
    "Reach",
    "RoleConflict",
    "audit_members",
    "audit_permissions",
    "build_matrix",
    "change_members",
    "clean_role_name",
    "create_role",
    "current_field_rules",
    "current_permission_ids",
    "delete_impact",
    "delete_role",
    "duplicate_role",
    "effective_field_rules",
    "effective_permissions",
    "fingerprint",
    "load_catalogue",
    "may_manage_roles",
    "members_of",
    "parse_field_rules",
    "parse_ids",
    "perm_label",
    "reach_for",
    "rename_role",
    "role_rows",
    "roles_of",
    "save_permissions",
    "supports_membership",
]

#: The four verbs Django creates for every model, in the order the matrix shows them.
ACTIONS: tuple[str, ...] = ("view", "add", "change", "delete")

#: ``Group.name``'s own limit, read from the field so a project on a patched Django
#: still gets the right answer.
MAX_NAME_LENGTH: int = Group._meta.get_field("name").max_length or 150


# ── who may open the screen ─────────────────────────────────────────────────


def may_manage_roles(request) -> bool:
    """Superusers, unless ``VANTAGE["ROLES_PERMISSION"]`` says otherwise.

    Superuser-only is the honest default for the screen that decides what everyone
    else may do. A permission string (``"auth.change_group"``) or a callable taking the
    request widens it — and widening it is safe precisely because of :class:`Reach`:
    whoever is let in can never hand out more than they already have.
    """
    from vantage.conf import vantage_settings

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    rule = vantage_settings.ROLES_PERMISSION
    is_superuser = bool(getattr(user, "is_superuser", False))
    if rule is None:
        return is_superuser
    if callable(rule):
        return bool(rule(request))
    return is_superuser or user.has_perm(str(rule))


# ── what the actor may touch ────────────────────────────────────────────────


def perm_label(permission: Permission) -> str:
    """``"shop.change_order"`` — the string ``user.has_perm`` takes, so it is the key
    both sides of every reach comparison use."""
    return f"{permission.content_type.app_label}.{permission.codename}"


@dataclass(frozen=True)
class Reach:
    """The permissions an actor may grant, revoke, or hand out through membership.

    ``everything`` for an active superuser; otherwise exactly the set
    ``user.get_all_permissions()`` reports, which already routes through every
    configured authentication backend — so a permission held through a custom backend
    counts, and one the actor merely *could* be given does not.
    """

    everything: bool
    held: frozenset[str] = frozenset()
    #: The actor's request, for their own field rules. See :meth:`covers_field`.
    request: Any = field(default=None, compare=False)

    def covers(self, permission: Permission) -> bool:
        return self.everything or perm_label(permission) in self.held

    def covers_all(self, permissions: Iterable[Permission]) -> bool:
        return self.everything or all(self.covers(p) for p in permissions)

    def covers_field(self, model: type[models.Model], name: str) -> bool:
        """Whether the actor may set a field rule on ``model.name`` for a role.

        Two conditions, mirroring the rule for permissions. They must hold ``change`` on
        the model — a rule decides what others may change there — and must be
        unrestricted on the field themselves: someone who cannot see ``cost`` lifting
        Support's rule on it is the same escalation as granting a permission they lack.
        """
        if self.everything:
            return True
        from django.contrib.auth import get_permission_codename

        opts = model._meta
        if f"{opts.app_label}.{get_permission_codename('change', opts)}" not in self.held:
            return False
        from vantage.fieldperms import access

        return access(self.request, model).can_change(name)


def reach_for(user, request=None) -> Reach:
    if user is not None and user.is_active and getattr(user, "is_superuser", False):
        return Reach(everything=True, request=request)
    if user is None or not user.is_authenticated:
        return Reach(everything=False, request=request)
    return Reach(everything=False, held=frozenset(user.get_all_permissions()), request=request)


# ── the permission catalogue ────────────────────────────────────────────────


@dataclass
class Catalogue:
    """Every ``Permission`` row, loaded once per request with its content type."""

    permissions: list[Permission]
    by_id: dict[int, Permission]

    def get(self, pk: int) -> Permission | None:
        return self.by_id.get(pk)


def load_catalogue() -> Catalogue:
    permissions = list(
        Permission.objects.select_related("content_type").order_by(
            "content_type__app_label", "content_type__model", "codename"
        )
    )
    return Catalogue(permissions=permissions, by_id={p.pk: p for p in permissions})


def current_permission_ids(group: Group) -> set[int]:
    return set(group.permissions.values_list("pk", flat=True))


def current_field_rules(group: Group) -> dict[tuple[int, str], str]:
    """The role's stored field rules, as ``{(content_type_id, field): level}``."""
    from vantage.models import FieldPermission

    return {
        (ct_id, name): level
        for ct_id, name, level in FieldPermission.objects.filter(group=group).values_list(
            "content_type_id", "field_name", "level"
        )
    }


def fingerprint(
    group: Group,
    permission_ids: Iterable[int],
    rules: dict[tuple[int, str], str] | None = None,
) -> str:
    """The optimistic-concurrency token for a role: its name, permissions and field rules.

    Membership is deliberately left out. Adding and removing members are idempotent
    deltas — "add Ada" means the same thing whatever else changed — so they cannot
    clobber anything, and folding them in would make the matrix report a conflict
    every time a colleague onboarded someone.

    ``rules`` are read from the database when not given. A role with none has the
    token it had before field rules existed, so a page open across an upgrade does
    not conflict with itself.
    """
    if rules is None:
        rules = current_field_rules(group) if group.pk is not None else {}
    parts = [str(group.pk), group.name, ",".join(str(i) for i in sorted(permission_ids))]
    if rules:
        entries = sorted(rules.items())
        parts.append(",".join(f"{ct}:{name}:{level}" for (ct, name), level in entries))
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def parse_field_rules(values: Iterable[str]) -> dict[tuple[int, str], str]:
    """``"12:price:readonly"`` entries, one per value or comma-separated, as a dict.

    The level is ``""`` (full access), ``"readonly"`` or ``"hidden"``. Anything else —
    a bad content type id, a field name that is not an identifier, an unknown level —
    is dropped without comment, for the reason :func:`parse_ids` gives. Whether the
    model and field exist, and whether the actor may touch them, is
    :func:`save_permissions`'s check, against the database and the registry.
    """
    from vantage.fieldperms import LEVELS

    rules: dict[tuple[int, str], str] = {}
    for value in values:
        for part in str(value).split(","):
            ct, _sep, rest = part.strip().partition(":")
            name, _sep2, level = rest.partition(":")
            if not ct.isdigit() or not name.isidentifier() or (level and level not in LEVELS):
                continue
            rules[(int(ct), name)] = level
    return rules


def parse_ids(values: Iterable[str]) -> set[int]:
    """Integers out of form values, which may be single ids or comma-separated runs.

    Anything that is not a positive integer is dropped without comment: a malformed id
    cannot name a permission, so it cannot be one the actor meant to change.
    """
    ids: set[int] = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part.isdigit() and int(part) > 0:
                ids.add(int(part))
    return ids


# ── the matrix ──────────────────────────────────────────────────────────────


@dataclass
class Cell:
    id: int
    codename: str
    name: str
    checked: bool
    locked: bool


@dataclass
class FieldCell:
    """One field's rule in the matrix: the stored level and what is shown now."""

    #: ``"<content type id>:<field name>"`` — the prefix of every value it posts.
    key: str
    name: str
    label: str
    #: The level as rendered: stored, or re-applied from a conflicting save.
    level: str
    #: The level stored when the page was built. The diff is taken against this.
    base: str
    #: The level ``field_permissions`` in code sets for this role: the floor.
    floor: str
    locked: bool


@dataclass
class ModelRow:
    key: str
    label: str
    app_label: str
    registered: bool
    cells: dict[str, Cell | None] = field(default_factory=dict)
    other: list[Cell] = field(default_factory=list)
    #: Field rules, for a model this panel registers. Empty otherwise.
    fields: list[FieldCell] = field(default_factory=list)

    @property
    def restricted(self) -> int:
        """How many fields carry a rule, stored or from code: the summary's count."""
        return sum(1 for f in self.fields if f.level or f.floor)

    @property
    def columns(self) -> list[tuple[str, Cell | None]]:
        return [(action, self.cells.get(action)) for action in ACTIONS]

    @property
    def search(self) -> str:
        words = [self.key, self.label, *(c.codename for c in self.all_cells)]
        return " ".join(words).lower()

    @property
    def all_cells(self) -> list[Cell]:
        return [c for c in self.cells.values() if c is not None] + self.other


@dataclass
class AppSection:
    key: str
    label: str
    rows: list[ModelRow]
    #: The "models this panel does not show" bucket, which the UI can hide.
    unregistered: bool = False

    @property
    def total(self) -> int:
        return sum(len(r.all_cells) for r in self.rows)

    @property
    def checked(self) -> int:
        return sum(1 for r in self.rows for c in r.all_cells if c.checked)


def build_matrix(
    site,
    catalogue: Catalogue,
    checked: set[int],
    reach: Reach,
    *,
    group: Group | None = None,
    rules: dict[tuple[int, str], str] | None = None,
    base_rules: dict[tuple[int, str], str] | None = None,
) -> list[AppSection]:
    """Rows of models grouped by app, standard verbs in columns, the rest in "other".

    A model this panel registers sits under its app. Everything else — framework
    tables, models a project excluded, content types whose model has since been
    deleted — goes into one trailing "Other models" section, because those permissions
    are real and grantable even though nobody can use them *here*, and hiding them
    would make the matrix lie about what a role holds.

    A registered model's row also carries its field rules: ``rules`` as displayed,
    ``base_rules`` as stored (they differ only after a conflict, when the actor's own
    changes are re-applied on top). Unregistered models get none — no page here would
    honour them.
    """
    rows: dict[tuple[str, str], ModelRow] = {}
    rules = rules or {}
    base_rules = rules if base_rules is None else base_rules
    for permission in catalogue.permissions:
        ct = permission.content_type
        key = (ct.app_label, ct.model)
        row = rows.get(key)
        if row is None:
            model = ct.model_class()
            registered = model is not None and site is not None and site.is_registered(model)
            label = (
                str(model._meta.verbose_name_plural).capitalize() if model is not None else ct.model
            )
            row = rows[key] = ModelRow(
                key=f"{ct.app_label}.{ct.model}",
                label=label,
                app_label=ct.app_label,
                registered=registered,
            )
            if registered:
                row.fields = _field_cells(site, model, ct.pk, group, rules, base_rules, reach)
        cell = Cell(
            id=permission.pk,
            codename=permission.codename,
            name=permission.name,
            checked=permission.pk in checked,
            locked=not reach.covers(permission),
        )
        action, _sep, rest = permission.codename.partition("_")
        if action in ACTIONS and rest == ct.model and row.cells.get(action) is None:
            row.cells[action] = cell
        else:
            row.other.append(cell)

    sections: dict[str, AppSection] = {}
    others: list[ModelRow] = []
    for row in rows.values():
        if not row.registered:
            others.append(row)
            continue
        section = sections.get(row.app_label)
        if section is None:
            section = sections[row.app_label] = AppSection(
                key=row.app_label, label=_app_label(row.app_label), rows=[]
            )
        section.rows.append(row)

    ordered = sorted(sections.values(), key=lambda s: str(s.label).lower())
    for section in ordered:
        section.rows.sort(key=lambda r: r.label.lower())
    if others:
        others.sort(key=lambda r: (_app_label(r.app_label).lower(), r.label.lower()))
        ordered.append(
            AppSection(key="_other", label=gettext("Other models"), rows=others, unregistered=True)
        )
    return ordered


def _field_cells(site, model, ct_id, group, rules, base_rules, reach) -> list[FieldCell]:
    from vantage.fieldperms import code_rules, rule_fields

    floor = code_rules(site.get_panel(model)).get(group.name, {}) if group is not None else {}
    return [
        FieldCell(
            key=f"{ct_id}:{f.name}",
            name=f.name,
            label=str(getattr(f, "verbose_name", f.name)).capitalize(),
            level=rules.get((ct_id, f.name), ""),
            base=base_rules.get((ct_id, f.name), ""),
            floor=floor.get(f.name, ""),
            locked=not reach.covers_field(model, f.name),
        )
        for f in rule_fields(model)
    ]


def _app_label(app_label: str) -> str:
    try:
        return str(django_apps.get_app_config(app_label).verbose_name)
    except LookupError:
        return app_label


# ── saving the matrix ───────────────────────────────────────────────────────


class RoleConflict(Exception):
    """Somebody changed the role between this actor reading it and writing it.

    Carries the state as it now stands, because the only useful response is to show
    it — with the actor's own intended changes re-applied on top, so nothing they did
    is lost and nothing the other person did is overwritten.
    """

    def __init__(
        self,
        group: Group,
        permission_ids: set[int],
        rules: dict[tuple[int, str], str] | None = None,
    ) -> None:
        super().__init__(f"Role {group.pk} was changed by someone else.")
        self.group = group
        self.permission_ids = permission_ids
        self.rules = current_field_rules(group) if rules is None else rules
        self.token = fingerprint(group, permission_ids, self.rules)


@dataclass
class PermissionChange:
    added: list[Permission]
    removed: list[Permission]
    #: Ids that arrived but were unknown or out of the actor's reach.
    ignored: int
    token: str
    #: ``(model label, field, from level, to level)`` per rule that moved.
    fields: list[tuple[str, str, str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.fields)


def _lock(group: Group) -> Group:
    """Take the role's row for the rest of the transaction and return it fresh.

    A no-op ``UPDATE`` rather than ``select_for_update`` — see the module docstring.
    Raises ``Group.DoesNotExist`` when the role was deleted under us.
    """
    if not Group.objects.filter(pk=group.pk).update(name=F("name")):
        raise Group.DoesNotExist
    return Group.objects.get(pk=group.pk)


def save_permissions(
    group: Group,
    *,
    token: str,
    add: set[int],
    remove: set[int],
    reach: Reach,
    catalogue: Catalogue,
    fields: dict[tuple[int, str], str] | None = None,
    site=None,
) -> PermissionChange:
    """Apply one reviewed diff to a role, atomically, or raise :class:`RoleConflict`.

    An id in both ``add`` and ``remove`` is contradictory and is dropped from both.

    ``fields`` are the field rules the actor set, ``{(content type id, field): level}``
    with ``""`` meaning full access. Each is checked against ``site``'s registry (the
    model must be one this panel shows, the field one a rule may name) and the actor's
    reach; whatever fails is counted in ``ignored``, like an out-of-reach permission.
    """
    contradictory = add & remove
    add, remove = add - contradictory, remove - contradictory

    def allowed(pk: int) -> bool:
        permission = catalogue.get(pk)
        return permission is not None and reach.covers(permission)

    wanted = _valid_field_rules(fields or {}, site, reach)
    ignored = (
        len(contradictory)
        + sum(1 for pk in add | remove if not allowed(pk))
        + len(fields or {})
        - len(wanted)
    )

    with transaction.atomic():
        locked = _lock(group)
        current = current_permission_ids(locked)
        rules = current_field_rules(locked)
        if fingerprint(locked, current, rules) != token:
            raise RoleConflict(locked, current, rules)
        to_add = {pk for pk in add if allowed(pk)} - current
        to_remove = {pk for pk in remove if allowed(pk)} & current
        if to_add:
            locked.permissions.add(*to_add)
        if to_remove:
            locked.permissions.remove(*to_remove)
        final = (current | to_add) - to_remove
        moved = {key: level for key, level in wanted.items() if rules.get(key, "") != level}
        _write_field_rules(locked, moved)
        final_rules = {k: v for k, v in {**rules, **moved}.items() if v}

    group.name = locked.name
    return PermissionChange(
        added=sorted((catalogue.by_id[pk] for pk in to_add), key=perm_label),
        removed=sorted((catalogue.by_id[pk] for pk in to_remove), key=perm_label),
        ignored=ignored,
        token=fingerprint(locked, final, final_rules),
        fields=sorted(
            (_model_label(ct), name, rules.get((ct, name), ""), level)
            for (ct, name), level in moved.items()
        ),
    )


def _valid_field_rules(fields, site, reach: Reach) -> dict[tuple[int, str], str]:
    """The submitted rules that name a real field of a registered model, within reach."""
    from django.contrib.contenttypes.models import ContentType

    from vantage.fieldperms import LEVELS, rule_fields

    if not fields or site is None:
        return {}
    valid: dict[tuple[int, str], str] = {}
    names_for: dict[int, set[str] | None] = {}
    models_for: dict[int, Any] = {}
    for (ct_id, name), level in fields.items():
        if level and level not in LEVELS:
            continue
        if ct_id not in names_for:
            try:
                model = ContentType.objects.get_for_id(ct_id).model_class()
            except ContentType.DoesNotExist:
                model = None
            registered = model is not None and site.is_registered(model)
            names_for[ct_id] = {f.name for f in rule_fields(model)} if registered else None
            models_for[ct_id] = model
        names = names_for[ct_id]
        if names is None or name not in names:
            continue
        if not reach.covers_field(models_for[ct_id], name):
            continue
        valid[(ct_id, name)] = level
    return valid


def _write_field_rules(group: Group, moved: dict[tuple[int, str], str]) -> None:
    """Two statements however many rules moved: clear the keys, insert the restrictions."""
    from django.db.models import Q

    from vantage.models import FieldPermission

    if not moved:
        return
    keys = Q()
    for ct_id, name in moved:
        keys |= Q(content_type_id=ct_id, field_name=name)
    FieldPermission.objects.filter(keys, group=group).delete()
    FieldPermission.objects.bulk_create(
        FieldPermission(group=group, content_type_id=ct_id, field_name=name, level=level)
        for (ct_id, name), level in moved.items()
        if level
    )


def _model_label(ct_id: int) -> str:
    from django.contrib.contenttypes.models import ContentType

    try:
        ct = ContentType.objects.get_for_id(ct_id)
    except ContentType.DoesNotExist:  # pragma: no cover - validated before this runs
        return str(ct_id)
    return f"{ct.app_label}.{ct.model}"


# ── creating, renaming, duplicating, deleting ───────────────────────────────


def clean_role_name(name: str, *, exclude_pk: int | None = None) -> str:
    """Trimmed, non-empty, within ``Group.name``'s length, and not already taken.

    Uniqueness is checked case-insensitively although the column is not: "Editors"
    and "editors" side by side in a picker is a mistake waiting to be made, and the
    database constraint would only catch the exact duplicate.
    """
    name = " ".join(str(name or "").split())
    if not name:
        raise ValidationError(_("A role needs a name."))
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            _("Role names are at most %(limit)d characters.") % {"limit": MAX_NAME_LENGTH}
        )
    clash = Group.objects.filter(name__iexact=name)
    if exclude_pk is not None:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise ValidationError(_("A role called “%(name)s” already exists.") % {"name": name})
    return name


def create_role(name: str) -> Group:
    return Group.objects.create(name=clean_role_name(name))


def name_references(name: str) -> list[str]:
    """Where code refers to a role by ``name``: field rules and two-factor requirements."""
    from vantage.sites import all_sites
    from vantage.sites import site as default_site
    from vantage.twofactor import setting as twofactor_setting

    found: list[str] = []
    if name in set(twofactor_setting("TWO_FACTOR_REQUIRED_ROLES") or ()):
        found.append("TWO_FACTOR_REQUIRED_ROLES")
    seen = set()
    for candidate in (default_site, *all_sites):
        for panel in candidate.panels():
            declared = getattr(panel, "field_permissions", None)
            if isinstance(declared, dict) and name in declared and id(panel) not in seen:
                seen.add(id(panel))
                found.append(f"{type(panel).__name__}.field_permissions")
    return found


def rename_role(group: Group, name: str, *, token: str) -> tuple[str, str]:
    """Returns ``(old, new)``. Token-checked like a matrix save: renaming a role out
    from under someone halfway through reviewing it is the same clobber."""
    new = clean_role_name(name, exclude_pk=group.pk)
    if new != group.name:
        bound = name_references(group.name)
        if bound:
            # Fail closed: these bind by name, so a rename would silently drop the
            # rule or the requirement for every member while leaving them every
            # permission the role grants.
            raise ValidationError(
                gettext(
                    "“%(name)s” is named in the project's code (%(where)s), so renaming it "
                    "here would drop those rules for its members. Change the code first."
                )
                % {"name": group.name, "where": ", ".join(bound)}
            )
    with transaction.atomic():
        locked = _lock(group)
        current = current_permission_ids(locked)
        if fingerprint(locked, current) != token:
            raise RoleConflict(locked, current)
        old = locked.name
        locked.name = new
        locked.save(update_fields=["name"])
    group.name = new
    return old, new


def unique_copy_name(name: str) -> str:
    """``Copy of Editors``, then ``Copy of Editors (2)`` and so on, within the limit."""
    stem = gettext("Copy of %(name)s") % {"name": name}
    taken = {
        n.lower()
        for n in Group.objects.filter(name__istartswith=stem[:100]).values_list("name", flat=True)
    }
    candidate = stem[:MAX_NAME_LENGTH]
    counter = 2
    while candidate.lower() in taken:
        suffix = f" ({counter})"
        candidate = stem[: MAX_NAME_LENGTH - len(suffix)] + suffix
        counter += 1
    return candidate


def duplicate_role(group: Group) -> tuple[Group, list[int]]:
    """A new role with the same permissions and none of the members.

    Members are not copied: a duplicate is a starting point for a *different* set of
    people, and silently granting the copy to everyone who held the original would be
    a grant nobody reviewed.
    """
    from vantage.models import FieldPermission

    with transaction.atomic():
        ids = sorted(current_permission_ids(group))
        copy = Group.objects.create(name=unique_copy_name(group.name))
        if ids:
            copy.permissions.add(*ids)
        # Field rules are part of the bundle: a copy of Support that could suddenly
        # change prices would be a grant nobody reviewed.
        FieldPermission.objects.bulk_create(
            FieldPermission(group=copy, content_type_id=ct, field_name=name, level=level)
            for (ct, name), level in current_field_rules(group).items()
        )
    return copy, ids


def delete_role(group: Group, *, token: str) -> Group:
    """Delete under the lock, and return a detached stand-in for the audit entry.

    The stand-in exists because Django blanks ``pk`` on delete, and an audit entry
    that cannot say which role went is the least useful entry in the table.
    """
    with transaction.atomic():
        locked = _lock(group)
        current = current_permission_ids(locked)
        if fingerprint(locked, current) != token:
            raise RoleConflict(locked, current)
        ghost = Group(pk=locked.pk, name=locked.name)
        locked.delete()
    return ghost


# ── listing ─────────────────────────────────────────────────────────────────


def role_rows():
    """Every role with its member and permission counts, in one query.

    Correlated subqueries rather than two ``Count`` annotations: counting over both
    many-to-many joins at once multiplies them, and ``distinct=True`` hides that by
    making the database build and de-duplicate the whole cross product per role.
    """
    perm_through = Group.permissions.through
    permission_count = (
        perm_through.objects.filter(group_id=OuterRef("pk"))
        .order_by()
        .values("group_id")
        .annotate(n=Count("pk"))
        .values("n")
    )
    queryset = Group.objects.annotate(
        permission_count=Coalesce(Subquery(permission_count), Value(0))
    )
    membership = _membership_field()
    if membership is not None:
        through = membership.remote_field.through
        group_column = membership.m2m_reverse_field_name()
        member_count = (
            through.objects.filter(**{group_column: OuterRef("pk")})
            .order_by()
            .values(group_column)
            .annotate(n=Count("pk"))
            .values("n")
        )
        queryset = queryset.annotate(member_count=Coalesce(Subquery(member_count), Value(0)))
    return queryset.order_by("name")


# ── membership ──────────────────────────────────────────────────────────────


def _membership_field() -> models.ManyToManyField | None:
    """The user model's many-to-many to ``Group``, whatever it is called.

    ``PermissionsMixin`` calls it ``groups``, which is the overwhelmingly common case,
    but a custom ``AUTH_USER_MODEL`` may not inherit the mixin at all — in which case
    there is no membership to manage and the members screen says so instead of 500ing.
    """
    user_model = get_user_model()
    try:
        candidate = user_model._meta.get_field("groups")
    except FieldDoesNotExist:
        candidate = None
    if isinstance(candidate, models.ManyToManyField) and candidate.related_model is Group:
        return candidate
    for f in user_model._meta.many_to_many:
        if f.related_model is Group:
            return f
    return None


def supports_membership() -> bool:
    return _membership_field() is not None


def _direct_field() -> models.ManyToManyField | None:
    user_model = get_user_model()
    for f in user_model._meta.many_to_many:
        if f.related_model is Permission:
            return f
    return None


def members_of(group: Group):
    membership = _membership_field()
    user_model = get_user_model()
    if membership is None:
        return user_model._default_manager.none()
    return user_model._default_manager.filter(**{membership.name: group}).order_by(
        user_model.USERNAME_FIELD
    )


def roles_of(user):
    membership = _membership_field()
    if membership is None:
        return Group.objects.none()
    return getattr(user, membership.name).order_by("name")


def change_members(group: Group, *, add: set[Any], remove: set[Any]) -> tuple[list, list]:
    """Add and remove members in two bulk statements. Returns the users that moved.

    Both directions are filtered against the current membership first, so the audit
    entry lists only people whose access actually changed — re-adding a member is not
    news, and recording it as such teaches readers to skim.
    """
    membership = _membership_field()
    if membership is None:
        return [], []
    user_model = get_user_model()
    manager = user_model._default_manager
    accessor = getattr(group, membership.remote_field.get_accessor_name())
    pk_field = user_model._meta.pk

    def clean(values: set[Any]) -> set[Any]:
        cleaned = set()
        for value in values:
            try:
                cleaned.add(pk_field.to_python(value))
            except ValidationError:
                continue
        return cleaned

    add, remove = clean(add), clean(remove)
    contradictory = add & remove
    add, remove = add - contradictory, remove - contradictory

    with transaction.atomic():
        current = set(accessor.values_list("pk", flat=True))
        joining = list(manager.filter(pk__in=add - current)) if add - current else []
        leaving = list(manager.filter(pk__in=remove & current)) if remove & current else []
        if joining:
            accessor.add(*joining)
        if leaving:
            accessor.remove(*leaving)
    return joining, leaving


# ── what a user can actually do ─────────────────────────────────────────────


@dataclass
class EffectiveRow:
    permission: Permission
    label: str
    direct: bool = False
    #: ``{"pk", "name"}`` per role granting it, so the page can link to each one.
    roles: list[dict[str, Any]] = field(default_factory=list)


def effective_permissions(user) -> list[tuple[str, list[EffectiveRow]]]:
    """The union of a user's direct permissions and every role's, with the source of
    each, grouped by app. Two queries whatever the number of roles.

    Only what the database grants. A custom authentication backend can add more, and
    a superuser holds everything regardless — the page says both in words rather than
    pretending this list is the whole story.
    """
    rows: dict[int, EffectiveRow] = {}

    direct = _direct_field()
    if direct is not None:
        for permission in getattr(user, direct.name).select_related("content_type"):
            row = rows.setdefault(permission.pk, EffectiveRow(permission, perm_label(permission)))
            row.direct = True

    membership = _membership_field()
    if membership is not None:
        through = Group.permissions.through
        links = (
            through.objects.filter(group__in=getattr(user, membership.name).all())
            .select_related("permission__content_type", "group")
            .order_by("group__name")
        )
        for link in links:
            permission = link.permission
            row = rows.setdefault(permission.pk, EffectiveRow(permission, perm_label(permission)))
            row.roles.append({"pk": link.group.pk, "name": link.group.name})

    grouped: dict[str, list[EffectiveRow]] = {}
    for row in sorted(rows.values(), key=lambda r: r.label):
        grouped.setdefault(_app_label(row.permission.content_type.app_label), []).append(row)
    return sorted(grouped.items(), key=lambda item: item[0].lower())


@dataclass
class FieldRestriction:
    """One model's restrictions for one user, for the effective-permissions page."""

    label: str
    hidden: list[str]
    readonly: list[str]


def effective_field_rules(user, site) -> list[FieldRestriction]:
    """What a user's roles hide and make read-only, per model this panel registers.

    The same resolution every page uses (:func:`vantage.fieldperms.resolve`), so the
    page cannot describe a rule the panel does not apply. Superusers have none: the
    bypass is the permission layer's, and the page says so in words.
    """
    from vantage import fieldperms

    if site is None or getattr(user, "is_superuser", False):
        return []
    rules = fieldperms._load(user)
    out: list[FieldRestriction] = []
    for panel in site.panels():
        answer = fieldperms.resolve(user, panel.model, panel=panel, rules=rules)
        if not answer:
            continue
        labels = {
            f.name: str(getattr(f, "verbose_name", f.name)).capitalize()
            for f in fieldperms.rule_fields(panel.model)
        }
        out.append(
            FieldRestriction(
                label=panel.get_label_plural(),
                hidden=sorted(labels.get(n, n) for n in answer.hidden),
                readonly=sorted(labels.get(n, n) for n in answer.readonly),
            )
        )
    return sorted(out, key=lambda item: str(item.label).lower())


# ── what a delete would do ──────────────────────────────────────────────────


@dataclass
class DeleteImpact:
    members: int
    permissions: int
    #: ``(user, permissions they would lose)``, worst first, capped for display.
    affected: list[tuple[Any, int]]
    affected_total: int


#: Users the preview names individually. The rest are a count.
IMPACT_NAMED = 20


def delete_impact(group: Group) -> DeleteImpact:
    """Who loses what if this role disappears — net of every other source.

    A member who also holds a permission through another role, or directly, does not
    lose it, and a preview that counted them would make every delete look alarming and
    teach people to click through it. Four queries whatever the size of the role.
    """
    role_perms = current_permission_ids(group)
    members = list(members_of(group))
    if not members or not role_perms:
        return DeleteImpact(len(members), len(role_perms), [], 0)

    member_ids = [m.pk for m in members]
    kept: dict[Any, set[int]] = {pk: set() for pk in member_ids}

    membership = _membership_field()
    if membership is not None:
        user_through = membership.remote_field.through
        user_column = membership.m2m_field_name()
        group_column = membership.m2m_reverse_field_name()
        other_groups = user_through.objects.filter(**{f"{user_column}__in": member_ids}).exclude(
            **{group_column: group.pk}
        )
        perm_through = Group.permissions.through
        group_perms: dict[int, set[int]] = {}
        for gid, pid in perm_through.objects.filter(
            group_id__in=other_groups.values(group_column), permission_id__in=role_perms
        ).values_list("group_id", "permission_id"):
            group_perms.setdefault(gid, set()).add(pid)
        for uid, gid in other_groups.values_list(f"{user_column}_id", f"{group_column}_id"):
            kept[uid] |= group_perms.get(gid, set())

    direct = _direct_field()
    if direct is not None:
        through = direct.remote_field.through
        user_column = direct.m2m_field_name()
        perm_column = direct.m2m_reverse_field_name()
        for uid, pid in through.objects.filter(
            **{f"{user_column}__in": member_ids, f"{perm_column}__in": role_perms}
        ).values_list(f"{user_column}_id", f"{perm_column}_id"):
            kept[uid].add(pid)

    losses = [(m, len(role_perms - kept[m.pk])) for m in members]
    losses = [item for item in losses if item[1]]
    losses.sort(key=lambda item: (-item[1], item[0].get_username()))
    return DeleteImpact(len(members), len(role_perms), losses[:IMPACT_NAMED], len(losses))


# ── the audit trail ─────────────────────────────────────────────────────────


def _labels(permissions: Iterable[Permission]) -> str:
    return ", ".join(perm_label(p) for p in permissions)


def _rule_labels(rules) -> str:
    """``shop.order.price: read-only, shop.order.cost: full access`` — greppable, plain."""
    words = {"": "full access", "readonly": "read-only", "hidden": "hidden"}
    return ", ".join(f"{label}.{name}: {words.get(level, level)}" for label, name, level in rules)


def audit_permissions(request, group: Group, change: PermissionChange) -> None:
    """One entry per save, naming every permission that moved in either direction.

    Stored as ``app.codename`` rather than the verbose name: it is the string a
    developer greps for, and the one ``has_perm`` takes, so the log can be checked
    against code without translation.
    """
    from vantage.audit import record
    from vantage.models import VantageLogEntry

    if not change:
        return
    changes: dict[str, dict[str, Any]] = {}
    if change.added:
        changes[gettext("Permissions added")] = {"to": _labels(change.added)}
    if change.removed:
        changes[gettext("Permissions removed")] = {"from": _labels(change.removed)}
    if change.fields:
        changes[gettext("Field access")] = {
            "from": _rule_labels((label, name, before) for label, name, before, _ in change.fields),
            "to": _rule_labels((label, name, after) for label, name, _, after in change.fields),
        }
    record(
        request,
        VantageLogEntry.Action.UPDATE,
        obj=group,
        changes=changes,
        action_name="role.permissions",
    )


def audit_members(request, group: Group, joining: list, leaving: list) -> None:
    from vantage.audit import record
    from vantage.models import VantageLogEntry

    if not joining and not leaving:
        return
    changes: dict[str, dict[str, Any]] = {}
    if joining:
        changes[gettext("Members added")] = {"to": ", ".join(u.get_username() for u in joining)}
    if leaving:
        changes[gettext("Members removed")] = {"from": ", ".join(u.get_username() for u in leaving)}
    record(
        request,
        VantageLogEntry.Action.UPDATE,
        obj=group,
        changes=changes,
        action_name="role.members",
    )
