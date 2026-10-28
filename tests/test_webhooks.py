"""Webhooks: what is sent, to whom, when, and what is never sent.

No test here touches the network. ``vantage.webhooks._post`` is the one function that
opens a socket, so the delivery tests replace it; the SSRF tests replace name resolution
instead and assert the socket is never opened; and the two tests that exercise the real
client talk to a throwaway server on loopback, which is the only thing they can reach.

Writes made through the panel announce themselves from ``on_commit``, and a pytest-django
test never commits, so the tests that fire a hook through a view run the callbacks
themselves.
"""

import datetime as dt
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.contrib.auth.models import Permission, User
from django.core import checks
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from tests.testapp.models import Author, ZCredential
from vantage import jobs, webhooks
from vantage.models import VantageLogEntry, Webhook, WebhookDelivery

LIST = "vantage:page-webhooks"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


@pytest.fixture
def hook(db):
    return Webhook.objects.create(
        name="Orders",
        url="https://hooks.example.com/in",
        secret="s3cret",
        events=["created", "updated", "deleted"],
    )


class Receiver:
    """Stands in for ``webhooks._post``: records the call, answers as told."""

    def __init__(self, *answers):
        self.answers = list(answers) or [(200, "ok")]
        self.calls = []

    def __call__(self, url, body, headers, *, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def receiver(monkeypatch):
    fake = Receiver()
    monkeypatch.setattr(webhooks, "_post", fake)
    return fake


@pytest.fixture
def queue(monkeypatch):
    """A pretend task backend that remembers what was handed to it."""
    handed = []
    monkeypatch.setattr(
        jobs,
        "detect_backend",
        lambda: jobs.Backend("rq", "RQ", True),
    )
    monkeypatch.setattr(
        webhooks, "_submit", lambda delivery_id, delay: handed.append((delivery_id, delay))
    )
    return handed


def add_author(client, **overrides):
    data = {"name": "Ada", "email": "ada@example.com", "bio": "", "website": "", "is_active": "on"}
    data.update(overrides)
    return client.post(reverse("vantage:testapp-author-add"), data)


# ── what fires, and what it says ────────────────────────────────────────────


class TestFiring:
    def test_a_create_through_the_panel_queues_one_signed_delivery(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            add_author(boss)

        delivery = WebhookDelivery.objects.get()
        assert delivery.status == WebhookDelivery.Status.SUCCEEDED
        body = json.loads(receiver.calls[0]["body"])
        author = Author.objects.get()
        assert body["event"] == "created"
        assert body["model"] == "testapp.author"
        assert body["object_id"] == str(author.pk)
        assert body["object_repr"] == "Ada"
        assert body["actor"]["username"] == "boss"
        assert body["changes"]["name"] == {"from": None, "to": "Ada"}
        assert body["timestamp"]

    def test_an_edit_carries_only_the_fields_that_moved(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        author = Author.objects.create(name="Ada", email="ada@example.com")
        url = reverse("vantage:testapp-author-change", args=[author.pk])

        with django_capture_on_commit_callbacks(execute=True):
            boss.post(
                url,
                {
                    "name": "Ada L",
                    "email": "ada@example.com",
                    "bio": "",
                    "website": "",
                    "is_active": "on",
                },
            )

        body = json.loads(receiver.calls[0]["body"])
        assert body["event"] == "updated"
        assert body["changes"] == {"name": {"from": "Ada", "to": "Ada L"}}

    def test_a_delete_is_announced_with_the_fields_it_had(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        author = Author.objects.create(name="Ada", email="ada@example.com")

        with django_capture_on_commit_callbacks(execute=True):
            boss.post(reverse("vantage:testapp-author-delete", args=[author.pk]))

        body = json.loads(receiver.calls[0]["body"])
        assert body["event"] == "deleted"
        assert body["changes"]["name"] == {"from": "Ada"}

    def test_nothing_is_sent_until_the_transaction_commits(self, boss, hook, receiver):
        add_author(boss)

        assert WebhookDelivery.objects.get().status == WebhookDelivery.Status.PENDING
        assert receiver.calls == []

    def test_a_hook_only_hears_about_what_it_subscribed_to(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        hook.resources = ["testapp.post"]
        hook.save()
        other = Webhook.objects.create(
            name="Deletes", url="https://hooks.example.com/d", events=["deleted"]
        )

        with django_capture_on_commit_callbacks(execute=True):
            add_author(boss)

        assert not WebhookDelivery.objects.exists()
        assert other.subscribes_to("testapp.author", "deleted")
        assert not other.subscribes_to("testapp.author", "created")

    def test_a_paused_hook_is_silent(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        hook.active = False
        hook.save()

        with django_capture_on_commit_callbacks(execute=True):
            add_author(boss)

        assert not WebhookDelivery.objects.exists()

    def test_the_setting_switches_every_hook_off(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        with override_settings(VANTAGE={"WEBHOOKS": False}):
            with django_capture_on_commit_callbacks(execute=True):
                add_author(boss)
            assert not WebhookDelivery.objects.exists()
            assert boss.get(reverse("vantage:dashboard")).status_code == 200

    def test_it_fires_with_the_audit_log_off(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        with override_settings(VANTAGE={"AUDIT_LOG": False}):
            with django_capture_on_commit_callbacks(execute=True):
                add_author(boss)

        assert not VantageLogEntry.objects.exists()
        assert WebhookDelivery.objects.get().status == WebhookDelivery.Status.SUCCEEDED

    def test_a_broken_webhook_table_cannot_break_the_save(self, boss, monkeypatch):
        monkeypatch.setattr(webhooks, "_notify", lambda *a: 1 / 0)

        assert add_author(boss).status_code == 302
        assert Author.objects.count() == 1

    def test_the_packages_own_tables_do_not_announce_themselves(self, hook, receiver):
        webhooks.notify(None, VantageLogEntry.Action.CREATE, obj=hook, changes={})

        assert not WebhookDelivery.objects.exists()


# ── secrets ─────────────────────────────────────────────────────────────────


class TestSensitiveFields:
    def test_a_sensitive_field_never_reaches_the_payload(self, boss, hook, receiver):
        credential = ZCredential.objects.create(name="Prod", api_token="sk-live-9999", pin="4321")
        changes = {
            "name": {"from": "Old", "to": "Prod"},
            "api_token": {"from": "sk-live-0000", "to": "sk-live-9999"},
            "pin": {"redacted": True},
            "card_number": {"from": "4111111111111111"},
        }

        webhooks.notify(None, VantageLogEntry.Action.UPDATE, obj=credential, changes=changes)

        sent = WebhookDelivery.objects.get().payload
        assert sent["changes"] == {"name": {"from": "Old", "to": "Prod"}}
        text = json.dumps(sent)
        for secret in ("sk-live-9999", "sk-live-0000", "4111111111111111", "api_token", "pin"):
            assert secret not in text

    def test_a_deleted_users_password_hash_is_not_sent(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        victim = User.objects.create_user("victim", password="correct-horse")
        stored_hash = victim.password

        with django_capture_on_commit_callbacks(execute=True):
            boss.post(reverse("vantage:auth-user-delete", args=[victim.pk]))

        body = receiver.calls[0]["body"].decode()
        assert "victim" in body
        assert stored_hash not in body
        assert "password" not in json.loads(body)["changes"]


# ── signing ─────────────────────────────────────────────────────────────────


class TestSigning:
    def test_the_signature_is_an_hmac_over_the_timestamp_and_the_exact_body(self, hook, receiver):
        delivery = webhooks.send_test(_request(), hook)

        call = receiver.calls[0]
        stamp = call["headers"]["X-Vantage-Timestamp"]
        expected = hmac.new(
            b"s3cret", stamp.encode() + b"." + call["body"], hashlib.sha256
        ).hexdigest()
        assert call["headers"]["X-Vantage-Signature"] == f"sha256={expected}"
        assert call["headers"]["X-Vantage-Event"] == "test"
        assert call["headers"]["X-Vantage-Delivery"] == str(delivery.uuid)
        assert call["headers"]["Content-Type"] == "application/json"

    def test_the_secret_is_never_in_the_request(self, hook, receiver):
        webhooks.send_test(_request(), hook)

        call = receiver.calls[0]
        assert "s3cret" not in call["body"].decode()
        assert all("s3cret" not in value for value in call["headers"].values())


def _request():
    from django.http import HttpRequest

    request = HttpRequest()
    request.user = User.objects.get_or_create(username="boss")[0]
    request.session = {}
    return request


# ── retries ─────────────────────────────────────────────────────────────────


class TestRetries:
    def make(self, hook):
        return WebhookDelivery.objects.create(webhook=hook, event="updated", payload={"id": "e1"})

    def test_a_server_error_is_retried_with_a_growing_wait(
        self, hook, queue, monkeypatch, django_capture_on_commit_callbacks
    ):
        delivery = self.make(hook)
        waits = []
        for answer in [(500, "boom")] * 3:
            monkeypatch.setattr(webhooks, "_post", Receiver(answer))
            delivery.refresh_from_db()
            delivery.next_attempt_at = None
            delivery.save()
            before = timezone.now()
            with django_capture_on_commit_callbacks(execute=True):
                webhooks.deliver(delivery.pk)
            delivery.refresh_from_db()
            waits.append(round((delivery.next_attempt_at - before).total_seconds()))

        assert delivery.status == WebhookDelivery.Status.RETRYING
        assert delivery.attempts == 3
        assert waits == [30, 60, 120]
        assert [delay for _id, delay in queue] == [30, 60, 120]

    def test_it_gives_up_after_the_maximum_attempts(self, hook, receiver, queue):
        receiver.answers = [(503, "down")]
        delivery = self.make(hook)

        with override_settings(VANTAGE={"WEBHOOKS_MAX_ATTEMPTS": 2}):
            webhooks.deliver(delivery.pk)
            WebhookDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=None)
            webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert delivery.attempts == 2
        assert delivery.response_code == 503
        assert delivery.next_attempt_at is None

    def test_a_client_error_is_not_retried(self, hook, receiver, queue):
        receiver.answers = [(404, "no such hook")]
        delivery = self.make(hook)

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert delivery.response_body == "no such hook"
        assert queue == []

    def test_a_timeout_is_retried(self, hook, receiver, queue):
        receiver.answers = [webhooks._Retryable("timed out")]
        delivery = self.make(hook)

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.Status.RETRYING
        assert delivery.error == "timed out"

    def test_a_later_success_ends_the_chain(self, hook, queue, monkeypatch):
        delivery = self.make(hook)
        monkeypatch.setattr(webhooks, "_post", Receiver((500, "x"), (204, "")))

        webhooks.deliver(delivery.pk)
        WebhookDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=None)
        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.Status.SUCCEEDED
        assert delivery.attempts == 2
        assert delivery.error == ""

    def test_a_task_that_arrives_early_waits_for_its_time(self, hook, receiver, queue):
        delivery = self.make(hook)
        WebhookDelivery.objects.filter(pk=delivery.pk).update(
            status="retrying", next_attempt_at=timezone.now() + dt.timedelta(minutes=5)
        )

        webhooks.deliver(delivery.pk)

        assert receiver.calls == []

    def test_a_finished_delivery_is_not_sent_twice(self, hook, receiver):
        delivery = self.make(hook)

        webhooks.deliver(delivery.pk)
        webhooks.deliver(delivery.pk)

        assert len(receiver.calls) == 1

    def test_the_response_kept_is_truncated(self, hook, receiver):
        receiver.answers = [(200, "x" * 50_000)]
        delivery = self.make(hook)

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert len(delivery.response_body) == webhooks.MAX_RESPONSE_CHARS
        assert delivery.duration_ms is not None

    def test_the_command_sends_what_is_due_and_leaves_the_rest(self, hook, receiver):
        due = self.make(hook)
        later = self.make(hook)
        WebhookDelivery.objects.filter(pk=due.pk).update(
            status="retrying", attempts=1, next_attempt_at=timezone.now() - dt.timedelta(seconds=1)
        )
        WebhookDelivery.objects.filter(pk=later.pk).update(
            status="retrying", attempts=1, next_attempt_at=timezone.now() + dt.timedelta(hours=1)
        )
        # Freshly created and still pending: its own queue task is on the way.
        fresh = self.make(hook)

        call_command("vantage_webhooks")

        statuses = {d.pk: d.status for d in WebhookDelivery.objects.all()}
        assert statuses[due.pk] == "succeeded"
        assert statuses[later.pk] == "retrying"
        assert statuses[fresh.pk] == "pending"

    def test_a_delivery_that_was_never_queued_is_found_by_the_command(self, hook, receiver):
        delivery = self.make(hook)
        WebhookDelivery.objects.filter(pk=delivery.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=1)
        )

        call_command("vantage_webhooks")

        delivery.refresh_from_db()
        assert delivery.status == "succeeded"

    def test_old_finished_deliveries_are_swept(self, hook):
        old = WebhookDelivery.objects.create(webhook=hook, event="updated", status="succeeded")
        stuck = WebhookDelivery.objects.create(webhook=hook, event="updated", status="retrying")
        WebhookDelivery.objects.update(created_at=timezone.now() - dt.timedelta(days=90))

        call_command("vantage_webhooks")

        assert not WebhookDelivery.objects.filter(pk=old.pk).exists()
        assert WebhookDelivery.objects.filter(pk=stuck.pk).exists()


# ── where the work runs ─────────────────────────────────────────────────────


class TestOffTheRequest:
    def test_with_a_task_backend_the_request_only_queues(
        self, boss, hook, receiver, queue, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            add_author(boss)

        delivery = WebhookDelivery.objects.get()
        assert queue == [(delivery.pk, 0)]
        assert receiver.calls == []

    def test_with_none_it_runs_in_process_with_a_short_timeout(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        with override_settings(VANTAGE={"WEBHOOKS_TIMEOUT": 30}):
            with django_capture_on_commit_callbacks(execute=True):
                add_author(boss)

        assert receiver.calls[0]["timeout"] == webhooks.INLINE_TIMEOUT

    def test_a_worker_uses_the_configured_timeout(self, hook, receiver):
        delivery = WebhookDelivery.objects.create(webhook=hook, event="updated")

        with override_settings(VANTAGE={"WEBHOOKS_TIMEOUT": 7}):
            webhooks.run_delivery(delivery.pk)

        assert receiver.calls[0]["timeout"] == 7

    def test_an_in_process_failure_waits_for_the_command_instead_of_sleeping(
        self, boss, hook, monkeypatch, django_capture_on_commit_callbacks
    ):
        fake = Receiver((500, "x"))
        monkeypatch.setattr(webhooks, "_post", fake)
        with django_capture_on_commit_callbacks(execute=True):
            add_author(boss)

        delivery = WebhookDelivery.objects.get()
        assert delivery.status == "retrying"
        assert delivery.next_attempt_at > timezone.now()
        assert len(fake.calls) == 1

    def test_a_queue_that_is_down_does_not_fail_the_save(
        self, boss, hook, monkeypatch, django_capture_on_commit_callbacks
    ):
        monkeypatch.setattr(jobs, "detect_backend", lambda: 1 / 0)

        with django_capture_on_commit_callbacks(execute=True):
            assert add_author(boss).status_code == 302

        assert WebhookDelivery.objects.get().status == "pending"


# ── the address ─────────────────────────────────────────────────────────────


def resolving_to(monkeypatch, *addresses):
    import socket

    opened = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses
        ],
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **kw: opened.append(a) or (_ for _ in ()).throw(OSError("closed")),
    )
    return opened


class TestAddressSafety:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.9",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "64:ff9b::7f00:1",
            "2002:7f00:1::1",
        ],
    )
    def test_a_name_that_resolves_to_a_private_address_is_refused_before_connecting(
        self, address, monkeypatch
    ):
        opened = resolving_to(monkeypatch, address)

        with pytest.raises(webhooks.UnsafeTarget):
            webhooks._post("http://innocent.example.com/h", b"{}", {}, timeout=1)

        assert opened == []

    def test_one_private_record_among_public_ones_is_enough_to_refuse(self, monkeypatch):
        opened = resolving_to(monkeypatch, "93.184.216.34", "10.0.0.5")

        with pytest.raises(webhooks.UnsafeTarget):
            webhooks._post("http://mixed.example.com/h", b"{}", {}, timeout=1)

        assert opened == []

    def test_the_connection_goes_to_the_address_that_was_checked(self, monkeypatch):
        opened = resolving_to(monkeypatch, "93.184.216.34")

        with pytest.raises(webhooks._Retryable):
            webhooks._post("http://public.example.com:8080/h", b"{}", {}, timeout=2)

        assert opened[0][0] == ("93.184.216.34", 8080)

    def test_a_literal_private_address_is_refused_without_resolving(self, monkeypatch):
        opened = resolving_to(monkeypatch)

        with pytest.raises(webhooks.UnsafeTarget):
            webhooks._post("http://169.254.169.254/latest/meta-data/", b"{}", {}, timeout=1)

        assert opened == []

    def test_the_refusal_is_recorded_and_never_retried(self, hook, monkeypatch):
        resolving_to(monkeypatch, "10.0.0.5")
        delivery = WebhookDelivery.objects.create(webhook=hook, event="updated")

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == WebhookDelivery.Status.FAILED
        assert "private" in delivery.error
        assert delivery.attempts == 1

    def test_the_setting_allows_a_receiver_on_your_own_network(self, monkeypatch):
        opened = resolving_to(monkeypatch, "10.0.0.5")

        with override_settings(VANTAGE={"WEBHOOKS_ALLOW_PRIVATE_NETWORKS": True}):
            with pytest.raises(webhooks._Retryable):
                webhooks._post("http://intranet.example.com/h", b"{}", {}, timeout=1)

        assert opened[0][0] == ("10.0.0.5", 80)

    @pytest.mark.parametrize(
        "url",
        ["ftp://example.com/x", "file:///etc/passwd", "http://", "http://user:pw@example.com/x"],
    )
    def test_only_plain_http_urls_without_credentials_are_accepted(self, url):
        assert webhooks.url_problem(url)

    def test_a_public_url_is_fine(self):
        assert webhooks.url_problem("https://hooks.example.com/in") is None


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Handler.seen.append((self.path, self.headers, self.rfile.read(length)))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"thanks")

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    """A real server on loopback: the only place the real client can be pointed."""
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler.seen
    server.shutdown()
    server.server_close()


class TestRealClient:
    @pytest.fixture(autouse=True)
    def _loopback_allowed(self, settings):
        settings.VANTAGE = {"WEBHOOKS_ALLOW_PRIVATE_NETWORKS": True}

    def test_it_posts_the_body_and_reads_the_answer(self, local_server):
        base, seen = local_server

        code, text = webhooks._post(
            f"{base}/in", b'{"a":1}', {"X-Vantage-Event": "test"}, timeout=2
        )

        assert (code, text) == (200, "thanks")
        path, headers, body = seen[0]
        assert (path, body) == ("/in", b'{"a":1}')
        assert headers["X-Vantage-Event"] == "test"

    def test_a_redirect_is_reported_and_not_followed(self, local_server):
        base, seen = local_server

        code, _text = webhooks._post(f"{base}/redirect", b"{}", {}, timeout=2)

        assert code == 302
        assert [path for path, *_ in seen] == ["/redirect"]

    def test_a_redirect_is_a_failed_delivery_that_is_not_retried(self, local_server, hook):
        base, _seen = local_server
        Webhook.objects.filter(pk=hook.pk).update(url=f"{base}/redirect")
        delivery = WebhookDelivery.objects.create(webhook=hook, event="updated")

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert (delivery.status, delivery.response_code) == ("failed", 302)

    def test_a_refused_connection_is_a_retryable_failure(self, hook):
        Webhook.objects.filter(pk=hook.pk).update(url="http://127.0.0.1:1/h")
        delivery = WebhookDelivery.objects.create(webhook=hook, event="updated")

        webhooks.deliver(delivery.pk)

        delivery.refresh_from_db()
        assert delivery.status == "retrying"
        assert delivery.error


# ── the page ────────────────────────────────────────────────────────────────


class TestPage:
    def form(self, **overrides):
        data = {
            "name": "CRM",
            "url": "https://hooks.example.com/crm",
            "resources": ["testapp.author"],
            "events": ["created", "updated"],
            "active": "on",
        }
        data.update(overrides)
        return data

    def test_a_superuser_sees_the_page_in_the_nav(self, boss):
        response = boss.get(reverse(LIST))

        assert response.status_code == 200
        assert reverse(LIST).encode() in boss.get(reverse("vantage:dashboard")).content

    def test_ordinary_staff_are_refused(self, client, db):
        client.force_login(User.objects.create_user("clerk", password="x", is_staff=True))

        assert client.get(reverse(LIST)).status_code == 403
        assert client.post(reverse("vantage:webhook-create"), self.form()).status_code == 403
        assert reverse(LIST).encode() not in client.get(reverse("vantage:dashboard")).content

    def test_the_dedicated_permission_is_enough(self, client, db):
        user = User.objects.create_user("ops", password="x", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="manage_webhooks"))
        client.force_login(user)

        assert client.get(reverse(LIST)).status_code == 200
        assert client.post(reverse("vantage:webhook-create"), self.form()).status_code == 302

    def test_the_setting_removes_the_page(self, boss):
        with override_settings(VANTAGE={"WEBHOOKS": False}):
            assert boss.get(reverse(LIST)).status_code == 403

    def test_creating_a_hook_shows_its_secret_exactly_once(self, boss):
        response = boss.post(reverse("vantage:webhook-create"), self.form(), follow=True)

        hook = Webhook.objects.get()
        assert hook.secret
        assert hook.created_by.username == "boss"
        assert hook.resources == ["testapp.author"]
        assert hook.secret.encode() in response.content
        assert hook.secret.encode() not in boss.get(reverse(LIST)).content
        assert (
            hook.secret.encode()
            not in boss.get(reverse("vantage:webhook-edit", args=[hook.pk])).content
        )

    def test_editing_keeps_the_secret_unless_a_new_one_is_asked_for(self, boss, hook):
        url = reverse("vantage:webhook-edit", args=[hook.pk])

        boss.post(url, self.form(name="Renamed"))
        hook.refresh_from_db()
        assert (hook.name, hook.secret) == ("Renamed", "s3cret")

        boss.post(url, self.form(rotate_secret="on"))
        hook.refresh_from_db()
        assert hook.secret != "s3cret"

    def test_a_bad_address_is_refused_by_the_form(self, boss):
        for url in ("ftp://example.com/x", "http://169.254.169.254/x", "not a url"):
            response = boss.post(reverse("vantage:webhook-create"), self.form(url=url))
            assert response.status_code == 200
            assert not Webhook.objects.exists()

    def test_at_least_one_event_is_required(self, boss):
        response = boss.post(reverse("vantage:webhook-create"), self.form(events=[]))

        assert response.status_code == 200
        assert not Webhook.objects.exists()

    def test_changes_to_hooks_are_audited_without_the_secret(self, boss):
        boss.post(reverse("vantage:webhook-create"), self.form())

        entry = VantageLogEntry.objects.get(action=VantageLogEntry.Action.CREATE)
        assert entry.object_repr == "CRM"
        assert Webhook.objects.get().secret not in json.dumps(entry.changes)

    def test_deleting_a_hook_removes_its_log(self, boss, hook):
        WebhookDelivery.objects.create(webhook=hook, event="updated")
        url = reverse("vantage:webhook-delete", args=[hook.pk])

        assert boss.get(url).status_code == 200
        boss.post(url)

        assert not Webhook.objects.exists()
        assert not WebhookDelivery.objects.exists()

    def test_read_only_mode_blocks_changes_but_not_reading(self, boss, hook):
        with override_settings(VANTAGE={"READ_ONLY": True}):
            assert boss.get(reverse(LIST)).status_code == 200
            assert boss.post(reverse("vantage:webhook-delete", args=[hook.pk])).status_code == 403
            assert boss.post(reverse("vantage:webhook-create"), self.form()).status_code == 403

        assert Webhook.objects.count() == 1

    def test_send_test_reports_what_the_receiver_said(self, boss, hook, receiver):
        response = boss.post(reverse("vantage:webhook-test", args=[hook.pk]))

        delivery = WebhookDelivery.objects.get()
        assert response["Location"] == reverse("vantage:webhook-delivery", args=[delivery.pk])
        assert (delivery.event, delivery.status, delivery.response_code) == (
            "test",
            "succeeded",
            200,
        )

    def test_a_failed_test_is_not_retried(self, boss, hook, receiver, queue):
        receiver.answers = [(500, "no")]

        boss.post(reverse("vantage:webhook-test", args=[hook.pk]))

        delivery = WebhookDelivery.objects.get()
        assert delivery.status == "failed"
        assert queue == []

    def test_a_paused_hook_can_still_be_tested(self, boss, hook, receiver):
        hook.active = False
        hook.save()

        boss.post(reverse("vantage:webhook-test", args=[hook.pk]))

        assert WebhookDelivery.objects.get().status == "succeeded"

    def test_redeliver_queues_a_copy_of_the_original_body(
        self, boss, hook, receiver, django_capture_on_commit_callbacks
    ):
        original = WebhookDelivery.objects.create(
            webhook=hook,
            event="updated",
            status="failed",
            payload={"id": "evt-1", "event": "updated"},
        )

        with django_capture_on_commit_callbacks(execute=True):
            boss.post(reverse("vantage:webhook-redeliver", args=[original.pk]))

        copy = WebhookDelivery.objects.exclude(pk=original.pk).get()
        assert copy.redelivery_of_id == original.pk
        assert copy.uuid != original.uuid
        assert copy.status == "succeeded"
        assert json.loads(receiver.calls[0]["body"]) == {"id": "evt-1", "event": "updated"}
        original.refresh_from_db()
        assert original.status == "failed"

    def test_the_log_filters_by_hook_and_status(self, boss, hook):
        other = Webhook.objects.create(name="Other", url="https://hooks.example.com/o")
        WebhookDelivery.objects.create(
            webhook=hook, event="updated", status="failed", error="HTTP 500"
        )
        WebhookDelivery.objects.create(webhook=other, event="created", status="succeeded")

        response = boss.get(reverse(LIST), {"webhook": hook.pk, "status": "failed"})

        rows = list(response.context["page_obj"])
        assert [d.webhook for d in rows] == [hook]

    def test_the_delivery_page_shows_the_body_and_the_response(self, boss, hook):
        delivery = WebhookDelivery.objects.create(
            webhook=hook,
            event="updated",
            status="failed",
            payload={"object_id": "7"},
            response_code=500,
            response_body="upstream exploded",
        )

        response = boss.get(reverse("vantage:webhook-delivery", args=[delivery.pk]))

        assert b"upstream exploded" in response.content
        assert b"&quot;object_id&quot;: &quot;7&quot;" in response.content

    def test_the_package_tables_get_no_generic_panel(self, boss):
        from vantage.registry import NotRegistered
        from vantage.sites import site

        for model in (Webhook, WebhookDelivery):
            with pytest.raises(NotRegistered):
                site.get_panel(model)


# ── system checks ───────────────────────────────────────────────────────────


class TestChecks:
    def run(self, **values):
        with override_settings(VANTAGE=values):
            from vantage.checks import check_vantage_settings

            return [e.id for e in check_vantage_settings(None)]

    def test_sensible_values_pass(self):
        assert self.run(WEBHOOKS_TIMEOUT=10, WEBHOOKS_MAX_ATTEMPTS=3, WEBHOOKS=True) == []

    def test_a_non_boolean_switch_is_e100(self):
        assert self.run(WEBHOOKS_ALLOW_PRIVATE_NETWORKS="yes") == ["vantage.E100"]

    @pytest.mark.parametrize("value", [0, -5, "5", None, True])
    def test_a_bad_limit_is_e101(self, value):
        assert self.run(WEBHOOKS_TIMEOUT=value) == ["vantage.E101"]

    def test_the_new_settings_are_recognised(self):
        errors = checks.run_checks(tags=["vantage"])
        assert not [e for e in errors if e.id == "vantage.E002"]
