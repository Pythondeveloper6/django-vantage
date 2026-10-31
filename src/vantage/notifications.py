"""Push notifications to Slack, Microsoft Teams and email for the events people miss.

An approval request nobody opens the panel to see is an approval that never happens;
a webhook that has stopped delivering is noticed when a downstream system is already
wrong. ``VANTAGE["NOTIFICATIONS"]`` names channels and the events each one wants::

    VANTAGE = {
        "NOTIFICATIONS": {
            "ops-slack": {
                "type": "slack",
                "url": env("SLACK_WEBHOOK_URL"),     # an incoming-webhook URL
                "events": ["webhook.failed", "job.failed"],
            },
            "approvers": {
                "type": "email",
                "to": "approvers",                   # everyone who may approve that model
                "events": ["approval.requested"],
            },
        },
    }

Events: ``approval.requested``, ``approval.approved``, ``approval.rejected``,
``approval.withdrawn``, ``webhook.failed`` (a delivery out of retries) and
``job.failed``.

What a message carries is deliberately thin: the event, the model, the record's safe
label, the *names* of the fields involved and a link. Never a value — a Slack channel is
not a place a masked or field-restricted value should turn up. Chat channels are posted
through the same guarded client as webhooks, so the address checks (no private
networks unless ``WEBHOOKS_ALLOW_PRIVATE_NETWORKS``), redirect refusal and timeouts all
apply. Delivery is best effort and never raises into the request that caused it; a
failure is logged to ``vantage.notifications``. Channel configuration is code-only:
the URLs are credentials.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

__all__ = ["EVENTS", "TYPES", "notify", "resolve_channels"]

logger = logging.getLogger("vantage.notifications")

EVENTS = frozenset(
    {
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.withdrawn",
        "webhook.failed",
        "job.failed",
    }
)
TYPES = frozenset({"slack", "teams", "email"})

#: Seconds one chat post may take. Short: it runs after a save, on the same worker.
TIMEOUT = 3.0


def resolve_channels(value: Any) -> dict[str, dict[str, Any]]:
    """Validate ``VANTAGE["NOTIFICATIONS"]``. Strict, like every other setting."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ImproperlyConfigured(
            f"VANTAGE['NOTIFICATIONS'] must be a dict of channels, got {type(value).__name__}."
        )
    channels: dict[str, dict[str, Any]] = {}
    for name, channel in value.items():
        where = f"VANTAGE['NOTIFICATIONS'][{name!r}]"
        if not isinstance(channel, dict):
            raise ImproperlyConfigured(f"{where} must be a dict.")
        unknown = set(channel) - {"type", "url", "to", "events"}
        if unknown:
            raise ImproperlyConfigured(f"{where} has unknown key(s): {', '.join(sorted(unknown))}.")
        kind = channel.get("type")
        if kind not in TYPES:
            raise ImproperlyConfigured(
                f"{where}['type'] must be one of {', '.join(sorted(TYPES))}."
            )
        events = channel.get("events")
        if not isinstance(events, list | tuple) or not events or set(events) - EVENTS:
            raise ImproperlyConfigured(
                f"{where}['events'] must be a non-empty list of: {', '.join(sorted(EVENTS))}."
            )
        if kind in ("slack", "teams"):
            url = channel.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ImproperlyConfigured(f"{where}['url'] must be an https:// webhook URL.")
        else:
            to = channel.get("to")
            valid = to == "approvers" or (
                isinstance(to, list | tuple) and to and all(isinstance(a, str) for a in to)
            )
            if not valid:
                raise ImproperlyConfigured(
                    f"{where}['to'] must be a list of addresses or \"approvers\"."
                )
        channels[str(name)] = {**channel, "events": frozenset(events)}
    return channels


def notify(
    event: str,
    *,
    title: str,
    lines: Iterable[str] = (),
    link: str | None = None,
    model=None,
) -> None:
    """Send ``event`` to every channel that asked for it, after the transaction commits.

    Never raises. ``model`` is needed only for ``"to": "approvers"`` email channels.
    """
    try:
        from vantage.conf import vantage_settings

        channels = [
            (name, channel)
            for name, channel in vantage_settings.NOTIFICATIONS.items()
            if event in channel["events"]
        ]
    except Exception:
        logger.exception("Could not read VANTAGE['NOTIFICATIONS'].")
        return
    if not channels:
        return
    body = [*lines, *([link] if link else [])]

    def send() -> None:
        for name, channel in channels:
            try:
                _send(channel, event, title, body, model)
            except Exception:
                logger.exception("Notification channel %r failed for %s.", name, event)

    transaction.on_commit(send)


def _send(channel: dict[str, Any], event: str, title: str, body: list[str], model) -> None:
    kind = channel["type"]
    if kind == "email":
        recipients = (
            _approver_addresses(model) if channel["to"] == "approvers" else list(channel["to"])
        )
        if not recipients:
            return
        from django.core.mail import send_mail

        # A record label is user data; a newline in it must not become a mail header.
        subject = " ".join(title.split())[:200]
        send_mail(subject, "\n".join(body) or subject, None, recipients, fail_silently=False)
        return
    text = "\n".join([f"*{title}*" if kind == "slack" else f"**{title}**", *body])
    payload = {"text": text}
    from vantage.webhooks import _post

    code, _response = _post(
        channel["url"],
        json.dumps(payload).encode(),
        {"Content-Type": "application/json", "User-Agent": "django-vantage-notifications"},
        timeout=TIMEOUT,
    )
    if not 200 <= code < 300:
        logger.warning("Notification channel answered HTTP %s for %s.", code, event)


def _approver_addresses(model) -> list[str]:
    """Active users holding ``<app>.approve_<model>``, directly or through a role."""
    if model is None:
        return []
    from vantage.conf import vantage_settings

    if vantage_settings.TENANT_MODEL:
        # Approvers are found across the whole user table; with tenancy on that would
        # mail one tenant's record label to another tenant's approvers. Refused rather
        # than guessed at: list the addresses explicitly instead.
        logger.warning('"to": "approvers" is not supported with TENANT_MODEL; skipped.')
        return []
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    opts = model._meta
    codename = f"approve_{opts.model_name}"
    perm = Q(
        user_permissions__codename=codename,
        user_permissions__content_type__app_label=opts.app_label,
    )
    role = Q(
        groups__permissions__codename=codename,
        groups__permissions__content_type__app_label=opts.app_label,
    )
    users = (
        get_user_model()
        .objects.filter(is_active=True)
        .filter(perm | role | Q(is_superuser=True))
        .exclude(email="")
        .distinct()
    )
    return sorted({user.email for user in users})
