"""System checks.

Misconfiguration should fail ``manage.py check``, not surface as a puzzling panel at
runtime. Every check here exists because the alternative is a silent wrong result.
"""

from __future__ import annotations

from django.core.checks import Error, register

from vantage.conf import CHOICES, DEFAULTS


@register()
def check_vantage_settings(app_configs, **kwargs):
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

    import difflib

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
