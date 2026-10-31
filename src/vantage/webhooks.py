"""Telling another system that a record changed, without letting it hurt this one.

A webhook is a POST to a URL somebody typed into a form. Everything in this module
exists because of that sentence: the URL is untrusted input that the *server* will
connect to, the receiver is outside our control and will be slow, down or hostile, and
the thing being sent is a description of the project's data.

**Events come from the audit trail's call sites.** :func:`notify` is called from
:func:`vantage.audit.record`, the one function every create, edit and delete made in the
panel already passes through, so a form save, an inline edit, an API delete and a move
on a board all fire the same hook with no second list of call sites to keep in step. The
consequence is the same scope the audit log has — *what someone did through this panel*,
not what happened to a row — and a webhook fires even when ``AUDIT_LOG`` is off. A
change made by ``shell``, a signal or an import job is not seen, and ``docs/webhooks.md``
says so.

**Nothing leaves the request until the request has committed.** :func:`notify` writes
one :class:`~vantage.models.WebhookDelivery` row per matching hook and hands each to the
job backend :mod:`vantage.jobs` detects, from ``on_commit``: a receiver that called back
before the write was visible would read the old record, and a transaction that rolled
back would have announced something that never happened. With no queue configured the
delivery runs in-process after commit, with a short timeout, and that is a documented
fallback rather than the design — the request pays for the receiver's latency.

**The URL is checked when it is used, not when it is saved.** A host name that resolved
to a public address at save time can resolve to ``169.254.169.254`` tomorrow, so the
check runs at send time, against the addresses the connection will actually use, and the
socket is opened to *that* address rather than to the name — resolving twice would let
the answer change between the check and the connect. Loopback, private, link-local,
shared, multicast and reserved ranges are refused unless
``VANTAGE["WEBHOOKS_ALLOW_PRIVATE_NETWORKS"]`` is true. Redirects are never followed (a
public URL that answers ``302 -> http://169.254.169.254/`` would otherwise walk straight
round the check), proxies from the environment are ignored for the same reason, the
response is read up to a small cap, and every attempt has a wall-clock deadline as well as
a timeout on each socket operation.

**Payloads carry no secret.** Sensitive fields, by the answer :mod:`vantage.sensitive`
gives every other surface, are dropped from ``changes`` altogether — not masked, not
listed as "changed" — and the record's label is the same ``safe_str`` the audit log uses.
Whoever receives the hook may be a third party.

**Retries are the job backend's, and the database is the safety net.** A failed attempt
is rescheduled with exponential backoff where the backend can delay a task (Celery, RQ
with a scheduler, a ``django.tasks`` backend that supports it). Where it cannot, and for
the in-process fallback, the row is left ``retrying`` with a ``next_attempt_at`` and
``manage.py vantage_webhooks`` — from cron — picks it up. A delivery nobody ever managed
to enqueue is found the same way.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from vantage.models import VantageLogEntry, Webhook, WebhookDelivery

__all__ = [
    "EVENTS",
    "MAX_RESPONSE_CHARS",
    "UnsafeTarget",
    "backoff_seconds",
    "build_payload",
    "deliver",
    "enabled",
    "enqueue",
    "may_manage",
    "new_secret",
    "notify",
    "redeliver",
    "retry_due",
    "run_delivery",
    "send_test",
    "sign",
    "sweep",
    "url_problem",
]

logger = logging.getLogger("vantage.webhooks")

#: The events a hook can subscribe to. ``test`` is sent by the "Send test" button and is
#: never part of a subscription.
EVENTS: tuple[tuple[str, Any], ...] = (
    ("created", _("Created")),
    ("updated", _("Changed")),
    ("deleted", _("Deleted")),
)

#: Which audit action is which event. A trashed row is *deleted* as far as a receiver is
#: concerned, and says so with ``soft_delete``; a restore is not an event, because the
#: receiver was never told the row was gone for good.
_ACTION_EVENTS = {
    VantageLogEntry.Action.CREATE: "created",
    VantageLogEntry.Action.UPDATE: "updated",
    VantageLogEntry.Action.DELETE: "deleted",
    VantageLogEntry.Action.TRASH: "deleted",
}

#: Longest slice of the receiver's answer that is kept. The log answers "what did they
#: say", and a 2 MB error page is not an answer.
MAX_RESPONSE_CHARS = 2000

#: Bytes read from a response before giving up on the rest.
_MAX_RESPONSE_BYTES = 8192

#: Ceiling for the in-process fallback, whatever ``WEBHOOKS_TIMEOUT`` says: the request
#: is waiting.
INLINE_TIMEOUT = 3.0

#: Wall-clock budget for *all* the in-process deliveries one save triggers. Hooks run one
#: after another in the request, so the sum is what the person waits for; whatever does
#: not fit is left ``pending`` for ``vantage_webhooks`` rather than sent inline.
INLINE_BUDGET = 3.0

#: A queued attempt may take this many ``WEBHOOKS_TIMEOUT``s in total (resolve, connect,
#: send, headers and body each get one), so a receiver that answers a byte at a time
#: cannot hold a worker for ever.
_DEADLINE_FACTOR = 3

#: The longest a retry is ever pushed back.
MAX_BACKOFF_SECONDS = 24 * 3600

#: A pending delivery older than this was never picked up, and :func:`retry_due` takes it.
STALE_PENDING_SECONDS = 120

#: Statuses that are not terminal.
_OPEN = (WebhookDelivery.Status.PENDING, WebhookDelivery.Status.RETRYING)


class UnsafeTarget(Exception):
    """The URL points somewhere a webhook may not go. Permanent: never retried."""


class _Retryable(Exception):
    """Something that may work next time: a timeout, a refused connection, a 5xx."""


# ── settings ────────────────────────────────────────────────────────────────


def _setting(name: str):
    from vantage.conf import vantage_settings

    return getattr(vantage_settings, name)


def enabled() -> bool:
    return bool(_setting("WEBHOOKS"))


def allow_private() -> bool:
    return bool(_setting("WEBHOOKS_ALLOW_PRIVATE_NETWORKS"))


def max_attempts() -> int:
    return max(int(_setting("WEBHOOKS_MAX_ATTEMPTS")), 1)


def backoff_seconds(attempts: int) -> int:
    """How long to wait after ``attempts`` failed attempts: 30s, 60s, 120s, ... a day at most."""
    base = float(_setting("WEBHOOKS_BACKOFF_SECONDS"))
    return int(min(base * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS))


def may_manage(request) -> bool:
    """Superusers, or anyone holding ``vantage.manage_webhooks``.

    A hook can send every change to every model to a URL of the manager's choosing, so
    the permission is its own rather than implied by editing some model.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not user.is_active:
        return False
    return bool(user.is_superuser) or user.has_perm("vantage.manage_webhooks")


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ── firing ──────────────────────────────────────────────────────────────────


def notify(request, action: str, *, obj, changes: dict | None) -> None:
    """Queue a delivery for every hook that wants this change. Never raises.

    Called by :func:`vantage.audit.record` with the same arguments it logs. A broken
    webhook table must not break the save it was going to announce, for the reason
    ``record`` gives about the audit table.
    """
    try:
        _notify(request, action, obj, changes or {})
    except Exception:  # deliberately broad: see the docstring
        logger.exception("Could not queue webhooks for %r.", action)


def _notify(request, action: str, obj, changes: dict) -> None:
    event = _ACTION_EVENTS.get(action)
    if event is None or obj is None or getattr(obj, "pk", None) is None or not enabled():
        return
    key = obj._meta.concrete_model._meta.label_lower
    # The package's own tables are not the project's data, and a hook on them would be
    # told about its own deliveries.
    if key.startswith("vantage."):
        return
    hooks = [h for h in Webhook.objects.filter(active=True) if h.subscribes_to(key, event)]
    if not hooks:
        return
    payload = build_payload(
        request, event, obj, changes, soft=action == VantageLogEntry.Action.TRASH
    )
    budget = _Budget(INLINE_BUDGET)  # shared: one save, however many hooks
    for hook in hooks:
        delivery = _create_delivery(hook, payload, event, key, str(obj.pk))
        enqueue(delivery.pk, budget=budget)


def build_payload(request, event: str, obj, changes: dict, *, soft: bool = False) -> dict:
    """The JSON body, minus anything :mod:`vantage.sensitive` says is a secret."""
    from vantage.safety import real_user
    from vantage.sensitive import safe_str, sensitive_names

    actor = real_user(request) if request is not None else None
    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    hidden = sensitive_names(obj)
    payload = {
        "id": str(uuid4()),
        "event": event,
        "model": obj._meta.concrete_model._meta.label_lower,
        "object_id": str(obj.pk),
        "object_repr": safe_str(obj),
        "actor": None if actor is None else {"id": actor.pk, "username": actor.get_username()},
        "timestamp": timezone.now().isoformat(),
        "changes": {
            name: change
            for name, change in changes.items()
            if name not in hidden and change != {"redacted": True}
        },
    }
    if soft:
        payload["soft_delete"] = True
    return payload


def _create_delivery(hook, payload, event, resource_key, object_id, **extra) -> WebhookDelivery:
    return WebhookDelivery.objects.create(
        webhook=hook,
        event=event,
        resource_key=resource_key,
        object_id=object_id[:255],
        payload=payload,
        **extra,
    )


# ── queueing ────────────────────────────────────────────────────────────────


def enqueue(delivery_id: int, *, delay: float = 0, budget: _Budget | None = None) -> None:
    """Hand a delivery to the job backend once the open transaction commits.

    ``on_commit`` is a no-op wrapper outside a transaction, so this is immediate in a
    plain request and deferred under ``ATOMIC_REQUESTS``. The callback swallows its own
    errors: Django would otherwise raise them into the request that has already
    succeeded. A delivery that could not be queued stays ``pending`` and
    :func:`retry_due` finds it. ``budget`` is shared by the hooks of one save and only
    matters when the delivery has to run in-process.
    """
    transaction.on_commit(lambda: _submit(delivery_id, delay, budget))


def _submit(delivery_id: int, delay: float, budget: _Budget | None = None) -> None:
    from vantage import jobs

    try:
        backend = jobs.detect_backend()
        if not backend.deferred:
            # A retry cannot sleep inside a request. It waits for `vantage_webhooks`.
            if not delay:
                left = INLINE_TIMEOUT if budget is None else budget.left()
                # Out of time: the row stays `pending` and the command takes it.
                if left > 0:
                    deliver(delivery_id, inline=True, budget=left)
            return
        queue = jobs.setting("JOB_QUEUE", "default")
        if backend.name == jobs.DJANGO_TASKS.name:
            from django.tasks import task

            job = task(queue_name=queue)(run_delivery)
            if delay:
                job = job.using(run_after=timezone.now() + dt.timedelta(seconds=delay))
            job.enqueue(delivery_id)
        elif backend.name == jobs.CELERY.name and _celery_delivery is not None:
            _celery_delivery.apply_async(args=[delivery_id], countdown=delay or None)
        elif backend.name == jobs.RQ.name:
            import django_rq

            if delay:
                django_rq.get_scheduler(queue).enqueue_in(
                    dt.timedelta(seconds=delay), run_delivery, delivery_id
                )
            else:
                django_rq.get_queue(queue).enqueue(run_delivery, delivery_id)
    except Exception:
        logger.exception("Could not queue webhook delivery %s.", delivery_id)


def run_delivery(delivery_id: int) -> None:
    """The task a worker runs. Module-level so every backend can import it by name."""
    deliver(delivery_id)


# ── delivering ──────────────────────────────────────────────────────────────


def deliver(
    delivery_id: int,
    *,
    retry: bool = True,
    inline: bool = False,
    budget: float | None = None,
) -> WebhookDelivery | None:
    """Make one attempt at a delivery and record the outcome. Returns the fresh row.

    The attempt is *claimed* with a conditional update on the attempt counter, so two
    workers handed the same delivery (a scheduled task and a cron sweep, say) cannot both
    send it. ``retry=False`` is for the test button, where one answer is the point.
    ``budget`` is the seconds an inline attempt may use in total: what is left of the
    save's :data:`INLINE_BUDGET`.
    """
    delivery = WebhookDelivery.objects.select_related("webhook").filter(pk=delivery_id).first()
    if delivery is None or delivery.is_terminal:
        return delivery
    now = timezone.now()
    if delivery.next_attempt_at and delivery.next_attempt_at > now and retry:
        # A queue that ignored the delay. The sweep will come back at the right time.
        return delivery
    claimed = WebhookDelivery.objects.filter(
        pk=delivery.pk, status__in=_OPEN, attempts=delivery.attempts
    ).update(attempts=F("attempts") + 1, last_attempt_at=now)
    if not claimed:
        return delivery
    delivery.refresh_from_db()
    hook = delivery.webhook
    if not hook.active and delivery.event != "test":
        return _finish(
            delivery, WebhookDelivery.Status.FAILED, error=str(_("Webhook is disabled."))
        )
    if not enabled():
        return _finish(
            delivery, WebhookDelivery.Status.FAILED, error=str(_("Webhooks are switched off."))
        )

    timeout = float(_setting("WEBHOOKS_TIMEOUT"))
    total = timeout * _DEADLINE_FACTOR
    if inline:
        timeout = min(timeout, INLINE_TIMEOUT)
        total = timeout if budget is None else min(timeout, budget)
    started = time.monotonic()
    code: int | None = None
    body = ""
    error = ""
    permanent = False
    try:
        code, body = _post(hook.url, *_request_for(delivery), timeout=timeout, total=total)
        if not 200 <= code < 300:
            error = f"HTTP {code}"
            permanent = code < 500 and code not in (408, 425, 429)
    except UnsafeTarget as exc:
        error, permanent = str(exc), True
    except _Retryable as exc:
        error = str(exc)
    except Exception as exc:  # a malformed URL, a codec error: never worth a retry loop
        logger.exception("Webhook delivery %s failed unexpectedly.", delivery.pk)
        error, permanent = f"{type(exc).__name__}: {exc}", True
    elapsed = int((time.monotonic() - started) * 1000)

    fields = {
        "response_code": code,
        "response_body": body[:MAX_RESPONSE_CHARS],
        "duration_ms": elapsed,
        "last_attempt_at": timezone.now(),
    }
    if not error:
        return _finish(delivery, WebhookDelivery.Status.SUCCEEDED, **fields)
    if permanent or not retry or delivery.attempts >= max_attempts():
        return _finish(delivery, WebhookDelivery.Status.FAILED, error=error, **fields)
    wait = backoff_seconds(delivery.attempts)
    delivery = _finish(
        delivery,
        WebhookDelivery.Status.RETRYING,
        error=error,
        next_attempt_at=timezone.now() + dt.timedelta(seconds=wait),
        **fields,
    )
    enqueue(delivery.pk, delay=wait)
    return delivery


def _finish(
    delivery: WebhookDelivery, status: str, *, error: str = "", **fields
) -> WebhookDelivery:
    fields.setdefault("next_attempt_at", None)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=status, error=error[:300], **fields
    )
    delivery.refresh_from_db()
    if status == WebhookDelivery.Status.FAILED and delivery.event != "test":
        from vantage.notifications import notify

        # The hook's name and the error only: the payload is record data.
        notify(
            "webhook.failed",
            title=f"Webhook delivery failed: {delivery.webhook.name} (#{delivery.pk})",
            lines=[f"{delivery.event} after {delivery.attempts} attempt(s): {error[:200]}"],
        )
    return delivery


def _request_for(delivery: WebhookDelivery) -> tuple[bytes, dict[str, str]]:
    """The exact bytes and headers for this attempt. The timestamp is fresh every time,
    so a retry is signed afresh rather than replaying an expired signature."""
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    stamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "django-vantage-webhooks",
        "X-Vantage-Event": delivery.event,
        "X-Vantage-Delivery": str(delivery.uuid),
        "X-Vantage-Timestamp": stamp,
    }
    if delivery.webhook.secret:
        headers["X-Vantage-Signature"] = sign(delivery.webhook.secret, stamp, body)
    return body, headers


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=<hex>`` over ``"<timestamp>.<body>"``.

    The timestamp is inside the signed string so a receiver can reject an old capture
    being replayed, which a signature over the body alone cannot tell them.
    """
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


# ── the test button, redelivery, and the sweep ──────────────────────────────


def send_test(request, hook: Webhook) -> WebhookDelivery:
    """Send a ``test`` event now, in this request, once. The answer is the point."""
    from vantage.safety import real_user

    actor = real_user(request)
    payload = {
        "id": str(uuid4()),
        "event": "test",
        "model": "",
        "object_id": "",
        "object_repr": "",
        "actor": None if actor is None else {"id": actor.pk, "username": actor.get_username()},
        "timestamp": timezone.now().isoformat(),
        "changes": {},
    }
    delivery = _create_delivery(hook, payload, "test", "", "")
    return deliver(delivery.pk, retry=False, inline=True) or delivery


def redeliver(delivery: WebhookDelivery) -> WebhookDelivery:
    """A fresh delivery carrying the original body, queued like any other."""
    copy = _create_delivery(
        delivery.webhook,
        delivery.payload,
        delivery.event,
        delivery.resource_key,
        delivery.object_id,
        redelivery_of=delivery,
    )
    enqueue(copy.pk)
    return copy


def retry_due(*, limit: int = 200) -> int:
    """Attempt every delivery whose time has come. Returns how many were tried.

    The retry path for backends that cannot delay a task, and the net under the ones that
    can: a delivery whose queued task was lost is still ``retrying`` in the table.
    """
    now = timezone.now()
    stale = now - dt.timedelta(seconds=STALE_PENDING_SECONDS)
    ids = list(
        WebhookDelivery.objects.filter(status__in=_OPEN)
        .filter(
            Q(next_attempt_at__lte=now)
            | Q(
                next_attempt_at__isnull=True,
                status=WebhookDelivery.Status.PENDING,
                created_at__lt=stale,
            )
        )
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    for pk in ids:
        deliver(pk, inline=True)
    return len(ids)


def sweep(*, days: int | None = None) -> int:
    """Delete finished deliveries past the retention window. Returns how many went."""
    window = days or float(_setting("WEBHOOKS_RETENTION_DAYS"))
    horizon = timezone.now() - dt.timedelta(days=window)
    deleted, _per_model = WebhookDelivery.objects.filter(
        status__in=WebhookDelivery.TERMINAL, created_at__lt=horizon
    ).delete()
    return deleted


# ── the safe HTTP client ────────────────────────────────────────────────────


def url_problem(url: str) -> str | None:
    """Why ``url`` cannot be a webhook target, or ``None``. Syntax and literal addresses
    only: a host *name* is judged when it is connected to, because that is when it is
    known what it points at."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return str(_("The address must start with http:// or https://."))
    if not parts.hostname:
        return str(_("The address has no host name."))
    if parts.username or parts.password:
        return str(_("Put credentials in the secret, not in the address."))
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return None
    if not allow_private() and _blocked(literal):
        return str(_("That address is on a private or local network, which webhooks may not call."))
    return None


#: Ranges refused whole, because nothing legitimate lives there or because the address
#: only means something once translated: IPv4-compatible (``::/96``), SIIT
#: (``::ffff:0:0:0/96``), deprecated site-local (``fec0::/10``) and NAT64 local-use.
_REFUSED_V6 = tuple(
    ipaddress.ip_network(n) for n in ("::/96", "::ffff:0:0:0/96", "fec0::/10", "64:ff9b:1::/48")
)


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Anything that is not an ordinary public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if any(ip in net for net in _REFUSED_V6):
            return True
        raw = ip.packed
        embedded = None
        if ip.ipv4_mapped is not None:
            embedded = ip.ipv4_mapped
        elif raw[:12] == bytes.fromhex("0064ff9b0000000000000000"):  # NAT64
            embedded = ipaddress.IPv4Address(raw[12:])
        elif raw[:2] == b"\x20\x02":  # 6to4
            embedded = ipaddress.IPv4Address(raw[2:6])
        if embedded is not None:
            return _blocked(embedded)
    return not ip.is_global or ip.is_multicast


class _Deadline:
    """One wall-clock limit over a whole attempt.

    Socket timeouts bound each operation, not the sum: a receiver that sends a byte every
    few seconds never trips them. When the limit passes, a timer shuts the watched sockets
    down, which wakes whatever is blocked on them — the TLS handshake, a send, the header
    read, the body read — and :func:`_post` reports the attempt as timed out.
    """

    def __init__(self, seconds: float):
        self.at = time.monotonic() + seconds
        self.expired = False
        self._socks: list[socket.socket] = []
        self._timer: threading.Timer | None = None

    def remaining(self) -> float:
        return self.at - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            self.expired = True
            raise _Retryable(str(_("The receiver took too long.")))

    def watch(self, sock: socket.socket) -> None:
        self._socks.append(sock)
        if self._timer is None:
            self._timer = threading.Timer(max(self.remaining(), 0), self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self) -> None:
        self.expired = True
        for sock in self._socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:  # already closed, or wrapped into a TLS socket
                pass

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


class _Budget:
    """The in-process time one save has for all its hooks. The clock starts at the first
    send, not at creation: ``on_commit`` can run long after the hooks were queued."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._end: float | None = None

    def left(self) -> float:
        if self._end is None:
            self._end = time.monotonic() + self.seconds
        return self._end - time.monotonic()


def _resolve(host: str, port: int, deadline: _Deadline | None = None) -> list[str]:
    """Every address ``host`` resolves to, all of which must be allowed.

    All, not any: a name with one public and one private record is refused outright,
    because which one the connection uses is not ours to bet on.
    """
    try:
        infos = _getaddrinfo(host, port, deadline)
    except socket.gaierror as exc:
        raise _Retryable(f"Could not resolve {host}: {exc.strerror or exc}") from exc
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not allow_private():
        for address in addresses:
            if _blocked(ipaddress.ip_address(address.split("%")[0])):
                raise UnsafeTarget(
                    str(
                        _(
                            "%(host)s resolves to a private or local address, which webhooks "
                            "may not call."
                        )
                    )
                    % {"host": host}
                )
    return addresses


def _getaddrinfo(host: str, port: int, deadline: _Deadline | None):
    """``getaddrinfo`` has no timeout of its own, so with a deadline it runs in a helper
    thread that is abandoned (it is a daemon, and ends when the resolver gives up)."""
    if deadline is None:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    deadline.check()
    outcome: dict[str, Any] = {}

    def lookup():
        try:
            outcome["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except BaseException as exc:  # handed to the caller, which re-raises it
            outcome["error"] = exc

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(max(deadline.remaining(), 0))
    if worker.is_alive():
        deadline.expired = True
        raise _Retryable(f"Timed out resolving {host}.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["infos"]


def _connect(host: str, port: int, timeout: float, deadline: _Deadline | None = None):
    """A connected socket to one of the *checked* addresses, never to the name."""
    last: OSError | None = None
    for address in _resolve(host, port, deadline):
        if deadline is not None:
            deadline.check()
            timeout = min(timeout, deadline.remaining())
        try:
            sock = socket.create_connection((address, port), timeout)
        except OSError as exc:
            last = exc
            continue
        if deadline is not None:
            deadline.watch(sock)
        return sock
    raise _Retryable(f"Could not connect to {host}: {last}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, deadline: _Deadline | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self):
        self.sock = _connect(self.host, self.port, self.timeout, self.deadline)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, deadline: _Deadline | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = deadline

    def connect(self):
        sock = _connect(self.host, self.port, self.timeout, self.deadline)
        # Certificate and host name are verified against the *name*, as usual. The
        # handshake is done by hand so the deadline watches the wrapped socket first.
        wrapped = self._context.wrap_socket(
            sock, server_hostname=self.host, do_handshake_on_connect=False
        )
        self.sock = wrapped
        if self.deadline is not None:
            self.deadline.watch(wrapped)
        wrapped.do_handshake()


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline: _Deadline | None = None):
        super().__init__()
        self.deadline = deadline

    def http_open(self, req):
        return self.do_open(partial(_GuardedHTTPConnection, deadline=self.deadline), req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline | None = None):
        super().__init__()
        self.deadline = deadline

    def https_open(self, req):
        return self.do_open(partial(_GuardedHTTPSConnection, deadline=self.deadline), req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not an instruction: the 3xx is recorded as the response."""

    def redirect_request(self, *args, **kwargs):
        return None


def _post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout: float,
    total: float | None = None,
) -> tuple[int, str]:
    """POST ``body`` and return ``(status, truncated response text)``.

    The one place that touches the network, which is why the tests replace it. Raises
    :class:`UnsafeTarget` for a refused address and :class:`_Retryable` for anything
    that might work later. ``timeout`` bounds each socket operation; ``total`` (default:
    ``timeout``) bounds the whole attempt, name lookup to last byte.
    """
    problem = url_problem(url)
    if problem:
        raise UnsafeTarget(problem)
    deadline = _Deadline(timeout if total is None else total)
    try:
        result = _exchange(url, body, headers, timeout, deadline)
    except UnsafeTarget:
        raise
    except Exception:
        if deadline.expired:
            raise _Retryable(str(_("The receiver took too long."))) from None
        raise
    finally:
        deadline.close()
    if deadline.expired:  # the answer was cut short by the shutdown
        raise _Retryable(str(_("The receiver took too long.")))
    return result


def _exchange(
    url: str, body: bytes, headers: dict[str, str], timeout: float, deadline: _Deadline
) -> tuple[int, str]:
    # `ProxyHandler({})` ignores HTTP(S)_PROXY: the address checked above is the one
    # connected to, and a proxy would be connected to instead.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect,
        _GuardedHTTPHandler(deadline),
        _GuardedHTTPSHandler(deadline),
    )
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _read(response)
    except urllib.error.HTTPError as exc:  # any non-2xx, redirects included
        with exc:
            return exc.code, _read(exc)
    except (UnsafeTarget, _Retryable):
        raise
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, UnsafeTarget | _Retryable):
            raise reason from exc
        raise _Retryable(str(reason)) from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise _Retryable(f"{type(exc).__name__}: {exc}") from exc


def _read(response) -> str:
    return response.read(_MAX_RESPONSE_BYTES).decode("utf-8", "replace")


# ── Celery ──────────────────────────────────────────────────────────────────

#: Bound at import for the reason :mod:`vantage.jobs` gives: a remote worker learns a
#: task exists by importing the module that declares it.
_celery_delivery = None

try:  # pragma: no cover - exercised only on a project with Celery installed
    from celery import shared_task as _shared_task
except ImportError:  # pragma: no cover
    _shared_task = None

if _shared_task is not None:  # pragma: no cover - only with Celery installed
    _celery_delivery = _shared_task(name="vantage.webhooks.run_delivery")(run_delivery)
