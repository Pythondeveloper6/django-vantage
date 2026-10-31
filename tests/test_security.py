"""The hardening layer: CSP, response headers, throttling, rate limits, uploads.

Every test here is written so that it fails when the protection is removed rather than
when the implementation is rearranged. That distinction matters more in this module
than anywhere else in the suite: a security test that passes because a mock said so is
worse than no test, because it is on the checklist with a tick next to it.

The module declares its own urlconf. Mounting the sign-in view *and* the panel behind
the real middleware stack is the thing under test — a throttle proved only by calling
its methods is a counter, not a defence.
"""

import io

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.template import Context, Template
from django.test import Client, RequestFactory
from django.urls import include, path, reverse

from vantage import security, site
from vantage.middleware import (
    LoginThrottleMiddleware,
    PanelSecurityMiddleware,
    is_panel_request,
    normalise_username,
    surface_for,
)

urlpatterns = [
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
]

HARDENED = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "vantage.middleware.LoginThrottleMiddleware",
    "vantage.middleware.PanelSecurityMiddleware",
]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _clean_cache():
    """Counters live in the cache, so a leaked one is a test failing in another file."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def hardened(settings):
    settings.ROOT_URLCONF = "tests.test_security"
    settings.MIDDLEWARE = HARDENED
    return settings


@pytest.fixture
def boss(client, db, hardened):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


def rf(**kwargs):
    return RequestFactory().get("/", **kwargs)


def nonce_context(value):
    """The context the real processor would build, not a hand-written string.

    Written this way because a hand-written ``' nonce="x"'`` is a plain ``str`` and the
    template engine escapes it into ``nonce=&quot;x&quot;`` — a test that passed on the
    escaped version would be asserting that the feature is broken.
    """
    request = rf()
    request.csp_nonce = value
    return security.csp_context(request)


# ── Content-Security-Policy ─────────────────────────────────────────────────


class FakeLazyNonce:
    """Django's ``LazyNonce``: falsy until generated, and truthy-looking afterwards.

    Reproduced rather than imported so the test still describes the trap on a Django
    version that spells it differently.
    """

    def __init__(self, value=""):
        self.value = value

    def __bool__(self):
        return bool(self.value)

    def __str__(self):
        return self.value or "generated-on-read"


class TestCspNonce:
    def test_no_nonce_source_is_the_empty_string(self):
        assert security.csp_nonce(rf()) == ""

    def test_reads_the_request_attribute(self):
        request = rf()
        request.csp_nonce = "abc123"
        assert security.csp_nonce(request) == "abc123"

    def test_a_falsy_lazy_nonce_is_still_materialised(self):
        """The documented trap: ``if request.csp_nonce`` reports no nonce on a request
        that has a perfectly good one, silently disabling the whole feature."""
        request = rf()
        request.csp_nonce = FakeLazyNonce()
        assert security.csp_nonce(request) == "generated-on-read"

    def test_a_nonce_object_that_raises_does_not_break_the_page(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        request = rf()
        request.csp_nonce = Hostile()
        assert security.csp_nonce(request) == ""

    def test_none_request_is_tolerated(self):
        assert security.csp_nonce(None) == ""


class TestNonceAttr:
    def test_emits_a_whole_attribute_with_its_leading_space(self):
        request = rf()
        request.csp_nonce = "abc123"
        assert security.nonce_attr(request) == ' nonce="abc123"'

    def test_emits_nothing_when_there_is_no_nonce(self):
        assert security.nonce_attr(rf()) == ""

    def test_the_value_is_escaped(self):
        request = rf()
        request.csp_nonce = '"><script>'
        assert "<script>" not in security.nonce_attr(request)

    def test_the_context_processor_publishes_one_name(self):
        request = rf()
        request.csp_nonce = "abc123"
        assert security.csp_context(request) == {"csp_nonce_attr": ' nonce="abc123"'}

    def test_it_does_not_shadow_djangos_own_csp_nonce_name(self):
        """Django 6.1's own context processor publishes ``csp_nonce``; publishing a
        different type under the same name would be a trap for anyone reading it."""
        assert "csp_nonce" not in security.csp_context(rf())


class TestTheShellCarriesTheNonce:
    """The whole point of the plumbing: the panel's inline blocks must be nonceable."""

    def test_the_base_template_nonces_its_inline_script_and_style(self):
        rendered = Template("{% include 'vantage/base.html' %}").render(
            Context(
                {
                    **nonce_context("N0NCE"),
                    "no_flash_script": "// theme",
                    "theme_css": ":root{}",
                    "settings": {},
                    "nav": [],
                }
            )
        )
        assert '<script nonce="N0NCE">' in rendered
        assert '<style nonce="N0NCE">' in rendered

    @pytest.mark.parametrize("template", ["vantage/login.html", "vantage/logged_out.html"])
    def test_the_auth_pages_nonce_theirs_too(self, template):
        rendered = Template("{% include '" + template + "' %}").render(
            Context(
                {
                    **nonce_context("N0NCE"),
                    "no_flash_script": "// theme",
                    "theme_css": ":root{}",
                    "settings": {},
                    "form": None,
                }
            )
        )
        assert '<script nonce="N0NCE">' in rendered
        assert '<style nonce="N0NCE">' in rendered

    def test_without_a_processor_the_output_is_unchanged(self):
        """A project that has not opted in must get byte-identical HTML."""
        rendered = Template("{% include 'vantage/base.html' %}").render(
            Context({"no_flash_script": "// theme", "theme_css": ":root{}", "settings": {}})
        )
        assert "<script>" in rendered
        assert "nonce" not in rendered


class TestInlineScan:
    def test_finds_an_inline_script(self):
        found = security.scan_inline_html("<script>alert(1)</script>", "t.html")
        assert [(f.kind, f.directive) for f in found] == [("script", "script-src")]

    def test_finds_an_inline_style_block(self):
        found = security.scan_inline_html("<style>a{}</style>", "t.html")
        assert [f.kind for f in found] == ["style"]

    def test_a_nonced_element_is_not_reported(self):
        assert security.scan_inline_html('<script nonce="x">a</script>') == []

    def test_the_template_variable_counts_as_a_nonce(self):
        assert security.scan_inline_html("<script{{ csp_nonce_attr }}>a</script>") == []

    def test_an_external_script_is_not_reported(self):
        assert security.scan_inline_html('<script src="/a.js" defer></script>') == []

    def test_an_external_stylesheet_is_not_reported(self):
        assert security.scan_inline_html('<link rel="stylesheet" href="/a.css">') == []

    def test_finds_a_style_attribute(self):
        found = security.scan_inline_html('<div style="color:red">x</div>')
        assert [(f.kind, f.directive) for f in found] == [("style-attr", "style-src-attr")]

    def test_a_style_attribute_cannot_be_fixed_with_a_nonce(self):
        found = security.scan_inline_html('<div style="color:red">x</div>')
        assert found[0].nonceable is False

    def test_a_script_element_can_be(self):
        assert security.scan_inline_html("<script>x</script>")[0].nonceable is True

    def test_finds_a_style_attribute_behind_a_template_tag(self):
        """``{% if brand %}style="…"`` is the one a whitespace-only scan walks past."""
        found = security.scan_inline_html('<div {% if brand %}style="a:b"{% endif %}>')
        assert [f.kind for f in found] == ["style-attr"]

    def test_finds_an_event_handler(self):
        found = security.scan_inline_html('<form onsubmit="return false"></form>')
        assert [(f.kind, f.directive) for f in found] == [("event-attr", "script-src-attr")]

    def test_an_attribute_merely_starting_with_on_is_not_a_handler(self):
        assert security.scan_inline_html('<div data-only="1" online="2"></div>') == []

    def test_the_line_number_is_reported(self):
        found = security.scan_inline_html("<p>a</p>\n<p>b</p>\n<script>x</script>")
        assert found[0].line == 3


class TestScanningTheShippedTemplates:
    def test_the_shell_no_longer_reports_an_unnonced_element(self):
        """base.html, login.html, form.html and friends must all be nonceable now."""
        kinds = {f.kind for f in security.scan_panel_templates()}
        assert "script" not in kinds
        assert "style" not in kinds

    def test_the_remaining_findings_are_attributes_only(self):
        findings = security.scan_panel_templates()
        assert findings, "the panel does still emit inline attributes; see docs"
        assert all(not f.nonceable for f in findings)

    def test_the_last_resort_error_page_is_exempt_and_says_why(self):
        assert "vantage/500.html" in security.CSP_EXEMPT_TEMPLATES
        assert security.CSP_EXEMPT_TEMPLATES["vantage/500.html"]
        assert not [f for f in security.scan_panel_templates() if "500" in f.template]

    def test_it_finds_the_shipped_templates_at_all(self):
        names = [name for name, _ in security.panel_templates()]
        assert "vantage/base.html" in names

    def test_a_project_override_shadows_the_shipped_file(self, settings, tmp_path):
        """The scan must report the template that will actually render."""
        override = tmp_path / "vantage"
        override.mkdir()
        (override / "base.html").write_text("<script>evil()</script>")
        settings.TEMPLATES = [
            {**settings.TEMPLATES[0], "DIRS": [str(tmp_path)]},
        ]
        found = [f for f in security.scan_panel_templates() if f.template == "vantage/base.html"]
        assert [f.kind for f in found] == ["script"]


# ── response headers ────────────────────────────────────────────────────────


class TestSecurityHeaders:
    def test_it_denies_framing_outright(self):
        response = security.apply_security_headers(HttpResponse())
        assert response["X-Frame-Options"] == "DENY"

    def test_it_refuses_mime_sniffing(self):
        response = security.apply_security_headers(HttpResponse())
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_it_keeps_panel_urls_out_of_the_referer(self):
        response = security.apply_security_headers(HttpResponse())
        assert response["Referrer-Policy"] == "same-origin"

    def test_it_keeps_the_panel_out_of_search_results(self):
        response = security.apply_security_headers(HttpResponse())
        assert "noindex" in response["X-Robots-Tag"]

    def test_it_never_caches_a_permission_scoped_page(self):
        response = security.apply_security_headers(HttpResponse())
        assert "no-store" in response["Cache-Control"]

    def test_it_does_not_overwrite_a_header_the_view_set(self):
        response = HttpResponse()
        response["Referrer-Policy"] = "no-referrer"
        security.apply_security_headers(response)
        assert response["Referrer-Policy"] == "no-referrer"

    def test_it_does_not_overwrite_the_views_caching_decision(self):
        response = HttpResponse()
        response["Cache-Control"] = "public, max-age=60"
        security.apply_security_headers(response)
        assert response["Cache-Control"] == "public, max-age=60"

    def test_force_overrides_both(self):
        response = HttpResponse()
        response["X-Frame-Options"] = "SAMEORIGIN"
        security.apply_security_headers(response, force=True)
        assert response["X-Frame-Options"] == "DENY"

    def test_it_honours_djangos_own_framing_exemption(self):
        response = HttpResponse()
        response.xframe_options_exempt = True
        security.apply_security_headers(response)
        assert "X-Frame-Options" not in response


class TestHeadersOnPanelResponses:
    def test_a_panel_page_is_not_frameable(self, boss):
        response = boss.get(reverse("vantage:dashboard"))
        assert response["X-Frame-Options"] == "DENY"

    def test_a_panel_page_does_not_leak_its_url(self, boss):
        response = boss.get(reverse("vantage:dashboard"))
        assert response["Referrer-Policy"] == "same-origin"

    def test_a_panel_page_is_not_cached(self, boss):
        assert "no-store" in boss.get(reverse("vantage:dashboard"))["Cache-Control"]

    def test_a_non_panel_page_is_left_alone(self, client, db, hardened):
        """The panel must not impose its headers on the site it is mounted inside."""
        response = client.get("/accounts/login/")
        assert "X-Frame-Options" not in response

    def test_a_404_gets_no_panel_headers(self, boss):
        """A resolver miss never reached a panel view, and saying otherwise would
        advertise where the panel is."""
        assert "X-Robots-Tag" not in boss.get("/admin/nothing-here/x/")

    def test_without_the_middleware_there_are_no_headers(self, client, db, settings):
        """The check that nags about this is nagging about something real."""
        settings.ROOT_URLCONF = "tests.test_security"
        settings.MIDDLEWARE = [m for m in HARDENED if "PanelSecurity" not in m]
        client.force_login(User.objects.create_superuser("a", "a@example.com", "x"))
        assert "X-Frame-Options" not in client.get(reverse("vantage:dashboard"))


# ── the client address ──────────────────────────────────────────────────────


class TestClientIp:
    def test_reads_remote_addr(self):
        assert security.client_ip(rf(REMOTE_ADDR="10.0.0.4")) == "10.0.0.4"

    def test_a_forwarded_header_is_ignored_by_default(self):
        """Trusting it by default lets an attacker mint a fresh bucket per request."""
        request = rf(REMOTE_ADDR="10.0.0.4", HTTP_X_FORWARDED_FOR="1.2.3.4")
        assert security.client_ip(request) == "10.0.0.4"

    def test_the_header_can_be_opted_into(self, settings):
        settings.VANTAGE = {}
        security.DEFAULTS["LOGIN_THROTTLE_IP_HEADER"] = "HTTP_X_FORWARDED_FOR"
        try:
            request = rf(REMOTE_ADDR="10.0.0.4", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.1")
            assert security.client_ip(request) == "1.2.3.4"
        finally:
            security.DEFAULTS["LOGIN_THROTTLE_IP_HEADER"] = None

    def test_garbage_is_not_a_bucket(self):
        assert security.client_ip(rf(REMOTE_ADDR="not-an-address")) == ""

    def test_whitespace_cannot_split_one_address_into_two(self):
        assert security.client_ip(rf(REMOTE_ADDR=" 1.2.3.4 ")) == "1.2.3.4"

    def test_ipv6_is_normalised(self):
        assert security.client_ip(rf(REMOTE_ADDR="2001:0db8::0001")) == "2001:db8::1"

    def test_no_meta_at_all(self):
        assert security.client_ip(object()) == ""


# ── the login throttle ──────────────────────────────────────────────────────


class TestThrottleKeys:
    def test_the_pair_is_the_key(self):
        key = security.LoginThrottle.cache_key
        assert key("ada", "1.2.3.4") != key("ada", "5.6.7.8")
        assert key("ada", "1.2.3.4") != key("grace", "1.2.3.4")

    def test_the_username_is_not_in_the_key(self):
        """Usernames are frequently email addresses; a cache key is not a place for one."""
        assert "ada@example.com" not in security.LoginThrottle.cache_key(
            "ada@example.com", "1.2.3.4"
        )

    def test_a_separator_cannot_be_smuggled_across_the_pair(self):
        key = security.LoginThrottle.cache_key
        assert key("a:b", "c") != key("a", "b:c")

    def test_the_key_is_memcached_safe(self):
        key = security.LoginThrottle.cache_key("a name with spaces", "1.2.3.4")
        assert " " not in key and len(key) < 250


class TestThrottleCounting:
    def test_a_fresh_pair_is_not_blocked(self):
        assert not security.LoginThrottle(limit=3).is_blocked("ada", "1.2.3.4")

    def test_it_blocks_at_the_limit(self):
        throttle = security.LoginThrottle(limit=3, window=60)
        for _ in range(3):
            throttle.record_failure("ada", "1.2.3.4")
        assert throttle.is_blocked("ada", "1.2.3.4")

    def test_it_does_not_block_below_the_limit(self):
        throttle = security.LoginThrottle(limit=3, window=60)
        for _ in range(2):
            throttle.record_failure("ada", "1.2.3.4")
        assert not throttle.is_blocked("ada", "1.2.3.4")

    def test_the_window_does_not_slide_forward_on_every_attempt(self):
        """A fixed window; otherwise a steady trickle extends a lockout forever."""
        throttle = security.LoginThrottle(limit=100, window=60)
        throttle.record_failure("ada", "1.2.3.4")
        key = throttle.cache_key("ada", "1.2.3.4")
        first = cache._expire_info.get(cache.make_key(key))
        throttle.record_failure("ada", "1.2.3.4")
        assert cache._expire_info.get(cache.make_key(key)) == first

    def test_success_clears_only_this_pair(self):
        throttle = security.LoginThrottle(limit=3, window=60)
        for _ in range(3):
            throttle.record_failure("ada", "1.2.3.4")
            throttle.record_failure("ada", "9.9.9.9")
        throttle.clear("ada", "1.2.3.4")
        assert not throttle.is_blocked("ada", "1.2.3.4")
        assert throttle.is_blocked("ada", "9.9.9.9")

    def test_a_limit_of_zero_switches_it_off(self):
        throttle = security.LoginThrottle(limit=0)
        throttle.record_failure("ada", "1.2.3.4")
        assert not throttle.is_blocked("ada", "1.2.3.4")

    def test_disabled_records_nothing(self):
        throttle = security.LoginThrottle(limit=3, enabled=False)
        for _ in range(10):
            throttle.record_failure("ada", "1.2.3.4")
        assert not security.LoginThrottle(limit=3).is_blocked("ada", "1.2.3.4")

    def test_a_broken_cache_fails_open(self, broken_cache):
        """A cache outage must not become a login outage."""
        throttle = security.LoginThrottle(limit=1, window=60, cache_alias="broken")
        throttle.record_failure("ada", "1.2.3.4")
        assert not throttle.is_blocked("ada", "1.2.3.4")

    def test_state_reports_what_is_left(self):
        throttle = security.LoginThrottle(limit=5, window=60)
        throttle.record_failure("ada", "1.2.3.4")
        assert throttle.state("ada", "1.2.3.4").remaining == 4


class BrokenCache:
    """A cache backend where every operation raises.

    Wired in under its own alias rather than over ``default``: a test that replaces the
    default cache with something that cannot even be cleared leaves every test after it
    erroring in setup, and the failure looks like it belongs to whichever file ran next.
    """

    def __init__(self, *args, **kwargs):
        pass

    def _fail(self, *args, **kwargs):
        raise RuntimeError("the cache is down")

    get = add = incr = set = delete = clear = _fail

    def close(self, **kwargs):
        """Django closes every configured cache when settings change; a backend that
        cannot be closed turns one test's override into every later test erroring."""


@pytest.fixture
def broken_cache(settings):
    settings.CACHES = {
        **settings.CACHES,
        "broken": {"BACKEND": "tests.test_security.BrokenCache"},
    }
    return settings


class TestNormaliseUsername:
    def test_case_is_folded(self):
        assert normalise_username("ADMIN") == normalise_username("admin")

    def test_whitespace_is_stripped(self):
        assert normalise_username("  admin ") == "admin"

    def test_non_ascii_case_pairs_fold_too(self):
        assert normalise_username("STRASSE") == normalise_username("strasse")

    def test_it_is_capped(self):
        assert len(normalise_username("a" * 10_000)) == 320

    def test_none_is_the_empty_string(self):
        assert normalise_username(None) == ""


# ── brute force, through the real stack ─────────────────────────────────────


LOGIN = "/accounts/login/"


def attempt(client, username="ada", password="wrong"):
    return client.post(LOGIN, {"username": username, "password": password})


@pytest.fixture
def small_throttle():
    """Three failures, so the tests read as arithmetic rather than as a loop."""
    security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 3
    yield
    security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 10


@pytest.mark.usefixtures("hardened", "small_throttle")
class TestBruteForce:
    @pytest.fixture(autouse=True)
    def _ada(self, db):
        return User.objects.create_user("ada", "ada@example.com", "correct-horse")

    def test_a_wrong_password_is_merely_refused(self, client):
        assert attempt(client).status_code == 200

    def test_the_fourth_attempt_is_refused_outright(self, client):
        for _ in range(3):
            attempt(client)
        assert attempt(client).status_code == 429

    def test_the_refusal_says_when_to_come_back(self, client):
        for _ in range(3):
            attempt(client)
        assert int(attempt(client)["Retry-After"]) > 0

    def test_the_refusal_is_never_cached(self, client):
        for _ in range(3):
            attempt(client)
        assert attempt(client)["Cache-Control"] == "no-store"

    def test_a_correct_password_still_works_below_the_limit(self, client):
        attempt(client)
        response = client.post(LOGIN, {"username": "ada", "password": "correct-horse"})
        assert response.status_code == 302

    def test_signing_in_clears_the_counter(self, client):
        for _ in range(2):
            attempt(client)
        client.post(LOGIN, {"username": "ada", "password": "correct-horse"})
        client.logout()
        assert attempt(client).status_code == 200

    def test_another_address_is_not_locked_out(self, client):
        """Username-only keying would hand anybody a denial of service against 'admin'."""
        for _ in range(4):
            attempt(client)
        assert (
            attempt(
                client,
            ).status_code
            == 429
        )
        response = client.post(
            LOGIN, {"username": "ada", "password": "wrong"}, REMOTE_ADDR="9.9.9.9"
        )
        assert response.status_code == 200

    def test_another_username_from_here_is_not_locked_out(self, client, db):
        """IP-only keying locks out a whole office when one person fumbles."""
        User.objects.create_user("grace", "g@example.com", "x")
        for _ in range(4):
            attempt(client)
        assert attempt(client, "grace").status_code == 200

    def test_case_does_not_buy_a_fresh_budget(self, client):
        for _ in range(3):
            attempt(client, "ada")
        assert attempt(client, "ADA").status_code == 429

    def test_leading_whitespace_does_not_either(self, client):
        for _ in range(3):
            attempt(client, "ada")
        assert attempt(client, " ada").status_code == 429

    def test_a_nonexistent_username_is_throttled_identically(self, client):
        for _ in range(3):
            attempt(client, "nobody")
        assert attempt(client, "nobody").status_code == 429

    def test_the_refusal_is_byte_identical_for_real_and_fake_accounts(self, client):
        for _ in range(3):
            attempt(client, "ada")
        real = attempt(client, "ada")
        for _ in range(3):
            attempt(client, "nobody")
        fake = attempt(client, "nobody")
        assert real.content == fake.content
        assert real.status_code == fake.status_code

    def test_the_refusal_never_names_the_account(self, client):
        for _ in range(3):
            attempt(client)
        assert b"ada" not in attempt(client).content.lower()

    def test_a_blocked_attempt_costs_no_password_hash(self, client, django_assert_num_queries):
        """Blocked before the view runs. No user lookup means no timing signal, and no
        hashing means the refusal cannot itself be the denial of service."""
        for _ in range(3):
            attempt(client)
        with django_assert_num_queries(0):
            attempt(client)

    def test_a_blank_username_is_not_a_shared_bucket(self, client):
        for _ in range(6):
            client.post(LOGIN, {"username": "", "password": "x"})
        assert attempt(client).status_code == 200

    def test_no_usable_address_means_no_username_only_lockout(self, client):
        """The pair key is the design; collapsing it to username-only is the bug."""
        for _ in range(6):
            client.post(LOGIN, {"username": "ada", "password": "x"}, REMOTE_ADDR="garbage")
        assert (
            client.post(
                LOGIN, {"username": "ada", "password": "x"}, REMOTE_ADDR="garbage"
            ).status_code
            == 200
        )

    def test_a_get_is_never_throttled(self, client):
        for _ in range(6):
            attempt(client)
        assert client.get(LOGIN).status_code == 200

    def test_the_switch_turns_it_off(self, client):
        security.DEFAULTS["LOGIN_THROTTLE"] = False
        try:
            for _ in range(6):
                attempt(client)
            assert attempt(client).status_code == 200
        finally:
            security.DEFAULTS["LOGIN_THROTTLE"] = True


class TestWithoutTheMiddleware:
    def test_nothing_is_counted(self, client, db, settings):
        """A project that has not installed it must be byte-for-byte unchanged."""
        settings.ROOT_URLCONF = "tests.test_security"
        settings.MIDDLEWARE = [m for m in HARDENED if "LoginThrottle" not in m]
        User.objects.create_user("ada", "ada@example.com", "correct-horse")
        security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 3
        try:
            for _ in range(6):
                attempt(client)
            assert attempt(client).status_code == 200
        finally:
            security.DEFAULTS["LOGIN_THROTTLE_LIMIT"] = 10


class TestLoginViewDetection:
    def test_a_non_login_view_is_not_matched(self):
        assert not LoginThrottleMiddleware.is_login_view(lambda request: None)

    def test_a_subclass_of_djangos_login_view_is(self):
        from django.contrib.auth.views import LoginView

        class Custom(LoginView):
            pass

        assert LoginThrottleMiddleware.is_login_view(Custom.as_view())

    def test_the_email_field_is_read_when_username_is_absent(self):
        request = RequestFactory().post("/", {"email": "Ada@Example.com"})
        assert LoginThrottleMiddleware.username_from(request) == "ada@example.com"


# ── per-surface rate limits ─────────────────────────────────────────────────


class TestRateLimitCounting:
    def limit(self, **kwargs):
        return security.RateLimit(surface="test", limit=3, window=60, **kwargs)

    def test_the_budget_is_spent_one_at_a_time(self):
        limit = self.limit()
        assert [limit.spend("u1").blocked for _ in range(3)] == [False, False, False]

    def test_the_request_after_the_budget_is_refused(self):
        limit = self.limit()
        for _ in range(3):
            limit.spend("u1")
        assert limit.spend("u1").blocked

    def test_actors_have_their_own_budgets(self):
        limit = self.limit()
        for _ in range(4):
            limit.spend("u1")
        assert not limit.spend("u2").blocked

    def test_surfaces_have_their_own_budgets(self):
        for _ in range(4):
            security.RateLimit("export", 3, 60).spend("u1")
        assert not security.RateLimit("import", 3, 60).spend("u1").blocked

    def test_peek_spends_nothing(self):
        limit = self.limit()
        for _ in range(10):
            limit.peek("u1")
        assert not limit.spend("u1").blocked

    def test_a_broken_cache_fails_open(self, broken_cache):
        assert not self.limit(cache_alias="broken").spend("u1").blocked

    def test_the_key_does_not_contain_the_actor(self):
        assert "ada@example.com" not in self.limit().cache_key("user:ada@example.com")


class TestRateLimitSettings:
    def test_the_defaults_cover_the_three_surfaces(self):
        assert set(security.RATE_LIMITS) == {"autocomplete", "export", "import", "reveal"}

    def test_an_unbudgeted_surface_has_no_limit(self):
        assert security.rate_limit_for("list") is None

    def test_the_master_switch_turns_everything_off(self):
        security.DEFAULTS["RATE_LIMIT"] = False
        try:
            assert security.rate_limit_for("autocomplete") is None
        finally:
            security.DEFAULTS["RATE_LIMIT"] = True

    def test_an_override_merges_rather_than_replaces(self):
        """Loosening exports must not silently switch autocomplete off."""
        security.DEFAULTS["RATE_LIMITS"] = {"export": (99, 60)}
        try:
            assert security.rate_limit_for("export").limit == 99
            assert security.rate_limit_for("autocomplete") is not None
        finally:
            security.DEFAULTS["RATE_LIMITS"] = None

    def test_a_surface_can_be_switched_off_explicitly(self):
        security.DEFAULTS["RATE_LIMITS"] = {"autocomplete": None}
        try:
            assert security.rate_limit_for("autocomplete") is None
        finally:
            security.DEFAULTS["RATE_LIMITS"] = None


class TestActorIdentity:
    def test_an_account_is_the_actor(self, db):
        request = rf()
        request.user = User.objects.create_user("ada", "a@example.com", "x")
        assert security.actor_for(request) == f"user:{request.user.pk}"

    def test_an_office_sharing_an_address_is_not_one_budget(self, db):
        one, two = (
            User.objects.create_user("a", "a@example.com", "x"),
            User.objects.create_user("b", "b@example.com", "x"),
        )
        first, second = rf(REMOTE_ADDR="1.2.3.4"), rf(REMOTE_ADDR="1.2.3.4")
        first.user, second.user = one, two
        assert security.actor_for(first) != security.actor_for(second)

    def test_anonymous_falls_back_to_the_address(self):
        assert security.actor_for(rf(REMOTE_ADDR="1.2.3.4")) == "addr:1.2.3.4"

    def test_no_address_is_still_a_bucket_rather_than_no_bucket(self):
        assert security.actor_for(rf(REMOTE_ADDR="garbage")) == "addr:unknown"


class TestSurfaceDetection:
    def make(self, url_name, **params):
        request = RequestFactory().get("/", params)
        request.resolver_match = type("M", (), {"url_name": url_name, "app_names": ["vantage"]})()
        return request

    def test_autocomplete(self):
        assert surface_for(self.make("autocomplete")) == "autocomplete"

    def test_the_import_wizard_entry(self):
        assert surface_for(self.make("testapp-post-import")) == "import"

    def test_every_step_of_the_wizard(self):
        assert surface_for(self.make("testapp-post-import-preview")) == "import"

    def test_an_export_is_recognised_by_its_parameter(self):
        assert surface_for(self.make("testapp-post-list", export="csv")) == "export"

    def test_a_plain_list_view_is_unbudgeted(self):
        assert surface_for(self.make("testapp-post-list")) is None

    def test_a_detail_page_is_unbudgeted(self):
        assert surface_for(self.make("testapp-post-detail")) is None

    def test_an_unresolved_request_is_nothing(self):
        assert surface_for(RequestFactory().get("/")) is None


class TestRefusalHooks:
    """The two refusals are documented as the subclassing seam, so they are tested as
    functions rather than only through a client."""

    def test_the_throttle_refusal_carries_no_body_for_a_machine_to_misread(self):
        state = security.ThrottleState(failures=5, limit=5, retry_after=42)
        response = PanelSecurityMiddleware.throttled_response(rf(), "export", state)
        assert response.status_code == 429
        assert response["Retry-After"] == "42"
        assert response["Cache-Control"] == "no-store"

    def test_an_upload_refusal_lists_the_reasons(self):
        response = PanelSecurityMiddleware.rejected_upload_response(rf(), ["it is HTML"])
        assert response.status_code == 400
        assert b"it is HTML" in response.content

    def test_a_subclass_can_replace_the_refusal(self):
        class Quiet(PanelSecurityMiddleware):
            @staticmethod
            def throttled_response(request, surface, state):
                return HttpResponse(status=204)

        state = security.ThrottleState(failures=5, limit=5, retry_after=1)
        assert Quiet.throttled_response(rf(), "export", state).status_code == 204


class TestIsPanelRequest:
    def test_an_unresolved_request_is_not_one(self):
        assert not is_panel_request(RequestFactory().get("/"))

    def test_a_second_site_under_its_own_instance_namespace_still_counts(self):
        request = RequestFactory().get("/")
        request.resolver_match = type(
            "M", (), {"url_name": "dashboard", "app_names": ["vantage"], "namespace": "partners"}
        )()
        assert is_panel_request(request)

    def test_another_app_is_not_the_panel(self):
        request = RequestFactory().get("/")
        request.resolver_match = type("M", (), {"url_name": "index", "app_names": ["shop"]})()
        assert not is_panel_request(request)


@pytest.fixture
def tiny_budget():
    security.DEFAULTS["RATE_LIMITS"] = {
        "autocomplete": (2, 60),
        "export": (2, 60),
        "import": (2, 60),
    }
    yield
    security.DEFAULTS["RATE_LIMITS"] = None


@pytest.mark.usefixtures("tiny_budget")
class TestRateLimitsThroughTheStack:
    def test_autocomplete_runs_out(self, boss):
        url = reverse("vantage:autocomplete")
        for _ in range(2):
            boss.get(url, {"model": "testapp.Author"})
        assert boss.get(url, {"model": "testapp.Author"}).status_code == 429

    def test_autocomplete_is_refused_in_json(self, boss):
        """Its caller is a fetch(); a plain-text body would throw in the parser and
        surface as a picker that spins forever."""
        url = reverse("vantage:autocomplete")
        for _ in range(3):
            response = boss.get(url, {"model": "testapp.Author"})
        assert response.status_code == 429
        assert response["Content-Type"].startswith("application/json")

    def test_autocomplete_below_the_budget_still_answers(self, boss):
        url = reverse("vantage:autocomplete")
        assert boss.get(url, {"model": "testapp.Author"}).status_code == 200

    def test_export_runs_out(self, boss):
        url = reverse("vantage:testapp-post-list")
        for _ in range(2):
            boss.get(url, {"export": "csv"})
        assert boss.get(url, {"export": "csv"}).status_code == 429

    def test_the_list_page_behind_the_export_is_untouched(self, boss):
        """The budget is on the expensive surface, not on reading the page."""
        url = reverse("vantage:testapp-post-list")
        for _ in range(5):
            boss.get(url, {"export": "csv"})
        assert boss.get(url).status_code == 200

    def test_import_runs_out(self, boss):
        url = reverse("vantage:testapp-post-import")
        for _ in range(2):
            boss.get(url)
        assert boss.get(url).status_code == 429

    def test_the_refusal_carries_retry_after(self, boss):
        url = reverse("vantage:autocomplete")
        for _ in range(3):
            response = boss.get(url, {"model": "testapp.Author"})
        assert response["Retry-After"] == "60"

    def test_another_account_has_its_own_budget(self, boss, django_user_model):
        url = reverse("vantage:autocomplete")
        for _ in range(3):
            boss.get(url, {"model": "testapp.Author"})
        assert boss.get(url, {"model": "testapp.Author"}).status_code == 429

        other = Client()
        other.force_login(django_user_model.objects.create_superuser("two", "t@e.com", "x"))
        assert other.get(url, {"model": "testapp.Author"}).status_code == 200

    def test_ordinary_pages_are_never_budgeted(self, boss):
        url = reverse("vantage:dashboard")
        assert [boss.get(url).status_code for _ in range(6)] == [200] * 6


# ── uploads ─────────────────────────────────────────────────────────────────


class TestSniffing:
    @pytest.mark.parametrize(
        ("head", "expected"),
        [
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"GIF89a", "image/gif"),
            (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
            (b"%PDF-1.7", "application/pdf"),
            (b"PK\x03\x04", "application/zip"),
            (b"MZ\x90\x00", "application/x-dosexec"),
            (b"#!/bin/sh", "text/x-shellscript"),
        ],
    )
    def test_signatures(self, head, expected):
        assert security.sniff(head) == expected

    def test_html(self):
        assert security.sniff(b"<!DOCTYPE html><html>") == "text/html"

    def test_html_behind_a_byte_order_mark_and_whitespace(self):
        """Which is how a browser reads it, and therefore the only reading that counts."""
        assert security.sniff(b"\xef\xbb\xbf\n  <html>") == "text/html"

    def test_svg(self):
        assert security.sniff(b'<svg xmlns="http://www.w3.org/2000/svg">') == "image/svg+xml"

    def test_svg_behind_an_xml_declaration(self):
        assert security.sniff(b'<?xml version="1.0"?><svg>') == "image/svg+xml"

    def test_plain_text_has_no_opinion(self):
        assert security.sniff(b"name,email\nada,ada@example.com") is None

    def test_nothing_has_no_opinion(self):
        assert security.sniff(b"") is None


def upload(name, content):
    return SimpleUploadedFile(name, content)


class TestInspectUpload:
    def test_a_real_png_is_accepted(self):
        assert security.inspect_upload(upload("logo.png", PNG)) == []

    def test_html_wearing_a_png_extension_is_refused(self):
        problems = security.inspect_upload(upload("logo.png", b"<html><script>x</script>"))
        assert problems

    def test_the_refusal_says_what_the_file_actually_is(self):
        problems = security.inspect_upload(upload("logo.png", b"<html>"))
        assert any("text/html" in p for p in problems)

    def test_a_jpeg_named_png_is_refused(self):
        assert security.inspect_upload(upload("logo.png", b"\xff\xd8\xff\xe0" + b"\x00" * 20))

    def test_a_png_named_jpg_is_refused(self):
        assert security.inspect_upload(upload("logo.jpg", PNG))

    def test_an_unrecognised_body_is_not_refused_on_type(self):
        """Absence of evidence is not evidence: a CSV must still import."""
        assert security.inspect_upload(upload("rows.csv", b"a,b\n1,2\n")) == []

    def test_an_oversized_file_is_refused(self):
        assert security.inspect_upload(upload("logo.png", PNG), max_bytes=8)

    def test_the_ceiling_message_is_in_kilobytes(self):
        problems = security.inspect_upload(upload("logo.png", PNG), max_bytes=8)
        assert "limit is 0 KB" in problems[0]

    def test_a_clean_svg_is_accepted(self):
        body = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        assert security.inspect_upload(upload("logo.svg", body)) == []

    @pytest.mark.parametrize(
        "body",
        [
            b'<svg xmlns="x"><script>alert(1)</script></svg>',
            b'<svg xmlns="x" onload="alert(1)"></svg>',
            b'<svg xmlns="x"><a href="javascript:alert(1)">x</a></svg>',
            b'<svg xmlns="x"><foreignObject><body/></foreignObject></svg>',
            b'<svg xmlns="x"><set attributeName="onload" to="alert(1)"/></svg>',
        ],
    )
    def test_a_hostile_svg_is_refused(self, body):
        assert security.inspect_upload(upload("logo.svg", body))

    def test_a_script_at_the_end_of_a_long_svg_is_still_found(self):
        body = b'<svg xmlns="x">' + b"<rect/>" * 4000 + b"<script>x</script></svg>"
        assert security.inspect_upload(upload("logo.svg", body))

    def test_the_file_position_is_restored(self):
        handle = upload("logo.png", PNG)
        handle.seek(3)
        security.inspect_upload(handle)
        assert handle.tell() == 3

    def test_the_file_is_still_readable_afterwards(self):
        handle = upload("logo.png", PNG)
        security.inspect_upload(handle)
        handle.seek(0)
        assert handle.read() == PNG

    def test_something_that_is_not_a_file_does_not_explode(self):
        assert security.inspect_upload(io.BytesIO(b"")) == []


class TestFileFieldSvgScreening:
    """``vantage.media.validate_file_upload`` is the *field-level* validator Django
    attaches to every ``FileField`` on every registered model (see
    ``vantage.widgets``, which wires ``upload_validator(db_field)`` onto the form
    field). Unlike ``PanelSecurityMiddleware``'s ``inspect_upload`` — which is only
    consulted when that opt-in middleware is installed — this validator runs on
    *every* save regardless of the middleware stack, exactly like
    ``vantage.models.validate_branding_upload`` does for the four branding fields.

    ``vantage.checks`` (``vantage.W059``'s hint text) and ``vantage.media``'s own
    module docstring both claim uploads are "screened" and that an SVG containing
    script is refused. That is true for ``ImageField`` (which refuses SVG outright)
    and true for the branding fields, but ``validate_file_upload`` — the validator
    a plain ``FileField`` gets — only checks that the sniffed content type agrees
    with the extension. A file that is honestly named ``.svg`` and honestly *is*
    an SVG sails through with no inspection of what is inside it.

    That gap is bigger than the branding fields it was modelled on: any user with
    ``add``/``change`` permission on *any* registered model carrying a plain
    ``FileField`` (an attachment, a document, an export — anything that is not an
    ``ImageField``) can store a ``<script>``-bearing SVG. ``vantage/partials/
    thumb.html`` links straight to the stored file (``target="_blank"``), so the
    next person who opens that attachment — reviewer, approver, superuser — runs
    the attacker's script from the panel's own origin with their session.
    """

    def _hostile(self, name="attachment.svg"):
        body = (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.cookie)</script></svg>'
        )
        return upload(name, body)

    def test_a_hostile_svg_correctly_named_is_refused(self):
        from django.core.exceptions import ValidationError

        from vantage.media import validate_file_upload

        with pytest.raises(ValidationError):
            validate_file_upload(self._hostile())

    def test_an_svg_event_handler_is_also_refused(self):
        from django.core.exceptions import ValidationError

        from vantage.media import validate_file_upload

        body = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>'
        with pytest.raises(ValidationError):
            validate_file_upload(upload("attachment.svg", body))

    def test_a_clean_svg_is_still_accepted(self):
        """The fix must not turn every SVG into a rejection, only a hostile one."""
        from vantage.media import validate_file_upload

        body = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
        validate_file_upload(upload("attachment.svg", body))  # must not raise

    def test_the_branding_validator_already_refuses_the_same_payload(self):
        """Proves this is a real inconsistency, not a hypothetical one: the exact
        same bytes are already refused for the four branding fields."""
        from django.core.exceptions import ValidationError

        from vantage.models import validate_branding_upload

        with pytest.raises(ValidationError):
            validate_branding_upload(self._hostile("logo.svg"))

    def test_the_middleware_layer_already_refuses_the_same_payload(self):
        """And ``inspect_upload`` — reachable only when the opt-in middleware is
        installed — already refuses it too. ``validate_file_upload`` is the odd one
        out among the three layers that all claim to screen the same threat."""
        assert security.inspect_upload(self._hostile()) != []


@pytest.mark.usefixtures("hardened")
class TestUploadScreeningThroughTheStack:
    def test_a_hostile_upload_is_refused_before_the_view(self, boss):
        response = boss.post(
            reverse("vantage:settings"),
            {"logo": upload("logo.png", b"<html><script>steal()</script></html>")},
        )
        assert response.status_code == 400

    def test_the_refusal_says_why(self, boss):
        response = boss.post(
            reverse("vantage:settings"),
            {"logo": upload("logo.png", b"<html>")},
        )
        assert b"HTML" in response.content

    def test_a_form_post_with_no_files_is_untouched(self, boss):
        assert boss.post(reverse("vantage:settings"), {"site_title": "x"}).status_code != 400

    def test_an_oversized_upload_is_refused(self, boss):
        security.DEFAULTS["UPLOAD_MAX_BYTES"] = 16
        try:
            response = boss.post(reverse("vantage:settings"), {"logo": upload("logo.png", PNG)})
            assert response.status_code == 400
        finally:
            security.DEFAULTS["UPLOAD_MAX_BYTES"] = 10 * 1024 * 1024

    def test_a_non_panel_upload_is_not_screened(self, client, db, hardened):
        """The panel must not police the rest of the site's forms."""
        response = client.post(LOGIN, {"file": upload("x.png", b"<html>")})
        assert response.status_code != 400


# ---------------------------------------------------------------------------------------
# Findings from the full security review. Each class below was written before its fix
# and failed against the code as it was.
# ---------------------------------------------------------------------------------------


class TestUrlColumnSchemes:
    """A URL column links only to schemes a browser will not execute.

    ``URLField`` validates its scheme in forms, but a row written by a script, a data
    migration or another service never passed through one, so the stored value is
    whatever someone put there — ``javascript:…`` included.
    """

    @pytest.fixture
    def panel(self, db):
        from tests.testapp.models import Author

        return site.get_panel(Author)

    @pytest.mark.parametrize(
        "value",
        [
            "javascript:alert(1)",
            " JavaScript:alert(1)",
            "java\tscript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
        ],
    )
    def test_an_executable_scheme_is_shown_but_not_linked(self, panel, value):
        from tests.testapp.models import Author
        from vantage.render import render_cell

        html = str(render_cell(panel, Author(name="a", email="a@x.io", website=value), "website"))
        assert "<a " not in html
        assert "href" not in html

    def test_an_http_url_is_still_a_link(self, panel):
        from tests.testapp.models import Author
        from vantage.render import render_cell

        obj = Author(name="a", email="a@x.io", website="https://example.com/p?q=1")
        html = str(render_cell(panel, obj, "website"))
        assert 'href="https://example.com/p?q=1"' in html


class TestImpersonateRedirect:
    """The impersonation form's ``next`` only ever lands back inside this site."""

    @pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/x"])
    def test_an_offsite_next_falls_back_to_the_dashboard(self, client, db, target):
        user = User.objects.create_user("s", "s@example.com", "x", is_staff=True)
        client.force_login(user)
        response = client.post(reverse("vantage:impersonate"), {"next": target})
        assert response.status_code == 302
        assert response["Location"] == reverse("vantage:dashboard")

    def test_a_local_next_is_kept(self, client, db):
        user = User.objects.create_user("s", "s@example.com", "x", is_staff=True)
        client.force_login(user)
        response = client.post(reverse("vantage:impersonate"), {"next": "/admin/audit/"})
        assert response["Location"] == "/admin/audit/"
