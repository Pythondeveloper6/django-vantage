# Single sign-on and role mapping

Vantage signs people in through Django's own authentication, so any SSO library that
logs a user into Django logs them into the panel: [mozilla-django-oidc], [django-allauth]
and [python-social-auth] all work against Microsoft Entra ID (Azure AD), Okta, Google
Workspace, Keycloak and other OpenID Connect providers.

What those libraries do not know is which of *your* roles a person's directory groups
should mean. `VANTAGE["ROLE_MAPPING"]` is that mapping, applied on every sign-in.

```python
VANTAGE = {
    "ROLE_MAPPING": {
        "CLAIM": "groups",          # the ID-token claim listing the person's groups
        "MAP": {
            # provider group (name or object id) -> Vantage role(s), i.e. auth.Group names
            "panel-finance": "Finance",
            "panel-admins": ["Support", "Finance"],
        },
        "SET_STAFF": True,          # mapped people get is_staff; unmapped people lose it
        "REMOVE_UNLISTED": True,    # drop managed roles the provider stopped asserting
    },
}
```

| Key | Default | Meaning |
|---|---|---|
| `CLAIM` | `"groups"` | Where the provider puts the groups. A list, or a space/comma-separated string |
| `MAP` | `{}` | Provider group to one role name or a list of them |
| `SET_STAFF` | `False` | Grant `is_staff` to anyone mapped to at least one role and remove it from anyone mapped to none. Superusers are never touched |
| `REMOVE_UNLISTED` | `True` | Remove a managed role at sign-in when the provider no longer asserts a group that maps to it |

## The rules

- **Only roles named in `MAP` are managed.** A role given by hand that the mapping never
  mentions is left alone, so switching SSO on does not strip an emergency account.
- **Removal follows the directory.** Taking someone out of a group removes the role at
  their next sign-in; off-boarding in the directory is off-boarding in the panel.
- **Never a superuser, never a new role.** A role name in `MAP` that does not exist is
  logged (`vantage.identity` logger) and skipped rather than created empty.
- **Audited.** Every change is an audit entry on the user ("Roles synchronised from the
  identity provider").

Permissions themselves stay on the roles, edited on the [roles screen](roles.md); the
mapping only decides membership. Two-factor requirements by role
(`TWO_FACTOR_REQUIRED_ROLES`) apply to mapped roles like any other — though with SSO
you will usually enforce MFA at the provider instead.

## Wiring it to your SSO library

=== "mozilla-django-oidc"

    ```python
    # myproject/auth.py
    from mozilla_django_oidc.auth import OIDCAuthenticationBackend
    from vantage.identity import MozillaOIDCRoleMixin

    class PanelOIDCBackend(MozillaOIDCRoleMixin, OIDCAuthenticationBackend):
        pass
    ```

    Point `AUTHENTICATION_BACKENDS` at `myproject.auth.PanelOIDCBackend`. With Entra ID,
    enable the `groups` claim in the app registration's token configuration.

=== "python-social-auth"

    ```python
    SOCIAL_AUTH_PIPELINE = [
        # ... the default steps ...
        "social_core.pipeline.user.user_details",
        "vantage.identity.social_auth_pipeline",
    ]
    ```

=== "django-allauth"

    ```python
    # apps.py ready()
    from allauth.socialaccount.signals import pre_social_login, social_account_updated
    from vantage.identity import allauth_sync

    pre_social_login.connect(allauth_sync)
    social_account_updated.connect(allauth_sync)
    ```

Anything else can call `vantage.identity.sync_roles(user, groups)` directly.

[mozilla-django-oidc]: https://mozilla-django-oidc.readthedocs.io/
[django-allauth]: https://docs.allauth.org/
[python-social-auth]: https://python-social-auth.readthedocs.io/
