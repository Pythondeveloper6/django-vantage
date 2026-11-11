"""Several authenticators per person.

What is tested is what the change could have loosened: a code from any confirmed device
signs in and a removed one does not; the lockout, replay step and sealed secret are still
per device; adding and removing a device needs proof; recovery codes outlive any one
device; and a reset never takes a device away without saying so.
"""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from tests.test_two_factor import PASSWORD, code_for, enrol, sign_in, signed_in_user_id
from tests.test_two_factor import urlpatterns as urlpatterns
from vantage import twofactor
from vantage.models import TwoFactorDevice, TwoFactorRecoveryCode, VantageLogEntry


@pytest.fixture(autouse=True)
def _mounted(settings):
    settings.ROOT_URLCONF = "tests.test_two_factor_devices"


@pytest.fixture
def boss(db):
    return User.objects.create_superuser("boss", "boss@example.com", PASSWORD)


@pytest.fixture
def clerk(db):
    return User.objects.create_user("clerk", "clerk@example.com", PASSWORD, is_staff=True)


def add_second(user, name="Spare"):
    """A second confirmed device, the way the add flow would leave it."""
    device = twofactor.add_device(user, None, name)
    device.confirmed_at = timezone.now()
    device.save()
    return device


@pytest.fixture
def two(boss):
    first, codes = enrol(boss)
    return first, add_second(boss), codes


@pytest.fixture
def verified(client, boss, two):
    first, second, codes = two
    client.force_login(boss)
    client.post(reverse("vantage:two_factor_verify"), {"code": code_for(first)})
    return first, second, codes


class TestSignInWithSeveralDevices:
    def test_a_code_from_either_device_signs_in(self, boss, two):
        first, second, _ = two
        for device in (first, second):
            client = Client()
            sign_in(client, boss)
            response = client.post(reverse("login_two_factor"), {"code": code_for(device)})
            assert response.status_code == 302
            assert signed_in_user_id(client) == str(boss.pk)

    def test_the_session_names_the_device_that_passed(self, boss, two):
        _, second, _ = two
        client = Client()
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": code_for(second)})
        assert client.session[twofactor.SESSION_VERIFIED]["tag"] == second.tag

    def test_a_code_for_no_device_is_refused(self, boss, two):
        client = Client()
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": "000000"})
        assert signed_in_user_id(client) is None

    def test_a_removed_device_is_refused(self, boss, two):
        first, second, _ = two
        code = code_for(second)
        twofactor.remove_device(None, second)
        client = Client()
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": code})
        assert signed_in_user_id(client) is None
        assert twofactor.device_for(boss).pk == first.pk

    def test_removing_a_device_ends_sessions_verified_against_it(self, client, boss, two):
        _first, second, _ = two
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(second)})
        assert client.get(reverse("vantage:dashboard")).status_code == 200
        twofactor.remove_device(None, second)
        session = client.session
        session.pop(twofactor.SESSION_STATE, None)  # the cached answer is allowed to age
        session.save()
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_a_session_verified_against_another_device_survives(self, client, boss, verified):
        _first, second, _ = verified
        twofactor.remove_device(None, second)
        session = client.session
        session.pop(twofactor.SESSION_STATE, None)
        session.save()
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_a_user_with_no_device_has_it_off(self, client, boss):
        response = sign_in(client, boss)
        assert response["Location"] != reverse("login_two_factor")
        assert signed_in_user_id(client) == str(boss.pk)
        assert twofactor.requirement(client.get(reverse("vantage:dashboard")).wsgi_request) is None


class TestPerDeviceSafety:
    def test_a_replayed_code_is_refused_per_device(self, boss, two):
        first, _second, _ = two
        code = code_for(first)
        assert twofactor.verify_any(boss, code)[0]
        outcome, device = twofactor.verify_any(boss, code)
        assert not outcome
        assert outcome.reason == "reused"
        assert device is None

    def test_one_devices_used_step_does_not_burn_the_others(self, boss, two):
        first, second, _ = two
        assert twofactor.verify_any(boss, code_for(first))[0]
        outcome, device = twofactor.verify_any(boss, code_for(second))
        assert outcome
        assert device.pk == second.pk

    def test_a_wrong_code_counts_on_every_device(self, boss, two):
        first, second, _ = two
        assert not twofactor.verify_any(boss, "000000")[0]
        assert [TwoFactorDevice.objects.get(pk=d.pk).failed_attempts for d in (first, second)] == [
            2,
            2,
        ]  # one answer, weighted by the two devices it was compared against

    def test_guessing_gets_no_more_tries_with_more_devices(self, boss, two, settings):
        limit = twofactor.setting("TWO_FACTOR_MAX_ATTEMPTS")
        outcomes = [twofactor.verify_any(boss, "000000")[0] for _ in range(limit)]
        assert not any(outcomes)
        last = twofactor.verify_any(boss, "000000")[0]
        assert last.reason == "locked"
        assert all(d.locked_until for d in TwoFactorDevice.objects.filter(user=boss))

    def test_a_locked_device_does_not_lock_the_other(self, boss, two):
        """The chosen behaviour: a lock is per device, so the other still answers."""
        first, second, _ = two
        TwoFactorDevice.objects.filter(pk=first.pk).update(
            locked_until=timezone.now() + timezone.timedelta(hours=1)
        )
        assert not twofactor.verify(first, code_for(first))
        outcome, device = twofactor.verify_any(boss, code_for(second))
        assert outcome
        assert device.pk == second.pk

    def test_a_locked_device_is_not_compared_against(self, boss, two):
        first, _second, _ = two
        TwoFactorDevice.objects.filter(pk=first.pk).update(
            locked_until=timezone.now() + timezone.timedelta(hours=1)
        )
        # The locked device's right code is refused even through verify_any.
        assert not twofactor.verify_any(boss, code_for(first))[0]

    def test_every_device_locked_is_a_lockout(self, boss, two):
        until = timezone.now() + timezone.timedelta(hours=1)
        TwoFactorDevice.objects.filter(user=boss).update(locked_until=until)
        outcome, _ = twofactor.verify_any(boss, "000000")
        assert outcome.reason == "locked"

    def test_each_secret_is_sealed_to_its_own_device(self, boss, two):
        first, second, _ = two
        TwoFactorDevice.objects.filter(pk=second.pk).update(
            secret=TwoFactorDevice.objects.get(pk=first.pk).secret
        )
        assert twofactor.device_key(TwoFactorDevice.objects.get(pk=second.pk)) is None

    def test_the_cap_holds(self, boss, two):
        for number in range(twofactor.MAX_DEVICES - 2):
            add_second(boss, f"Extra {number}")
        with pytest.raises(ValueError):
            twofactor.add_device(boss)

    def test_a_second_device_cannot_be_started_as_a_first(self, boss, two):
        with pytest.raises(ValueError):
            twofactor.start_enrolment(boss)

    def test_names_are_cleaned_and_default(self, boss):
        enrol(boss)
        assert twofactor.add_device(boss, None, "   ").name == "Authenticator"
        assert twofactor.add_device(boss, None, "A\n  B" + "x" * 100).name.startswith("A B")
        assert len(twofactor.add_device(boss, None, "x" * 100).name) == 64


class TestAccountPage:
    URL = "vantage:two_factor"

    def test_it_lists_the_devices(self, client, boss, verified):
        page = client.get(reverse(self.URL)).content.decode()
        assert "Authenticator" in page
        assert "Spare" in page

    def test_adding_needs_proof(self, client, boss, verified):
        client.post(reverse(self.URL), {"action": "start", "name": "Tablet"})
        assert TwoFactorDevice.objects.filter(user=boss).count() == 2
        client.post(reverse(self.URL), {"action": "start", "name": "Tablet", "password": "wrong"})
        assert TwoFactorDevice.objects.filter(user=boss).count() == 2

    def test_adding_with_proof_then_confirming(self, client, boss, verified):
        _first, _, codes = verified
        client.post(reverse(self.URL), {"action": "start", "name": "Tablet", "password": PASSWORD})
        pending = TwoFactorDevice.objects.get(user=boss, confirmed_at__isnull=True)
        assert pending.name == "Tablet"
        # An unscanned device does not sign anyone in.
        assert len(twofactor.devices_for(boss)) == 2
        page = client.get(reverse(self.URL)).content.decode()
        assert "Add Tablet" in page
        response = client.post(reverse(self.URL), {"action": "confirm", "code": code_for(pending)})
        assert "Save your recovery codes" not in response.content.decode()
        assert len(twofactor.devices_for(boss)) == 3
        assert VantageLogEntry.objects.filter(action_name="two_factor.device_added").exists()
        assert VantageLogEntry.objects.filter(action_name="two_factor.device_started").exists()
        # Their recovery codes are untouched.
        assert twofactor.verify_any(boss, codes[0])[0]

    def test_the_secret_is_not_shown_again_after_confirmation(self, client, boss, verified):
        first, second, _ = verified
        page = client.get(reverse(self.URL)).content.decode()
        for device in (first, second):
            key = twofactor.b32(twofactor.device_key(TwoFactorDevice.objects.get(pk=device.pk)))
            assert key not in page
            assert " ".join(key[i : i + 4] for i in range(0, len(key), 4)) not in page

    def test_removing_needs_proof(self, client, boss, verified):
        _, second, _ = verified
        client.post(reverse(self.URL), {"action": "remove", "device": second.pk})
        assert len(twofactor.devices_for(boss)) == 2
        client.post(reverse(self.URL), {"action": "remove", "device": second.pk, "password": "no"})
        assert len(twofactor.devices_for(boss)) == 2

    def test_removing_with_the_password(self, client, boss, verified):
        _, second, _ = verified
        client.post(
            reverse(self.URL), {"action": "remove", "device": second.pk, "password": PASSWORD}
        )
        assert [d.name for d in twofactor.devices_for(boss)] == ["Authenticator"]
        assert VantageLogEntry.objects.filter(action_name="two_factor.device_removed").exists()
        assert not VantageLogEntry.objects.filter(action_name="two_factor.disabled").exists()

    def test_removing_with_a_current_code(self, client, boss, verified):
        first, second, _ = verified
        client.post(
            reverse(self.URL),
            {"action": "remove", "device": second.pk, "code": code_for(first, 1)},
        )
        assert len(twofactor.devices_for(boss)) == 1

    def test_removing_the_last_one_switches_it_off_and_clears_the_codes(
        self, client, boss, verified
    ):
        first, second, _ = verified
        twofactor.remove_device(None, second)
        client.post(
            reverse(self.URL), {"action": "remove", "device": first.pk, "password": PASSWORD}
        )
        assert twofactor.device_for(boss) is None
        assert not TwoFactorRecoveryCode.objects.filter(user=boss).exists()
        assert VantageLogEntry.objects.filter(action_name="two_factor.disabled").exists()
        fresh = Client()
        sign_in(fresh, boss)
        assert signed_in_user_id(fresh) == str(boss.pk)

    def test_someone_elses_device_cannot_be_removed(self, client, boss, clerk, verified):
        theirs, _ = enrol(clerk)
        client.post(
            reverse(self.URL), {"action": "remove", "device": theirs.pk, "password": PASSWORD}
        )
        assert TwoFactorDevice.objects.filter(pk=theirs.pk).exists()

    def test_a_wrong_proof_counts_toward_the_lock(self, client, boss, verified):
        _, second, _ = verified
        client.post(reverse(self.URL), {"action": "remove", "device": second.pk, "password": "no"})
        assert [d.failed_attempts for d in TwoFactorDevice.objects.filter(user=boss)] == [2, 2]

    def test_switching_off_removes_every_device(self, client, boss, verified):
        client.post(reverse(self.URL), {"action": "disable", "password": PASSWORD})
        assert not TwoFactorDevice.objects.filter(user=boss).exists()
        assert not TwoFactorRecoveryCode.objects.filter(user=boss).exists()

    def test_new_views_are_csrf_protected(self, boss, verified):
        _, second, _ = verified
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(boss)
        response = strict.post(reverse(self.URL), {"action": "remove", "device": second.pk})
        assert response.status_code == 403
        assert len(twofactor.devices_for(boss)) == 2


class TestRecoveryCodes:
    def test_they_belong_to_the_person_not_a_device(self, boss, two):
        first, second, codes = two
        twofactor.remove_device(None, first)
        outcome, device = twofactor.verify_any(boss, codes[0])
        assert outcome.reason == "recovery"
        assert device.pk == second.pk
        # Single use.
        assert not twofactor.verify_any(boss, codes[0])[0]

    def test_they_are_stored_hashed_per_user(self, boss, two):
        _, _, codes = two
        stored = set(
            TwoFactorRecoveryCode.objects.filter(user=boss).values_list("code_hash", flat=True)
        )
        assert stored == {twofactor.hash_recovery_code(c) for c in codes}
        assert not any(c.replace("-", "") in h for c in codes for h in stored)

    def test_a_recovery_code_signs_in(self, boss, two):
        _, _, codes = two
        client = Client()
        sign_in(client, boss)
        client.post(reverse("login_two_factor"), {"code": codes[0]})
        assert signed_in_user_id(client) == str(boss.pk)

    def test_one_persons_code_is_no_good_for_another(self, boss, clerk, two):
        _, _, codes = two
        enrol(clerk)
        assert not twofactor.verify_any(clerk, codes[0])[0]


class TestReset:
    def test_resetting_one_device_leaves_the_others(self, client, boss, clerk):
        enrol(clerk)
        spare = add_second(clerk)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_users"), {"user": clerk.pk, "device": spare.pk})
        assert [d.name for d in twofactor.devices_for(clerk)] == ["Authenticator"]
        assert TwoFactorRecoveryCode.objects.filter(user=clerk).exists()
        assert VantageLogEntry.objects.filter(action_name="two_factor.reset").exists()

    def test_resetting_the_person_removes_all_and_says_how_many(self, client, boss, clerk):
        enrol(clerk)
        add_second(clerk)
        client.force_login(boss)
        response = client.post(reverse("vantage:two_factor_users"), {"user": clerk.pk}, follow=True)
        assert twofactor.device_for(clerk) is None
        assert not TwoFactorRecoveryCode.objects.filter(user=clerk).exists()
        assert "2 device(s) removed" in response.content.decode()

    def test_the_page_lists_each_device(self, client, boss, clerk):
        enrol(clerk)
        add_second(clerk, "Spare phone")
        client.force_login(boss)
        page = client.get(reverse("vantage:two_factor_users")).content.decode()
        assert "Spare phone" in page
        assert "Reset all 2" in page

    def test_another_persons_device_id_resets_nothing(self, client, boss, clerk):
        enrol(clerk)
        mine, _ = enrol(boss)
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_users"), {"user": clerk.pk, "device": mine.pk})
        assert twofactor.device_for(clerk) is not None
        assert TwoFactorDevice.objects.filter(pk=mine.pk).exists()

    def test_the_management_command_removes_every_device(self, boss, clerk):
        from django.core.management import call_command

        enrol(clerk)
        add_second(clerk)
        call_command("vantage_two_factor_reset", "clerk")
        assert not TwoFactorDevice.objects.filter(user=clerk).exists()


@pytest.mark.django_db(transaction=True)
class TestMigration:
    def test_an_existing_device_and_its_codes_carry_over(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("vantage", "0022_two_factor_lockouts")])
        old = executor.loader.project_state([("vantage", "0022_two_factor_lockouts")]).apps
        try:
            user = old.get_model("auth", "User").objects.create(username="legacy")
            device = old.get_model("vantage", "TwoFactorDevice").objects.create(
                user_id=user.pk, secret="sealed", tag="a" * 32, confirmed_at=timezone.now()
            )
            old.get_model("vantage", "TwoFactorRecoveryCode").objects.create(
                device_id=device.pk, code_hash="h" * 64
            )

            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())

            kept = TwoFactorDevice.objects.get(tag="a" * 32)
            assert kept.user_id == user.pk
            assert kept.name == "Authenticator"
            assert kept.confirmed_at is not None
            code = TwoFactorRecoveryCode.objects.get()
            assert code.user_id == user.pk
            assert code.code_hash == "h" * 64
            # And a second device is now possible.
            TwoFactorDevice.objects.create(user_id=user.pk, secret="s2", tag="b" * 32, name="Spare")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
class TestMigrationReverse:
    def test_migrating_back_gives_each_code_its_owners_device(self):
        user = User.objects.create(username="rev")
        device = TwoFactorDevice.objects.create(
            user_id=user.pk, secret="sealed", tag="c" * 32, confirmed_at=timezone.now()
        )
        TwoFactorRecoveryCode.objects.create(user_id=user.pk, code_hash="k" * 64)
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([("vantage", "0023_two_factor_many_devices")])
            old = executor.loader.project_state([("vantage", "0023_two_factor_many_devices")]).apps
            code = old.get_model("vantage", "TwoFactorRecoveryCode").objects.get()
            assert code.device_id == device.pk
            assert code.user_id == user.pk
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        assert TwoFactorRecoveryCode.objects.get().user_id == user.pk


class TestSeveralDevicesLockout:
    @pytest.mark.parametrize("count", [1, 2, 3, 5])
    def test_lockout_after_ceil_max_over_devices(self, boss, count):
        enrol(boss)
        for number in range(count - 1):
            add_second(boss, f"D{number}")
        limit = twofactor.setting("TWO_FACTOR_MAX_ATTEMPTS")
        allowed = -(-limit // count)
        for _ in range(allowed - 1):
            assert twofactor.verify_any(boss, "000000")[0].reason == "invalid"
        assert twofactor.verify_any(boss, "000000")[0].reason == "locked"
        assert all(d.locked_until for d in TwoFactorDevice.objects.filter(user=boss))


class TestRemovalIsImmediate:
    def test_a_session_verified_with_a_removed_device_is_regated_at_once(self, boss, two):
        first, second, _ = two
        one = Client()
        one.force_login(boss)
        one.post(reverse("vantage:two_factor_verify"), {"code": code_for(second)})
        dashboard = reverse("vantage:dashboard")
        assert one.get(dashboard).status_code == 200  # state now cached in the session
        other = Client()
        other.force_login(boss)
        other.post(reverse("vantage:two_factor_verify"), {"code": code_for(first)})
        twofactor.remove_device(None, second)  # as if from the other session
        assert one.get(dashboard).status_code == 302

    def test_disabling_regates_other_sessions_at_once(self, boss, two):
        first, _second, _ = two
        one = Client()
        one.force_login(boss)
        one.post(reverse("vantage:two_factor_verify"), {"code": code_for(first)})
        dashboard = reverse("vantage:dashboard")
        assert one.get(dashboard).status_code == 200
        TwoFactorDevice.objects.filter(pk=first.pk).delete()
        # Another device remains, but the one this session used is gone.
        assert one.get(dashboard).status_code == 302


class TestPendingExpires:
    def test_an_old_pending_device_is_dropped(self, boss):
        enrol(boss)
        pending = twofactor.add_device(boss, None, "New")
        assert twofactor.device_for(boss, confirmed=False).pk == pending.pk
        TwoFactorDevice.objects.filter(pk=pending.pk).update(
            created_at=timezone.now() - timezone.timedelta(minutes=11)
        )
        assert twofactor.device_for(boss, confirmed=False) is None
        assert not TwoFactorDevice.objects.filter(pk=pending.pk).exists()


class TestRemovingYourOwnSignInDevice:
    """The session that removes the device it signed in with, on a code from another."""

    URL = "vantage:two_factor"

    @staticmethod
    def _age(client):
        session = client.session
        session.pop(twofactor.SESSION_STATE, None)
        session.save()

    def _signed_in_with(self, boss, device):
        client = Client()
        client.force_login(boss)
        client.post(reverse("vantage:two_factor_verify"), {"code": code_for(device)})
        return client

    def test_a_code_from_another_device_keeps_this_session_verified(self, boss, two):
        first, second, _ = two
        client = self._signed_in_with(boss, first)
        client.post(
            reverse(self.URL),
            {"action": "remove", "device": first.pk, "code": code_for(second)},
        )
        assert [d.pk for d in twofactor.devices_for(boss)] == [second.pk]
        assert client.session[twofactor.SESSION_VERIFIED]["tag"] == second.tag
        self._age(client)
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_a_recovery_code_keeps_this_session_verified(self, boss, two):
        first, second, codes = two
        client = self._signed_in_with(boss, first)
        client.post(
            reverse(self.URL),
            {"action": "remove", "device": first.pk, "code": codes[0]},
        )
        assert [d.pk for d in twofactor.devices_for(boss)] == [second.pk]
        self._age(client)
        assert client.get(reverse("vantage:dashboard")).status_code == 200

    def test_the_password_alone_keeps_the_re_gate(self, boss, two):
        first, _second, _ = two
        client = self._signed_in_with(boss, first)
        client.post(
            reverse(self.URL), {"action": "remove", "device": first.pk, "password": PASSWORD}
        )
        assert len(twofactor.devices_for(boss)) == 1
        self._age(client)
        assert client.get(reverse("vantage:dashboard")).status_code == 302

    def test_another_session_verified_with_the_removed_device_stays_re_gated(self, boss, two):
        first, second, _ = two
        client = self._signed_in_with(boss, first)
        other = Client()
        other.force_login(boss)
        # The next step's code, since the first one is spent by the other session.
        other.post(reverse("vantage:two_factor_verify"), {"code": code_for(first, 1)})
        assert other.get(reverse("vantage:dashboard")).status_code == 200
        client.post(
            reverse(self.URL),
            {"action": "remove", "device": first.pk, "code": code_for(second)},
        )
        self._age(other)
        assert other.get(reverse("vantage:dashboard")).status_code == 302


class TestManagePageSections:
    URL = "vantage:two_factor"

    def test_each_action_is_its_own_form_with_its_own_proof(self, client, boss, verified):
        page = client.get(reverse(self.URL)).content.decode()
        assert page.count('name="action" value="remove"') == 2
        for action in ("start", "codes", "disable"):
            assert page.count(f'name="action" value="{action}"') == 1
        assert "Device to remove" not in page
        assert page.count('name="password"') == 5

    def test_a_refused_proof_is_reported_in_its_own_section(self, client, boss, verified):
        first, _second, _ = verified
        page = client.post(
            reverse(self.URL), {"action": "remove", "device": first.pk, "password": "no"}
        ).content.decode()
        assert page.count('role="alert"') == 1
        assert f'id="v-2fa-remove-{first.pk}-code" maxlength="64"' in page
        assert "aria-invalid" in page

    def test_the_second_device_text_does_not_ask_for_the_account_name(self, client, boss, verified):
        client.post(reverse(self.URL), {"action": "start", "password": PASSWORD, "name": "Spare2"})
        page = client.get(reverse(self.URL)).content.decode()
        assert "name it" not in page
        assert "Confirm and add device" in page
