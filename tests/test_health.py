"""System diagnostics.

A health page that 500s when something is unhealthy is worse than none, so most of
these tests are about probes failing safely.
"""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from vantage import health
from vantage.health import Check, _safe, run_checks


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


class TestProbes:
    def test_every_probe_returns_a_check(self, db):
        results = run_checks()
        assert len(results) == len(health.PROBES)
        assert all(isinstance(c, Check) for c in results)

    def test_statuses_are_from_the_known_set(self, db):
        assert all(c.status in {"ok", "warn", "fail", "info"} for c in run_checks())

    def test_migrations_are_reported_applied_in_a_migrated_test_database(self, db):
        check = next(c for c in run_checks() if c.label == "Migrations")
        assert check.status == "ok"

    def test_the_database_probe_names_the_backend(self, db):
        check = next(c for c in run_checks() if c.label == "Database")
        assert check.status == "ok"
        assert "sqlite" in check.detail

    def test_the_cache_probe_round_trips_a_value(self, db):
        assert next(c for c in run_checks() if c.label == "Cache").status == "ok"

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    )
    def test_a_dummy_cache_is_a_warning_not_a_pass(self, db):
        """It accepts writes and returns nothing, so a naive probe would call it fine."""
        check = next(c for c in run_checks() if c.label == "Cache")
        assert check.status == "warn"

    @override_settings(DEBUG=True)
    def test_debug_on_is_flagged(self, db):
        assert next(c for c in run_checks() if c.label == "Debug mode").status == "warn"

    @override_settings(DEBUG=False)
    def test_debug_off_passes(self, db):
        assert next(c for c in run_checks() if c.label == "Debug mode").status == "ok"

    @override_settings(MEDIA_ROOT="")
    def test_missing_media_root_is_flagged_because_uploads_would_fail(self, db):
        assert next(c for c in run_checks() if c.label == "Media storage").status == "warn"


class TestFailureIsolation:
    def test_a_raising_probe_becomes_a_failed_check(self):
        """The whole point: an unhealthy system must still render its own diagnosis."""

        def explode():
            raise RuntimeError("no connection")

        check = _safe("Database", explode)
        assert check.status == "fail"
        assert "no connection" in check.detail
        assert check.label == "Database"

    def test_a_raising_probe_does_not_propagate(self, monkeypatch, db):
        def explode():
            raise OSError("disk gone")

        monkeypatch.setattr(health, "PROBES", (("Database", explode),))
        results = run_checks()
        assert len(results) == 1
        assert results[0].status == "fail"


class TestPage:
    def test_it_renders(self, boss):
        assert boss.get(reverse("vantage:page-health")).status_code == 200

    def test_it_lists_every_check(self, boss):
        response = boss.get(reverse("vantage:page-health"))
        assert len(response.context["checks"]) == len(health.PROBES)

    def test_the_worst_status_summarises_the_page(self, boss):
        assert boss.get(reverse("vantage:page-health")).context["worst"] in {
            "ok",
            "warn",
            "fail",
        }

    def test_status_is_available_to_a_screen_reader_not_only_as_colour(self, boss):
        body = boss.get(reverse("vantage:page-health")).content.decode()
        assert "Status:" in body

    def test_a_non_superuser_is_refused(self, client, db):
        """The detail is deployment information: paths, versions, backends."""
        client.force_login(User.objects.create_user("plain", password="x", is_staff=True))
        assert client.get(reverse("vantage:page-health")).status_code == 403

    def test_it_is_not_offered_in_the_sidebar_to_a_non_superuser(self, client, db):
        client.force_login(User.objects.create_user("plain2", password="x", is_staff=True))
        body = client.get(reverse("vantage:dashboard")).content.decode()
        assert reverse("vantage:page-health") not in body
