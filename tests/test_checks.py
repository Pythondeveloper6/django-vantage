"""System checks: the correctness family, and the deployment family.

The deployment checks are the interesting half to test, because their whole design is
about *when they stay quiet*. A check that fires on a laptop trains everybody to ignore
the output, so roughly half the tests here assert silence.

This module doubles as an empty urlconf. Several tests need a project that has Vantage
installed but has not mounted it — which is a real configuration, and the one every
deployment check must say nothing about.
"""

import pytest
from django.core.checks import Warning

from vantage import checks

#: A project with the app installed and the panel not mounted.
urlpatterns = []

UNMOUNTED = "tests.test_checks"


@pytest.fixture
def unmounted(settings):
    settings.ROOT_URLCONF = UNMOUNTED
    return settings


@pytest.fixture
def mounted(settings):
    """The default urlconf mounts the panel at /admin/; named so tests say so."""
    settings.ROOT_URLCONF = "tests.urls"
    return settings


def ids(messages):
    return sorted(m.id for m in messages)


def run(check, **_):
    return check(app_configs=None)


# ── correctness: the VANTAGE dict ───────────────────────────────────────────


class TestSettingsCheck:
    def test_no_setting_at_all_is_fine(self, settings):
        del settings.VANTAGE
        assert checks.check_vantage_settings(None) == []

    def test_a_valid_dict_passes(self, settings):
        settings.VANTAGE = {"SITE_TITLE": "Acme", "THEME": "dark"}
        assert checks.check_vantage_settings(None) == []

    def test_a_non_dict_is_an_error(self, settings):
        settings.VANTAGE = ["SITE_TITLE"]
        assert ids(checks.check_vantage_settings(None)) == ["vantage.E001"]

    def test_an_unknown_key_is_an_error(self, settings):
        settings.VANTAGE = {"SITE_TITEL": "Acme"}
        assert ids(checks.check_vantage_settings(None)) == ["vantage.E002"]

    def test_a_typo_gets_a_suggestion(self, settings):
        settings.VANTAGE = {"SITE_TITEL": "Acme"}
        assert "SITE_TITLE" in checks.check_vantage_settings(None)[0].hint

    def test_an_unrecognisable_key_gets_no_suggestion(self, settings):
        settings.VANTAGE = {"QQQQQQ": 1}
        assert checks.check_vantage_settings(None)[0].hint is None

    def test_an_invalid_choice_is_an_error(self, settings):
        settings.VANTAGE = {"THEME": "sepia"}
        assert ids(checks.check_vantage_settings(None)) == ["vantage.E003"]

    def test_the_error_names_the_allowed_values(self, settings):
        settings.VANTAGE = {"THEME": "sepia"}
        assert "light" in checks.check_vantage_settings(None)[0].msg

    @pytest.mark.parametrize("value", ["indigo", "#12345", 6366, ""])
    def test_an_unreadable_brand_colour_is_an_error(self, settings, value):
        """It is rendered into every page's inline stylesheet, so it fails every
        render, the sign-in page included, unless check catches it first."""
        settings.VANTAGE = {"BRAND_COLOR": value}
        assert ids(checks.check_vantage_settings(None)) == ["vantage.E004"]

    @pytest.mark.parametrize("value", ["big", "8 px", "1rem; color: red", 4])
    def test_a_radius_that_is_not_a_css_length_is_an_error(self, settings, value):
        settings.VANTAGE = {"BRAND_RADIUS": value}
        assert ids(checks.check_vantage_settings(None)) == ["vantage.E005"]

    def test_valid_brand_values_pass(self, settings):
        settings.VANTAGE = {"BRAND_COLOR": "#0d9488", "BRAND_RADIUS": "0.75rem"}
        assert checks.check_vantage_settings(None) == []


# ── the deployment family, as a family ──────────────────────────────────────


class TestTheDeploymentFamily:
    def test_every_one_of_them_is_registered(self):
        """Written as a loop over the tuple so adding a check adds its registration
        test — the failure mode being guarded against is a check that exists, is
        correct, and is never run."""
        from django.core.checks.registry import registry

        registered = set(registry.get_checks(include_deployment_checks=True))
        assert set(checks.DEPLOYMENT_CHECKS) <= registered

    def test_none_of_them_runs_without_deploy(self):
        from django.core.checks.registry import registry

        everyday = set(registry.get_checks(include_deployment_checks=False))
        assert not (set(checks.DEPLOYMENT_CHECKS) & everyday)

    def test_every_one_of_them_can_actually_fire(self):
        """The other half of the silence tests. A check that is quiet under a
        deliberately terrible configuration is not cautious, it is broken."""
        for check in checks.DEPLOYMENT_CHECKS:
            assert _force(check), check.__name__

    def test_their_ids_are_all_in_the_documented_range(self):
        """``vantage.W05x``. The docstring promises it and the hints reference it."""
        found = set()
        for check in checks.DEPLOYMENT_CHECKS:
            found.update(m.id for m in _force(check))
        assert len(found) >= len(checks.DEPLOYMENT_CHECKS) - 1  # W056 is shared
        assert all(i.startswith("vantage.W05") for i in found), found

    def test_every_one_of_them_is_silent_on_an_unmounted_project(self, unmounted, settings):
        """Being installed and being exposed are different facts. A project that never
        mounted the panel has nothing behind a URL to protect."""
        settings.DEBUG = True
        settings.SESSION_COOKIE_SECURE = False
        settings.VANTAGE = {"AUDIT_LOG": False, "IMPORT_MAX_BYTES": 0}
        for check in checks.DEPLOYMENT_CHECKS:
            assert run(check) == [], check.__name__

    def test_every_one_of_them_returns_warnings_not_errors(self):
        """A deployment check must never break `manage.py check --deploy --fail-level`
        for someone who has consciously accepted the risk."""
        for check in checks.DEPLOYMENT_CHECKS:
            messages = _force(check)
            assert messages and all(isinstance(m, Warning) for m in messages)

    def test_every_one_of_them_has_a_hint(self):
        for check in checks.DEPLOYMENT_CHECKS:
            messages = _force(check)
            assert messages and all(m.hint for m in messages), check.__name__


def _force(check):
    """Run one check under settings crafted to make it speak, so the shape of its
    output can be asserted without knowing which check it is."""
    from django.test import override_settings

    with override_settings(
        ROOT_URLCONF="tests.urls",
        DEBUG=True,
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_SAMESITE=None,
        SECURE_SSL_REDIRECT=False,
        SECURE_HSTS_SECONDS=0,
        MIDDLEWARE=[],
        MEDIA_URL="/media/",
        CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
        VANTAGE={"AUDIT_LOG": False, "IMPORT_MAX_BYTES": 0},
        SECURE_CSP={"DIRECTIVES": {"script-src": ["'self'", "'nonce-x'"]}},
    ):
        return check(app_configs=None)


# ── W050: DEBUG ─────────────────────────────────────────────────────────────


class TestDebug:
    def test_debug_off_is_silent(self, mounted, settings):
        settings.DEBUG = False
        assert run(checks.check_deploy_debug) == []

    def test_debug_on_behind_a_mounted_panel_warns(self, mounted, settings):
        settings.DEBUG = True
        assert ids(run(checks.check_deploy_debug)) == ["vantage.W050"]

    def test_the_hint_says_what_a_traceback_page_would_expose(self, mounted, settings):
        settings.DEBUG = True
        hint = run(checks.check_deploy_debug)[0].hint
        assert "local variables" in hint


# ── W051: the cache the counters live in ────────────────────────────────────


LOCMEM = "django.core.cache.backends.locmem.LocMemCache"
DUMMY = "django.core.cache.backends.dummy.DummyCache"
REDIS = "django.core.cache.backends.redis.RedisCache"

THROTTLE = "vantage.middleware.LoginThrottleMiddleware"
PANEL = "vantage.middleware.PanelSecurityMiddleware"


class TestCache:
    def test_a_real_cache_is_silent(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": REDIS}}
        settings.MIDDLEWARE = [THROTTLE]
        assert run(checks.check_deploy_cache) == []

    def test_a_dummy_cache_under_the_throttle_warns(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": DUMMY}}
        settings.MIDDLEWARE = [THROTTLE]
        assert ids(run(checks.check_deploy_cache)) == ["vantage.W051"]

    def test_the_dummy_warning_names_the_throttle(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": DUMMY}}
        settings.MIDDLEWARE = [THROTTLE]
        assert "login throttle" in run(checks.check_deploy_cache)[0].msg

    def test_a_dummy_cache_with_nothing_depending_on_it_is_silent(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": DUMMY}}
        settings.MIDDLEWARE = []
        settings.VANTAGE = {"LIST_COUNT_ESTIMATES": False}
        assert run(checks.check_deploy_cache) == []

    def test_locmem_under_the_throttle_says_it_is_per_process(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": LOCMEM}}
        settings.MIDDLEWARE = [THROTTLE]
        assert "per-process" in run(checks.check_deploy_cache)[0].msg

    def test_locmem_under_the_rate_limits_says_so_too(self, mounted, settings):
        """The honest degradation the docs promise: N workers, N times the budget."""
        settings.CACHES = {"default": {"BACKEND": LOCMEM}}
        settings.MIDDLEWARE = [PANEL]
        message = run(checks.check_deploy_cache)[0]
        assert "rate limits" in message.msg
        assert "N times the configured budget" in message.hint

    def test_locmem_names_both_counters_when_both_are_installed(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": LOCMEM}}
        settings.MIDDLEWARE = [THROTTLE, PANEL]
        assert "login throttle and rate limits" in run(checks.check_deploy_cache)[0].msg

    def test_locmem_with_the_rate_limits_switched_off_is_silent(self, mounted, settings):
        settings.CACHES = {"default": {"BACKEND": LOCMEM}}
        settings.MIDDLEWARE = [PANEL]
        settings.VANTAGE = {"LIST_COUNT_ESTIMATES": False}
        from vantage import security

        security.DEFAULTS["RATE_LIMIT"] = False
        try:
            assert run(checks.check_deploy_cache) == []
        finally:
            security.DEFAULTS["RATE_LIMIT"] = True

    def test_locmem_with_only_estimates_is_silent(self, mounted, settings):
        """Estimates on locmem are merely slower, not falsely reassuring."""
        settings.CACHES = {"default": {"BACKEND": LOCMEM}}
        settings.MIDDLEWARE = []
        assert run(checks.check_deploy_cache) == []


# ── W052 / W053: transport and the session cookie ───────────────────────────


SECURITY_MW = "django.middleware.security.SecurityMiddleware"


class TestTransport:
    def test_a_complete_configuration_is_silent(self, mounted, settings):
        settings.MIDDLEWARE = [SECURITY_MW]
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_HSTS_SECONDS = 31536000
        settings.SECURE_CONTENT_TYPE_NOSNIFF = True
        assert run(checks.check_deploy_transport) == []

    def test_every_gap_is_reported_in_one_warning(self, mounted, settings):
        settings.MIDDLEWARE = []
        settings.SECURE_SSL_REDIRECT = False
        settings.SECURE_HSTS_SECONDS = 0
        messages = run(checks.check_deploy_transport)
        assert len(messages) == 1
        assert messages[0].msg.count(";") >= 2

    def test_a_missing_security_middleware_is_a_gap(self, mounted, settings):
        settings.MIDDLEWARE = []
        settings.SECURE_SSL_REDIRECT = True
        settings.SECURE_HSTS_SECONDS = 31536000
        assert "SecurityMiddleware is not installed" in run(checks.check_deploy_transport)[0].msg


class TestSessionCookie:
    def test_a_secure_cookie_is_silent(self, mounted, settings):
        settings.SESSION_COOKIE_SECURE = True
        settings.SESSION_COOKIE_HTTPONLY = True
        settings.SESSION_COOKIE_SAMESITE = "Lax"
        assert run(checks.check_deploy_session_cookie) == []

    def test_an_insecure_cookie_warns(self, mounted, settings):
        settings.SESSION_COOKIE_SECURE = False
        assert ids(run(checks.check_deploy_session_cookie)) == ["vantage.W053"]

    def test_samesite_none_is_reported_where_django_says_nothing(self, mounted, settings):
        settings.SESSION_COOKIE_SECURE = True
        settings.SESSION_COOKIE_SAMESITE = None
        assert "cross-site" in run(checks.check_deploy_session_cookie)[0].msg

    def test_the_hint_says_what_the_cookie_is_worth(self, mounted, settings):
        settings.SESSION_COOKIE_SECURE = False
        assert "every model" in run(checks.check_deploy_session_cookie)[0].hint


# ── W054 / W055: import ceiling and the audit log ───────────────────────────


class TestImportLimit:
    def test_a_sane_ceiling_is_silent(self, mounted, settings):
        settings.VANTAGE = {"IMPORT_MAX_BYTES": 5 * 1024 * 1024}
        assert run(checks.check_deploy_import_limit) == []

    def test_zero_does_not_mean_no_imports(self, mounted, settings):
        settings.VANTAGE = {"IMPORT_MAX_BYTES": 0}
        assert "does not disable" in run(checks.check_deploy_import_limit)[0].msg

    def test_a_non_number_is_reported(self, mounted, settings):
        settings.VANTAGE = {"IMPORT_MAX_BYTES": "5mb"}
        assert "not a byte count" in run(checks.check_deploy_import_limit)[0].msg

    def test_an_effectively_unbounded_ceiling_is_reported(self, mounted, settings):
        settings.VANTAGE = {"IMPORT_MAX_BYTES": 512 * 1024 * 1024}
        assert "effectively unbounded" in run(checks.check_deploy_import_limit)[0].msg

    def test_the_hint_mentions_the_web_server_in_front(self, mounted, settings):
        settings.VANTAGE = {"IMPORT_MAX_BYTES": 512 * 1024 * 1024}
        assert "web server" in run(checks.check_deploy_import_limit)[0].hint


class TestAuditLog:
    def test_on_is_silent(self, mounted, settings):
        settings.VANTAGE = {"AUDIT_LOG": True}
        assert run(checks.check_deploy_audit_log) == []

    def test_off_warns_because_the_gap_cannot_be_filled_later(self, mounted, settings):
        settings.VANTAGE = {"AUDIT_LOG": False}
        assert ids(run(checks.check_deploy_audit_log)) == ["vantage.W055"]


# ── W056: the two CSP questions ─────────────────────────────────────────────


NONCE_POLICY = {"DIRECTIVES": {"script-src": ["'self'", "'nonce-abc'"]}}
PROCESSOR = "vantage.security.csp_context"


def with_processor(settings, present=True):
    engine = dict(settings.TEMPLATES[0])
    options = dict(engine.get("OPTIONS") or {})
    processors = list(options.get("context_processors") or [])
    if present and PROCESSOR not in processors:
        processors.append(PROCESSOR)
    if not present:
        processors = [p for p in processors if p != PROCESSOR]
    options["context_processors"] = processors
    engine["OPTIONS"] = options
    settings.TEMPLATES = [engine]


class TestCspNonceProcessor:
    def test_no_policy_means_no_opinion(self, mounted, settings):
        assert run(checks.check_deploy_csp) == []

    def test_a_nonce_policy_without_the_processor_warns(self, mounted, settings):
        settings.SECURE_CSP = NONCE_POLICY
        with_processor(settings, present=False)
        assert ids(run(checks.check_deploy_csp)) == ["vantage.W056"]

    def test_installing_the_processor_settles_it(self, mounted, settings):
        settings.SECURE_CSP = NONCE_POLICY
        with_processor(settings, present=True)
        assert run(checks.check_deploy_csp) == []

    def test_a_report_only_policy_counts_too(self, mounted, settings):
        settings.SECURE_CSP_REPORT_ONLY = NONCE_POLICY
        with_processor(settings, present=False)
        assert ids(run(checks.check_deploy_csp)) == ["vantage.W056"]

    def test_a_policy_with_no_nonce_is_not_this_checks_business(self, mounted, settings):
        settings.SECURE_CSP = {"DIRECTIVES": {"script-src": ["'self'"]}}
        with_processor(settings, present=False)
        assert run(checks.check_deploy_csp) == []


class TestCspInlineAudit:
    def test_without_a_policy_it_says_nothing(self, mounted, settings):
        """Telling a project with no CSP which directives it is not enforcing is a
        lecture, not a check."""
        assert run(checks.check_deploy_csp_inline) == []

    def test_a_strict_policy_is_told_what_it_would_break(self, mounted, settings):
        settings.SECURE_CSP = {"DIRECTIVES": {"default-src": ["'self'"]}}
        assert ids(run(checks.check_deploy_csp_inline)) == ["vantage.W056"]

    def test_it_names_the_directive_to_edit(self, mounted, settings):
        settings.SECURE_CSP = {"DIRECTIVES": {"default-src": ["'self'"]}}
        assert "style-src-attr" in run(checks.check_deploy_csp_inline)[0].msg

    def test_it_points_at_the_templates(self, mounted, settings):
        settings.SECURE_CSP = {"DIRECTIVES": {"default-src": ["'self'"]}}
        assert ".html:" in run(checks.check_deploy_csp_inline)[0].msg

    def test_the_hint_says_a_nonce_cannot_fix_an_attribute(self, mounted, settings):
        settings.SECURE_CSP = {"DIRECTIVES": {"default-src": ["'self'"]}}
        assert "cannot authorise an attribute" in run(checks.check_deploy_csp_inline)[0].hint

    def test_a_policy_that_allows_them_is_silent(self, mounted, settings):
        settings.SECURE_CSP = {
            "DIRECTIVES": {
                "default-src": ["'self'"],
                "style-src-attr": ["'unsafe-inline'"],
                "script-src-attr": ["'unsafe-inline'"],
            }
        }
        assert run(checks.check_deploy_csp_inline) == []

    def test_it_follows_the_browsers_own_fallback_chain(self, mounted, settings):
        """``style-src-attr`` falls back to ``style-src``, then to ``default-src``.
        Getting that wrong makes the check cry wolf at a policy that works."""
        settings.SECURE_CSP = {
            "DIRECTIVES": {
                "default-src": ["'self'"],
                "style-src": ["'self'", "'unsafe-inline'"],
                "script-src-attr": ["'unsafe-inline'"],
            }
        }
        assert run(checks.check_deploy_csp_inline) == []

    def test_django_csps_setting_name_is_recognised(self, mounted, settings):
        settings.CONTENT_SECURITY_POLICY = {"DIRECTIVES": {"default-src": ["'self'"]}}
        assert ids(run(checks.check_deploy_csp_inline)) == ["vantage.W056"]

    def test_a_flat_policy_without_a_directives_key_works_too(self, mounted, settings):
        settings.SECURE_CSP = {"default-src": ["'self'"]}
        assert ids(run(checks.check_deploy_csp_inline)) == ["vantage.W056"]


# ── W057 / W058: the two middlewares ────────────────────────────────────────


class TestLoginThrottleCheck:
    def test_installed_is_silent(self, mounted, settings):
        settings.MIDDLEWARE = [THROTTLE]
        assert run(checks.check_deploy_login_throttle) == []

    def test_missing_warns(self, mounted, settings):
        settings.MIDDLEWARE = []
        assert ids(run(checks.check_deploy_login_throttle)) == ["vantage.W057"]

    @pytest.mark.parametrize(
        "other",
        [
            "axes.middleware.AxesMiddleware",
            "django_ratelimit.middleware.RatelimitMiddleware",
            "defender.middleware.FailedLoginMiddleware",
        ],
    )
    def test_someone_elses_solution_is_accepted(self, mounted, settings, other):
        """A checks framework that cannot be satisfied gets switched off."""
        settings.MIDDLEWARE = [other]
        assert run(checks.check_deploy_login_throttle) == []


class TestPanelMiddlewareCheck:
    def test_installed_is_silent(self, mounted, settings):
        settings.MIDDLEWARE = [PANEL]
        assert run(checks.check_deploy_panel_middleware) == []

    def test_missing_warns(self, mounted, settings):
        settings.MIDDLEWARE = []
        assert ids(run(checks.check_deploy_panel_middleware)) == ["vantage.W058"]

    def test_djangos_xframe_middleware_is_not_a_substitute(self, mounted, settings):
        """Its default is SAMEORIGIN, which still allows the panel to be framed."""
        settings.MIDDLEWARE = ["django.middleware.clickjacking.XFrameOptionsMiddleware"]
        assert ids(run(checks.check_deploy_panel_middleware)) == ["vantage.W058"]

    def test_the_hint_names_the_headers_that_go_missing(self, mounted, settings):
        settings.MIDDLEWARE = []
        hint = run(checks.check_deploy_panel_middleware)[0].hint
        assert "X-Frame-Options" in hint and "Referrer-Policy" in hint


# ── W059: uploads on the panel's own origin ─────────────────────────────────


class TestMediaOrigin:
    def test_a_relative_media_url_warns(self, mounted, settings):
        settings.MEDIA_URL = "/media/"
        assert ids(run(checks.check_deploy_media_origin)) == ["vantage.W059"]

    def test_it_names_a_model_that_holds_files(self, mounted, settings):
        settings.MEDIA_URL = "/media/"
        assert "testapp.Author" in run(checks.check_deploy_media_origin)[0].msg

    def test_a_separate_host_settles_it(self, mounted, settings):
        settings.MEDIA_URL = "https://media.example.com/"
        assert run(checks.check_deploy_media_origin) == []

    def test_a_protocol_relative_host_settles_it_too(self, mounted, settings):
        settings.MEDIA_URL = "//media.example.com/"
        assert run(checks.check_deploy_media_origin) == []

    def test_the_hint_says_why_the_filter_is_not_a_boundary(self, mounted, settings):
        settings.MEDIA_URL = "/media/"
        hint = run(checks.check_deploy_media_origin)[0].hint
        assert "filter, not a boundary" in hint


# ── panel_is_mounted ────────────────────────────────────────────────────────


class TestPanelIsMounted:
    def test_true_when_the_dashboard_reverses(self, mounted):
        assert checks.panel_is_mounted() is True

    def test_false_when_it_does_not(self, unmounted):
        assert checks.panel_is_mounted() is False

    def test_a_urlconf_that_raises_is_treated_as_not_mounted(self, settings):
        """`check` is the command people run when something is already broken; a checks
        module that raises while diagnosing a broken urlconf has removed the tool."""
        settings.ROOT_URLCONF = "tests.no_such_urlconf_module"
        assert checks.panel_is_mounted() is False
