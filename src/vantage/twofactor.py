"""Two-factor sign-in: authenticator-app codes (TOTP), recovery codes, and the gate.

Everything that decides whether a code is right, and whether a session may use the
panel, lives here so it can be tested without HTTP; :mod:`vantage.twofactorviews` and
the second sign-in step in :mod:`vantage.authviews` are thin.

Five decisions shape it.

**The half-signed-in session is anonymous.** After a correct password, the sign-in
view does not call ``login()``. It writes a short-lived *pending* record into the
session — who, through which backend, until when — and the session stays anonymous
until a code is accepted. So nothing anywhere in the project, not only the panel,
treats someone who knows only the password as signed in. ``login()`` runs once, after
the code, which is also what rotates the session key.

**Every panel route is behind the gate, not just the pages someone remembered.**
:func:`guard_patterns` wraps every URL the site builds, so a page added next year — a
plugin's, a dashboard's, an API endpoint's — is covered without knowing this module
exists. The few routes that must stay open (the second step itself, enrolment while it
is required, the translation catalogue) say so with an attribute; forgetting that
attribute fails closed. A session that signed in through some *other* login view, such
as ``django.contrib.admin``'s, is sent to the second step before the panel will show it
anything — but only the panel. That other login view's own pages are not the panel's,
and a password-only session can use them: ``django.contrib.admin`` is held to the same
requirement only if :class:`AdminTwoFactorMiddleware` is installed, and ``check
--deploy`` says so (``vantage.W095``) when it is mounted beside a requirement without it.

**A code works once.** The step it was accepted for is stored and the next code must be
for a later step, enforced with a conditional ``UPDATE`` so that two requests racing
with the same code cannot both win. Attempts count against the *account*, not the
session, so an attacker who has the password gains nothing by opening fresh sessions;
each is counted before its answer is compared, so nor by sending them in parallel; too
many in a row locks the second step for a few minutes.

**The secret is sealed, and shown only until it is confirmed.** It is encrypted with a
key derived from ``SECRET_KEY`` (HMAC-SHA256 as a stream cipher, then
encrypt-then-MAC), so a copy of the database alone does not hand out working
authenticators. The tag also covers whose secret it is and which enrolment, so a value
copied into another row is refused. ``SECRET_KEY_FALLBACKS`` are tried on the way out,
and a value only a fallback opens is sealed again under the current key at its owner's
next sign-in, so rotating the key the documented way does not lock anybody out.

**The gate costs no queries on an ordinary request.** Whether the person has an
authenticator and whether one is required is worked out when they sign in, stored in
the session, and re-checked at most once a minute — or at once when the requirement
settings change. A colleague's reset, or a new role membership, therefore takes effect
within :data:`RECHECK_SECONDS` rather than instantly; the alternative was two queries
on every page, autocomplete keystroke and API call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from urllib.parse import quote, urlencode

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, transaction
from django.db.models import F, Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext

from vantage.conf import vantage_settings

__all__ = [
    "DIGITS",
    "PERIOD",
    "AdminTwoFactorMiddleware",
    "Outcome",
    "admin_site_of",
    "begin_pending",
    "complete_login",
    "confirm_enrolment",
    "device_for",
    "disable",
    "enabled",
    "guard_patterns",
    "hotp",
    "is_required",
    "matching_step",
    "open_sealed",
    "pending_user",
    "provisioning_uri",
    "regenerate_codes",
    "requirement",
    "reset",
    "seal",
    "seal_context",
    "setting",
    "start_enrolment",
    "totp",
    "unseal",
    "verify",
    "verify_password",
]

logger = logging.getLogger("vantage.security")

#: RFC 6238's defaults, and the only values every authenticator app supports. Not
#: settings: an app that silently ignores ``digits=8`` produces codes that never match.
DIGITS = 6
PERIOD = 30
#: Steps either side of now that are accepted, for a phone clock a little off.
WINDOW = 1
#: How long the half-signed-in state lasts between the password and the code.
PENDING_SECONDS = 300
#: How stale the session's cached answer to "is a second step owed?" may get.
RECHECK_SECONDS = 60
RECOVERY_CODE_COUNT = 10
#: 32 symbols, so each one is exactly five bits: no ``0``/``o`` or ``1``/``l`` to misread
#: off a printout. Sixteen of them make 80 bits per code.
RECOVERY_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
RECOVERY_LENGTH = 16

SESSION_PENDING = "vantage-2fa-pending"
SESSION_VERIFIED = "vantage-2fa-verified"
SESSION_STATE = "vantage-2fa-state"

#: What :func:`requirement` returns.
VERIFY = "verify"
ENROL = "enrol"


# ── RFC 4226 / RFC 6238 ─────────────────────────────────────────────────────


def hotp(key: bytes, counter: int, *, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    """The HOTP value for ``counter``, as a zero-padded string (RFC 4226 §5.3)."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp(
    key: bytes, at: float, *, digits: int = DIGITS, period: int = PERIOD, digest=hashlib.sha1
) -> str:
    """The TOTP value at Unix time ``at`` (RFC 6238 §4)."""
    return hotp(key, int(at // period), digits=digits, digest=digest)


def normalise_code(value: object) -> str:
    """Strip what people paste around a code: spaces, hyphens, and case."""
    return "".join(str(value or "").split()).replace("-", "").lower()[:64]


def is_totp_shaped(code: str) -> bool:
    return len(code) == DIGITS and code.isascii() and code.isdigit()


def is_recovery_shaped(code: str) -> bool:
    return len(code) == RECOVERY_LENGTH and all(c in RECOVERY_ALPHABET for c in code)


def matching_step(key: bytes, code: str, at: float) -> int | None:
    """The time step within :data:`WINDOW` that ``code`` is right for, or ``None``.

    Every candidate is computed and compared, with :func:`hmac.compare_digest`,
    before anything is decided, so the time taken does not say which step was close.
    """
    if not is_totp_shaped(code):
        return None
    current = int(at // PERIOD)
    found = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(hotp(key, step).encode(), code.encode()) and found is None:
            found = step
    return found


def new_secret() -> bytes:
    """160 random bits: RFC 4226's recommended key length for HMAC-SHA-1."""
    return secrets.token_bytes(20)


def b32(key: bytes) -> str:
    """The key as authenticator apps want it typed: base32, no padding."""
    return base64.b32encode(key).decode("ascii").rstrip("=")


def issuer() -> str:
    return setting("TWO_FACTOR_ISSUER") or str(vantage_settings.SITE_TITLE)


def setting(name: str):
    """One of the ``TWO_FACTOR*`` settings, read without touching the database.

    They are code-only — never in the settings screen's allowlist — so the stored row
    cannot change them, and skipping it keeps the gate free on requests that would
    otherwise never have read a setting at all: an autocomplete keystroke, an API call.
    Validated here as well, because such a request may be the first to read them.
    """
    from vantage.conf import two_factor_problem
    from vantage.siteconfig import code_defaults

    values = code_defaults()
    problem = two_factor_problem(values)
    if problem:
        raise ImproperlyConfigured(problem)
    return values[name]


def enabled() -> bool:
    return bool(setting("TWO_FACTOR"))


def provisioning_uri(user, key: bytes) -> str:
    """The ``otpauth://`` URI an app reads out of the QR code (Key URI Format).

    The issuer goes in both the label and the parameter: older apps read only the
    first and newer ones prefer the second.
    """
    name = issuer()
    label = f"{quote(name, safe='')}:{quote(user.get_username(), safe='@')}"
    query = urlencode(
        {
            "secret": b32(key),
            "issuer": name,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD,
        },
        quote_via=quote,
    )
    return f"otpauth://totp/{label}?{query}"


# ── sealing the secret ──────────────────────────────────────────────────────

#: What :func:`seal` writes. ``v2`` binds the value to the row it belongs to;
#: ``v1`` did not, and is still read so that enrolled people keep signing in — and
#: re-sealed as ``v2`` the next time one of them does.
_SEAL_VERSION = "v2"
_SEAL_VERSIONS = frozenset({"v1", "v2"})


def _seal_keys(secret_key: str) -> tuple[bytes, bytes]:
    """Two independent keys from one ``SECRET_KEY``: one to encrypt, one to sign."""
    encrypt = salted_hmac("vantage.twofactor.encrypt", b"", secret=secret_key, algorithm="sha256")
    sign = salted_hmac("vantage.twofactor.sign", b"", secret=secret_key, algorithm="sha256")
    return encrypt.digest(), sign.digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    stream = b""
    counter = 0
    while len(stream) < length:
        stream += hmac.new(key, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return stream[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream, strict=True))


def _signed(version: str, context: str, nonce: bytes, ciphertext: bytes) -> bytes:
    """What the tag covers. From ``v2`` on, the context, length-prefixed."""
    if version == "v1":
        return b"v1" + nonce + ciphertext
    bound = context.encode()
    return version.encode() + struct.pack(">I", len(bound)) + bound + nonce + ciphertext


def seal_context(user_id, tag: str) -> str:
    """What a device's secret is bound to: whose it is, and which enrolment."""
    return f"vantage.twofactor.device:{user_id}:{tag}"


def seal(plaintext: bytes, context: str = "") -> str:
    """Encrypt-then-MAC with keys derived from ``SECRET_KEY``.

    The stream is HMAC-SHA256 over a random 128-bit nonce and a counter — a PRF in
    counter mode, the construction HKDF's expand step is built on — so no cipher
    beyond the standard library is needed, and the tag means a tampered row is
    refused rather than turned into a different secret.

    The tag also covers ``context`` — for a device, :func:`seal_context` — so a value
    copied into another row is refused too. Without that, someone who can write to the
    database but not read ``SECRET_KEY`` could copy their own sealed secret over a
    colleague's and sign in as them with their own phone.
    """
    nonce = secrets.token_bytes(16)
    encrypt, sign = _seal_keys(django_settings.SECRET_KEY)
    ciphertext = _xor(plaintext, _keystream(encrypt, nonce, len(plaintext)))
    signed = _signed(_SEAL_VERSION, context, nonce, ciphertext)
    tag = hmac.new(sign, signed, hashlib.sha256).digest()
    body = base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")
    return f"{_SEAL_VERSION}${body}"


def open_sealed(token: str, context: str = "") -> tuple[bytes, bool]:
    """``(plaintext, stale)``, or :class:`ValueError` if no known key made this token.

    ``stale`` means it should be sealed again: it is an older format, or it opened
    only with one of the ``SECRET_KEY_FALLBACKS`` — which are meant to be dropped once
    a rotation is over, and would take every value still sealed with them along.
    """
    version, _, body = (token or "").partition("$")
    if version not in _SEAL_VERSIONS:
        raise ValueError("Unrecognised sealed value.")
    try:
        raw = base64.urlsafe_b64decode(body.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Malformed sealed value.") from exc
    if len(raw) < 16 + 32:
        raise ValueError("Malformed sealed value.")
    nonce, ciphertext, tag = raw[:16], raw[16:-32], raw[-32:]
    signed = _signed(version, context, nonce, ciphertext)
    keys = [django_settings.SECRET_KEY, *getattr(django_settings, "SECRET_KEY_FALLBACKS", [])]
    for index, secret_key in enumerate(keys):
        encrypt, sign = _seal_keys(secret_key)
        expected = hmac.new(sign, signed, hashlib.sha256).digest()
        if hmac.compare_digest(expected, tag):
            plaintext = _xor(ciphertext, _keystream(encrypt, nonce, len(ciphertext)))
            return plaintext, index > 0 or version != _SEAL_VERSION
    raise ValueError("The sealed value was not made with this SECRET_KEY.")


def unseal(token: str, context: str = "") -> bytes:
    """The plaintext, or :class:`ValueError` if no known key made this token."""
    return open_sealed(token, context)[0]


# ── recovery codes ──────────────────────────────────────────────────────────


def new_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, RECOVERY_LENGTH, 4))


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalise_code(code).encode("ascii")).hexdigest()


def _issue_codes(device) -> list[str]:
    """Replace every recovery code ``device`` has with a fresh set, and return them.

    The plaintext exists only in the return value. It is shown once, on the page that
    called this, and is unrecoverable afterwards by anyone — including the owner.
    """
    from vantage.models import TwoFactorRecoveryCode

    codes = [new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    with transaction.atomic():
        TwoFactorRecoveryCode.objects.filter(device=device).delete()
        TwoFactorRecoveryCode.objects.bulk_create(
            [TwoFactorRecoveryCode(device=device, code_hash=hash_recovery_code(c)) for c in codes]
        )
    return codes


def remaining_codes(device) -> int:
    return device.codes.filter(used_at__isnull=True).count()


# ── devices ─────────────────────────────────────────────────────────────────


def device_for(user, *, confirmed: bool = True):
    """The person's authenticator, confirmed (the default) or still being set up."""
    from vantage.models import TwoFactorDevice

    if user is None or not getattr(user, "is_authenticated", False) or user.pk is None:
        return None
    return TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=not confirmed).first()


def start_enrolment(user):
    """A new, unconfirmed device with a fresh secret, replacing any half-finished one.

    Refuses while a confirmed one exists: replacing a working authenticator is
    "turn it off, then on again", and turning it off needs proof of who you are.
    """
    from vantage.models import TwoFactorDevice

    with transaction.atomic():
        if TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
            raise ValueError("Two-factor sign-in is already on for this account.")
        TwoFactorDevice.objects.filter(user=user).delete()
        tag = secrets.token_hex(16)
        return TwoFactorDevice.objects.create(
            user=user, secret=seal(new_secret(), seal_context(user.pk, tag)), tag=tag
        )


def _open_device(device) -> tuple[bytes | None, bool]:
    try:
        return open_sealed(device.secret, seal_context(device.user_id, device.tag))
    except ValueError:
        logger.error(
            "Vantage could not unseal the two-factor secret for user id=%s. Was "
            "SECRET_KEY changed without adding the old key to SECRET_KEY_FALLBACKS, or "
            "was the stored value copied from another row?",
            device.user_id,
        )
        return None, False


def device_key(device) -> bytes | None:
    """The device's secret, or ``None`` if it can no longer be unsealed.

    ``None`` rather than an exception, because the caller's honest answer is the same
    as for a wrong code — and logged loudly, because the only causes are a
    ``SECRET_KEY`` changed without keeping the old one in ``SECRET_KEY_FALLBACKS``, and
    a sealed value moved from the row it was made for.
    """
    return _open_device(device)[0]


def _refresh_seal(device) -> None:
    """Seal the secret again if it is an older format or under a fallback key.

    Done after a right answer, so a value that is about to stop working is replaced
    by its owner's ordinary use rather than by a migration that needs the key.
    Conditional on the stored value, so two sign-ins racing write it once.
    """
    from vantage.models import TwoFactorDevice

    key, stale = _open_device(device)
    if key is None or not stale:
        return
    TwoFactorDevice.objects.filter(pk=device.pk, secret=device.secret).update(
        secret=seal(key, seal_context(device.user_id, device.tag))
    )


@dataclass(frozen=True)
class Outcome:
    """What :func:`verify` decided. Truthy when the code was accepted."""

    ok: bool
    #: ``"totp"`` or ``"recovery"`` on success; ``"invalid"``, ``"reused"`` or
    #: ``"locked"`` otherwise.
    reason: str
    retry_after: int = 0

    def __bool__(self) -> bool:
        return self.ok

    @property
    def message(self) -> str:
        if self.reason == "locked":
            minutes = max(1, -(-self.retry_after // 60))
            return gettext("Too many wrong codes. Wait %(minutes)s minute(s) and try again.") % {
                "minutes": minutes
            }
        if self.reason == "reused":
            return gettext("That code has already been used. Wait for the next one.")
        return gettext("That code is not right. Check the app and try again.")


def _locked_for(device) -> int:
    if device.locked_until is None:
        return 0
    remaining = (device.locked_until - timezone.now()).total_seconds()
    return int(remaining) + 1 if remaining > 0 else 0


@dataclass(frozen=True)
class _Attempt:
    """A reserved attempt: :func:`reserve_attempt` said yes, and counted it."""

    #: This attempt used up the last one, so the lock is already on.
    last: bool


def reserve_attempt(device) -> _Attempt | Outcome:
    """Count one attempt against the account *before* its answer is compared.

    The order is the whole point. Counting only after a wrong answer leaves a window
    between "is it locked?" and "count this failure", and a burst of parallel requests
    all fit through it: every one of them is compared before any is counted. Here the
    check and the count are one conditional ``UPDATE`` — not locked, under the limit,
    add one — and only a request that changed a row may compare anything. However the
    requests interleave, at most ``TWO_FACTOR_MAX_ATTEMPTS`` answers are compared per
    lockout.

    The attempt that reaches the limit switches the lock on at once, before its own
    answer is known; if that answer is right, :func:`_succeeded` lifts it again. Both
    statements share a transaction, so a crash between them cannot leave the counter
    at the limit with no lock to expire it.

    Returns the reservation, or the refusal to show.
    """
    from vantage.models import TwoFactorDevice

    limit = setting("TWO_FACTOR_MAX_ATTEMPTS")
    seconds = setting("TWO_FACTOR_LOCKOUT_SECONDS")
    now = timezone.now()
    rows = TwoFactorDevice.objects.filter(pk=device.pk)
    with transaction.atomic():
        reserved = rows.filter(
            Q(locked_until__isnull=True) | Q(locked_until__lte=now),
            failed_attempts__lt=limit,
        ).update(failed_attempts=F("failed_attempts") + 1, locked_until=None)
        if not reserved:
            device.refresh_from_db(fields=["locked_until", "failed_attempts"])
            return Outcome(False, "locked", _locked_for(device) or seconds)
        last = rows.filter(failed_attempts__gte=limit).update(
            failed_attempts=0, locked_until=now + timedelta(seconds=seconds)
        )
    if last:
        logger.warning(
            "Vantage locked two-factor sign-in for user id=%s after %s attempts.",
            device.user_id,
            limit,
        )
    return _Attempt(last=bool(last))


def _failed(attempt: _Attempt, reason: str = "invalid") -> Outcome:
    """The answer was wrong. Already counted; all that is left is what to say."""
    if attempt.last:
        return Outcome(False, "locked", setting("TWO_FACTOR_LOCKOUT_SECONDS"))
    return Outcome(False, reason)


def _succeeded(device, *, where: dict | None = None, **fields) -> int:
    """The answer was right: the count starts again and any lock it set is lifted."""
    from vantage.models import TwoFactorDevice

    rows = TwoFactorDevice.objects.filter(pk=device.pk, **(where or {}))
    return rows.update(failed_attempts=0, locked_until=None, **fields)


def verify(
    device, code: object, *, allow_recovery: bool = True, at: float | None = None
) -> Outcome:
    """Check one code — an app code, or a recovery code if allowed — and spend it.

    The attempt is counted before the code is even looked at (see
    :func:`reserve_attempt`), so the lockout is a lockout and not merely a slower
    oracle — also for a burst of requests sent in parallel.
    """
    from vantage.models import TwoFactorRecoveryCode

    attempt = reserve_attempt(device)
    if isinstance(attempt, Outcome):
        return attempt
    device.refresh_from_db()

    at = time.time() if at is None else at
    value = normalise_code(code)

    if is_totp_shaped(value):
        key = device_key(device)
        step = matching_step(key, value, at) if key is not None else None
        if step is not None:
            # Conditional on the stored step, so of two requests racing with one code,
            # exactly one updates a row; the other is a replay and is told so.
            now = timezone.now()
            if _succeeded(device, where={"last_step__lt": step}, last_step=step, last_used_at=now):
                _refresh_seal(device)
                return Outcome(True, "totp")
            return _failed(attempt, "reused")
    elif allow_recovery and is_recovery_shaped(value):
        spent = TwoFactorRecoveryCode.objects.filter(
            device=device, code_hash=hash_recovery_code(value), used_at__isnull=True
        ).update(used_at=timezone.now())
        if spent:
            _succeeded(device, last_used_at=timezone.now())
            _refresh_seal(device)
            return Outcome(True, "recovery")

    return _failed(attempt)


def verify_password(device, user, password: str) -> Outcome:
    """The password, as proof for a form that weakens two-factor sign-in.

    Through the same gate as a code: a wrong password counts toward the same lock, and
    is counted before it is checked. The password is the cheaper thing to guess, and a
    lockout either could step around is not one.
    """
    attempt = reserve_attempt(device)
    if isinstance(attempt, Outcome):
        return attempt
    if user.check_password(password):
        _succeeded(device)
        return Outcome(True, "password")
    return _failed(attempt)


def confirm_enrolment(request, device, code: object) -> tuple[Outcome, list[str]]:
    """Turn a pending device on if ``code`` proves an app holds its secret.

    Returns the outcome and, on success, the recovery codes to show exactly once.
    The confirming code's step is recorded, so it cannot then be replayed at sign-in.
    """
    outcome = verify(device, code, allow_recovery=False)
    if not outcome:
        return outcome, []
    device.refresh_from_db()
    device.confirmed_at = timezone.now()
    device.save(update_fields=["confirmed_at"])
    codes = _issue_codes(device)
    mark_verified(request, device)
    _audit(request, device.user, "two_factor.enabled", gettext("off"), gettext("on"))
    return outcome, codes


def regenerate_codes(request, device) -> list[str]:
    codes = _issue_codes(device)
    _audit(
        request,
        device.user,
        "two_factor.codes",
        gettext("previous set"),
        gettext("new set of %(count)s") % {"count": len(codes)},
        field=gettext("Recovery codes"),
    )
    return codes


def disable(request, device) -> None:
    """Switch the person's own two-factor sign-in off. The caller has checked proof."""
    user = device.user
    device.delete()
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(SESSION_VERIFIED, None)
        refresh_state(request, user)
    _audit(request, user, "two_factor.disabled", gettext("on"), gettext("off"))


def reset(request, target) -> bool:
    """An administrator switching *someone else's* two-factor sign-in off.

    Audited under the administrator's name. Their sessions notice within
    :data:`RECHECK_SECONDS`, and are sent to enrol again if a role requires it.
    """
    from vantage.models import TwoFactorDevice

    deleted, _ = TwoFactorDevice.objects.filter(user=target).delete()
    if not deleted:
        return False
    logger.warning(
        "Vantage two-factor sign-in was reset for user id=%s by %s.",
        target.pk,
        _actor_name(request),
    )
    _audit(request, target, "two_factor.reset", gettext("on"), gettext("off"))
    return True


def _actor_name(request) -> str:
    from vantage.safety import real_user

    if request is None or not hasattr(request, "user"):
        return "a management command"
    actor = real_user(request)
    return f"{actor.get_username()} (id={actor.pk})"


def _audit(request, user, action_name: str, before: str, after: str, *, field=None) -> None:
    from vantage.audit import record
    from vantage.models import VantageLogEntry

    record(
        request,
        VantageLogEntry.Action.UPDATE,
        obj=user,
        changes={str(field or gettext("Two-factor sign-in")): {"from": before, "to": after}},
        action_name=action_name,
    )


# ── who must use it ─────────────────────────────────────────────────────────


def _requirement_fingerprint() -> str:
    roles = sorted(setting("TWO_FACTOR_REQUIRED_ROLES"))
    superusers = setting("TWO_FACTOR_REQUIRED_SUPERUSERS")
    raw = f"{superusers}|{'|'.join(roles)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_required(user) -> bool:
    """Whether this person may not use the panel without a second step."""
    if setting("TWO_FACTOR_REQUIRED_SUPERUSERS") and user.is_superuser:
        return True
    roles = list(setting("TWO_FACTOR_REQUIRED_ROLES"))
    groups = getattr(user, "groups", None)
    if not roles or groups is None:
        return False
    return groups.filter(name__in=roles).exists()


# ── the session ─────────────────────────────────────────────────────────────


def refresh_state(request, user) -> dict:
    """Work out, and remember in the session, what this person owes the gate."""
    device = device_for(user)
    state = {
        "uid": str(user.pk),
        "tag": device.tag if device else None,
        "required": is_required(user),
        "at": int(time.time()),
        "conf": _requirement_fingerprint(),
    }
    request.session[SESSION_STATE] = state
    return state


def _state(request, user) -> dict:
    state = request.session.get(SESSION_STATE)
    if (
        isinstance(state, dict)
        and state.get("uid") == str(user.pk)
        and state.get("conf") == _requirement_fingerprint()
        and 0 <= time.time() - state.get("at", 0) < RECHECK_SECONDS
    ):
        return state
    return refresh_state(request, user)


def mark_verified(request, device) -> None:
    """Record that this session passed the second step against ``device``."""
    request.session[SESSION_VERIFIED] = {"uid": str(device.user_id), "tag": device.tag}
    refresh_state(request, device.user)


def is_verified(request, user, tag: str | None) -> bool:
    marker = request.session.get(SESSION_VERIFIED)
    return (
        tag is not None
        and isinstance(marker, dict)
        and marker.get("uid") == str(user.pk)
        and hmac.compare_digest(str(marker.get("tag", "")), tag)
    )


def requirement(request) -> str | None:
    """What the gate needs from this request first: :data:`VERIFY`, :data:`ENROL`, or nothing.

    Asked of the *real* user, not the impersonated one: the person at the keyboard is
    the one who has, or has not, proved who they are.
    """
    if not setting("TWO_FACTOR"):
        return None
    if getattr(request, "session", None) is None:
        return None
    from vantage.safety import real_user

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    user = real_user(request)
    if not user.is_staff:
        return None
    state = _state(request, user)
    if state["tag"]:
        return None if is_verified(request, user, state["tag"]) else VERIFY
    return ENROL if state["required"] else None


def carry(request) -> dict:
    """The two-factor keys of a session about to be flushed by ``login()``.

    Impersonation calls ``login()`` to become someone else, which flushes the session;
    but the person at the keyboard has not changed, and making them pass the second
    step again to start or stop impersonating would be friction for nothing.
    """
    session = getattr(request, "session", None) or {}
    return {key: session[key] for key in (SESSION_VERIFIED, SESSION_STATE) if key in session}


def restore(request, carried: dict) -> None:
    for key, value in carried.items():
        request.session[key] = value


def on_logged_in(sender, request=None, user=None, **kwargs) -> None:
    """Prime the session's answer at sign-in, so the first panel page costs nothing.

    Connected for every login in the project, so it is careful: a project that has not
    run the migration yet gets a log line, not a failed sign-in.
    """
    if request is None or getattr(request, "session", None) is None or user is None:
        return
    if not setting("TWO_FACTOR"):
        return
    try:
        with transaction.atomic():
            refresh_state(request, user)
    except DatabaseError:
        logger.exception("Vantage could not read two-factor state at sign-in.")
        request.session.pop(SESSION_STATE, None)


# ── the half-signed-in state ────────────────────────────────────────────────


def begin_pending(request, user, redirect_to: str) -> None:
    """Remember that ``user`` got the password right, without signing them in.

    The session key is rotated here too: a session id an attacker planted before the
    password was typed must not be the one that later carries the pending state.
    """
    request.session.cycle_key()
    request.session[SESSION_PENDING] = {
        "uid": str(user.pk),
        "backend": getattr(user, "backend", "") or "",
        "hash": user.get_session_auth_hash(),
        "at": int(time.time()),
        "next": redirect_to,
    }


def clear_pending(request) -> None:
    request.session.pop(SESSION_PENDING, None)


def pending_user(request):
    """``(user, pending)`` for a live half-signed-in session, or ``(None, None)``.

    Live means: recent, the user still exists and is active, the password has not
    changed since it was typed, the backend is still configured, and the user still
    has an authenticator to answer with.
    """
    from django.contrib.auth import get_user_model

    session = getattr(request, "session", None)
    pending = session.get(SESSION_PENDING) if session is not None else None
    if not isinstance(pending, dict):
        return None, None
    if not 0 <= time.time() - pending.get("at", 0) < PENDING_SECONDS:
        clear_pending(request)
        return None, None
    if pending.get("backend") not in django_settings.AUTHENTICATION_BACKENDS:
        clear_pending(request)
        return None, None
    model = get_user_model()
    try:
        user = model._default_manager.get(pk=model._meta.pk.to_python(pending.get("uid")))
    except (model.DoesNotExist, ValueError, TypeError):
        clear_pending(request)
        return None, None
    current_hash = user.get_session_auth_hash()
    if not getattr(user, "is_active", True) or not hmac.compare_digest(
        str(pending.get("hash", "")), current_hash
    ):
        clear_pending(request)
        return None, None
    return user, pending


def complete_login(request, user, device, backend: str) -> None:
    """Sign in for real: ``login()`` (which rotates the session key), then the proof."""
    from django.contrib.auth import login

    clear_pending(request)
    login(request, user, backend=backend)
    mark_verified(request, device)


# ── the gate ────────────────────────────────────────────────────────────────

#: Routes that never hold data and that the second-step pages themselves load.
OPEN_ROUTES = frozenset({"jsi18n", "ui_theme_css", "theme"})


def _allows(callback, name: str | None) -> frozenset[str]:
    if name in OPEN_ROUTES:
        return frozenset({VERIFY, ENROL})
    view_class = getattr(callback, "view_class", None)
    return frozenset(getattr(view_class, "two_factor_allows", ()) or ())


def refusal(request, needed: str) -> HttpResponse:
    """Where to send a request the gate stopped.

    A page load is redirected to the step it owes, with the page as ``next``. Anything
    else — a form post, an autocomplete ``fetch``, an API call — gets a 403 it can
    show, because a redirect to an HTML form is not something a script can follow.
    """
    match = getattr(request, "resolver_match", None)
    namespace = getattr(match, "namespace", None)
    name = "two_factor_verify" if needed == VERIFY else "two_factor"
    url_name = getattr(match, "url_name", "") or ""
    message = (
        gettext("Finish signing in with your authentication code first.")
        if needed == VERIFY
        else gettext("Set up two-factor sign-in on your account page first.")
    )
    if url_name == "autocomplete" or url_name.startswith("api-"):
        response = JsonResponse({"error": message, "two_factor": needed}, status=403)
    elif request.method in ("GET", "HEAD"):
        try:
            target = reverse(f"vantage:{name}", current_app=namespace)
        except NoReverseMatch:  # pragma: no cover - the site builds both routes
            return HttpResponse(message, status=403, content_type="text/plain; charset=utf-8")
        response = HttpResponseRedirect(f"{target}?{urlencode({'next': request.get_full_path()})}")
    else:
        response = HttpResponse(message, status=403, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    return response


def gated(view, name: str | None = None):
    """``view``, refusing first if the session owes the gate something it does not allow."""
    allowed = _allows(view, name)

    @wraps(view)
    def guarded(request, *args, **kwargs):
        needed = requirement(request)
        if needed is not None and needed not in allowed:
            return refusal(request, needed)
        return view(request, *args, **kwargs)

    guarded.two_factor_gated = True
    return guarded


def guard_patterns(patterns: list) -> list:
    """Put every route a site builds behind :func:`gated`, in place.

    ``functools.wraps`` carries the view's attributes across — ``view_class``,
    ``csrf_exempt`` — so the login throttle, CSRF and anything else reading them
    sees exactly what it saw before.

    An ``include()`` is followed all the way down, so a site, a plugin or a subclass
    that groups its routes under one is gated route by route like everything else.
    Skipping it would have been the opposite of failing closed: a whole subtree open
    because of how its ``urls.py`` happened to be arranged.
    """
    for pattern in patterns:
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            guard_patterns(nested)
            continue
        callback = getattr(pattern, "callback", None)
        if callback is None or getattr(callback, "two_factor_gated", False):
            continue
        pattern.callback = gated(callback, getattr(pattern, "name", None))
    return patterns


# ── django.contrib.admin, if asked ──────────────────────────────────────────


def admin_site_of(view):
    """The ``django.contrib.admin`` site ``view`` is a page of, or ``None``.

    ``AdminSite.get_urls`` marks its own views with ``admin_site`` and
    ``ModelAdmin.get_urls`` marks theirs with ``model_admin``; both have done so for
    as long as the admin has had class-based sites. Its sign-in view is not marked,
    which is right: it is the one page a signed-out person has to reach.
    """
    site = getattr(view, "admin_site", None)
    if site is None:
        site = getattr(getattr(view, "model_admin", None), "admin_site", None)
    if site is None:
        return None
    from django.contrib.admin import AdminSite

    return site if isinstance(site, AdminSite) else None


class AdminTwoFactorMiddleware:
    """Hold ``django.contrib.admin`` to the panel's two-factor requirement.

    Opt-in, after ``AuthenticationMiddleware``. Without it, a session signed in at
    ``/admin/login/`` has the password and nothing else, and Django's admin — whose
    sign-in this package does not own — lets it in. With it, every admin page asks the
    same question the panel's gate asks, of the same person, and a session that owes a
    code (or an enrolment) is sent to the panel's page for it, with the admin page as
    ``next``; posts and the admin's autocomplete get a 403. Signing out stays open.

    ``process_view`` rather than ``__call__``, so it sees the resolved view and can
    recognise the admin by what its views are marked with rather than by where it
    happens to be mounted.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        site = admin_site_of(view_func)
        if site is None:
            return None
        if getattr(view_func, "__wrapped__", None) in (site.logout, site.i18n_javascript):
            return None
        needed = requirement(request)
        return None if needed is None else refusal(request, needed)
