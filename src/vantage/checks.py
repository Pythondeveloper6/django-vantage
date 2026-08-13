"""System checks.

Misconfiguration should fail ``manage.py check``, not surface as a puzzling panel at
runtime. Every check here exists because the alternative is a silent wrong result.

Checks are registered from :meth:`vantage.apps.SimpleVantageConfig.ready`, not at import
time, so the app registry is populated before any model introspection runs.
"""

from __future__ import annotations

import difflib

from django.core.checks import Error

from vantage.conf import CHOICES, DEFAULTS


def check_vantage_settings(app_configs, **kwargs) -> list[Error]:
    """Reject unknown keys and invalid choices in the ``VANTAGE`` setting."""
    from django.conf import settings as django_settings

    errors: list[Error] = []
    user = getattr(django_settings, "VANTAGE", None)

    if user is None:
        return errors

    if not isinstance(user, dict):
        return [
            Error(
                f"The VANTAGE setting must be a dict, got {type(user).__name__}.",
                id="vantage.E001",
            )
        ]

    for key in sorted(set(user) - set(DEFAULTS)):
        close = difflib.get_close_matches(key, DEFAULTS, n=1, cutoff=0.6)
        errors.append(
            Error(
                f"{key!r} is not a recognised Vantage setting.",
                hint=f"Did you mean {close[0]!r}?" if close else None,
                id="vantage.E002",
            )
        )

    for key, allowed in CHOICES.items():
        if key in user and user[key] not in allowed:
            errors.append(
                Error(
                    f"VANTAGE[{key!r}] must be one of {', '.join(allowed)}; "
                    f"got {user[key]!r}.",
                    id="vantage.E003",
                )
            )

    return errors


def check_panels(app_configs, **kwargs) -> list[Error]:
    """Validate every registered panel on every live site.

    Panels left entirely at their inferred defaults are skipped — there is no
    user-written configuration to be wrong.
    """
    from vantage.panels import ModelPanel
    from vantage.sites import all_sites

    if app_configs is not None:
        app_configs = set(app_configs)

    errors: list[Error] = []
    for site in all_sites:
        for panel in site.registry.panels():
            if type(panel) is ModelPanel:
                continue
            if app_configs is not None and panel.opts.app_config not in app_configs:
                continue
            errors.extend(panel.check())
    return errors
