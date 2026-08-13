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
        if self.superuser_bypass and user.is_superuser:
            return True
        return user.has_perm(self.perm_name(model, action))

    def has_object_perm(self, request, model: type[Model], action: str, obj) -> bool:
        """Whether the user may act on one specific row.

        Short-circuits to the model-level answer when no backend can actually answer an
        object-level question, which avoids a pointless walk through the backend chain
        on every row and matches what ``contrib.admin`` effectively does.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if self.superuser_bypass and user.is_superuser:
            return True
        if not self.supports_object_permissions:
            return self.has_model_perm(request, model, action)
        return user.has_perm(self.perm_name(model, action), obj)

    def filter_queryset(self, request, queryset: QuerySet, action: str = "view") -> QuerySet:
        """Narrow ``queryset`` to the rows this user may see.

        The base implementation does no row filtering: model-level permissions have
        already gated entry, which is exactly Django's own model. Override this — never
        a loop over :meth:`has_object_perm` — to add row scoping.
        """
        return queryset

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


_cached: PanelPermissions | None = None


def get_permissions() -> PanelPermissions:
    """The configured permission resolver, instantiated once."""
    global _cached
    if _cached is None:
        from django.utils.module_loading import import_string

        from vantage.conf import vantage_settings

        path = vantage_settings.PERMISSIONS
        _cached = import_string(path)() if path else PanelPermissions()
    return _cached


def _reset() -> None:
    """Drop the cached resolver. Used by the settings-changed signal and by tests."""
    global _cached
    _cached = None
