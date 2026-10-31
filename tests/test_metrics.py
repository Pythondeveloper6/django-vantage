"""The Prometheus endpoint and slow-request logging."""

import logging

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from vantage import metrics
from vantage.conf import vantage_settings
from vantage.models import VantageJob

pytestmark = pytest.mark.django_db

URL = "vantage:metrics"


@pytest.fixture
def enabled(settings):
    settings.VANTAGE = {"METRICS": True, "METRICS_TOKEN": "s3cret-token"}
    vantage_settings.reset()
    yield
    vantage_settings.reset()


@pytest.fixture(autouse=True)
def fresh_counters():
    metrics._reset_counters()
    yield
    metrics._reset_counters()


class TestEndpoint:
    def test_off_by_default_is_a_404(self, client):
        assert client.get(reverse(URL)).status_code == 404

    def test_needs_the_token(self, client, enabled):
        response = client.get(reverse(URL))
        assert response.status_code == 401
        assert "Bearer" in response["WWW-Authenticate"]
        wrong = client.get(reverse(URL), HTTP_AUTHORIZATION="Bearer nope")
        assert wrong.status_code == 401

    def test_a_staff_member_who_is_not_a_superuser_is_refused(self, client, enabled):
        client.force_login(User.objects.create_user("s", "s@example.com", "x", is_staff=True))
        assert client.get(reverse(URL)).status_code == 401

    def test_a_superuser_may_open_it(self, client, enabled):
        client.force_login(User.objects.create_superuser("b", "b@example.com", "x"))
        assert client.get(reverse(URL)).status_code == 200

    def test_the_exposition_format(self, client, enabled):
        VantageJob.objects.create(status=VantageJob.Status.FAILED, total=0)
        response = client.get(reverse(URL), HTTP_AUTHORIZATION="Bearer s3cret-token")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain; version=0.0.4")
        assert response["Cache-Control"] == "no-store"
        body = response.content.decode()
        assert "# TYPE vantage_jobs gauge" in body
        assert 'vantage_jobs{status="failed"} 1' in body
        assert 'vantage_jobs{status="pending"} 0' in body
        assert "vantage_info{version=" in body


class TestRender:
    def test_labels_are_escaped(self):
        text = metrics.render([("m", "help", "gauge", [({"l": 'a"b\nc'}, 2.0)])])
        assert 'm{l="a\\"b\\nc"} 2' in text

    def test_unlabelled(self):
        assert "m 3\n" in metrics.render([("m", "h", "counter", [({}, 3.0)])])


class TestSlowRequests:
    @pytest.fixture
    def middleware(self, settings):
        settings.MIDDLEWARE = [*settings.MIDDLEWARE, "vantage.metrics.SlowRequestMiddleware"]

    def test_panel_requests_are_counted_with_a_bounded_label(self, client, middleware):
        client.force_login(User.objects.create_superuser("b", "b@example.com", "x"))
        client.get(reverse("vantage:testapp-post-list"))
        samples = {name: values for name, _h, _t, values in metrics.collect()}
        labels = [labels for labels, _ in samples["vantage_process_requests_total"]]
        assert {"view": "list", "status": "2xx"} in labels

    def test_slow_requests_are_logged_with_structured_fields(
        self, client, middleware, settings, caplog
    ):
        settings.VANTAGE = {"SLOW_REQUEST_MS": 0.0001}
        vantage_settings.reset()
        client.force_login(User.objects.create_superuser("b", "b@example.com", "x"))
        with caplog.at_level(logging.WARNING, logger="vantage.requests"):
            client.get(reverse("vantage:dashboard"))
        vantage_settings.reset()
        (record,) = [r for r in caplog.records if r.name == "vantage.requests"]
        assert record.vantage_view == "dashboard"
        assert record.vantage_status == 200
