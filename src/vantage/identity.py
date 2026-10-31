"""Single sign-on: map an identity provider's groups onto Vantage roles.

Vantage does not log anyone in through OpenID Connect itself — mozilla-django-oidc,
django-allauth and python-social-auth all do that well, against Entra ID (Azure AD),
Okta, Google Workspace, Keycloak and the rest. What none of them knows is which of
*your* roles (``auth.Group``) a person's directory groups should mean. That mapping is
this module: one function, :func:`sync_roles`, plus thin adapters for the three
libraries, all driven by ``VANTAGE["ROLE_MAPPING"]``::

    VANTAGE = {
        "ROLE_MAPPING": {
            "CLAIM": "groups",                       # where the IdP puts group ids
            "MAP": {
                "3f1c...-finance-ops": ["Finance"],  # IdP group -> Vantage role(s)
                "panel-admins": ["Support", "Finance"],
            },
            "SET_STAFF": True,   # mapped people may sign in to the panel at all
        },
    }

The rules, chosen to fail closed:

* Only roles named in ``MAP`` are ever *managed*. A role granted by hand that the
  mapping never mentions is left alone, so turning SSO on cannot strip a break-glass
  account of the access it was given deliberately.
* A managed role the provider no longer asserts is removed at the next sign-in. Group
  removal in the directory is the off-boarding path, and a mapping that only ever added
  roles would quietly turn every leaver into a standing account.
* The mapping never makes anyone a superuser, and never creates a role: a typo in a role
  name is logged and skipped rather than minting an empty group someone could later
  fill with permissions.
* Every change is written to the audit log against the user.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "ROLE_MAPPING_DEFAULTS",
    "MozillaOIDCRoleMixin",
    "allauth_sync",
    "external_groups",
    "resolve_role_mapping",
    "social_auth_pipeline",
    "sync_roles",
]

logger = logging.getLogger("vantage.identity")

#: The keys of ``VANTAGE["ROLE_MAPPING"]``, with their defaults.
ROLE_MAPPING_DEFAULTS: dict[str, Any] = {
    # The claim holding the list of groups. Entra ID and Okta both use "groups" by
    # default; Keycloak commonly uses "groups" or "roles".
    "CLAIM": "groups",
    # {provider group: role name or list of role names}.
    "MAP": {},
    # Grant is_staff to anyone the mapping gives at least one role, and take it away from
    # anyone it gives none (superusers excepted). Opt-in: with it on, the directory is
    # the only way into the panel, including for staff accounts made by hand.
    "SET_STAFF": False,
    # Remove managed roles the provider no longer asserts.
    "REMOVE_UNLISTED": True,
}


def resolve_role_mapping(value: Any) -> dict[str, Any]:
    """Validate ``VANTAGE["ROLE_MAPPING"]`` and merge it over the defaults."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ImproperlyConfigured(
            f"VANTAGE['ROLE_MAPPING'] must be a dict, got {type(value).__name__}."
        )
    unknown = sorted(set(value) - set(ROLE_MAPPING_DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(
            f"Unrecognised key(s) in VANTAGE['ROLE_MAPPING']: {', '.join(unknown)}. "
            f"Expected {', '.join(ROLE_MAPPING_DEFAULTS)}."
        )
    resolved = {**ROLE_MAPPING_DEFAULTS, **value}
    mapping = resolved["MAP"]
    if not isinstance(mapping, dict):
        raise ImproperlyConfigured("VANTAGE['ROLE_MAPPING']['MAP'] must be a dict.")
    normalised: dict[str, tuple[str, ...]] = {}
    for group, roles in mapping.items():
        roles = [roles] if isinstance(roles, str) else roles
        if not isinstance(roles, list | tuple) or not all(
            isinstance(role, str) and role.strip() for role in roles
        ):
            raise ImproperlyConfigured(
                f"VANTAGE['ROLE_MAPPING']['MAP'][{group!r}] must be a role name or a list "
                f"of role names; got {roles!r}."
            )
        normalised[str(group)] = tuple(roles)
    resolved["MAP"] = normalised
    if not isinstance(resolved["CLAIM"], str) or not resolved["CLAIM"]:
        raise ImproperlyConfigured("VANTAGE['ROLE_MAPPING']['CLAIM'] must be a claim name.")
    return resolved


def _config() -> dict[str, Any]:
    from vantage.conf import vantage_settings

    return vantage_settings.ROLE_MAPPING


def external_groups(claims: dict[str, Any] | None, claim: str | None = None) -> list[str]:
    """The provider's groups out of a claims dict, tolerating the usual shapes.

    A list of strings is the norm; a single string (some providers send one group
    unwrapped) and a space- or comma-separated string are accepted too. Anything else
    is treated as "no groups" — which removes managed roles, the safe direction.
    """
    claim = claim or _config()["CLAIM"]
    value = (claims or {}).get(claim)
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if isinstance(item, str | int)]
    return []


def sync_roles(user, groups: Iterable[str], *, config: dict[str, Any] | None = None):
    """Make ``user``'s managed roles match what ``groups`` maps to.

    Returns ``(added, removed)``, two sorted lists of role names. Safe to call on every
    sign-in: it writes nothing when nothing changed.
    """
    from django.contrib.auth.models import Group

    config = config or _config()
    mapping: dict[str, tuple[str, ...]] = config["MAP"]
    managed = {role for roles in mapping.values() for role in roles}
    if not managed:
        return [], []
    wanted = {role for group in groups for role in mapping.get(str(group), ())}

    existing = {group.name: group for group in Group.objects.filter(name__in=managed)}
    for missing in sorted(managed - set(existing)):
        logger.warning(
            "VANTAGE['ROLE_MAPPING'] names the role %r, which does not exist; skipped.",
            missing,
        )
    current = set(user.groups.filter(name__in=managed).values_list("name", flat=True))
    to_add = sorted((wanted & set(existing)) - current)
    to_remove = sorted(current - wanted) if config["REMOVE_UNLISTED"] else []

    if to_add:
        user.groups.add(*(existing[name] for name in to_add))
    if to_remove:
        user.groups.remove(*(existing[name] for name in to_remove))

    staff_changed = False
    if config["SET_STAFF"] and not user.is_superuser:
        should_be_staff = bool(wanted & set(existing))
        if user.is_staff != should_be_staff:
            user.is_staff = should_be_staff
            user.save(update_fields=["is_staff"])
            staff_changed = True

    if to_add or to_remove or staff_changed:
        from vantage import audit

        changes: dict[str, Any] = {
            "roles": {
                "from": sorted(current),
                "to": sorted((current | set(to_add)) - set(to_remove)),
            }
        }
        if staff_changed:
            changes["is_staff"] = {"from": not user.is_staff, "to": user.is_staff}
        audit.record(
            None,
            "update",
            obj=user,
            changes=changes,
            action_name="Roles synchronised from the identity provider",
        )
        # The permission cache on the user object predates the change.
        for attr in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            user.__dict__.pop(attr, None)
    return to_add, to_remove


# ── adapters for the common SSO libraries ────────────────────────────────────


class MozillaOIDCRoleMixin:
    """Mix into a ``mozilla_django_oidc.auth.OIDCAuthenticationBackend`` subclass.

    ::

        class Backend(MozillaOIDCRoleMixin, OIDCAuthenticationBackend):
            pass

    ``create_user`` and ``update_user`` are the library's documented hooks; both still
    run the parent's version first.
    """

    def create_user(self, claims):
        user = super().create_user(claims)  # type: ignore[misc]
        sync_roles(user, external_groups(claims))
        return user

    def update_user(self, user, claims):
        user = super().update_user(user, claims)  # type: ignore[misc]
        sync_roles(user, external_groups(claims))
        return user


def social_auth_pipeline(backend=None, user=None, response=None, *args, **kwargs):
    """A python-social-auth pipeline step. Append ``"vantage.identity.social_auth_pipeline"``
    after ``social_core.pipeline.user.user_details`` in ``SOCIAL_AUTH_PIPELINE``."""
    if user is not None:
        sync_roles(user, external_groups(response or {}))
    return {}


def allauth_sync(request=None, sociallogin=None, **kwargs):
    """A receiver for django-allauth's ``pre_social_login``/``social_account_updated``.

    ::

        from allauth.socialaccount.signals import social_account_updated
        social_account_updated.connect(vantage.identity.allauth_sync)
    """
    user = getattr(sociallogin, "user", None)
    account = getattr(sociallogin, "account", None)
    if user is None or getattr(user, "pk", None) is None:
        return
    sync_roles(user, external_groups(getattr(account, "extra_data", None) or {}))
