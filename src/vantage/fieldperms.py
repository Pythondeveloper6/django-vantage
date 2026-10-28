"""Field-level permissions: what one person may see, and what they may change, per field.

Django's permissions stop at the model: someone who may change an order may change
every column of it. "Support can see the price but not change it" needs one more level,
and it has two settings per field — **hidden** (the field does not exist for you) and
**read-only** (you can see it, you cannot write it). No rule means full access, so a
project that never sets one behaves exactly as before.

**Rules belong to roles.** The roles matrix edits ``auth.Group``, so a stored rule is a
:class:`~vantage.models.FieldPermission` row keyed to a group, and a panel can state the
same thing in code with one line::

    field_permissions = {"Support": {"price": "readonly", "cost": "hidden"}}

Both apply, and where both speak about one field for one role the stricter wins: the
code is the floor the screen cannot lift. Superusers have no rules, the same bypass
:class:`~vantage.permissions.PanelPermissions` gives them everywhere else.

**Several roles combine the way Django's own permissions do: most permissive wins.**
Holding a model through two roles, one of which hides ``price``, you see ``price``,
because the other role grants the model without hiding it. Two refinements keep that
from being a loophole, and both are about *which* roles get a vote:

* Only roles that grant something on the model vote. A "Newsletter" role with no order
  permissions says nothing about orders, so it cannot lift Support's rule on them. (If
  no role grants the model at all — access came from a direct grant or a custom
  backend — the roles that carry a rule for it are the ones that vote.)
* For *writing*, only roles that grant ``add`` or ``change`` on the model vote. A
  view-only role that leaves ``price`` alone cannot combine with a change role that
  makes ``price`` read-only into "may change ``price``".

**One question, asked everywhere.** :func:`access` is the answer for one request and one
model, cached on the request. Everything else in the package asks it — the panel's
getters drop hidden paths from columns, filters, search and ordering; forms drop hidden
fields and render read-only ones as text; every write path refuses a read-only name
server-side; the JSON API, exports, history, the audit log and data quality leave hidden
fields out — so a field cannot be hidden on the list and printed in the CSV because two
modules disagreed about it. It is the same shape :mod:`vantage.sensitive` has, with the
one difference that the answer depends on who is asking.

What this does *not* cover is listed in ``docs/field-permissions.md``: your own code
(a method column, a template, a ``save_model`` hook), webhooks, and anything that reads
the database without going through the panel.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db import models

__all__ = [
    "HIDDEN",
    "LEVELS",
    "READONLY",
    "UNRESTRICTED",
    "FieldAccess",
    "access",
    "code_rules",
    "editable_fields",
    "hidden_names",
    "hide_entry_fields",
    "is_hidden_path",
    "normalise_level",
    "refused",
    "resolve",
    "rule_fields",
    "safe_label",
    "strip_hidden",
    "unfillable",
    "unfillable_message",
    "without_hidden",
]

READONLY = "readonly"
HIDDEN = "hidden"
#: The restrictions a rule can state, mildest first. Full access is the absence of one.
LEVELS: tuple[str, ...] = (READONLY, HIDDEN)

_RANK = {"": 0, READONLY: 1, HIDDEN: 2}

#: Spellings accepted in ``field_permissions``, so ``"read-only"`` is not a typo trap.
_ALIASES = {
    "readonly": READONLY,
    "read-only": READONLY,
    "read_only": READONLY,
    "hidden": HIDDEN,
    "hide": HIDDEN,
    "": "",
    "full": "",
    "none": "",
}

_RULES_ATTR = "_vantage_field_rules"
_ACCESS_ATTR = "_vantage_field_access"


@dataclass(frozen=True)
class FieldAccess:
    """One person's restrictions on one model. Falsy when there are none."""

    #: Fields that do not exist for this person: never shown, never written.
    hidden: frozenset[str] = frozenset()
    #: Fields shown but not writable. Disjoint from ``hidden``.
    readonly: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.hidden or self.readonly)

    @property
    def locked(self) -> frozenset[str]:
        """Every field this person may not write: hidden or read-only."""
        return self.hidden | self.readonly

    def can_view(self, name: str) -> bool:
        return name not in self.hidden

    def can_change(self, name: str) -> bool:
        return name not in self.hidden and name not in self.readonly

    def level(self, name: str) -> str:
        if name in self.hidden:
            return HIDDEN
        if name in self.readonly:
            return READONLY
        return ""


UNRESTRICTED = FieldAccess()


def normalise_level(value: Any) -> str | None:
    """``"read-only"`` to ``"readonly"``, ``""`` for full access, ``None`` when invalid."""
    if value is None:
        return ""
    return _ALIASES.get(str(value).strip().lower())


# ── where the rules come from ───────────────────────────────────────────────


def code_rules(panel) -> dict[str, dict[str, str]]:
    """``panel.field_permissions``, normalised. Malformed entries are skipped.

    Skipped rather than raised on, so one typo cannot take every page of the panel
    down; ``manage.py check`` reports them (``vantage.E110`` to ``E114``).
    """
    declared = getattr(panel, "field_permissions", None) if panel is not None else None
    if not isinstance(declared, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for role, fields in declared.items():
        if not isinstance(fields, dict):
            continue
        cleaned = {}
        for name, raw in fields.items():
            level = normalise_level(raw)
            if level:
                cleaned[str(name)] = level
        if cleaned:
            out[str(role)] = cleaned
    return out


def rule_fields(model: type[models.Model]) -> list[models.Field]:
    """The fields a rule may name: every concrete field and many-to-many, minus the pk.

    The primary key is left out because hiding it hides the record's identity, which
    every link, URL and row in the panel is built from — a rule on it could not be
    honoured without breaking the page, so it is not offered.
    """
    from vantage.introspection import concrete_fields, m2m_fields

    return [f for f in [*concrete_fields(model), *m2m_fields(model)] if not f.primary_key]


def _panel_for(model, site=None):
    from vantage.sensitive import _panel_for as lookup

    return lookup(model, site)


def _sites_declare_code_rules() -> bool:
    """Whether any registered panel states rules in code. Decides one query."""
    from vantage.sites import all_sites
    from vantage.sites import site as default_site

    for candidate in (default_site, *all_sites):
        for panel in candidate.panels():
            if getattr(panel, "field_permissions", None):
                return True
    return False


@dataclass
class _UserRules:
    """Everything one person's resolution needs, loaded at most once per request."""

    user: Any
    #: ``{content_type_id: {group_id: {field: level}}}`` from the database.
    stored: dict[int, dict[int, dict[str, str]]]
    #: ``{group_id: name}``, loaded only when some panel declares rules in code.
    names: dict[int, str] | None
    #: ``{content_type_id: {group_id: {action, ...}}}``, loaded on first need.
    _grants: dict[int, dict[int, set[str]]] | None = None

    def grants(self) -> dict[int, dict[int, set[str]]]:
        if self._grants is None:
            from django.contrib.auth.models import Group

            through = Group.permissions.through
            rows = through.objects.filter(group_id__in=_membership(self.user)).values_list(
                "group_id", "permission__content_type_id", "permission__codename"
            )
            grants: dict[int, dict[int, set[str]]] = {}
            for gid, ct_id, codename in rows:
                action = codename.partition("_")[0]
                grants.setdefault(ct_id, {}).setdefault(gid, set()).add(action)
            self._grants = grants
        return self._grants


def _membership(user):
    """The pks of ``user``'s roles, as a subquery. Empty for a user model without groups."""
    from vantage.roles import roles_of

    return roles_of(user).order_by().values("pk")


def _load(user) -> _UserRules:
    from vantage.models import FieldPermission

    names = None
    if _sites_declare_code_rules():
        from vantage.roles import roles_of

        names = dict(roles_of(user).order_by().values_list("pk", "name"))
    stored: dict[int, dict[int, dict[str, str]]] = {}
    if names is None or names:
        rows = FieldPermission.objects.filter(group_id__in=_membership(user)).values_list(
            "content_type_id", "group_id", "field_name", "level"
        )
        for ct_id, gid, name, level in rows:
            stored.setdefault(ct_id, {}).setdefault(gid, {})[name] = level
    return _UserRules(user=user, stored=stored, names=names)


def _combine(per_role: dict[int, dict[str, str]], grants: dict[int, set[str]]) -> FieldAccess:
    """Most permissive across the roles that get a vote. See the module docstring."""
    voters = [gid for gid in grants if grants[gid]]
    if not voters:
        voters = list(per_role)
    writers = [gid for gid in voters if grants.get(gid, set()) & {"add", "change"}] or voters
    named = {name for rules in per_role.values() for name in rules}

    def rank(gid: int, name: str) -> int:
        return _RANK.get(per_role.get(gid, {}).get(name, ""), 0)

    hidden = {name for name in named if all(rank(g, name) >= 2 for g in voters)}
    readonly = {name for name in named - hidden if all(rank(g, name) >= 1 for g in writers)}
    return FieldAccess(hidden=frozenset(hidden), readonly=frozenset(readonly))


def resolve(user, model: type[models.Model], *, panel=None, rules: _UserRules | None = None):
    """``user``'s restrictions on ``model``, computed from scratch. No superuser bypass.

    The bypass is :class:`~vantage.permissions.PanelPermissions`'s, applied before this
    is asked; calling it directly — the effective-permissions page does, for an account
    that is not the one signed in — answers what the roles say.
    """
    from django.contrib.contenttypes.models import ContentType

    if user is None or not getattr(user, "is_authenticated", False):
        return UNRESTRICTED
    rules = rules if rules is not None else _load(user)
    ct_id = ContentType.objects.get_for_model(model, for_concrete_model=False).pk
    per_role: dict[int, dict[str, str]] = {
        gid: dict(fields) for gid, fields in rules.stored.get(ct_id, {}).items()
    }
    if rules.names:
        panel = panel if panel is not None else _panel_for(model)
        by_name = {name: gid for gid, name in rules.names.items()}
        for role, fields in code_rules(panel).items():
            gid = by_name.get(role)
            if gid is None:
                continue
            merged = per_role.setdefault(gid, {})
            for name, level in fields.items():
                if _RANK[level] > _RANK.get(merged.get(name, ""), 0):
                    merged[name] = level
    per_role = {gid: fields for gid, fields in per_role.items() if fields}
    if not per_role:
        return UNRESTRICTED
    return _combine(per_role, rules.grants().get(ct_id, {}))


# ── the one question ────────────────────────────────────────────────────────


def access(request, target, site=None) -> FieldAccess:
    """The restrictions on ``target`` (a panel, a model or an instance) for this request.

    Answered by the configured permission resolver's ``field_access`` — so a project
    with its own :class:`~vantage.permissions.PanelPermissions` subclass can replace the
    rule — and cached on the request, because a list page asks once per column per row.
    No request, no restrictions: code without one (a webhook, a management command) is
    not acting for a person.
    """
    if request is None:
        return UNRESTRICTED
    model = _model_of(target)
    cache = getattr(request, _ACCESS_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, _ACCESS_ATTR, cache)
        except AttributeError:  # pragma: no cover - an exotic request-like object
            pass
    key = model._meta.label_lower
    if key in cache:
        return cache[key]
    from vantage.permissions import PanelPermissions, get_permissions

    resolver = get_permissions()
    # A resolver written before this existed (duck-typed rather than subclassed) gets
    # the default rule rather than an AttributeError on every page.
    ask = getattr(resolver, "field_access", None) or PanelPermissions().field_access
    answer = ask(request, model) or UNRESTRICTED
    cache[key] = answer
    return answer


def _model_of(target) -> type[models.Model]:
    """A panel's model, a model, or an instance's class."""
    if hasattr(target, "get_sensitive_fields"):
        return target.model
    return target if isinstance(target, type) else type(target)


def user_rules(request) -> _UserRules | None:
    """The request's loaded rules, loading them on first use. For the resolver."""
    user = getattr(request, "user", None)
    if user is None:
        return None
    cached = getattr(request, _RULES_ATTR, None)
    if cached is None or cached.user is not user:
        cached = _load(user)
        try:
            setattr(request, _RULES_ATTR, cached)
        except AttributeError:  # pragma: no cover
            pass
    return cached


def forget(request) -> None:
    """Drop what this request has cached, after a rule changed mid-request."""
    for attr in (_RULES_ATTR, _ACCESS_ATTR):
        if hasattr(request, attr):
            delattr(request, attr)


def hidden_names(request, target) -> frozenset[str]:
    return access(request, target).hidden


def is_hidden_path(request, target, path: str) -> bool:
    """Whether a column, filter, search or sort path reaches a field hidden from this person.

    Every hop is judged by *its own* model's rules, so ``customer__email`` is hidden when
    ``email`` is hidden on the customer, whichever list asks. A name that is not a field
    (a method, a property) is hidden only when a rule names it outright.
    """
    if request is None:
        return False
    from vantage.introspection import resolve_path

    clean = str(path).lstrip("-^=@")
    model = _model_of(target)
    hops = resolve_path(model, clean)
    if not hops:
        return clean.split("__")[0] in access(request, target).hidden
    for hop in hops:
        owner = target if hop.model is model else hop.model
        if hop.name in access(request, owner).hidden:
            return True
    return False


def strip_hidden(request, target, paths: Iterable[str]) -> list[str]:
    """``paths`` minus anything that reaches a hidden field, order kept."""
    paths = list(paths)
    if request is None:
        return paths
    return [path for path in paths if not is_hidden_path(request, target, path)]


def without_hidden(request, target, names: Iterable[str]) -> list[str]:
    """Plain field names minus the hidden ones. Cheaper than paths, for form field lists."""
    hidden = hidden_names(request, target)
    return [name for name in names if name not in hidden]


def editable_fields(request, target, names: Iterable[str]) -> list[str]:
    """``names`` minus everything this person may not write."""
    locked = access(request, target).locked
    return [name for name in names if name not in locked]


def refused(request, target, names: Iterable[str]) -> list[str]:
    """The names in ``names`` this person may not write, for a write path to refuse."""
    locked = access(request, target).locked
    return [name for name in names if name in locked]


def unfillable(request, target) -> list[str]:
    """Labels of locked fields a new record cannot do without, for a create to refuse.

    A field this person may not write is left out of the create form, so the row gets
    the field's default — and a ``NOT NULL`` column with no default (a price, a foreign
    key) has nothing to fall back on. Saying so on the form beats an ``IntegrityError``.
    """
    locked = access(request, target).locked
    if not locked:
        return []
    labels = []
    for model_field in rule_fields(_model_of(target)):
        if model_field.name not in locked or model_field.many_to_many:
            continue
        if model_field.null or getattr(model_field, "auto_now", False):
            continue
        if getattr(model_field, "auto_now_add", False):
            continue
        if model_field.get_default() is None:
            labels.append(str(getattr(model_field, "verbose_name", model_field.name)))
    return labels


def unfillable_message(request, target) -> str | None:
    """The sentence a create form shows when :func:`unfillable` is not empty."""
    from django.utils.translation import gettext as _

    labels = unfillable(request, target)
    if not labels:
        return None
    return _(
        "This record cannot be created here: %(fields)s must be filled in, and you cannot set it."
    ) % {"fields": ", ".join(labels)}


def safe_label(request, obj, site=None) -> str:
    """:func:`~vantage.sensitive.safe_str`, also refusing a ``__str__`` that prints a
    field hidden from this person.

    Used where a label is the whole answer — autocomplete options and search results —
    so a ``__str__`` of ``f"{self.name} ({self.price})"`` does not show Support the price
    the list page hides from them. Only loaded values are compared; it costs no query.
    """
    from vantage.sensitive import MIN_LEAK_LENGTH, safe_str

    text = safe_str(obj, site)
    hidden = hidden_names(request, obj) if obj is not None else frozenset()
    if not hidden or not text:
        return text
    meta = obj._meta
    loaded = getattr(obj, "__dict__", {})
    for name in hidden:
        try:
            field = meta.get_field(name)
        except Exception:
            continue
        value = loaded.get(getattr(field, "attname", name))
        if value is None:
            continue
        needle = str(value)
        if len(needle) >= MIN_LEAK_LENGTH and needle in text:
            return f"{str(meta.verbose_name).capitalize()} #{obj.pk}"
    return text


def hide_entry_fields(request, entries) -> list:
    """Mark audit entries so their diffs leave out fields hidden from this person.

    The entry's :attr:`~vantage.models.VantageLogEntry.change_list` honours the mark.
    One answer per model, however many entries share it.
    """
    from django.contrib.contenttypes.models import ContentType

    entries = list(entries)
    if request is None:
        return entries
    for entry in entries:
        if not entry.content_type_id or not entry.changes:
            continue
        try:
            model = ContentType.objects.get_for_id(entry.content_type_id).model_class()
        except ContentType.DoesNotExist:
            continue
        if model is None:
            continue
        hidden = hidden_names(request, model)
        if hidden:
            entry.vantage_hidden_fields = hidden
    return entries
