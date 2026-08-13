"""Read-only mode and impersonation.

Both exist for the same reason: an admin panel is usually pointed at production, and
the two most common needs there are "let me look without any chance of breaking
something" and "show me exactly what this user sees".

Read-only is enforced in the permission layer rather than in the views. A guard in the
views would have to be remembered on every new write path, and the one that gets
forgotten is the one that matters. Denying the permission means every existing check —
buttons, actions, forms, the API — refuses at once, and a new write path inherits the
refusal without anyone thinking about it.

Impersonation is deliberately narrow: superusers only, never onto another superuser,
loud while it is happening, and recorded. The failure mode being guarded against is
someone quietly acquiring rights they do not have and leaving no trace.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

__all__ = [
    "IMPERSONATE_SESSION_KEY",
    "WRITE_ACTIONS",
    "ImpersonationError",
    "can_impersonate",
    "impersonating",
    "read_only_reason",
    "real_user",
    "start_impersonation",
    "stop_impersonation",
]

logger = logging.getLogger("vantage.security")

#: The actions read-only mode refuses. "view" is conspicuously absent.
WRITE_ACTIONS = frozenset({"add", "change", "delete"})

IMPERSONATE_SESSION_KEY = "vantage-impersonating"
IMPERSONATOR_SESSION_KEY = "vantage-impersonator"


class ImpersonationError(Exception):
    """Raised when an impersonation attempt is not allowed."""


def read_only_reason() -> str | None:
    """Why writes are refused right now, or ``None`` if they are not."""
    from vantage.conf import vantage_settings

    value = vantage_settings.READ_ONLY
    if not value:
        return None
    # A string doubles as the explanation shown to the user, which beats a bare
    # "permission denied" for something that is a deliberate operational choice.
    return value if isinstance(value, str) else "This panel is in read-only mode."


# ── impersonation ───────────────────────────────────────────────────────────


def impersonating(request) -> bool:
    return bool(getattr(request, "session", None) and request.session.get(IMPERSONATE_SESSION_KEY))


def real_user(request):
    """The account actually signed in, even while impersonating someone else."""
    session = getattr(request, "session", None)
    if not session or not session.get(IMPERSONATOR_SESSION_KEY):
        return request.user
    try:
        return get_user_model()._default_manager.get(pk=session[IMPERSONATOR_SESSION_KEY])
    except get_user_model().DoesNotExist:  # pragma: no cover - the account was deleted
        return request.user


def can_impersonate(user) -> bool:
    return bool(user and user.is_authenticated and user.is_superuser)


def start_impersonation(request, target) -> None:
    """Become ``target`` for the rest of this session.

    Django's login machinery is used rather than a request-level swap, so every
    downstream permission check sees the target naturally and nothing has to know
    impersonation exists.
    """
    from django.contrib.auth import login

    actor = real_user(request)

    if not can_impersonate(actor):
        raise ImpersonationError("Only superusers may impersonate.")
    if target.pk == actor.pk:
        raise ImpersonationError("You are already signed in as that user.")
    if target.is_superuser:
        # Otherwise impersonation is a lateral move between superusers that muddies
        # the audit trail without granting anything useful.
        raise ImpersonationError("Superusers cannot be impersonated.")
    if not target.is_active:
        raise ImpersonationError("That account is inactive.")

    logger.warning(
        "impersonation started: %s (id=%s) is now acting as %s (id=%s)",
        actor.get_username(),
        actor.pk,
        target.get_username(),
        target.pk,
    )

    login(request, target, backend="django.contrib.auth.backends.ModelBackend")
    # Written after login() because login flushes the session.
    request.session[IMPERSONATE_SESSION_KEY] = target.pk
    request.session[IMPERSONATOR_SESSION_KEY] = actor.pk


def stop_impersonation(request):
    """Return to the original account."""
    from django.contrib.auth import login

    session = getattr(request, "session", None)
    if not session or not session.get(IMPERSONATOR_SESSION_KEY):
        raise ImpersonationError("Not currently impersonating.")

    actor_pk = session[IMPERSONATOR_SESSION_KEY]
    try:
        actor = get_user_model()._default_manager.get(pk=actor_pk)
    except get_user_model().DoesNotExist as exc:  # pragma: no cover
        raise ImpersonationError("The original account no longer exists.") from exc

    logger.warning(
        "impersonation ended: %s (id=%s) returned from %s",
        actor.get_username(),
        actor.pk,
        request.user.get_username(),
    )

    login(request, actor, backend="django.contrib.auth.backends.ModelBackend")
    request.session.pop(IMPERSONATE_SESSION_KEY, None)
    request.session.pop(IMPERSONATOR_SESSION_KEY, None)
    return actor
