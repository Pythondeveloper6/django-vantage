"""Middleware: ``LoginThrottleMiddleware`` and ``PanelSecurityMiddleware`` — see
``vantage/middleware.py``.

``tests/test_security.py`` already drives both classes through a real, hardened
middleware stack end to end — brute-force throttling, CSP nonces, response headers,
per-surface rate limits and upload screening are all covered there, with the login form
and the panel mounted side by side under one urlconf. This file does not repeat any of
that. It covers what is left:

* **Scoping.** Both middleware claim to act on specific requests and leave everything
  else untouched — ``PanelSecurityMiddleware`` on the panel's own views,
  ``LoginThrottleMiddleware`` on whatever resolves to a Django ``LoginView`` regardless
  of whether it is part of the panel at all. Proved here against a plain, ordinary view
  that is neither a login form nor a panel URL, mounted in the same urlconf — at the
  ``process_view``/``__call__`` level directly, and through a real request.
* **Degradation.** The module promises to fail open rather than fail loud when its cache
  is unreachable, and to survive a request that never passed through
  ``AuthenticationMiddleware`` (and therefore never through ``SessionMiddleware``
  either) without raising. Both are exercised directly.
* **Signal safety.** ``_connect_signals`` is documented as idempotent so that a process
  running WSGI and ASGI handlers, or a test that rebuilds the middleware stack, does not
  silently halve the throttle's budget by counting every failure twice.
* **A genuine bug**, found while writing the above and since fixed: every setting
  :mod:`vantage.security` documents for these two middleware — ``LOGIN_THROTTLE_LIMIT``,
  ``RATE_LIMIT``, ``UPLOAD_MAX_BYTES`` and the rest — was unregistered in
  ``vantage.conf.DEFAULTS``, and that registry rejects unknown keys on purpose. So
  configuring one the only way the module documents, ``VANTAGE =
  {"LOGIN_THROTTLE_LIMIT": 3}``, raised ``ImproperlyConfigured`` the next time anything
  read any setting — which nearly every panel page does. Following the documentation
  took the site down, several requests later, pointing at an unrelated key.

  They are registered now, but read from the raw dict rather than through the settings
  cascade: the cascade consults the database, and the login throttle must answer
  without one while somebody is hammering the form. It also keeps these out of the
  panel's own settings screen, because a UI that can switch off rate limiting is a UI
  that can be used to switch off rate limiting.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import include, path, reverse

from vantage import middleware, security, site
from vantage.middleware import LoginThrottleMiddleware, PanelSecurityMiddleware


def outside_view(request):
    """An ordinary view: not a login form, not part of the panel."""
    return HttpResponse("outside ok")


urlpatterns = [
    path("outside/", outside_view, name="outside"),
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.test_middleware")]

HARDENED = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "vantage.middleware.LoginThrottleMiddleware",
    "vantage.middleware.PanelSecurityMiddleware",
]

LOGIN = "/accounts/login/"


@pytest.fixture(autouse=True)
def _clean_cache():
    """Counters live in the cache, so a leaked one is a test failing in another file."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def hardened(settings):
    settings.MIDDLEWARE = HARDENED
    return settings


@pytest.fixture
def boss(client, db, hardened):
    client.force_login(User.objects.create_superuser("boss", "boss@example.com", "x"))
    return client


def resolver_match(url_name, app_names=("vantage",)):
    return type("M", (), {"url_name": url_name, "app_names": list(app_names)})()


# ── scoping: the panel's own views, and nowhere else ─────────────────────────


class TestPanelSecurityScopingThroughARealRequest:
    """``/outside/`` resolves to no app namespace at all — the shape of every URL a
    project owns that is not this package's."""

    @pytest.fixture
    def tiny_export_budget(self):
        security.DEFAULTS["RATE_LIMITS"] = {"export": (2, 60)}
        yield
        security.DEFAULTS["RATE_LIMITS"] = None

    def test_rate_limiting_never_touches_a_plain_view(
        self, client, hardened, tiny_export_budget
    ):
        for _ in range(5):
            assert client.get("/outside/", {"export": "csv"}).status_code == 200

    def test_headers_never_appear_on_a_plain_view(self, client, hardened):
        response = client.get("/outside/")
        assert "X-Frame-Options" not in response
        assert "X-Robots-Tag" not in response

    def test_uploads_are_never_screened_on_a_plain_view(self, client, hardened):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = client.post(
            "/outside/",
            {"file": SimpleUploadedFile("x.png", b"<html><script>steal()</script></html>")},
        )
        assert response.status_code == 200


class TestLoginThrottleScopingIsDifferentOnPurpose:
    """The module docstring is explicit that the two middleware disagree about what
    counts as "covered": the throttle follows the view class, not the panel's own
    urlconf, so it protects a login view the panel never mounted, and stays out of a
    POST view that merely shares a field name with a login form."""

    def test_a_non_login_post_view_is_never_throttled(self, client, hardened):
        security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 1
        try:
            for _ in range(5):
                response = client.post(
                    "/outside/", {"username": "ada", "password": "wrong"}
                )
                assert response.status_code == 200
        finally:
            security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 10

    def test_it_still_protects_a_login_view_outside_the_panel_headers_scope(
        self, client, hardened, db
    ):
        """``/accounts/login/`` gets no ``X-Frame-Options`` (see
        ``TestHeadersOnPanelResponses`` in ``test_security.py``) — proof it is not
        ``is_panel_request`` — yet the throttle still counts against it."""
        User.objects.create_user("ada", "ada@example.com", "correct-horse")
        security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 2
        try:
            for _ in range(2):
                client.post(LOGIN, {"username": "ada", "password": "wrong"})
            assert client.post(LOGIN, {"username": "ada", "password": "wrong"}).status_code == 429
        finally:
            security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 10


class TestScopingAtTheMiddlewareLevel:
    """The same guarantees, isolated from the URL resolver and the database: hand-built
    requests and a spy in place of ``get_response``."""

    def test_process_view_short_circuits_for_a_non_panel_resolver_match(self, monkeypatch):
        mw = PanelSecurityMiddleware(lambda r: HttpResponse())
        request = RequestFactory().get("/")
        request.resolver_match = resolver_match("index", app_names=["shop"])

        def boom(*args, **kwargs):
            raise AssertionError("must not run for a request outside the panel")

        monkeypatch.setattr(mw, "check_rate_limit", boom)
        monkeypatch.setattr(mw, "check_uploads", boom)
        assert mw.process_view(request, None, [], {}) is None

    def test_call_applies_headers_only_to_a_panel_response(self):
        def get_response(request):
            request.resolver_match = resolver_match("dashboard")
            return HttpResponse()

        response = PanelSecurityMiddleware(get_response)(RequestFactory().get("/"))
        assert response["X-Frame-Options"] == "DENY"

    def test_call_skips_headers_for_a_request_that_never_resolved(self):
        """The shape of a 404: the view never ran, so ``resolver_match`` is never set."""
        mw = PanelSecurityMiddleware(lambda r: HttpResponse())
        response = mw(RequestFactory().get("/"))
        assert "X-Frame-Options" not in response


# ── degradation: no cache, no authenticated user ─────────────────────────────


class TestDegradesWithoutACache:
    """The design says a cache outage is a budget outage, not a login outage or a panel
    outage. Proved here with an alias that is not configured at all, which is what a
    deployment actually does wrong — a typo'd ``CACHES`` alias, not a cache that answers
    with an error on every call.
    """

    def test_rate_limiting_fails_open_when_its_cache_alias_does_not_exist(self, boss):
        security.DEFAULTS["RATE_LIMIT_CACHE"] = "does-not-exist-at-all"
        try:
            url = reverse("vantage:autocomplete")
            for _ in range(3):
                response = boss.get(url, {"model": "testapp.Author"})
                assert response.status_code == 200
        finally:
            security.DEFAULTS["RATE_LIMIT_CACHE"] = "default"

    def test_login_throttle_fails_open_when_its_cache_alias_does_not_exist(
        self, client, hardened, db
    ):
        User.objects.create_user("ada", "ada@example.com", "correct-horse")
        security.DEFAULTS["LOGIN_THROTTLE_CACHE"] = "does-not-exist-at-all"
        security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 2
        try:
            for _ in range(5):
                response = client.post(LOGIN, {"username": "ada", "password": "wrong"})
                # Never a 429: with nowhere to count, nothing is ever blocked. Still a
                # plain, ordinary refusal from the login form itself, not a 500.
                assert response.status_code == 200
        finally:
            security.DEFAULTS["LOGIN_THROTTLE_CACHE"] = "default"
            security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 10


class TestDegradesWithoutAnAuthenticatedUser:
    """What a request looks like when ``SessionMiddleware`` (and therefore
    ``AuthenticationMiddleware``) never ran: ``request.user`` was never attached at
    all — not even as ``AnonymousUser`` — because nothing in the stack set it."""

    def test_check_rate_limit_tolerates_a_missing_user_attribute(self):
        mw = PanelSecurityMiddleware(lambda r: HttpResponse())
        request = RequestFactory().get("/", {"model": "testapp.Author"})
        request.resolver_match = resolver_match("autocomplete")
        assert not hasattr(request, "user")

        assert mw.check_rate_limit(request) is None  # first request: not blocked
        assert security.actor_for(request) == "addr:127.0.0.1"

    def test_check_uploads_tolerates_a_missing_user_attribute(self):
        """Upload screening never reads ``request.user`` at all; a request with no user
        attached must be screened exactly as any other request is."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        mw = PanelSecurityMiddleware(lambda r: HttpResponse())
        request = RequestFactory().post(
            "/", {"logo": SimpleUploadedFile("x.png", b"<html>")}
        )
        assert not hasattr(request, "user")
        response = mw.check_uploads(request)
        assert response is not None
        assert response.status_code == 400


# ── signal safety ─────────────────────────────────────────────────────────────


class TestSignalIdempotency:
    def test_instantiating_the_middleware_twice_does_not_double_count_a_failure(
        self, monkeypatch
    ):
        from django.contrib.auth.signals import user_login_failed

        monkeypatch.setattr(middleware, "_connected", False)
        LoginThrottleMiddleware(lambda r: HttpResponse())
        LoginThrottleMiddleware(lambda r: HttpResponse())

        request = RequestFactory().post("/")
        setattr(request, middleware.REQUEST_ATTR, ("ada", "1.2.3.4"))
        user_login_failed.send(sender=None, credentials={}, request=request)

        throttle = security.LoginThrottle(limit=5, window=60)
        assert throttle.state("ada", "1.2.3.4").failures == 1


# ── a genuine bug: the documented settings cannot actually be set ───────────


class TestSecuritySettingsAreConfigurable:
    """The nine keys documented on ``vantage.security.DEFAULTS`` are really settable.

    They were not. ``_setting()`` forwards to ``vantage_settings`` only for keys that
    ``vantage.conf.DEFAULTS`` registers, and none of these were registered — while the
    module documented every one of them, with a rationale, as something a project sets
    through ``VANTAGE = {...}``.

    Writing one in did not fail quietly, and did not fail on the setting itself. The
    settings layer validates the *whole* dict against the *whole* registry on the next
    read, so it raised ``ImproperlyConfigured`` the next time anything touched any
    setting — which is nearly every page render. Following the documentation took the
    site down, several requests later, pointing at an unrelated key.
    """

    @pytest.mark.parametrize(
        "key,value",
        [
            ("LOGIN_THROTTLE", False),
            ("LOGIN_THROTTLE_LIMIT", 3),
            ("LOGIN_THROTTLE_WINDOW", 60),
            ("LOGIN_THROTTLE_CACHE", "default"),
            ("LOGIN_THROTTLE_IP_HEADER", "HTTP_X_FORWARDED_FOR"),
            ("RATE_LIMIT", False),
            ("RATE_LIMIT_CACHE", "default"),
            ("RATE_LIMITS", {"export": (1, 1)}),
            ("UPLOAD_MAX_BYTES", 100),
        ],
    )
    def test_writing_it_the_documented_way_is_accepted_and_read_back(
        self, settings, key, value
    ):
        from vantage import security
        from vantage.conf import vantage_settings

        settings.VANTAGE = {key: value}
        assert vantage_settings.SITE_TITLE  # an unrelated setting still resolves
        assert security._setting(key) == value

    def test_an_unset_key_still_falls_back_to_the_modules_own_default(self, settings):
        """conf registers these as None so the reasoning stays in one file."""
        from vantage import security

        settings.VANTAGE = {}
        assert security._setting("LOGIN_THROTTLE_LIMIT") == security.DEFAULTS[
            "LOGIN_THROTTLE_LIMIT"
        ]

    def test_a_real_panel_page_renders_with_one_set(self, boss, settings):
        settings.VANTAGE = {"RATE_LIMIT": False}
        assert boss.get(reverse("vantage:dashboard")).status_code == 200
