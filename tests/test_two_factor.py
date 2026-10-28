"""Two-factor sign-in.

Most of these tests are about what it refuses: a password without a code, a code twice,
a hundredth guess, a session signed in somewhere else, a form posted from another site.
A second factor that can be stepped around anywhere is decoration, so the gate is tested
from every direction a request can arrive.

This module is its own urlconf, like ``test_auth_pages``, because the sign-in flow is
the thing under test and it needs ``vantage.authviews`` mounted beside the panel.
"""

import hashlib
import json
import time
from datetime import UTC

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import include, path, reverse

from vantage import qr, site, twofactor
from vantage.checks import check_deploy_two_factor_admin, check_two_factor
from vantage.conf import vantage_settings
from vantage.models import TwoFactorDevice, TwoFactorRecoveryCode, VantageLogEntry
from vantage.registry import DEFAULT_EXCLUDE

urlpatterns = [
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
]

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _mounted(settings):
    settings.ROOT_URLCONF = "tests.test_two_factor"


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", PASSWORD)


@pytest.fixture
def clerk(db):
    return User.objects.create_user("clerk", "clerk@example.com", PASSWORD, is_staff=True)


def enrol(user):
    """A confirmed device, set up the way the enrolment page would leave it."""
    from django.utils import timezone

    device = twofactor.start_enrolment(user)
    device.confirmed_at = timezone.now()
    device.save()
    return device, twofactor._issue_codes(device)


def seal_v1(plaintext: bytes) -> str:
    """A value in the first sealed format, which bound it to nothing."""
    import base64
    import hmac
    import secrets

    from django.conf import settings

    nonce = secrets.token_bytes(16)
    encrypt, sign = twofactor._seal_keys(settings.SECRET_KEY)
    ciphertext = twofactor._xor(plaintext, twofactor._keystream(encrypt, nonce, len(plaintext)))
    tag = hmac.new(sign, b"v1" + nonce + ciphertext, hashlib.sha256).digest()
    return "v1$" + base64.urlsafe_b64encode(nonce + ciphertext + tag).decode("ascii")


def code_for(device, steps_ahead: int = 0) -> str:
    """A current app code. ``steps_ahead`` reaches the next step, for a second code."""
    key = twofactor.device_key(TwoFactorDevice.objects.get(pk=device.pk))
    return twofactor.totp(key, time.time() + twofactor.PERIOD * steps_ahead)


def sign_in(client, user):
    return client.post(reverse("login"), {"username": user.username, "password": PASSWORD})


def signed_in_user_id(client):
    return client.session.get("_auth_user_id")


# ── the algorithm ───────────────────────────────────────────────────────────


class TestRFC4226:
    """Appendix D of RFC 4226: HOTP for the ASCII secret "12345678901234567890"."""

    EXPECTED = [
        "755224", "287082", "359152", "969429", "338314",
        "254676", "287922", "162583", "399871", "520489",
    ]  # fmt: skip

    def test_hotp_vectors(self):
        key = b"12345678901234567890"
        assert [twofactor.hotp(key, counter) for counter in range(10)] == self.EXPECTED


class TestRFC6238:
    """Appendix B of RFC 6238, all three hash functions, eight digits."""

    SEEDS = {
        hashlib.sha1: b"12345678901234567890",
        hashlib.sha256: b"12345678901234567890123456789012",
        hashlib.sha512: b"1234567890123456789012345678901234567890123456789012345678901234",
    }
    VECTORS = [
        (59, "94287082", "46119246", "90693936"),
        (1111111109, "07081804", "68084774", "25091201"),
        (1111111111, "14050471", "67062674", "99943326"),
        (1234567890, "89005924", "91819424", "93441116"),
        (2000000000, "69279037", "90698825", "38618901"),
        (20000000000, "65353130", "77737706", "47863826"),
    ]

    @pytest.mark.parametrize(("at", "sha1", "sha256", "sha512"), VECTORS)
    def test_vectors(self, at, sha1, sha256, sha512):
        for digest, expected in zip(
            (hashlib.sha1, hashlib.sha256, hashlib.sha512), (sha1, sha256, sha512), strict=True
        ):
            assert twofactor.totp(self.SEEDS[digest], at, digits=8, digest=digest) == expected

    def test_the_six_digit_code_is_the_last_six_of_the_eight(self):
        """Truncation is modulo 10^digits, so the production code is checkable too."""
        assert twofactor.totp(self.SEEDS[hashlib.sha1], 59) == "287082"


class TestMatching:
    KEY = b"12345678901234567890"
    AT = 1111111111

    def test_the_current_step_matches(self):
        code = twofactor.totp(self.KEY, self.AT)
        assert twofactor.matching_step(self.KEY, code, self.AT) == self.AT // 30

    @pytest.mark.parametrize("offset", [-1, 1])
    def test_one_step_either_side_matches(self, offset):
        code = twofactor.totp(self.KEY, self.AT + 30 * offset)
        assert twofactor.matching_step(self.KEY, code, self.AT) == self.AT // 30 + offset

    @pytest.mark.parametrize("offset", [-3, -2, 2, 3])
    def test_further_away_does_not(self, offset):
        code = twofactor.totp(self.KEY, self.AT + 30 * offset)
        assert twofactor.matching_step(self.KEY, code, self.AT) is None

    @pytest.mark.parametrize("junk", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", None])
    def test_anything_not_six_ascii_digits_is_refused(self, junk):
        assert twofactor.matching_step(self.KEY, twofactor.normalise_code(junk), self.AT) is None

    def test_spaces_and_hyphens_people_paste_are_ignored(self):
        code = twofactor.totp(self.KEY, self.AT)
        spaced = f" {code[:3]} {code[3:]}-"
        assert twofactor.matching_step(self.KEY, twofactor.normalise_code(spaced), self.AT)

    def test_the_comparison_is_constant_time(self, monkeypatch):
        """Every candidate goes through hmac.compare_digest, never ``==``."""
        calls = []
        real = twofactor.hmac.compare_digest
        monkeypatch.setattr(
            twofactor.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b)
        )
        twofactor.matching_step(self.KEY, twofactor.totp(self.KEY, self.AT), self.AT)
        assert len(calls) == 2 * twofactor.WINDOW + 1


class TestProvisioningUri:
    def test_it_is_a_standard_key_uri(self, boss):
        uri = twofactor.provisioning_uri(boss, b"12345678901234567890")
        assert uri.startswith("otpauth://totp/Django%20Vantage:boss?")
        assert "secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ" in uri
        assert "issuer=Django%20Vantage" in uri
        assert "algorithm=SHA1" in uri and "digits=6" in uri and "period=30" in uri

    def test_the_issuer_setting_names_it(self, boss):
        with override_settings(VANTAGE={"TWO_FACTOR_ISSUER": "Northwind Ops"}):
            uri = twofactor.provisioning_uri(boss, b"x" * 20)
        assert uri.startswith("otpauth://totp/Northwind%20Ops:boss?")


# ── the secret at rest ──────────────────────────────────────────────────────


class TestSealing:
    def test_round_trip(self):
        secret = twofactor.new_secret()
        assert twofactor.unseal(twofactor.seal(secret)) == secret

    def test_the_stored_value_does_not_contain_the_secret(self):
        secret = b"12345678901234567890"
        sealed = twofactor.seal(secret)
        assert secret not in sealed.encode()
        assert twofactor.b32(secret) not in sealed

    def test_sealing_twice_gives_different_values(self):
        """A random nonce per seal, so two accounts never share a stored value."""
        secret = twofactor.new_secret()
        assert twofactor.seal(secret) != twofactor.seal(secret)

    def test_a_tampered_value_is_refused(self):
        sealed = twofactor.seal(twofactor.new_secret())
        body = bytearray(__import__("base64").urlsafe_b64decode(sealed[3:]))
        body[20] ^= 1
        forged = sealed[:3] + __import__("base64").urlsafe_b64encode(bytes(body)).decode()
        with pytest.raises(ValueError):
            twofactor.unseal(forged)

    def test_another_secret_key_cannot_open_it(self, settings):
        sealed = twofactor.seal(twofactor.new_secret())
        settings.SECRET_KEY = "a-completely-different-key"
        with pytest.raises(ValueError):
            twofactor.unseal(sealed)

    def test_a_rotated_key_still_opens_it_through_the_fallbacks(self, settings):
        secret = twofactor.new_secret()
        sealed = twofactor.seal(secret)
        old = settings.SECRET_KEY
        settings.SECRET_KEY = "the-new-key"
        settings.SECRET_KEY_FALLBACKS = [old]
        assert twofactor.unseal(sealed) == secret

    @pytest.mark.parametrize("junk", ["", "v2$abc", "v3$abc", "v1$", "v2$", "v1$!!!", "plain"])
    def test_junk_is_refused(self, junk):
        with pytest.raises(ValueError):
            twofactor.unseal(junk)

    def test_the_database_row_is_sealed(self, boss):
        device, _ = enrol(boss)
        key = twofactor.device_key(device)
        assert twofactor.b32(key) not in device.secret
        assert device.secret.startswith("v2$")

    def test_a_value_opens_only_in_its_own_context(self):
        secret = twofactor.new_secret()
        sealed = twofactor.seal(secret, "row-1")
        assert twofactor.unseal(sealed, "row-1") == secret
        for other in ("row-2", "", "row-1 "):
            with pytest.raises(ValueError):
                twofactor.unseal(sealed, other)

    def test_a_secret_copied_to_another_device_fails_closed(self, boss, clerk):
        """Someone who can write the table but not read SECRET_KEY copies their own
        sealed secret over a colleague's, then signs in as them with their own phone.
        The value is bound to its owner and enrolment, so it no longer opens there."""
        mine, _ = enrol(clerk)
        theirs, _ = enrol(boss)
        TwoFactorDevice.objects.filter(pk=theirs.pk).update(secret=mine.secret)
        my_code = code_for(mine)
        assert twofactor.device_key(TwoFactorDevice.objects.get(pk=theirs.pk)) is None
        assert not twofactor.verify(TwoFactorDevice.objects.get(pk=theirs.pk), my_code)
        # The tag is part of the binding too: a re-enrolment's row is a different row.
        TwoFactorDevice.objects.filter(pk=mine.pk).update(tag="0" * 32)
        assert twofactor.device_key(TwoFactorDevice.objects.get(pk=mine.pk)) is None

    def test_a_v1_value_still_verifies_and_is_sealed_again_as_v2(self, boss):
        device, _ = enrol(boss)
        key = twofactor.device_key(device)
        TwoFactorDevice.objects.filter(pk=device.pk).update(secret=seal_v1(key))
        assert twofactor.verify(device, code_for(device))
        device.refresh_from_db()
        assert device.secret.startswith("v2$")
        assert twofactor.device_key(device) == key

    def test_a_value_only_a_fallback_key_opens_is_sealed_again(self, boss, settings):
        device, _ = enrol(boss)
        key = twofactor.device_key(device)
        old = settings.SECRET_KEY
        settings.SECRET_KEY = "the-new-key"
        settings.SECRET_KEY_FALLBACKS = [old]
        assert twofactor.verify(device, code_for(device))
        # Dropping the fallback, as the rotation's last step, no longer locks them out.
        settings.SECRET_KEY_FALLBACKS = []
        device.refresh_from_db()
        assert twofactor.device_key(device) == key

    def test_a_current_value_is_left_alone(self, boss):
        device, _ = enrol(boss)
        before = device.secret
        assert twofactor.verify(device, code_for(device))
        device.refresh_from_db()
        assert device.secret == before


# ── the QR code ─────────────────────────────────────────────────────────────


def _text(n):
    return "".join(chr(33 + (i * 7) % 90) for i in range(n))


class TestQR:
    """Fingerprints of codes made by an independent encoder (segno 1.6) at the same
    version and mask, so these compare module for module, not "it looks square"."""

    GOLDEN = [
        ("hello world", 1, 0, "ff11d03a5d4c7271b10c332b4fa40be7ba354863ec7af342b03e7358f250d276"),
        (
            "otpauth://totp/Acme%20Corp:alice%40example.com?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
            "&issuer=Acme%20Corp&algorithm=SHA1&digits=6&period=30",
            8,
            3,
            "2e296559ff9c501fb5b36062b94ba425e192ff872a5c67c0e7e3864447490459",
        ),
        (_text(40), 3, 6, "2295607d4295bf5e3c125b60ab695cf177e811042062b9834923dbfb2454ed9a"),
        (_text(180), 10, 1, "a6bb310e2096d36298df38500ad1e2eb96e55e7cd52dd710abd7bbbff0f13f6d"),
        (_text(300), 13, 5, "1f4bb8de8ab0db43f295836c14dc4c5e935d9c8f5c46aaaca1273e9580015ce7"),
        (_text(600), 20, 7, "2e2bd1bf31251e189340b1271fb8fe3cfca643b4b52755b364f07ded826aa5f5"),
    ]

    @pytest.mark.parametrize(("data", "version", "mask", "digest"), GOLDEN)
    def test_matches_an_independent_encoder(self, data, version, mask, digest):
        matrix = qr.encode(data, version=version, mask=mask)
        bits = "".join("1" if dark else "0" for row in matrix for dark in row)
        assert hashlib.sha256(bits.encode()).hexdigest() == digest

    def test_picks_the_smallest_version_that_fits(self):
        assert len(qr.encode("hello world")) == 21  # version 1
        assert len(qr.encode(_text(139))) == 8 * 4 + 17

    def test_too_long_is_refused_rather_than_truncated(self):
        with pytest.raises(qr.QRTooLong):
            qr.encode("x" * 700)

    def test_the_svg_is_black_on_white_with_a_label(self):
        markup = qr.svg(qr.encode("hi"), label='scan "me" <now>')
        assert markup.startswith("<svg")
        assert 'fill="#fff"' in markup and 'fill="#000"' in markup
        assert 'aria-label="scan &quot;me&quot; &lt;now&gt;"' in markup


# ── verifying a code ────────────────────────────────────────────────────────


class TestVerify:
    def test_a_right_code_is_accepted(self, boss):
        device, _ = enrol(boss)
        assert twofactor.verify(device, code_for(device)).reason == "totp"

    def test_the_same_code_twice_is_a_replay(self, boss):
        device, _ = enrol(boss)
        code = code_for(device)
        assert twofactor.verify(device, code)
        second = twofactor.verify(device, code)
        assert not second and second.reason == "reused"

    def test_an_earlier_step_is_refused_after_a_later_one(self, boss):
        device, _ = enrol(boss)
        assert twofactor.verify(device, code_for(device, 1))
        assert not twofactor.verify(device, code_for(device, 0))

    def test_a_recovery_code_works_once(self, boss):
        device, codes = enrol(boss)
        assert twofactor.verify(device, codes[0]).reason == "recovery"
        assert not twofactor.verify(device, codes[0])
        assert twofactor.verify(device, codes[1].upper().replace("-", " "))

    def test_recovery_codes_are_stored_only_as_hashes(self, boss):
        _device, codes = enrol(boss)
        stored = list(TwoFactorRecoveryCode.objects.values_list("code_hash", flat=True))
        assert len(stored) == twofactor.RECOVERY_CODE_COUNT
        for code in codes:
            assert code not in stored and twofactor.normalise_code(code) not in stored

    def test_recovery_codes_carry_eighty_bits(self, boss):
        _, codes = enrol(boss)
        for code in codes:
            raw = twofactor.normalise_code(code)
            assert len(raw) == 16 and set(raw) <= set(twofactor.RECOVERY_ALPHABET)
        assert len(twofactor.RECOVERY_ALPHABET) == 32

    def test_too_many_wrong_codes_lock_it(self, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 3, "TWO_FACTOR_LOCKOUT_SECONDS": 600}
        device, codes = enrol(boss)
        outcomes = [twofactor.verify(device, "000000") for _ in range(3)]
        assert [o.reason for o in outcomes] == ["invalid", "invalid", "locked"]
        # Locked means locked: the right code and a recovery code are refused too.
        assert twofactor.verify(device, code_for(device)).reason == "locked"
        assert twofactor.verify(device, codes[0]).reason == "locked"

    def test_the_lock_lifts(self, boss, settings):
        from datetime import timedelta

        from django.utils import timezone

        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 1}
        device, _ = enrol(boss)
        assert twofactor.verify(device, "000000").reason == "locked"
        TwoFactorDevice.objects.filter(pk=device.pk).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        assert twofactor.verify(device, code_for(device))

    def test_a_right_code_resets_the_count(self, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 3}
        device, _ = enrol(boss)
        twofactor.verify(device, "000000")
        twofactor.verify(device, "000000")
        # The third attempt reaches the limit, so the lock goes on before the code is
        # compared; a right code lifts it again.
        assert twofactor.verify(device, code_for(device))
        device.refresh_from_db()
        assert device.failed_attempts == 0
        assert device.locked_until is None

    @pytest.mark.parametrize("kind", ["app code", "recovery code", "password"])
    def test_a_burst_compares_no_more_than_the_limit(self, boss, settings, monkeypatch, kind):
        """However requests interleave, at most MAX_ATTEMPTS answers are compared.

        Each comparison starts the next request before it finishes, which is the worst
        case for a check-then-count lockout: every request is past the "is it locked?"
        question before any failure has been counted. The attempt is counted first, so
        the chain stops at the limit, and the rest of the burst is refused unread.
        """
        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 5, "TWO_FACTOR_LOCKOUT_SECONDS": 600}
        device, _ = enrol(boss)

        def fresh():
            return TwoFactorDevice.objects.get(pk=device.pk)

        owner, name, again = {
            "app code": (twofactor, "device_key", lambda: twofactor.verify(fresh(), "000000")),
            "recovery code": (
                twofactor,
                "hash_recovery_code",
                lambda: twofactor.verify(fresh(), "aaaa-aaaa-aaaa-aaaa"),
            ),
            "password": (
                User,
                "check_password",
                lambda: twofactor.verify_password(fresh(), boss, "wrong"),
            ),
        }[kind]
        real = getattr(owner, name)
        burst = {"sent": 1, "compared": 0}

        def compare(*args, **kwargs):
            burst["compared"] += 1
            if burst["sent"] < 40:
                burst["sent"] += 1
                again()
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, compare)
        again()
        while burst["sent"] < 40:
            burst["sent"] += 1
            assert again().reason == "locked"
        assert burst["compared"] == 5
        assert fresh().locked_until is not None

    def test_a_secret_that_cannot_be_unsealed_fails_closed(self, boss, settings):
        device, _ = enrol(boss)
        code = code_for(device)
        settings.SECRET_KEY = "rotated-without-a-fallback"
        assert not twofactor.verify(device, code)


# ── signing in ──────────────────────────────────────────────────────────────


class TestSignIn:
    def test_without_two_factor_the_password_is_enough(self, client, boss):
        response = sign_in(client, boss)
        assert response.status_code == 302
        assert signed_in_user_id(client) == str(boss.pk)

    def test_with_it_the_password_alone_signs_nobody_in(self, client, boss):
        enrol(boss)
        response = sign_in(client, boss)
        assert response["Location"] == reverse("login_two_factor")
        assert signed_in_user_id(client) is None

    def test_the_half_signed_in_session_has_no_panel_access(self, client, boss):
        enrol(boss)
        sign_in(client, boss)
        assert client.get(reverse("vantage:dashboard")).status_code == 302
        assert reverse("login") in client.get(reverse("vantage:dashboard"))["Location"]
        assert client.get(reverse("vantage:api-schema")).status_code == 401
        assert client.get(reverse("vantage:two_factor")).status_code == 302

    def test_the_right_code_signs_in_and_rotates_the_session(self, client, boss):
        device, _ = enrol(boss)
        sign_in(client, boss)
        pending_key = client.session.session_key
        response = client.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert response.status_code == 302
        assert signed_in_user_id(client) == str(boss.pk)
        assert client.session.session_key != pending_key
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_the_session_key_is_rotated_after_the_password_too(self, client, boss):
        """A session id planted before the password is typed must not carry on."""
        enrol(boss)
        client.get(reverse("login"))
        before = client.session.session_key
        sign_in(client, boss)
        assert client.session.session_key != before

    def test_it_goes_where_the_password_step_was_going(self, client, boss):
        device, _ = enrol(boss)
        client.post(
            reverse("login") + "?next=/admin/testapp/author/",
            {"username": "boss", "password": PASSWORD, "next": "/admin/testapp/author/"},
        )
        response = client.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert response["Location"] == "/admin/testapp/author/"

    def test_a_wrong_code_says_so_and_signs_nobody_in(self, client, boss):
        enrol(boss)
        sign_in(client, boss)
        response = client.post(reverse("login_two_factor"), {"code": "000000"})
        assert response.status_code == 200
        assert "not right" in response.content.decode()
        assert signed_in_user_id(client) is None

    def test_a_code_cannot_be_replayed_into_a_second_sign_in(self, boss):
        device, _ = enrol(boss)
        code = code_for(device)
        first, second = Client(), Client()
        sign_in(first, boss)
        sign_in(second, boss)
        first.post(reverse("login_two_factor"), {"code": code})
        response = second.post(reverse("login_two_factor"), {"code": code})
        assert "already been used" in response.content.decode()
        assert signed_in_user_id(second) is None

    def test_a_recovery_code_signs_in_once(self, boss):
        _, codes = enrol(boss)
        first, second = Client(), Client()
        sign_in(first, boss)
        first.post(reverse("login_two_factor"), {"code": codes[0]})
        assert signed_in_user_id(first) == str(boss.pk)
        sign_in(second, boss)
        second.post(reverse("login_two_factor"), {"code": codes[0]})
        assert signed_in_user_id(second) is None

    def test_spending_a_recovery_code_is_audited(self, client, boss):
        _, codes = enrol(boss)
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": codes[0]})
        entry = VantageLogEntry.objects.get(action_name="two_factor.recovery_used")
        assert entry.object_id == str(boss.pk)
        assert "9 left" in json.dumps(entry.changes)
        assert codes[0] not in json.dumps(entry.changes)

    def test_lockout_follows_the_account_not_the_session(self, boss, settings):
        """Fresh sessions do not buy fresh guesses."""
        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 3}
        device, _ = enrol(boss)
        for _ in range(3):
            attacker = Client()
            sign_in(attacker, boss)
            attacker.post(reverse("login_two_factor"), {"code": "000000"})
        victim = Client()
        sign_in(victim, boss)
        response = victim.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert "Too many wrong codes" in response.content.decode()
        assert signed_in_user_id(victim) is None

    def test_a_lockout_throws_the_password_step_away(self, client, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR_MAX_ATTEMPTS": 1}
        enrol(boss)
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": "000000"})
        assert twofactor.SESSION_PENDING not in client.session
        assert client.get(reverse("login_two_factor"))["Location"] == reverse("login")

    def test_without_a_password_step_there_is_no_second(self, client, boss):
        enrol(boss)
        assert client.get(reverse("login_two_factor"))["Location"] == reverse("login")
        assert client.post(reverse("login_two_factor"), {"code": "123456"}).status_code == 302

    def test_the_password_step_expires(self, client, boss, monkeypatch):
        device, _ = enrol(boss)
        sign_in(client, boss)
        monkeypatch.setattr(twofactor, "PENDING_SECONDS", 0)
        client.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert signed_in_user_id(client) is None

    def test_a_password_change_in_between_voids_it(self, client, boss):
        device, _ = enrol(boss)
        sign_in(client, boss)
        boss.set_password("something-else-entirely")
        boss.save()
        client.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert signed_in_user_id(client) is None

    def test_the_page_renders_with_the_code_field(self, client, boss):
        enrol(boss)
        sign_in(client, boss)
        body = client.get(reverse("login_two_factor")).content.decode()
        assert 'autocomplete="one-time-code"' in body
        assert "Start again with your password" in body

    def test_switched_off_the_password_is_enough_again(self, client, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR": False}
        enrol(boss)
        sign_in(client, boss)
        assert signed_in_user_id(client) == str(boss.pk)
        assert client.get(reverse("vantage:dashboard")).status_code == 200


# ── the gate ────────────────────────────────────────────────────────────────


class TestGate:
    """A session that signed in somewhere else — force_login stands in for it."""

    def test_an_unverified_session_is_sent_to_the_second_step(self, client, boss):
        enrol(boss)
        client.force_login(boss)
        response = client.get(reverse("vantage:testapp-author-list"))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("vantage:two_factor_verify"))
        assert "next=%2Fadmin%2Ftestapp%2Fauthor%2F" in response["Location"]

    @pytest.mark.parametrize(
        "name", ["vantage:dashboard", "vantage:account", "vantage:settings", "vantage:audit"]
    )
    def test_every_page_is_behind_it(self, client, boss, name):
        enrol(boss)
        client.force_login(boss)
        assert client.get(reverse(name)).status_code == 302

    def test_the_api_and_autocomplete_answer_in_json(self, client, boss):
        enrol(boss)
        client.force_login(boss)
        for url in (reverse("vantage:api-schema"), reverse("vantage:autocomplete")):
            response = client.get(url, {"model": "testapp.Author"})
            assert response.status_code == 403
            assert json.loads(response.content)["two_factor"] == "verify"

    def test_routes_inside_an_include_are_behind_it(self):
        def inner(request):
            return HttpResponse("data")

        patterns = [path("x/", include([path("y/", include([path("z/", inner)]))]))]
        twofactor.guard_patterns(patterns)
        assert patterns[0].url_patterns[0].url_patterns[0].callback.two_factor_gated

    def test_a_site_whose_urls_add_an_include_is_gated_all_the_way_down(
        self, client, boss, settings
    ):
        """A subclass appending its own routes after super()'s, grouped under an
        include(), is gated like the rest — not open because of how it is arranged."""
        from types import ModuleType

        from vantage.sites import VantageSite

        def secret_page(request):
            return HttpResponse("rows of data")

        class GroupingSite(VantageSite):
            def get_urls(self):
                extra = include([path("deeper/", include([path("page/", secret_page)]))])
                return [*super().get_urls(), path("extra/", extra)]

        urlconf = ModuleType("grouped_urls")
        urlconf.urlpatterns = [path("grouped/", GroupingSite(name="grouped").urls)]
        settings.ROOT_URLCONF = urlconf
        enrol(boss)
        client.force_login(boss)
        response = client.get("/grouped/extra/deeper/page/")
        assert response.status_code == 302
        assert "two-factor/verify" in response["Location"]
        assert b"rows of data" not in response.content

    def test_a_post_is_refused_not_redirected(self, client, boss):
        enrol(boss)
        client.force_login(boss)
        response = client.post(reverse("vantage:testapp-author-add"), {"name": "x"})
        assert response.status_code == 403

    def test_the_set_up_page_is_not_reachable_with_only_a_password(self, client, boss):
        """It is the page that switches two-factor off."""
        enrol(boss)
        client.force_login(boss)
        response = client.post(
            reverse("vantage:two_factor"), {"action": "disable", "password": PASSWORD}
        )
        assert response.status_code == 403
        assert TwoFactorDevice.objects.filter(user=boss).exists()

    def test_answering_opens_the_panel_and_rotates_the_session(self, client, boss):
        device, _ = enrol(boss)
        client.force_login(boss)
        before = client.session.session_key
        response = client.post(
            reverse("vantage:two_factor_verify") + "?next=/admin/testapp/author/",
            {"code": code_for(device)},
        )
        assert response["Location"] == "/admin/testapp/author/"
        assert client.session.session_key != before
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_next_cannot_leave_the_site(self, client, boss):
        device, _ = enrol(boss)
        client.force_login(boss)
        response = client.post(
            reverse("vantage:two_factor_verify"),
            {"code": code_for(device), "next": "https://evil.example/"},
        )
        assert response["Location"] == reverse("vantage:dashboard")

    def test_without_two_factor_nothing_changes(self, client, boss):
        client.force_login(boss)
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_the_gate_costs_no_queries_once_signed_in(
        self, client, boss, django_assert_num_queries
    ):
        """Primed at sign-in: an autocomplete keystroke pays session, user, options."""
        client.force_login(boss)
        with django_assert_num_queries(3):
            client.get(reverse("vantage:autocomplete"), {"model": "testapp.Author"})

    def test_a_reset_elsewhere_reaches_a_live_session(self, client, boss, monkeypatch):
        device, _ = enrol(boss)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(device)})
        # Re-enrolling elsewhere makes this session's proof name the wrong device.
        twofactor.reset(None, boss)
        enrol(boss)
        monkeypatch.setattr(twofactor, "RECHECK_SECONDS", 0)
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_impersonating_keeps_the_proof(self, client, boss, clerk):
        device, _ = enrol(boss)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(device)})
        client.post(reverse("vantage:impersonate"), {"user": clerk.pk})
        assert client.get(reverse("vantage:dashboard")).status_code == 200
        # And the impersonated account's second factor is not theirs to change.
        assert client.get(reverse("vantage:two_factor")).status_code == 403


class TestRequired:
    def test_a_required_superuser_is_sent_to_set_it_up(self, client, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        client.force_login(boss)
        response = client.get(reverse("vantage:dashboard"))
        assert response["Location"].startswith(reverse("vantage:two_factor"))
        page = client.get(response["Location"])
        assert page.status_code == 200
        assert "Your role requires two-factor sign-in" in page.content.decode()

    def test_it_takes_effect_on_a_live_session(self, client, boss, settings):
        client.force_login(boss)
        assert client.get(reverse("vantage:dashboard")).status_code == 200
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_a_named_role_requires_it(self, client, clerk, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        clerk.groups.add(Group.objects.create(name="Finance"))
        client.force_login(clerk)
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_other_roles_do_not(self, client, clerk, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        clerk.groups.add(Group.objects.create(name="Support"))
        client.force_login(clerk)
        assert not twofactor.is_required(clerk)
        assert client.get(reverse("vantage:dashboard")).status_code != 302

    def test_superusers_are_not_required_by_default(self, boss):
        assert not twofactor.is_required(boss)

    def test_setting_it_up_opens_the_panel(self, client, boss, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        device = TwoFactorDevice.objects.get(user=boss)
        client.post(reverse("vantage:two_factor"), {"action": "confirm", "code": code_for(device)})
        assert client.get(reverse("vantage:dashboard")).status_code == 200


# ── enrolment and the account page ─────────────────────────────────────────


class TestEnrolment:
    def test_the_account_page_offers_it(self, client, boss):
        client.force_login(boss)
        body = client.get(reverse("vantage:account")).content.decode()
        assert reverse("vantage:two_factor") in body
        assert "Set up two-factor sign-in" in body

    def test_starting_shows_the_key_the_link_and_a_qr_code(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        response = client.get(reverse("vantage:two_factor"))
        body = response.content.decode()
        key = twofactor.b32(twofactor.device_key(TwoFactorDevice.objects.get(user=boss)))
        assert " ".join(key[i : i + 4] for i in range(0, 32, 4)) in body
        assert "otpauth://totp/" in body
        assert "<svg" in body
        assert (
            response["Cache-Control"].startswith("no-store")
            or "no-store" in response["Cache-Control"]
        )

    def test_it_is_not_on_until_a_code_confirms_it(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        response = client.post(
            reverse("vantage:two_factor"), {"action": "confirm", "code": "000000"}
        )
        assert "not right" in response.content.decode()
        assert twofactor.device_for(boss) is None
        # Not on, so signing in still takes only the password.
        fresh = Client()
        sign_in(fresh, boss)
        assert signed_in_user_id(fresh) == str(boss.pk)

    def test_a_recovery_code_cannot_confirm_it(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        client.post(
            reverse("vantage:two_factor"), {"action": "confirm", "code": "abcd-efgh-ijkm-npqr"}
        )
        assert twofactor.device_for(boss) is None

    def test_confirming_shows_recovery_codes_once(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        device = TwoFactorDevice.objects.get(user=boss)
        key = twofactor.b32(twofactor.device_key(device))
        response = client.post(
            reverse("vantage:two_factor"), {"action": "confirm", "code": code_for(device)}
        )
        body = response.content.decode()
        assert "Save your recovery codes" in body
        assert "no-store" in response["Cache-Control"]
        shown = body.split("<code>")[1:]
        assert len(shown) == twofactor.RECOVERY_CODE_COUNT
        # Afterwards: neither the codes nor the secret are on the page again.
        later = client.get(reverse("vantage:two_factor")).content.decode()
        assert "Save your recovery codes" not in later
        assert key[:8] not in later and "otpauth://" not in later
        assert "Two-factor sign-in is on" in later

    def test_the_confirming_code_cannot_be_reused_to_sign_in(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        device = TwoFactorDevice.objects.get(user=boss)
        code = code_for(device)
        client.post(reverse("vantage:two_factor"), {"action": "confirm", "code": code})
        assert twofactor.verify(device, code).reason == "reused"

    def test_enabling_is_audited(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        device = TwoFactorDevice.objects.get(user=boss)
        client.post(reverse("vantage:two_factor"), {"action": "confirm", "code": code_for(device)})
        assert VantageLogEntry.objects.filter(action_name="two_factor.enabled").exists()

    def test_every_step_of_it_is_audited_under_the_person(self, client, boss):
        """Starting, confirming, and switching off: whoever enrols an account first
        owns its second factor, so each step leaves a trail with a name on it."""
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        device = TwoFactorDevice.objects.get(user=boss)
        client.post(reverse("vantage:two_factor"), {"action": "confirm", "code": code_for(device)})
        client.post(reverse("vantage:two_factor"), {"action": "disable", "password": PASSWORD})
        entries = VantageLogEntry.objects.filter(action_name__startswith="two_factor.")
        assert [e.action_name for e in entries.order_by("pk")] == [
            "two_factor.started",
            "two_factor.enabled",
            "two_factor.disabled",
        ]
        assert {e.actor for e in entries} == {boss}
        assert {e.object_id for e in entries} == {str(boss.pk)}

    def test_cancelling_throws_the_secret_away(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        client.post(reverse("vantage:two_factor"), {"action": "cancel"})
        assert not TwoFactorDevice.objects.filter(user=boss).exists()

    def test_starting_again_issues_a_new_secret(self, client, boss):
        client.force_login(boss)
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        first = TwoFactorDevice.objects.get(user=boss).secret
        client.post(reverse("vantage:two_factor"), {"action": "start"})
        assert TwoFactorDevice.objects.get(user=boss).secret != first


class TestChangingIt:
    @pytest.fixture
    def verified(self, client, boss):
        device, codes = enrol(boss)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(device)})
        return device, codes

    def test_switching_off_needs_proof(self, client, boss, verified):
        response = client.post(reverse("vantage:two_factor"), {"action": "disable"})
        assert "Enter a current code or your password" in response.content.decode()
        assert twofactor.device_for(boss) is not None

    def test_a_wrong_password_does_not_switch_it_off_and_counts(self, client, boss, verified):
        device, _ = verified
        client.post(reverse("vantage:two_factor"), {"action": "disable", "password": "nope"})
        assert twofactor.device_for(boss) is not None
        device.refresh_from_db()
        assert device.failed_attempts == 1

    def test_the_password_switches_it_off(self, client, boss, verified):
        client.post(reverse("vantage:two_factor"), {"action": "disable", "password": PASSWORD})
        assert twofactor.device_for(boss) is None
        assert VantageLogEntry.objects.filter(action_name="two_factor.disabled").exists()
        fresh = Client()
        sign_in(fresh, boss)
        assert signed_in_user_id(fresh) == str(boss.pk)

    def test_a_current_code_switches_it_off(self, client, boss, verified):
        device, _ = verified
        client.post(
            reverse("vantage:two_factor"), {"action": "disable", "code": code_for(device, 1)}
        )
        assert twofactor.device_for(boss) is None

    def test_new_recovery_codes_replace_the_old(self, client, boss, verified):
        device, old = verified
        response = client.post(
            reverse("vantage:two_factor"), {"action": "codes", "password": PASSWORD}
        )
        assert "Save your recovery codes" in response.content.decode()
        assert not twofactor.verify(device, old[0])
        assert VantageLogEntry.objects.filter(action_name="two_factor.codes").exists()


class TestReset:
    def test_a_superuser_resets_someone_else_and_it_is_audited(self, client, boss, clerk):
        enrol(clerk)
        client.force_login(boss)
        page = client.get(reverse("vantage:two_factor_users"))
        assert "clerk" in page.content.decode()
        client.post(reverse("vantage:two_factor_users"), {"user": clerk.pk})
        assert twofactor.device_for(clerk) is None
        entry = VantageLogEntry.objects.get(action_name="two_factor.reset")
        assert entry.actor == boss
        assert entry.object_id == str(clerk.pk)

    def test_the_screen_shows_when_each_was_last_set_up(self, client, boss, clerk):
        """Enrolling needs only the password, so the first person to do it owns the
        account's second factor. The set-up time is how a superuser spots one the
        owner does not remember doing."""
        from datetime import datetime

        device, _ = enrol(clerk)
        when = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
        TwoFactorDevice.objects.filter(pk=device.pk).update(confirmed_at=when)
        client.force_login(boss)
        page = client.get(reverse("vantage:two_factor_users")).content.decode()
        assert "Set up" in page
        assert 'datetime="2026-03-04T' in page

    def test_not_your_own(self, client, boss):
        device, _ = enrol(boss)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(device)})
        client.post(reverse("vantage:two_factor_users"), {"user": boss.pk})
        assert twofactor.device_for(boss) is not None

    def test_staff_who_are_not_superusers_cannot(self, client, boss, clerk):
        enrol(boss)
        client.force_login(clerk)
        assert client.get(reverse("vantage:two_factor_users")).status_code == 403
        client.post(reverse("vantage:two_factor_users"), {"user": boss.pk})
        assert twofactor.device_for(boss) is not None

    def test_the_management_command(self, clerk, capsys):
        enrol(clerk)
        call_command("vantage_two_factor_reset", "clerk")
        assert twofactor.device_for(clerk) is None
        assert "now off" in capsys.readouterr().out
        assert VantageLogEntry.objects.get(action_name="two_factor.reset").actor is None


# ── django.contrib.admin beside the panel ─────────────────────────────────


class StubAdminSite(django_admin.AdminSite):
    """Django's admin, with the two pages these tests open answered without templates:
    ``django.contrib.admin`` is not installed here, so its templates are not either."""

    def index(self, request, extra_context=None):
        return HttpResponse("admin index")

    def logout(self, request, extra_context=None):
        return HttpResponse("signed out")


ADMIN_MIDDLEWARE = "vantage.twofactor.AdminTwoFactorMiddleware"


@pytest.fixture
def beside_admin(settings):
    """The panel and Django's admin, both mounted: the setup the bypass lives in."""
    from types import ModuleType

    from tests.testapp.models import Author

    admin_site = StubAdminSite(name="djadmin")
    admin_site.register(Author)
    urlconf = ModuleType("beside_admin_urls")
    urlconf.urlpatterns = [
        path("accounts/", include("vantage.authviews")),
        path("admin/", site.urls),
        path("djadmin/", admin_site.urls),
    ]
    settings.ROOT_URLCONF = urlconf
    return admin_site


class TestDjangoAdmin:
    def test_without_the_middleware_a_password_is_enough_for_the_admin(
        self, client, boss, beside_admin
    ):
        """The gap the middleware and W095 exist for: the panel is gated, the admin is
        not, and a session signed in at the admin's login has only the password."""
        enrol(boss)
        client.force_login(boss)
        assert client.get("/djadmin/").content == b"admin index"
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_with_it_the_admin_asks_for_the_code(self, client, boss, beside_admin, settings):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, ADMIN_MIDDLEWARE]
        device, _ = enrol(boss)
        client.force_login(boss)
        response = client.get("/djadmin/")
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("vantage:two_factor_verify"))
        assert "next=%2Fdjadmin%2F" in response["Location"]
        answered = client.post(
            response["Location"], {"code": code_for(device), "next": "/djadmin/"}
        )
        assert answered["Location"] == "/djadmin/"
        assert client.get("/djadmin/").content == b"admin index"

    def test_a_model_admins_pages_are_covered_too(self, client, boss, beside_admin, settings):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, ADMIN_MIDDLEWARE]
        enrol(boss)
        client.force_login(boss)
        assert client.get("/djadmin/testapp/author/").status_code == 302
        assert client.post("/djadmin/testapp/author/add/", {"name": "x"}).status_code == 403

    def test_a_required_enrolment_is_asked_for_first(self, client, boss, beside_admin, settings):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, ADMIN_MIDDLEWARE]
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        client.force_login(boss)
        response = client.get("/djadmin/")
        assert response["Location"].startswith(reverse("vantage:two_factor") + "?")

    def test_signing_out_and_signing_in_stay_open(self, client, boss, beside_admin, settings):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, ADMIN_MIDDLEWARE]
        enrol(boss)
        client.force_login(boss)
        assert client.post("/djadmin/logout/").content == b"signed out"
        assert twofactor.admin_site_of(beside_admin.login) is None

    def test_nobody_owing_anything_passes_straight_through(
        self, client, clerk, beside_admin, settings
    ):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, ADMIN_MIDDLEWARE]
        client.force_login(clerk)
        assert client.get("/djadmin/").content == b"admin index"

    def test_it_leaves_everything_else_alone(self, rf, boss):
        """Only views the admin marked as its own; anything else is not its business."""
        middleware = twofactor.AdminTwoFactorMiddleware(lambda request: HttpResponse())
        request = rf.get("/")
        assert middleware.process_view(request, lambda request: None, (), {}) is None


class TestAdminCheck:
    """``vantage.W095``: a requirement with Django's admin mounted beside it, unheld."""

    @pytest.fixture
    def required(self, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_ROLES": ["Finance"]}
        return settings

    def test_it_fires_and_says_where(self, beside_admin, required):
        warnings = check_deploy_two_factor_admin(None)
        assert [w.id for w in warnings] == ["vantage.W095"]
        assert "/djadmin/" in warnings[0].msg
        assert ADMIN_MIDDLEWARE in warnings[0].hint

    def test_superusers_alone_are_a_requirement_too(self, beside_admin, settings):
        settings.VANTAGE = {"TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        assert [w.id for w in check_deploy_two_factor_admin(None)] == ["vantage.W095"]

    def test_the_middleware_silences_it(self, beside_admin, required):
        required.MIDDLEWARE = [*required.MIDDLEWARE, ADMIN_MIDDLEWARE]
        assert check_deploy_two_factor_admin(None) == []

    def test_silent_when_nobody_is_required(self, beside_admin):
        assert check_deploy_two_factor_admin(None) == []

    def test_silent_when_two_factor_is_off(self, beside_admin, settings):
        settings.VANTAGE = {"TWO_FACTOR": False, "TWO_FACTOR_REQUIRED_SUPERUSERS": True}
        assert check_deploy_two_factor_admin(None) == []

    def test_silent_without_the_admin(self, required):
        assert check_deploy_two_factor_admin(None) == []

    def test_it_runs_only_under_deploy(self):
        from django.core.checks.registry import registry

        assert check_deploy_two_factor_admin in registry.get_checks(include_deployment_checks=True)
        assert check_deploy_two_factor_admin not in registry.get_checks()


class TestCsrf:
    """Every form here is csrf_protect-ed, so it holds without CsrfViewMiddleware too."""

    def test_the_second_sign_in_step(self, boss):
        device, _ = enrol(boss)
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("login"))
        token = client.cookies["csrftoken"].value
        client.post(
            reverse("login"),
            {"username": "boss", "password": PASSWORD, "csrfmiddlewaretoken": token},
        )
        response = client.post(reverse("login_two_factor"), {"code": code_for(device)})
        assert response.status_code == 403
        assert signed_in_user_id(client) is None

    def test_the_set_up_page(self, boss):
        client = Client(enforce_csrf_checks=True)
        client.force_login(boss)
        response = client.post(reverse("vantage:two_factor"), {"action": "start"})
        assert response.status_code == 403
        assert not TwoFactorDevice.objects.exists()

    def test_the_reset_screen(self, boss, clerk):
        enrol(clerk)
        client = Client(enforce_csrf_checks=True)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_users"), {"user": clerk.pk})
        assert twofactor.device_for(clerk) is not None


# ── configuration ───────────────────────────────────────────────────────────


class TestConfiguration:
    def test_the_tables_are_never_panel_models(self):
        assert {"vantage.twofactordevice", "vantage.twofactorrecoverycode"} <= DEFAULT_EXCLUDE

    def test_defaults_ask_nothing_of_anybody(self):
        assert vantage_settings.TWO_FACTOR is True
        assert vantage_settings.TWO_FACTOR_REQUIRED_SUPERUSERS is False
        assert vantage_settings.TWO_FACTOR_REQUIRED_ROLES == []

    @pytest.mark.parametrize(
        ("value", "check_id"),
        [
            ({"TWO_FACTOR_REQUIRED_ROLES": "Finance"}, "vantage.E095"),
            ({"TWO_FACTOR_REQUIRED_ROLES": ["", "x"]}, "vantage.E095"),
            ({"TWO_FACTOR_REQUIRED_SUPERUSERS": "yes"}, "vantage.E095"),
            ({"TWO_FACTOR_ISSUER": "Acme: Ops"}, "vantage.E096"),
            ({"TWO_FACTOR_ISSUER": ""}, "vantage.E096"),
        ],
    )
    def test_bad_values_are_errors(self, settings, value, check_id):
        settings.VANTAGE = value
        assert [e.id for e in check_two_factor(None)] == [check_id]
        with pytest.raises(ImproperlyConfigured):
            vantage_settings.TWO_FACTOR  # noqa: B018

    def test_good_values_pass(self, settings):
        settings.VANTAGE = {
            "TWO_FACTOR_REQUIRED_ROLES": ["Finance"],
            "TWO_FACTOR_REQUIRED_SUPERUSERS": True,
            "TWO_FACTOR_ISSUER": "Northwind",
        }
        assert check_two_factor(None) == []

    def test_no_session_middleware_is_an_error(self, settings):
        settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if "SessionMiddleware" not in m]
        assert [e.id for e in check_two_factor(None)] == ["vantage.E097"]

    def test_unless_it_is_switched_off(self, settings):
        settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if "SessionMiddleware" not in m]
        settings.VANTAGE = {"TWO_FACTOR": False}
        assert check_two_factor(None) == []
