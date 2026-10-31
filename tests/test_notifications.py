"""Slack, Teams and email notifications (``VANTAGE["NOTIFICATIONS"]``)."""

# ruff: noqa: F811 - the approval fixtures are imported and requested by name.

import json

import pytest
from django.core import mail
from django.core.exceptions import ImproperlyConfigured

from tests.test_approvals import abe, ask, decide, order, panel, rita  # noqa: F401
from vantage import notifications, webhooks
from vantage.conf import vantage_settings

pytestmark = pytest.mark.django_db

SLACK = {"type": "slack", "url": "https://hooks.example.com/T/1", "events": ["approval.requested"]}


class Recorder:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, body, headers, *, timeout, total=None):
        self.calls.append((url, json.loads(body)))
        return self.status, "ok"


@pytest.fixture
def channels(settings):
    def configure(**config):
        settings.VANTAGE = {"NOTIFICATIONS": config}
        vantage_settings.reset()

    yield configure
    vantage_settings.reset()


@pytest.fixture
def posted(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(webhooks, "_post", recorder)
    return recorder


class TestApprovals:
    def test_a_request_posts_to_slack_without_values(
        self, client, rita, order, channels, posted, django_capture_on_commit_callbacks
    ):
        channels(ops=SLACK)
        with django_capture_on_commit_callbacks(execute=True):
            ask(client, rita, order, status="paid")
        ((url, payload),) = posted.calls
        assert url == SLACK["url"]
        assert "Approval requested" in payload["text"]
        assert "1 field(s)" in payload["text"]
        assert "paid" not in payload["text"]
        assert "/testapp-order/" in payload["text"] or "order" in payload["text"]

    def test_email_to_approvers(
        self, client, rita, abe, order, channels, django_capture_on_commit_callbacks
    ):
        channels(approvers={"type": "email", "to": "approvers", "events": ["approval.requested"]})
        with django_capture_on_commit_callbacks(execute=True):
            ask(client, rita, order, status="paid")
        (message,) = mail.outbox
        assert message.to == ["abe@example.com"]

    def test_a_decision_is_its_own_event(
        self, client, rita, abe, order, channels, posted, django_capture_on_commit_callbacks
    ):
        channels(ops={**SLACK, "type": "teams", "events": ["approval.rejected"]})
        change_request = ask(client, rita, order, status="paid")
        client.force_login(abe)
        with django_capture_on_commit_callbacks(execute=True):
            decide(client, change_request, "reject", reason="No")
        ((_, payload),) = posted.calls
        assert payload["text"].startswith("**Change rejected")

    def test_unsubscribed_events_send_nothing(
        self, client, rita, order, channels, posted, django_capture_on_commit_callbacks
    ):
        channels(ops={**SLACK, "events": ["job.failed"]})
        with django_capture_on_commit_callbacks(execute=True):
            ask(client, rita, order, status="paid")
        assert posted.calls == []


class TestDelivery:
    def test_a_failing_channel_never_raises(self, channels, monkeypatch, caplog):
        channels(ops={**SLACK, "events": ["job.failed"]})

        def boom(*args, **kwargs):
            raise OSError("down")

        monkeypatch.setattr(webhooks, "_post", boom)
        captured = []
        monkeypatch.setattr(
            "django.db.transaction.on_commit", lambda fn, *a, **k: captured.append(fn)
        )
        notifications.notify("job.failed", title="x")
        captured[0]()  # does not raise
        assert "failed for job.failed" in caplog.text

    def test_chat_posts_go_through_the_guarded_client(self, channels):
        """A private address is refused by the same checks webhooks use."""
        channels(ops={**SLACK, "url": "https://127.0.0.1/hook", "events": ["job.failed"]})
        with pytest.raises(webhooks.UnsafeTarget):
            notifications._send(vantage_settings.NOTIFICATIONS["ops"], "job.failed", "t", [], None)

    def test_approvers_are_skipped_with_tenancy(self, settings, caplog):
        from tests.testapp.models import Order

        settings.VANTAGE = {"TENANT_MODEL": "testapp.TntOrg", "TENANT_EXEMPT": ["testapp.Order"]}
        vantage_settings.reset()
        try:
            assert notifications._approver_addresses(Order) == []
        finally:
            vantage_settings.reset()


class TestValidation:
    @pytest.mark.parametrize(
        "value",
        [
            [],
            {"x": {"type": "pager", "events": ["job.failed"]}},
            {"x": {"type": "slack", "url": "http://insecure", "events": ["job.failed"]}},
            {"x": {**SLACK, "events": ["nope"]}},
            {"x": {**SLACK, "events": []}},
            {"x": {"type": "email", "to": "a@example.com", "events": ["job.failed"]}},
            {"x": {**SLACK, "extra": 1}},
        ],
    )
    def test_rejected(self, value):
        with pytest.raises(ImproperlyConfigured):
            notifications.resolve_channels(value)

    def test_accepted(self):
        resolved = notifications.resolve_channels({"ops": SLACK})
        assert resolved["ops"]["events"] == frozenset({"approval.requested"})
