"""The single choke point for every access decision in the panel.

Three concerns that look alike and must not be conflated:

1. **Model-level** — may this user see this section at all? One check per page.
2. **Object-level** — may this user act on *this* row? Only ever used where N is 1.
3. **Queryset-level** — which rows may this user see? Must be answered in bulk.

Keeping them apart is not tidiness, it is the difference between a list page that
runs two queries and one that runs a hundred. Django's ``ModelBackend`` returns an
empty permission set whenever ``obj is not None``, *and* only populates its permission
cache when ``obj is None`` — so calling ``has_perm(perm, obj)` once per row on a
hundred-row page executes a hundred uncached queries under django-guardian. That is
the single most common performance failure in object-permission admin UIs, and the
reason :meth:`PanelPermissions.filter_queryset` exists.

Third-party backends are adopted rather than reimplemented: django-guardian and
django-rules both work by being in ``AUTHENTICATION_BACKENDS``, so ``has_perm`` already
routes to them. Only the bulk path needs an adapter, because ``rules`` has no queryset
story at all and guardian's lives in a shortcut function.
"""

from __future__ import annotations

from functools import cached_property

from django.contrib.auth import get_permission_codename
from django.db.models import Model, QuerySet

__all__ = ["PanelPermissions", "get_permissions"]

#: The actions every model gets by default, mapped to Django's codename verbs.
ACTIONS = ("view", "add", "change", "delete")


class PanelPermissions:
    """Resolves access. Subclass and point ``VANTAGE["PERMISSIONS"]`` at it to change."""

    #: Superusers bypass every check. Django's own admin does the same, and a panel
    #: that can lock out the superuser is a support burden, not a security feature.
    superuser_bypass = True

    # ── naming ──────────────────────────────────────────────────────────────

    def perm_name(self, model: type[Model], action: str) -> str:
        """``("blog.Post", "change")`` -> ``"blog.change_post"``.

        Always via ``get_permission_codename``, never an f-string on ``model_name``:
        the helper respects proxy models and a custom ``Meta.model_name``.
        """
        opts = model._meta
        return f"{opts.app_label}.{get_permission_codename(action, opts)}"

    # ── the three checks ────────────────────────────────────────────────────

    def has_model_perm(self, request, model: type[Model], action: str) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        # Before the superuser bypass: read-only mode is an operational choice about
        # the deployment, not about who is asking.
        if self.is_read_only(action) or self.is_model_read_only(model, action):
            return False
        override = self.panel_override(request, model, action)
        if override is not None:
            return override
        if self.superuser_bypass and user.is_superuser:
            return True
        perm = self.perm_name(model, action)
        # Time-boxed elevation is asked only after the standing answer is no, so
        # someone without a grant pays one query per request, on their first refusal.
        return user.has_perm(perm) or _elevated(request, perm)

    @staticmethod
    def is_read_only(action: str) -> bool:
        """Whether the panel currently refuses this action outright."""
        from vantage.safety import WRITE_ACTIONS, read_only_reason

        return action in WRITE_ACTIONS and read_only_reason() is not None

    @staticmethod
    def is_model_read_only(model: type[Model], action: str) -> bool:
        """Whether ``model`` itself refuses this action (``ModelPanel.read_only``).

        Like :meth:`is_read_only`, asked before the superuser bypass and before a
        panel's own ``has_*_permission``: a table the project does not own is not made
        writable by who is asking.
        """
        from vantage.safety import WRITE_ACTIONS, model_read_only

        if action not in WRITE_ACTIONS:
            return False
        state = model_read_only(model)
        return state is not None and state.refuses(action)

    def has_object_perm(self, request, model: type[Model], action: str, obj) -> bool:
        """Whether the user may act on one specific row.

        Short-circuits to the model-level answer when no backend can actually answer an
        object-level question, which avoids a pointless walk through the backend chain
        on every row and matches what ``contrib.admin`` effectively does.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if self.is_read_only(action) or self.is_model_read_only(model, action):
            return False
        override = self.panel_override(request, model, action, obj)
        if override is not None:
            return override
        if self.superuser_bypass and user.is_superuser:
            return True
        if not self.supports_object_permissions:
            return self.has_model_perm(request, model, action)
        perm = self.perm_name(model, action)
        # An elevated permission is a model-level grant, so it covers every row — the
        # same meaning ``accept_global_perms`` gives a standing one below.
        return user.has_perm(perm, obj) or _elevated(request, perm)

    # ── the panel's own answer ──────────────────────────────────────────────

    def panel_override(self, request, model: type[Model], action: str, obj=None) -> bool | None:
        """Let a panel answer for itself via ``has_<action>_permission``.

        ``contrib.admin`` users already know these methods, the API design lists them
        as ``ModelPanel`` hooks, and the adapter in ``vantage.contrib.django_admin``
        translates a ``ModelAdmin``'s versions straight into them. Returning ``None``
        here means "no opinion", which is the case for every panel that does not
        define one — so this costs an attribute lookup and nothing else.

        Consulted *before* the superuser bypass, deliberately. An admin that says
        nobody may delete these rows means nobody, and adopting it into Vantage must
        not quietly widen it to every superuser.

        The signature is ``(request, obj=None)`` for all four verbs, including ``add``
        — ``contrib.admin`` omits ``obj`` on that one alone, and one uniform signature
        is worth more than bug-compatibility with an inconsistency. The adapter bridges
        the difference so an adopted ``has_add_permission(self, request)`` still works.
        """
        hook = _panel_hook(model, action)
        if hook is None:
            return None
        return bool(hook(request, obj))

    def filter_queryset(self, request, queryset: QuerySet, action: str = "view") -> QuerySet:
        """Narrow ``queryset`` to the rows this user may see.

        The base implementation does no row filtering: model-level permissions have
        already gated entry, which is exactly Django's own model. Override this — never
        a loop over :meth:`has_object_perm` — to add row scoping.
        """
        return queryset

    # ── field level ─────────────────────────────────────────────────────────

    def field_access(self, request, model: type[Model]):
        """This user's per-field restrictions on ``model``. See :mod:`vantage.fieldperms`.

        A fourth question beside the three above, and asked the same way: once per
        request and model, cached by :func:`vantage.fieldperms.access`, never per row.
        Superusers have none. Override to source the rules from somewhere else; return a
        :class:`~vantage.fieldperms.FieldAccess`.
        """
        from vantage import fieldperms

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return fieldperms.UNRESTRICTED
        if self.superuser_bypass and user.is_superuser:
            return fieldperms.UNRESTRICTED
        answer = fieldperms.resolve(user, model, rules=fieldperms.user_rules(request))
        if answer:
            # A role held through a grant votes like a membership, and only ever lifts.
            from vantage.breakglass import widen

            answer = widen(request, user, model, answer)
        return answer

    def has_field_perm(self, request, model: type[Model], name: str, action: str) -> bool:
        """Whether this user may ``"view"`` or ``"change"`` one field of ``model``.

        A convenience over :meth:`field_access`. Says nothing about the model itself:
        ask :meth:`has_model_perm` for that first, as every caller in the package does.
        """
        from vantage import fieldperms

        answer = fieldperms.access(request, model)
        return answer.can_view(name) if action == "view" else answer.can_change(name)

    # ── capability detection ────────────────────────────────────────────────

    @cached_property
    def supports_object_permissions(self) -> bool:
        """Whether any configured backend does something when ``obj`` is not None.

        ``ModelBackend`` demonstrably does not: it returns an empty set the moment an
        object is passed. If it is the only backend, object checks are dead weight.
        """
        from django.contrib.auth import get_backends
        from django.contrib.auth.backends import ModelBackend

        return any(
            hasattr(backend, "has_perm") and type(backend) is not ModelBackend
            for backend in get_backends()
        )

    # ── panel-facing helpers ────────────────────────────────────────────────

    def visible_panels(self, request, panels):
        """The panels this user may see at all — what the sidebar is built from."""
        return [p for p in panels if self.has_model_perm(request, p.model, "view")]

    def allowed_actions(self, request, model: type[Model], obj=None) -> set[str]:
        """Every action this user may take, for rendering buttons without guessing."""
        check = (
            (lambda action: self.has_object_perm(request, model, action, obj))
            if obj is not None
            else (lambda action: self.has_model_perm(request, model, action))
        )
        return {action for action in ACTIONS if check(action)}


class GuardianPermissions(PanelPermissions):
    """Row scoping via django-guardian, which is the only one with a bulk API.

    Enabled by pointing ``VANTAGE["PERMISSIONS"]`` here. Not auto-enabled: whether
    object permissions should narrow the admin is a policy decision, not a
    consequence of having the package installed.
    """

    def filter_queryset(self, request, queryset: QuerySet, action: str = "view") -> QuerySet:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return queryset.none()
        if self.superuser_bypass and user.is_superuser:
            return queryset

        perm = self.perm_name(queryset.model, action)
        if _elevated(request, perm):
            return queryset

        from guardian.shortcuts import get_objects_for_user

        return get_objects_for_user(
            user,
            self.perm_name(queryset.model, action),
            klass=queryset,
            # A model-level grant should still mean "all rows", or granting someone
            # blanket access would paradoxically show them nothing.
            accept_global_perms=True,
            with_superuser=True,
        )


def _elevated(request, perm: str) -> bool:
    """Whether an active break-glass grant gives this request ``perm``. See
    :mod:`vantage.breakglass`: computed from the clock at check time, cached per request."""
    from vantage.breakglass import holds

    return holds(request, perm)


def _panel_hook(model: type[Model], action: str):
    """``has_<action>_permission`` from the panel registered for ``model``, if any.

    The resolver is handed a model, not a panel, so the panel has to be looked up. The
    default site is tried first because it is the one virtually every project mounts;
    other sites are consulted only for a hook the default site's panel does not
    define. That order is the conservative one: a panel that denies something is
    honoured wherever that model appears, rather than a second mounted site quietly
    reopening what the first one closed.
    """
    from vantage.sites import all_sites
    from vantage.sites import site as default_site

    for candidate in (default_site, *all_sites):
        if not candidate.is_registered(model):
            continue
        hook = getattr(candidate.get_panel(model), f"has_{action}_permission", None)
        if hook is not None:
            return hook
    return None


_cached: PanelPermissions | None = None


def get_permissions() -> PanelPermissions:
    """The configured permission resolver, instantiated once."""
    global _cached
    if _cached is None:
        from django.utils.module_loading import import_string

        from vantage.conf import vantage_settings
        from vantage.tenancy import enforce_on

        path = vantage_settings.PERMISSIONS
        # Tenant scoping is composed onto the resolved *instance* rather than being a
        # fourth layer beside these three: it is a queryset-level answer to the same
        # question ``filter_queryset`` already answers, and every call site in the
        # package already goes through it. Wrapping the instance is what makes it
        # unforgettable — a subclass that overrides ``filter_queryset`` without calling
        # ``super()`` is still scoped. No-op unless VANTAGE["TENANT_MODEL"] is set.
        _cached = enforce_on(import_string(path)() if path else PanelPermissions())
    return _cached


def _reset() -> None:
    """Drop the cached resolver. Used by the settings-changed signal and by tests."""
    global _cached
    _cached = None
