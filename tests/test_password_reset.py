"""The opt-in password reset flow on the sign-in page.

This module is its own urlconf, like ``test_auth_pages``: the flow is reached from the
sign-in page and the panel's routes sit beside it, so both have to be mounted.

Most of these tests are about what the flow refuses to say or do: a different answer
for an address that has an account, a link that works twice, a link to a host the
request merely claimed, a reset that walks around two-factor sign-in.
"""

import re
import time
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import include, path, reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from vantage import passwordreset, site
from vantage.models import VantageLogEntry

from .test_two_factor import PASSWORD, code_for, enrol

urlpatterns = [
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
]

NEW_PASSWORD = "a-brand-new-passphrase-42"


@pytest.fixture(autouse=True)
def _mounted(settings):
    settings.ROOT_URLCONF = "tests.test_password_reset"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.DEFAULT_FROM_EMAIL = "panel@example.com"
    settings.VANTAGE = {"PASSWORD_RESET": True}
    cache.clear()
    mail.outbox.clear()
    yield
    cache.clear()


@pytest.fixture
def clerk(db):
    return User.objects.create_user("clerk", "clerk@example.com", PASSWORD, is_staff=True)


def ask(client, email, **extra):
    """Submit the request form and wait for the (background) email to be handed off."""
    response = client.post(reverse("vantage:password_reset"), {"email": email}, **extra)
    passwordreset.wait_for_mail()
    return response


def link_in(message) -> str:
    return re.search(r"https?://\S+", message.body).group(0)


def path_of(link: str) -> str:
    return re.sub(r"^https?://[^/]+", "", link)


def reset_link(client, email="clerk@example.com") -> str:
    ask(client, email)
    assert len(mail.outbox) == 1
    return link_in(mail.outbox[0])


def follow_link(client, link):
    """Open the emailed link and land on the token-free form, as a browser would."""
    return client.get(path_of(link), follow=True)


def set_password(client, link, password=NEW_PASSWORD):
    response = follow_link(client, link)
    assert response.status_code == 200
    form_url = response.redirect_chain[-1][0]
    return client.post(form_url, {"new_password1": password, "new_password2": password})


# ── off by default ──────────────────────────────────────────────────────────


class TestOffByDefault:
    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.VANTAGE = {}

    def test_every_route_is_a_404(self, client, db):
        uid = urlsafe_base64_encode(force_bytes(1))
        for url in (
            reverse("vantage:password_reset"),
            reverse("vantage:password_reset_done"),
            reverse("vantage:password_reset_confirm", args=[uid, "abc-def"]),
            reverse("vantage:password_reset_complete"),
        ):
            assert client.get(url).status_code == 404, url
        assert (
            client.post(reverse("vantage:password_reset"), {"email": "a@b.co"}).status_code == 404
        )

    def test_no_link_on_the_sign_in_page(self, client, db):
        body = client.get(reverse("login")).content.decode()
        assert "Forgot password" not in body
        assert "password-reset" not in body

    def test_nothing_is_sent_when_off(self, client, clerk):
        client.post(reverse("vantage:password_reset"), {"email": clerk.email})
        passwordreset.wait_for_mail()
        assert mail.outbox == []


class TestSignInLink:
    def test_the_link_is_shown_when_on(self, client, db):
        body = client.get(reverse("login")).content.decode()
        assert "Forgot password?" in body
        assert reverse("vantage:password_reset") in body


# ── the request form ────────────────────────────────────────────────────────


class TestRequestForm:
    def test_renders_with_hardening_headers(self, client, db):
        response = client.get(reverse("vantage:password_reset"))
        assert response.status_code == 200
        assert response["Referrer-Policy"] == "same-origin"
        assert "no-store" in response["Cache-Control"]
        body = response.content.decode()
        assert '<label for="id_email">' in body
        assert 'name="csrfmiddlewaretoken"' in body

    def test_same_answer_for_known_and_unknown_addresses(self, client, clerk):
        known = ask(client, clerk.email)
        unknown = ask(Client(), "nobody@example.com")
        assert known.status_code == unknown.status_code == 302
        assert known["Location"] == unknown["Location"] == reverse("vantage:password_reset_done")
        done = client.get(known["Location"]).content.decode()
        assert "if an account exists" in done.lower()

    def test_a_malformed_address_is_a_form_error_not_a_lookup(self, client, clerk):
        response = ask(client, "not-an-address")
        assert response.status_code == 200
        assert mail.outbox == []

    def test_email_is_sent_to_an_eligible_user(self, client, clerk):
        ask(client, clerk.email)
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == [clerk.email]
        assert message.from_email == "panel@example.com"
        assert path_of(link_in(message)).startswith(
            reverse("vantage:password_reset_confirm", args=["x", "y"]).rsplit("/", 3)[0]
        )

    def test_address_match_ignores_case_and_whitespace(self, client, clerk):
        ask(client, "  CLERK@Example.COM ")
        assert len(mail.outbox) == 1

    @pytest.mark.parametrize(
        "change",
        [
            {"is_active": False},
            {"is_staff": False},
            {"password": "!unusable"},
        ],
        ids=["inactive", "not-staff", "unusable-password"],
    )
    def test_nothing_is_sent_to_ineligible_users(self, client, clerk, change):
        for key, value in change.items():
            setattr(clerk, key, value)
        clerk.save()
        response = ask(client, clerk.email)
        assert response["Location"] == reverse("vantage:password_reset_done")
        assert mail.outbox == []

    def test_a_user_with_no_password_at_all_gets_nothing(self, client, clerk):
        clerk.set_unusable_password()
        clerk.save()
        ask(client, clerk.email)
        assert mail.outbox == []

    def test_the_email_is_off_the_request_path(self, client, clerk):
        """A slow mail server must not make an existing address slower to answer."""

        def slow(self, messages):
            time.sleep(0.6)
            return len(messages)

        with mock.patch("django.core.mail.backends.locmem.EmailBackend.send_messages", slow):
            started = time.monotonic()
            client.post(reverse("vantage:password_reset"), {"email": clerk.email})
            elapsed = time.monotonic() - started
            passwordreset.wait_for_mail()
        assert elapsed < 0.5

    def test_a_mail_failure_does_not_change_the_answer(self, client, clerk):
        with mock.patch(
            "django.core.mail.backends.locmem.EmailBackend.send_messages",
            side_effect=OSError("smtp down"),
        ):
            response = ask(client, clerk.email)
        assert response["Location"] == reverse("vantage:password_reset_done")

    def test_the_subject_template_is_overridable_and_clean(self, client, clerk):
        ask(client, clerk.email)
        subject = mail.outbox[0].subject
        assert "\n" not in subject
        assert subject.strip()


# ── the link ────────────────────────────────────────────────────────────────


class TestLinkHost:
    def test_link_uses_the_request_host(self, client, clerk):
        assert reset_link(client).startswith("http://testserver/")

    def test_a_spoofed_host_cannot_produce_a_link(self, client, clerk):
        """ALLOWED_HOSTS is the boundary, and an unknown address behind it must not
        answer differently from a known one."""
        known = client.post(
            reverse("vantage:password_reset"), {"email": clerk.email}, HTTP_HOST="evil.example"
        )
        unknown = client.post(
            reverse("vantage:password_reset"),
            {"email": "ghost@example.com"},
            HTTP_HOST="evil.example",
        )
        passwordreset.wait_for_mail()
        assert known.status_code == unknown.status_code == 400
        assert mail.outbox == []

    def test_x_forwarded_host_is_ignored_unless_django_trusts_it(self, client, clerk):
        ask(client, clerk.email, HTTP_X_FORWARDED_HOST="evil.example")
        assert "evil.example" not in mail.outbox[0].body
        assert "testserver" in link_in(mail.outbox[0])

    def test_an_explicit_base_url_wins(self, client, clerk, settings):
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "PASSWORD_RESET_BASE_URL": "https://panel.example.com/",
        }
        link = reset_link(client)
        assert link.startswith("https://panel.example.com/admin/")
        assert "//admin" not in link.replace("https://", "")

    def test_an_explicit_base_url_beats_a_wildcard_host(self, client, clerk, settings):
        settings.ALLOWED_HOSTS = ["*"]
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "PASSWORD_RESET_BASE_URL": "https://panel.example.com",
        }
        ask(client, clerk.email, HTTP_HOST="evil.example")
        assert link_in(mail.outbox[0]).startswith("https://panel.example.com/")
        assert "evil.example" not in mail.outbox[0].body


# ── the token ───────────────────────────────────────────────────────────────


class TestToken:
    def test_the_link_sets_a_password_once(self, client, clerk):
        link = reset_link(client)
        done = set_password(client, link)
        assert done.status_code == 302
        assert done["Location"] == reverse("vantage:password_reset_complete")
        clerk.refresh_from_db()
        assert clerk.check_password(NEW_PASSWORD)

        again = follow_link(Client(), link)
        assert b"no longer valid" in again.content
        assert again.status_code == 200

    def test_the_token_leaves_the_address_bar(self, client, clerk):
        link = reset_link(client)
        response = client.get(path_of(link))
        assert response.status_code == 302
        assert "set-password" in response["Location"]
        assert link.rsplit("/", 2)[-2] not in response["Location"]
        assert response["Referrer-Policy"] == "no-referrer"
        assert "no-store" in response["Cache-Control"]

    def test_the_form_page_carries_the_hardening_headers(self, client, clerk):
        response = follow_link(client, reset_link(client))
        assert response["Referrer-Policy"] == "same-origin"
        assert "no-store" in response["Cache-Control"]
        assert 'name="referrer" content="same-origin"' in response.content.decode()

    def test_wrong_token_and_wrong_uid_are_refused(self, client, clerk):
        link = reset_link(client)
        parts = path_of(link).rstrip("/").split("/")
        uid, token = parts[-2], parts[-1]
        bad_token = path_of(link).replace(token, token[:-1] + ("a" if token[-1] != "a" else "b"))
        other = User.objects.create_user("other", "o@example.com", PASSWORD, is_staff=True)
        bad_uid = path_of(link).replace(uid, urlsafe_base64_encode(force_bytes(other.pk)))
        for url in (bad_token, bad_uid, path_of(link).replace(uid, "!!!")):
            response = client.get(url, follow=True)
            assert b"no longer valid" in response.content, url

    def test_a_token_from_djangos_own_generator_is_not_accepted(self, client, clerk):
        uid = urlsafe_base64_encode(force_bytes(clerk.pk))
        token = default_token_generator.make_token(clerk)
        url = reverse("vantage:password_reset_confirm", args=[uid, token])
        assert b"no longer valid" in client.get(url, follow=True).content

    def test_the_session_token_alone_does_nothing_for_another_account(self, client, clerk):
        link = reset_link(client)
        client.get(path_of(link))
        other = User.objects.create_user("other", "o@example.com", PASSWORD, is_staff=True)
        url = reverse(
            "vantage:password_reset_confirm",
            args=[urlsafe_base64_encode(force_bytes(other.pk)), "set-password"],
        )
        response = client.post(url, {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        assert response.status_code == 200
        other.refresh_from_db()
        assert not other.check_password(NEW_PASSWORD)

    def test_the_link_expires_on_its_own_timeout(self, client, clerk, settings):
        settings.PASSWORD_RESET_TIMEOUT = 10**9  # Django's global value must not decide
        settings.VANTAGE = {"PASSWORD_RESET": True, "PASSWORD_RESET_TIMEOUT_MINUTES": 5}
        link = reset_link(client)
        future = time.time() + 6 * 60
        with mock.patch.object(passwordreset.ResetTokenGenerator, "_now") as now:
            from datetime import datetime

            now.return_value = datetime.fromtimestamp(future)
            response = client.get(path_of(link), follow=True)
        assert b"no longer valid" in response.content

    def test_the_link_is_valid_inside_its_timeout(self, client, clerk, settings):
        settings.PASSWORD_RESET_TIMEOUT = 1  # Django's global value must not decide
        link = reset_link(client)
        assert b"New password" in follow_link(client, link).content

    def test_changing_the_password_elsewhere_voids_the_link(self, client, clerk):
        link = reset_link(client)
        clerk.set_password("changed-by-someone-else-9")
        clerk.save()
        assert b"no longer valid" in follow_link(client, link).content

    def test_signing_in_voids_the_link(self, client, clerk):
        link = reset_link(client)
        Client().post(reverse("login"), {"username": "clerk", "password": PASSWORD})
        assert b"no longer valid" in follow_link(Client(), link).content

    def test_a_deactivated_user_cannot_use_an_earlier_link(self, client, clerk):
        link = reset_link(client)
        clerk.is_active = False
        clerk.save()
        assert b"no longer valid" in follow_link(client, link).content

    def test_a_post_to_the_real_token_url_changes_nothing(self, client, clerk):
        link = reset_link(client)
        response = client.post(
            path_of(link), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )
        clerk.refresh_from_db()
        assert response.status_code == 302
        assert not clerk.check_password(NEW_PASSWORD)


# ── choosing the password ───────────────────────────────────────────────────


class TestNewPassword:
    def test_validators_are_enforced(self, client, clerk, settings):
        settings.AUTH_PASSWORD_VALIDATORS = [
            {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
            {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
        ]
        link = reset_link(client)
        response = set_password(client, link, "short")
        assert response.status_code == 200
        assert "too short" in response.content.decode()
        clerk.refresh_from_db()
        assert clerk.check_password(PASSWORD)
        # The token is still usable after a rejected attempt.
        assert set_password(client, link, NEW_PASSWORD).status_code == 302

    def test_mismatched_passwords_are_rejected(self, client, clerk):
        link = reset_link(client)
        response = follow_link(client, link)
        form_url = response.redirect_chain[-1][0]
        bad = client.post(form_url, {"new_password1": NEW_PASSWORD, "new_password2": "different"})
        assert bad.status_code == 200
        clerk.refresh_from_db()
        assert clerk.check_password(PASSWORD)

    def test_the_form_fields_are_labelled(self, client, clerk):
        body = follow_link(client, reset_link(client)).content.decode()
        assert '<label for="id_new_password1">' in body
        assert '<label for="id_new_password2">' in body
        assert 'autocomplete="new-password"' in body

    def test_the_reset_does_not_sign_anyone_in(self, client, clerk):
        set_password(client, reset_link(client))
        assert "_auth_user_id" not in client.session
        assert client.get("/admin/").status_code == 302

    def test_the_new_password_works_at_the_sign_in_page(self, client, clerk):
        set_password(client, reset_link(client))
        fresh = Client()
        assert (
            fresh.post(reverse("login"), {"username": "clerk", "password": PASSWORD}).status_code
            == 200
        )
        ok = fresh.post(reverse("login"), {"username": "clerk", "password": NEW_PASSWORD})
        assert ok.status_code == 302

    def test_the_complete_page_points_back_to_sign_in(self, client, clerk):
        set_password(client, reset_link(client))
        response = client.get(reverse("vantage:password_reset_complete"))
        assert response.status_code == 200
        assert reverse("login") in response.content.decode()
        assert response["Referrer-Policy"] == "same-origin"

    def test_other_sessions_are_signed_out(self, client, clerk):
        elsewhere = Client()
        elsewhere.force_login(clerk)
        assert elsewhere.get("/admin/").status_code == 200
        set_password(client, reset_link(client))
        assert elsewhere.get("/admin/").status_code == 302


# ── two-factor ──────────────────────────────────────────────────────────────


class TestTwoFactor:
    def test_a_reset_does_not_bypass_two_factor(self, client, clerk):
        device, _ = enrol(clerk)
        set_password(client, reset_link(client))

        fresh = Client()
        response = fresh.post(reverse("login"), {"username": "clerk", "password": NEW_PASSWORD})
        assert response["Location"] == reverse("login_two_factor")
        assert "_auth_user_id" not in fresh.session
        assert fresh.get("/admin/").status_code == 302

        ok = fresh.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert ok.status_code == 302
        assert "_auth_user_id" in fresh.session

    def test_the_authenticator_is_left_in_place(self, client, clerk):
        from vantage.models import TwoFactorDevice

        enrol(clerk)
        set_password(client, reset_link(client))
        assert TwoFactorDevice.objects.filter(user=clerk, confirmed_at__isnull=False).exists()

    def test_a_half_finished_sign_in_is_dropped_by_the_reset(self, client, clerk):
        device, _ = enrol(clerk)
        half = Client()
        half.post(reverse("login"), {"username": "clerk", "password": PASSWORD})
        set_password(client, reset_link(client))
        response = half.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert "_auth_user_id" not in half.session
        assert response.status_code == 302
        assert response["Location"] == reverse("login")


# ── rate limits ─────────────────────────────────────────────────────────────


class TestRateLimits:
    def test_per_address_limit_silently_stops_the_email(self, client, clerk, settings):
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "RATE_LIMITS": {"password_reset_address": (2, 3600)},
        }
        for _ in range(5):
            response = ask(client, clerk.email)
            assert response["Location"] == reverse("vantage:password_reset_done")
        assert len(mail.outbox) == 2

    def test_the_address_budget_is_not_per_ip(self, clerk, settings):
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "RATE_LIMITS": {"password_reset_address": (1, 3600)},
        }
        for n in range(4):
            ask(Client(REMOTE_ADDR=f"10.0.0.{n + 1}"), clerk.email)
        assert len(mail.outbox) == 1

    def test_per_ip_limit_covers_many_addresses(self, db, settings):
        for n in range(6):
            User.objects.create_user(f"u{n}", f"u{n}@example.com", PASSWORD, is_staff=True)
        settings.VANTAGE = {"PASSWORD_RESET": True, "RATE_LIMITS": {"password_reset": (3, 3600)}}
        client = Client(REMOTE_ADDR="192.0.2.7")
        for n in range(6):
            ask(client, f"u{n}@example.com")
        assert len(mail.outbox) == 3
        # Another address is not affected by that one's budget.
        ask(Client(REMOTE_ADDR="192.0.2.8"), "u5@example.com")
        assert len(mail.outbox) == 4

    def test_limited_and_unlimited_requests_look_the_same(self, client, clerk, settings):
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "RATE_LIMITS": {"password_reset_address": (1, 3600)},
        }
        first = ask(client, clerk.email)
        second = ask(client, clerk.email)
        assert (first.status_code, first["Location"]) == (second.status_code, second["Location"])

    def test_the_defaults_exist(self):
        from vantage import security

        for surface in ("password_reset", "password_reset_address", "password_reset_confirm"):
            assert surface in security.RATE_LIMITS

    def test_guessing_tokens_is_throttled(self, client, clerk, settings):
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "RATE_LIMITS": {"password_reset_confirm": (3, 3600)},
        }
        uid = urlsafe_base64_encode(force_bytes(clerk.pk))
        url = reverse("vantage:password_reset_confirm", args=[uid, "abc-" + "d" * 20])
        codes = [client.get(url).status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]


# ── the audit trail ─────────────────────────────────────────────────────────


class TestAudit:
    def test_request_and_completion_are_recorded_without_the_token(self, client, clerk):
        client.defaults["REMOTE_ADDR"] = "203.0.113.9"
        link = reset_link(client)
        set_password(client, link)

        requested = VantageLogEntry.objects.get(action_name="Password reset requested")
        completed = VantageLogEntry.objects.get(action_name="Password reset completed")
        for entry in (requested, completed):
            assert entry.object_id == str(clerk.pk)
            assert entry.ip_address == "203.0.113.9"
            assert "clerk" in entry.object_repr

        token = path_of(link).rstrip("/").rsplit("/", 1)[-1]
        blob = " ".join(
            f"{e.action_name} {e.object_repr} {e.changes} {e.user_agent}"
            for e in VantageLogEntry.objects.all()
        )
        assert token not in blob
        assert NEW_PASSWORD not in blob
        assert "pbkdf2" not in blob

    def test_unknown_addresses_leave_no_entry(self, client, db):
        ask(client, "ghost@example.com")
        assert not VantageLogEntry.objects.filter(action_name__startswith="Password reset").exists()

    def test_it_works_when_the_audit_log_is_off(self, client, clerk, settings):
        settings.VANTAGE = {"PASSWORD_RESET": True, "AUDIT_LOG": False}
        set_password(client, reset_link(client))
        assert VantageLogEntry.objects.count() == 0


# ── modes ───────────────────────────────────────────────────────────────────


class TestReadOnlyMode:
    def test_a_reset_still_works_in_read_only_mode(self, client, clerk, settings):
        settings.VANTAGE = {"PASSWORD_RESET": True, "READ_ONLY": True}
        done = set_password(client, reset_link(client))
        assert done.status_code == 302
        clerk.refresh_from_db()
        assert clerk.check_password(NEW_PASSWORD)


# ── settings ────────────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults(self, settings):
        from vantage.conf import vantage_settings

        settings.VANTAGE = {}
        assert vantage_settings.PASSWORD_RESET is False
        assert vantage_settings.PASSWORD_RESET_TIMEOUT_MINUTES == 60
        assert vantage_settings.PASSWORD_RESET_BASE_URL == ""

    def test_the_timeout_has_a_floor(self, settings):
        from django.core.exceptions import ImproperlyConfigured

        from vantage.conf import vantage_settings

        settings.VANTAGE = {"PASSWORD_RESET_TIMEOUT_MINUTES": 4}
        with pytest.raises(ImproperlyConfigured):
            _ = vantage_settings.PASSWORD_RESET

    def test_not_editable_from_the_settings_screen(self):
        from vantage.siteconfig import UI_EDITABLE

        assert not {k for k in UI_EDITABLE if k.startswith("PASSWORD_RESET")}

    def test_checks(self, settings):
        from vantage import checks

        settings.VANTAGE = {"PASSWORD_RESET": True, "PASSWORD_RESET_BASE_URL": "panel.example.com"}
        ids = [e.id for e in checks.check_vantage_settings(None)]
        assert "vantage.E126" in ids
        settings.VANTAGE = {"PASSWORD_RESET": "yes"}
        ids = [e.id for e in checks.check_vantage_settings(None)]
        assert "vantage.E126" in ids

    def test_deploy_warning_for_a_wildcard_host(self, settings):
        from vantage import checks

        settings.ALLOWED_HOSTS = ["*"]
        settings.VANTAGE = {"PASSWORD_RESET": True}
        assert [w.id for w in checks.check_deploy_password_reset_base_url(None)] == ["vantage.W117"]
        settings.VANTAGE = {
            "PASSWORD_RESET": True,
            "PASSWORD_RESET_BASE_URL": "https://p.example.com",
        }
        assert checks.check_deploy_password_reset_base_url(None) == []
        settings.VANTAGE = {}
        assert checks.check_deploy_password_reset_base_url(None) == []
