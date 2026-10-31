"""Identity-provider groups mapped onto roles (``VANTAGE["ROLE_MAPPING"]``)."""

import pytest
from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from vantage import identity
from vantage.conf import vantage_settings
from vantage.models import VantageLogEntry

pytestmark = pytest.mark.django_db

MAPPING = {
    "ROLE_MAPPING": {
        "MAP": {"idp-finance": "Finance", "idp-admins": ["Support", "Finance"]},
        "SET_STAFF": True,
    }
}


@pytest.fixture(autouse=True)
def roles():
    for name in ("Finance", "Support", "Hand-granted"):
        Group.objects.create(name=name)
    vantage_settings.reset()
    yield
    vantage_settings.reset()


@pytest.fixture
def person():
    return User.objects.create_user("pat", "pat@example.com", "x")


def role_names(user):
    return sorted(user.groups.values_list("name", flat=True))


@pytest.fixture
def mapped(settings):
    settings.VANTAGE = MAPPING
    vantage_settings.reset()


@pytest.mark.usefixtures("mapped")
class TestSync:
    def test_mapped_groups_grant_roles_and_staff(self, person):
        added, removed = identity.sync_roles(person, ["idp-admins", "unrelated"])
        assert (added, removed) == (["Finance", "Support"], [])
        assert role_names(person) == ["Finance", "Support"]
        person.refresh_from_db()
        assert person.is_staff

    def test_a_group_no_longer_asserted_removes_its_role(self, person):
        identity.sync_roles(person, ["idp-admins"])
        added, removed = identity.sync_roles(person, ["idp-finance"])
        assert (added, removed) == ([], ["Support"])
        assert role_names(person) == ["Finance"]

    def test_leaving_every_group_removes_roles_and_staff(self, person):
        identity.sync_roles(person, ["idp-admins"])
        identity.sync_roles(person, [])
        person.refresh_from_db()
        assert role_names(person) == []
        assert not person.is_staff

    def test_unmanaged_roles_are_left_alone(self, person):
        person.groups.add(Group.objects.get(name="Hand-granted"))
        identity.sync_roles(person, [])
        assert role_names(person) == ["Hand-granted"]

    def test_never_creates_a_role_or_a_superuser(self, person):
        config = identity.resolve_role_mapping({"MAP": {"g": ["Ghost"]}})
        assert identity.sync_roles(person, ["g"], config=config) == ([], [])
        assert not Group.objects.filter(name="Ghost").exists()
        person.refresh_from_db()
        assert not person.is_superuser

    def test_changes_are_audited_and_no_op_writes_nothing(self, person):
        identity.sync_roles(person, ["idp-finance"])
        assert VantageLogEntry.objects.count() == 1
        entry = VantageLogEntry.objects.get()
        assert entry.changes["roles"] == {"from": [], "to": ["Finance"]}
        identity.sync_roles(person, ["idp-finance"])
        assert VantageLogEntry.objects.count() == 1

    def test_a_superuser_keeps_staff(self):
        boss = User.objects.create_superuser("boss", "b@example.com", "x")
        identity.sync_roles(boss, [])
        boss.refresh_from_db()
        assert boss.is_staff


class TestClaims:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (["a", "b"], ["a", "b"]),
            ("a", ["a"]),
            ("a b,c", ["a", "b", "c"]),
            (None, []),
            ({"a": 1}, []),
        ],
    )
    def test_shapes(self, value, expected):
        assert identity.external_groups({"groups": value}, "groups") == expected

    @override_settings(VANTAGE=MAPPING)
    def test_mozilla_mixin_syncs_on_update(self, person):
        class Base:
            def update_user(self, user, claims):
                return user

        class Backend(identity.MozillaOIDCRoleMixin, Base):
            pass

        Backend().update_user(person, {"groups": ["idp-finance"]})
        assert role_names(person) == ["Finance"]

    @override_settings(VANTAGE=MAPPING)
    def test_social_auth_pipeline(self, person):
        assert identity.social_auth_pipeline(user=person, response={"groups": "idp-finance"}) == {}
        assert role_names(person) == ["Finance"]


class TestValidation:
    @pytest.mark.parametrize(
        "value",
        [[], {"NOPE": 1}, {"MAP": []}, {"MAP": {"g": [1]}}, {"CLAIM": ""}],
    )
    def test_malformed_mapping_is_rejected(self, value):
        with pytest.raises(ImproperlyConfigured):
            identity.resolve_role_mapping(value)

    def test_through_settings(self):
        with override_settings(VANTAGE={"ROLE_MAPPING": {"MAPP": {}}}):
            with pytest.raises(ImproperlyConfigured):
                _ = vantage_settings.ROLE_MAPPING

    def test_empty_mapping_manages_nothing(self, person):
        person.groups.add(Group.objects.get(name="Finance"))
        assert identity.sync_roles(person, []) == ([], [])
        assert role_names(person) == ["Finance"]
