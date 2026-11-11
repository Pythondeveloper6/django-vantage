"""Agent identities: bearer-token access to the JSON API, narrowed by rules.

An agent is an ordinary service user (with an ordinary role) plus an
:class:`~vantage.models.AgentIdentity` that can only *remove* access from it. Every
permission, field and tenancy check applies to the service user unchanged.

**Confinement.** An agent reaches the JSON API and nothing else. Every route that is
not an :class:`vantage.api.ApiView` is wrapped by :func:`guard_patterns`, which refuses
a request carrying a ``Bearer vta_`` token with a 403. The roles matrix, the account and
two-factor pages, impersonation, break-glass, webhooks, reports and settings are all
HTML routes, so none of them can be reached with a token.

**Fail closed.** A request with any ``Authorization: Bearer`` header is an agent
request: if the token is bad, the answer is 401, never a fall-back to the session.
"""

from __future__ import annotations

import hashlib
import secrets
import string
import time
import uuid
from datetime import timedelta

from django.conf import settings as django_settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone

from vantage.conf import DEFAULTS, vantage_settings

__all__ = [
    "AgentAuthError",
    "AgentError",
    "authenticate_bearer",
    "check_access",
    "check_rate",
    "check_write_budget",
    "create_identity",
    "current_agent",
    "current_run",
    "enabled",
    "guard_patterns",
    "has_bearer",
    "rotate_token",
    "visible_panels",
]

TOKEN_PREFIX = "vta_"  # noqa: S105 - a scheme marker, not a secret
PREFIX_LENGTH = 8
_ALPHABET = string.ascii_letters + string.digits
#: ``last_used_at`` and a run's ``last_seen_at`` are written at most this often.
TOUCH_SECONDS = 60
RUN_KEY_MAX = 64


class AgentError(Exception):
    """A refused identity change (for example a superuser as service user)."""


class AgentAuthError(Exception):
    """A bearer token that must not be accepted. ``reason`` is for logs and tests; the
    response never says which part was wrong."""

    def __init__(self, reason: str, status: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.detail = "Invalid or expired token."


def enabled() -> bool:
    """Whether agent identities are on.

    Read from ``settings.VANTAGE`` directly rather than through ``vantage_settings``:
    the check runs on every request, and resolving the settings object reads the
    database layer on a cold cache, a cost an autocomplete keystroke should not pay. It
    is also deliberately code-only: ``AGENTS`` is not in ``siteconfig.UI_EDITABLE``, so
    nobody can switch machine access on from a settings screen.
    """
    from django.conf import settings

    return bool((getattr(settings, "VANTAGE", None) or {}).get("AGENTS", DEFAULTS["AGENTS"]))


# ── tokens ──────────────────────────────────────────────────────────────────


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_token() -> tuple[str, str, str]:
    """``(token, prefix, hash)``."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(PREFIX_LENGTH))
    secret = secrets.token_urlsafe(32)
    return f"{TOKEN_PREFIX}{prefix}_{secret}", prefix, _hash(secret)


def _split(token: str) -> tuple[str, str] | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    parts = token[len(TOKEN_PREFIX) :].split("_", 1)
    if len(parts) != 2 or len(parts[0]) != PREFIX_LENGTH or not parts[1]:
        return None
    return parts[0], parts[1]


def _refuse_superuser(user) -> None:
    if getattr(user, "is_superuser", False):
        raise AgentError(
            "An agent must never run as a superuser. Use a normal account with a role."
        )


def create_identity(
    name: str,
    user,
    owner=None,
    *,
    access: str = "read",
    allowed_models=(),
    allow_delete: bool = False,
    require_approval: bool = False,
    expires_at=None,
    rate_limit_per_minute: int | None = None,
    max_writes_per_run: int = 0,
):
    """Create an identity. Returns ``(identity, plaintext_token)``.

    The token is returned once and cannot be recovered: only its hash is stored.
    """
    from vantage.models import AgentIdentity

    _refuse_superuser(user)
    if access not in ("read", "write"):
        raise AgentError("access must be 'read' or 'write'.")
    token, prefix, digest = _new_token()
    if rate_limit_per_minute is None:
        rate_limit_per_minute = int(vantage_settings.AGENT_RATE_LIMIT_PER_MINUTE)
    identity = AgentIdentity.objects.create(
        name=name,
        user=user,
        owner=owner,
        token_prefix=prefix,
        token_hash=digest,
        access=access,
        allowed_models=sorted({str(m).lower() for m in allowed_models}),
        allow_delete=allow_delete,
        require_approval=require_approval,
        rate_limit_per_minute=rate_limit_per_minute,
        max_writes_per_run=max_writes_per_run,
        expires_at=expires_at,
    )
    return identity, token


def rotate_token(identity) -> str:
    """Replace the token. The old one stops working at once. Returns the new one."""
    token, prefix, digest = _new_token()
    identity.token_prefix = prefix
    identity.token_hash = digest
    identity.save(update_fields=["token_prefix", "token_hash"])
    return token


def has_bearer(request) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    return header[:7].lower() == "bearer "


def _bearer_token(request) -> str:
    return request.META.get("HTTP_AUTHORIZATION", "")[7:].strip()


# ── authentication ──────────────────────────────────────────────────────────

_DUMMY_HASH = _hash("not-a-real-secret")


def authenticate_bearer(request):
    """Authenticate ``Authorization: Bearer vta_...`` and return the identity.

    Sets ``request.user`` (the service user), ``request.vantage_agent`` and
    ``request.vantage_run``. Raises :class:`AgentAuthError` for every refusal; the
    caller must turn that into a 401 and must not fall back to a session.
    """
    from vantage.models import AgentIdentity

    if not enabled():
        raise AgentAuthError("feature off")
    parsed = _split(_bearer_token(request))
    if parsed is None:
        raise AgentAuthError("malformed")
    prefix, secret = parsed
    identity = (
        AgentIdentity.objects.select_related("user", "owner").filter(token_prefix=prefix).first()
    )
    # Hash and compare even for an unknown prefix, so timing does not say which failed.
    stored = identity.token_hash if identity else _DUMMY_HASH
    matches = secrets.compare_digest(_hash(secret), stored)
    if identity is None:
        raise AgentAuthError("unknown prefix")
    if not matches:
        raise AgentAuthError("wrong secret")
    if identity.revoked_at is not None:
        raise AgentAuthError("revoked")
    if identity.expires_at is not None and identity.expires_at <= timezone.now():
        raise AgentAuthError("expired")
    user = identity.user
    if not user.is_active:
        raise AgentAuthError("service user inactive")
    if user.is_superuser:
        raise AgentAuthError("service user is a superuser")

    request.user = user
    request.vantage_agent = identity
    request.vantage_run = None
    request._vantage_run_key = _run_key(request)
    # A client-named run is created now; otherwise only a write (or audit) needs one.
    if request.META.get("HTTP_X_VANTAGE_RUN", "").strip() or request.method not in (
        "GET",
        "HEAD",
        "OPTIONS",
    ):
        current_run(request)
    _touch(identity)
    return identity


def _run_key(request) -> str:
    key = request.META.get("HTTP_X_VANTAGE_RUN", "").strip()[:RUN_KEY_MAX]
    return key or uuid.uuid4().hex


def _touch(identity) -> None:
    from vantage.models import AgentIdentity

    now = timezone.now()
    last = identity.last_used_at
    if last is None or now - last >= timedelta(seconds=TOUCH_SECONDS):
        AgentIdentity.objects.filter(pk=identity.pk).update(last_used_at=now)
        identity.last_used_at = now


def current_agent(request):
    """The authenticated :class:`AgentIdentity`, or ``None``."""
    return getattr(request, "vantage_agent", None)


def current_run(request):
    """The :class:`AgentRun` of this request (created on first use), or ``None``."""
    identity = current_agent(request)
    if identity is None:
        return None
    run = getattr(request, "vantage_run", None)
    if run is not None:
        return run
    from vantage.models import AgentRun

    key = getattr(request, "_vantage_run_key", None) or _run_key(request)
    run, created = AgentRun.objects.get_or_create(identity=identity, run_key=key)
    if not created and timezone.now() - run.last_seen_at >= timedelta(seconds=TOUCH_SECONDS):
        AgentRun.objects.filter(pk=run.pk).update(last_seen_at=timezone.now())
    request.vantage_run = run
    return run


# ── guardrails ──────────────────────────────────────────────────────────────


def _verb(request) -> str:
    method = request.method.upper()
    if method in ("GET", "HEAD", "OPTIONS"):
        return "read"
    return "delete" if method == "DELETE" else "write"


def _api_error(status: int, detail: str, **extra):
    from vantage.api import ApiError

    return ApiError(status, detail, **extra)


def model_allowed(identity, model_or_panel) -> bool:
    allowed = identity.allowed_models or []
    if not allowed:
        return True
    opts = getattr(model_or_panel, "opts", None) or model_or_panel._meta
    return opts.label_lower in {str(a).lower() for a in allowed}


def visible_panels(request, panels):
    """``panels`` narrowed to the agent's ``allowed_models`` (unchanged for a person)."""
    identity = current_agent(request)
    if identity is None:
        return list(panels)
    return [p for p in panels if model_allowed(identity, p)]


def check_access(request, panel, verb: str | None = None) -> None:
    """Refuse what the identity's rules forbid. No-op for a non-agent request.

    ``verb`` is ``"read"``, ``"write"`` or ``"delete"``; by default it follows the HTTP
    method. A model outside ``allowed_models`` is a 404, so its existence is not shown.
    """
    identity = current_agent(request)
    if identity is None:
        return
    if not model_allowed(identity, panel):
        raise _api_error(404, "No such model.")
    verb = verb or _verb(request)
    if verb != "read" and identity.access != "write":
        raise _api_error(403, "This agent is read-only.")
    if verb == "delete" and not identity.allow_delete:
        raise _api_error(403, "This agent may not delete records.")


def check_rate(request) -> None:
    """Count this request against the identity's per-minute budget; 429 when spent."""
    identity = current_agent(request)
    if identity is None or not identity.rate_limit_per_minute:
        return
    window = int(time.time() // 60)
    key = f"vantage:agent-rate:{identity.pk}:{window}"
    cache.add(key, 0, timeout=120)
    try:
        count = cache.incr(key)
    except ValueError:
        # A cache that stores nothing (the dummy backend): nothing to count against.
        return
    if count > identity.rate_limit_per_minute:
        from vantage.api import RateLimited

        raise RateLimited(60 - int(time.time() % 60))


def check_write_budget(request, rows: int = 1) -> None:
    """Spend one write of the run's budget; refuse (403) when ``max_writes_per_run`` is spent.

    The check and the increment are one conditional ``UPDATE``, so concurrent requests
    cannot overshoot.
    """
    from django.db.models import F

    from vantage.models import AgentRun

    identity = current_agent(request)
    if identity is None:
        return
    run = current_run(request)
    fields = {
        "writes": F("writes") + 1,
        "rows_changed": F("rows_changed") + max(rows, 0),
        "deletes": F("deletes") + (1 if request.method.upper() == "DELETE" else 0),
    }
    queryset = AgentRun.objects.filter(pk=run.pk)
    if identity.max_writes_per_run:
        queryset = queryset.filter(writes__lt=identity.max_writes_per_run)
    if not queryset.update(**fields):
        raise _api_error(403, "This run has used its write budget.", code="write_budget_exceeded")
    run.refresh_from_db(fields=["writes", "rows_changed", "deletes"])


# ── labels ──────────────────────────────────────────────────────────────────


def actor_label(identity) -> str:
    owner = identity.owner
    if owner is not None:
        return f"Agent {identity.name} (for {owner.get_username()})"
    return f"Agent {identity.name}"


# ── confinement and CSRF ────────────────────────────────────────────────────


def _is_api(callback) -> bool:
    from vantage.api import ApiView

    view_class = getattr(callback, "view_class", None)
    return isinstance(view_class, type) and issubclass(view_class, ApiView)


def csrf_response(request):
    """Run Django's CSRF check by hand. ``None`` if it passes (or is not installed)."""
    if "django.middleware.csrf.CsrfViewMiddleware" not in django_settings.MIDDLEWARE:
        return None
    from django.middleware.csrf import CsrfViewMiddleware

    return CsrfViewMiddleware(lambda r: HttpResponse()).process_view(request, None, (), {})


def _confined(view):
    from functools import wraps

    @wraps(view)
    def guarded(request, *args, **kwargs):
        if enabled() and has_bearer(request) and _bearer_token(request).startswith(TOKEN_PREFIX):
            return HttpResponse("Agent tokens cannot be used on this page.", status=403)
        return view(request, *args, **kwargs)

    guarded.agent_confined = True
    return guarded


def guard_patterns(patterns: list) -> list:
    """Confine agents to the API, in place.

    API views are marked CSRF-exempt here and re-check CSRF themselves in
    ``ApiView.authenticate`` unless the request is a token request with no session
    cookie. Every other route refuses a request that carries an agent token.
    """
    for pattern in patterns:
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            guard_patterns(nested)
            continue
        callback = getattr(pattern, "callback", None)
        if callback is None or getattr(callback, "agent_confined", False):
            continue
        if _is_api(callback):
            callback.csrf_exempt = True
            callback.agent_confined = True
        else:
            pattern.callback = _confined(callback)
    return patterns
