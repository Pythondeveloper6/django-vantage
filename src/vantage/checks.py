"""System checks.

Misconfiguration should fail ``manage.py check``, not surface as a puzzling panel at
runtime. Every check here exists because the alternative is a silent wrong result.

Checks are registered from :meth:`vantage.apps.SimpleVantageConfig.ready`, not at import
time, so the app registry is populated before any model introspection runs.

Two families live here, and the difference between them is when they run.

**Correctness checks** (``vantage.E0xx``) run on every ``manage.py check``. They catch
configuration that is wrong everywhere: a misspelled setting, a panel referring to a
field that does not exist. Being wrong in development is the point — that is where you
want to find out.

**Deployment checks** (``vantage.W05x``) run only under ``--deploy``. They are about the
gap between a laptop and the internet, and every one of them is *correct* on a laptop.
Registering them in the normal set would train everybody to ignore the output, which
costs more than the checks are worth.

The deployment set deliberately overlaps Django's own ``security.W0xx`` in three places
(cookie flags, transport, framing). That is not an oversight. Django's are written for
any site and are advisory by design; these fire only when the panel is actually mounted,
and the hint says what is behind the panel — which is every model in the database,
editable, by whoever holds that cookie. A duplicated warning with the right consequence
attached is worth more than a silent deferral to someone else's.

The ids run ``W050`` to ``W059`` and each one is a separate decision, so that silencing
the one you have consciously accepted does not silence the nine you have not. ``W056``
is the exception: it covers two messages about the same question — whether the panel and
your Content-Security-Policy can both be true — because being told half of that answer
is worse than being told none of it.
"""

from __future__ import annotations

import difflib

from django.core.checks import Error, Warning

from vantage.conf import CHOICES, DEFAULTS


def check_vantage_settings(app_configs, **kwargs) -> list[Error]:
    """Reject unknown keys and invalid choices in the ``VANTAGE`` setting."""
    from django.conf import settings as django_settings

    errors: list[Error] = []
    user = getattr(django_settings, "VANTAGE", None)

    if user is None:
        return errors

    if not isinstance(user, dict):
        return [
            Error(
                f"The VANTAGE setting must be a dict, got {type(user).__name__}.",
                id="vantage.E001",
            )
        ]

    for key in sorted(set(user) - set(DEFAULTS)):
        close = difflib.get_close_matches(key, DEFAULTS, n=1, cutoff=0.6)
        errors.append(
            Error(
                f"{key!r} is not a recognised Vantage setting.",
                hint=f"Did you mean {close[0]!r}?" if close else None,
                id="vantage.E002",
            )
        )

    for key, allowed in CHOICES.items():
        if key in user and user[key] not in allowed:
            errors.append(
                Error(
                    f"VANTAGE[{key!r}] must be one of {', '.join(allowed)}; "
                    f"got {user[key]!r}.",
                    id="vantage.E003",
                )
            )

    errors.extend(_check_brand(user))
    return errors


def _check_brand(user: dict) -> list[Error]:
    """The brand colour and radius are rendered into every page's inline stylesheet,
    the sign-in page's included, so a typo in either is not a cosmetic problem: an
    unreadable colour raises on every render. Caught here instead, with the same
    validators the settings screen applies to the values it stores."""
    from django.core.exceptions import ValidationError

    from vantage.models import validate_brand_color, validate_css_length

    errors: list[Error] = []
    for key, validate, check_id, hint in (
        ("BRAND_COLOR", validate_brand_color, "vantage.E004", "Use a hex colour like '#6366f1'."),
        ("BRAND_RADIUS", validate_css_length, "vantage.E005", "Use a length like '0.5rem'."),
    ):
        value = user.get(key)
        if value is None:
            continue
        try:
            if not isinstance(value, str):
                raise ValidationError(f"expected a string, got {type(value).__name__}")
            validate(value)
        except ValidationError as exc:
            errors.append(
                Error(
                    f"VANTAGE[{key!r}] is invalid: {' '.join(exc.messages)}",
                    hint=hint,
                    id=check_id,
                )
            )
    return errors


def check_panels(app_configs, **kwargs) -> list[Error]:
    """Validate every registered panel on every live site.

    Panels left entirely at their inferred defaults are skipped — there is no
    user-written configuration to be wrong.
    """
    from vantage.panels import ModelPanel
    from vantage.sites import all_sites

    if app_configs is not None:
        app_configs = set(app_configs)

    errors: list[Error] = []
    for site in all_sites:
        for panel in site.registry.panels():
            if type(panel) is ModelPanel:
                continue
            if app_configs is not None and panel.opts.app_config not in app_configs:
                continue
            errors.extend(panel.check())
    return errors


# ── deployment checks ───────────────────────────────────────────────────────
#
# Registered with ``deploy=True``, so they run under ``manage.py check --deploy`` and
# stay quiet otherwise. Every one of them is silent unless the panel is actually
# reachable: a project that installs the app but never mounts the URLs has an admin
# panel nobody can open, and warning it about its cookie flags is noise.


def panel_is_mounted() -> bool:
    """Whether a Vantage site is reachable over HTTP.

    Asked by reversing the dashboard rather than by inspecting ``INSTALLED_APPS``,
    because being installed and being *exposed* are different facts and only the
    second one is what these checks are about. A project can have the app on for the
    models and the templatetags and never mount ``vantage.urls``.

    Every failure mode is treated as "not mounted". This runs during ``check``, which
    is the command people run when something is already broken; a checks module that
    raises while diagnosing a broken urlconf has removed the tool they were using.
    """
    from django.urls import reverse

    try:
        reverse("vantage:dashboard")
    except Exception:  # NoReverseMatch, ImproperlyConfigured, a urlconf that raises
        return False
    return True


def _vantage_setting(name: str):
    """One Vantage setting, resolved through the settings layer, never raising.

    Two sets of defaults, so two paths. The hardening keys belong to
    :mod:`vantage.security` and are asked of it directly: ``conf.DEFAULTS`` registers
    them as ``None`` so they are *settable*, but ``None`` there means "no opinion", and
    reading it as the value would have a check quietly decide the feature is off. This
    docstring predicted that failure before the registration landed and then it
    happened — ``check_deploy_cache`` stopped seeing the rate limiter as installed.

    Everything else resolves through the settings layer and picks up database
    overrides.
    """
    from vantage.security import DEFAULTS as SECURITY_DEFAULTS

    if name in SECURITY_DEFAULTS:
        from vantage.security import _setting

        return _setting(name)

    from vantage.conf import vantage_settings

    try:
        return getattr(vantage_settings, name)
    except Exception:  # a broken VANTAGE dict is check_vantage_settings' problem
        pass
    return DEFAULTS.get(name)


def _middleware() -> list[str]:
    from django.conf import settings as django_settings

    return [str(item) for item in getattr(django_settings, "MIDDLEWARE", None) or []]


def check_deploy_debug(app_configs, **kwargs) -> list[Warning]:
    """``DEBUG`` on, with the panel mounted. (``vantage.W050``)

    Django already warns about ``DEBUG`` in the abstract. This one is separate because
    the consequence is specific and much worse than the general case: with ``DEBUG``
    on, any unhandled exception inside the panel renders a traceback page carrying
    local variables, and the local variables on an admin panel's stack are rows of
    production data, the settings dict, and frequently a database password.
    """
    from django.conf import settings as django_settings

    if not getattr(django_settings, "DEBUG", False) or not panel_is_mounted():
        return []
    return [
        Warning(
            "DEBUG is True and the Vantage panel is mounted.",
            hint=(
                "An unhandled error in the panel will render a traceback page "
                "containing local variables — which here means rows of your data and "
                "the contents of settings. Set DEBUG = False."
            ),
            id="vantage.W050",
        )
    ]


def check_deploy_cache(app_configs, **kwargs) -> list[Warning]:
    """A cache that cannot hold anything, under features that assume it can.
    (``vantage.W051``)

    Three features degrade silently rather than loudly, which is why this is a check
    and not a runtime error:

    * row-count estimates (``LIST_COUNT_ESTIMATES``) re-run a ``COUNT`` or an
      ``EXPLAIN`` on every render, turning a caching optimisation into a per-request
      cost on the largest tables in the database;
    * the login throttle counts failures in the cache, so on a dummy backend it
      records nothing, blocks nobody, and looks exactly like a working throttle;
    * the panel's per-surface rate limits count the same way and fail the same way.

    The last two are the reason this is worth a warning rather than a note. A limiter
    that silently does not limit is worse than no limiter, because it is on the
    checklist with a tick next to it.
    """
    from django.conf import settings as django_settings

    if not panel_is_mounted():
        return []

    caches = getattr(django_settings, "CACHES", None) or {}
    default = caches.get("default") or {}
    backend = str(default.get("BACKEND", ""))

    throttled = any("vantage.middleware.LoginThrottleMiddleware" in m for m in _middleware())
    limited = any("vantage.middleware.PanelSecurityMiddleware" in m for m in _middleware())
    limited = limited and bool(_vantage_setting("RATE_LIMIT"))
    estimates = bool(_vantage_setting("LIST_COUNT_ESTIMATES"))
    if not throttled and not limited and not estimates:
        return []

    wants = []
    if throttled:
        wants.append("the login throttle")
    if limited:
        wants.append("the panel rate limits")
    if estimates:
        wants.append("row-count estimates")
    wanted = ", ".join(wants[:-1]) + (" and " if len(wants) > 1 else "") + wants[-1]

    if not backend or "dummy" in backend.lower():
        return [
            Warning(
                f"The default cache is a dummy backend, but {wanted} depend(s) on it.",
                hint=(
                    "Nothing written to a dummy cache is ever read back. Every counter "
                    "will count to zero forever and refuse nobody. Configure a real "
                    "CACHES['default'] — Redis, Memcached, or the database backend."
                ),
                id="vantage.W051",
            )
        ]

    if "locmem" in backend.lower() and (throttled or limited):
        counters = " and ".join(
            name
            for name, on in (("login throttle", throttled), ("rate limits", limited))
            if on
        )
        return [
            Warning(
                f"The {counters} are installed but the default cache is per-process.",
                hint=(
                    "LocMemCache is not shared between workers, so with N workers a "
                    "caller gets N times the configured budget and a lockout applies "
                    "only to whichever worker happened to answer. On one process this "
                    "is correct and this warning is noise; on any real deployment the "
                    "protection is a fraction of what it reads as. Use a shared cache."
                ),
                id="vantage.W051",
            )
        ]

    return []


def check_deploy_transport(app_configs, **kwargs) -> list[Warning]:
    """``SECURE_*`` gaps in front of a mounted panel. (``vantage.W052``)

    Reported as one warning listing every gap rather than one per setting, because
    they are one decision — "is this panel only reachable over TLS" — and answering it
    fixes all of them at once.

    Overlaps Django's ``security.W004``/``W006``/``W008``. Deliberately: those are
    skipped entirely when ``SecurityMiddleware`` is not installed, which is exactly the
    configuration that needs them most, and their hints do not know that what is behind
    this particular URL is every row in the database.
    """
    from django.conf import settings as django_settings

    if not panel_is_mounted():
        return []

    gaps: list[str] = []
    if not any("django.middleware.security.SecurityMiddleware" in m for m in _middleware()):
        gaps.append("SecurityMiddleware is not installed")
    if not getattr(django_settings, "SECURE_SSL_REDIRECT", False):
        gaps.append("SECURE_SSL_REDIRECT is off")
    if not getattr(django_settings, "SECURE_HSTS_SECONDS", 0):
        gaps.append("SECURE_HSTS_SECONDS is 0")
    if not getattr(django_settings, "SECURE_CONTENT_TYPE_NOSNIFF", True):
        gaps.append("SECURE_CONTENT_TYPE_NOSNIFF is off")

    if not gaps:
        return []
    return [
        Warning(
            "The Vantage panel is mounted without full transport security: "
            + "; ".join(gaps)
            + ".",
            hint=(
                "A session cookie for this panel is a credential for every model in "
                "the database. Terminate TLS in front of the panel and set "
                "SECURE_SSL_REDIRECT = True, SECURE_HSTS_SECONDS to a real value, and "
                "SECURE_CONTENT_TYPE_NOSNIFF = True. The last one also stops an "
                "uploaded logo being sniffed into something executable."
            ),
            id="vantage.W052",
        )
    ]


def check_deploy_session_cookie(app_configs, **kwargs) -> list[Warning]:
    """A session cookie without ``Secure`` or without ``HttpOnly``. (``vantage.W053``)

    ``SameSite`` is checked too, and is the one Django's own deploy checks do not
    cover. ``SESSION_COOKIE_SAMESITE = None`` sends the panel's session cookie on
    cross-site requests, which turns every state-changing GET in the panel into a
    CSRF target that the CSRF middleware does not see.
    """
    from django.conf import settings as django_settings

    if not panel_is_mounted():
        return []

    gaps: list[str] = []
    if not getattr(django_settings, "SESSION_COOKIE_SECURE", False):
        gaps.append("SESSION_COOKIE_SECURE is False, so the cookie is sent over plain HTTP")
    if not getattr(django_settings, "SESSION_COOKIE_HTTPONLY", True):
        gaps.append("SESSION_COOKIE_HTTPONLY is False, so script can read the cookie")
    if getattr(django_settings, "SESSION_COOKIE_SAMESITE", "Lax") is None:
        gaps.append("SESSION_COOKIE_SAMESITE is None, so the cookie is sent cross-site")

    if not gaps:
        return []
    return [
        Warning(
            "The session cookie protecting the Vantage panel is under-protected: "
            + "; ".join(gaps)
            + ".",
            hint=(
                "Whoever holds this cookie holds the panel, and the panel holds every "
                "model you registered. Set SESSION_COOKIE_SECURE = True and leave "
                "SESSION_COOKIE_HTTPONLY and SESSION_COOKIE_SAMESITE at their defaults."
            ),
            id="vantage.W053",
        )
    ]


def check_deploy_import_limit(app_configs, **kwargs) -> list[Warning]:
    """An unbounded ``IMPORT_MAX_BYTES``. (``vantage.W054``)

    The importer reads the whole upload into memory to parse it. Without a ceiling,
    one authenticated user with ``add`` on any model can exhaust a worker's memory
    with a single request, and the panel is where the people with ``add`` are.

    Zero is called out separately from a large number because zero does not mean
    "no imports" — ``importer._cap`` treats a falsy value as *unset* and falls back to
    its own default, so a project that wrote ``0`` intending to switch imports off has
    a 5 MB limit and does not know it.
    """
    if not panel_is_mounted():
        return []

    value = _vantage_setting("IMPORT_MAX_BYTES")

    if value in (0, None, False):
        return [
            Warning(
                f"VANTAGE['IMPORT_MAX_BYTES'] is {value!r}, which does not disable "
                "imports.",
                hint=(
                    "A falsy value is read as 'unset' and falls back to the 5 MB "
                    "default, so imports are still enabled at that size. To refuse "
                    "imports, deny the panel's add permission or set a small cap."
                ),
                id="vantage.W054",
            )
        ]

    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return [
            Warning(
                f"VANTAGE['IMPORT_MAX_BYTES'] is {value!r}, which is not a byte count.",
                hint="Set an integer number of bytes, for example 5 * 1024 * 1024.",
                id="vantage.W054",
            )
        ]

    if numeric > 64 * 1024 * 1024:
        return [
            Warning(
                f"VANTAGE['IMPORT_MAX_BYTES'] is {numeric} bytes "
                f"({numeric // (1024 * 1024)} MB), which is effectively unbounded.",
                hint=(
                    "The upload is decoded into memory to be parsed, so this is the "
                    "amount of RAM one authenticated user can claim per request. Also "
                    "set a body-size limit in the web server in front of Django — this "
                    "cap runs after the request has already been received."
                ),
                id="vantage.W054",
            )
        ]
    return []


def check_deploy_audit_log(app_configs, **kwargs) -> list[Warning]:
    """``AUDIT_LOG`` switched off on a mounted panel. (``vantage.W055``)

    Not a vulnerability and not always wrong — some deployments log elsewhere. It is
    a warning because it is unrecoverable: the log is the only record of who changed
    what through the panel, and it cannot be reconstructed after the fact. The one
    time anybody wants it is the one time it is too late to turn on.
    """
    if not panel_is_mounted() or _vantage_setting("AUDIT_LOG"):
        return []
    return [
        Warning(
            "VANTAGE['AUDIT_LOG'] is off, so panel changes are not being recorded.",
            hint=(
                "Nothing will record who edited or deleted a row through the panel, "
                "and the gap cannot be filled in later. Leave it on unless another "
                "system is capturing the same thing with the acting user attached."
            ),
            id="vantage.W055",
        )
    ]


def check_deploy_csp(app_configs, **kwargs) -> list[Warning]:
    """A nonce-based CSP that the panel's shell will not satisfy. (``vantage.W056``)

    The shell inlines a ``<style>`` block of theme tokens and a blocking ``<script>``
    that stamps the saved theme before first paint. Both accept a nonce, but only if
    ``vantage.security.csp_context`` is in the template context processors — that is
    what puts the attribute on the tags.

    Without it the policy is not merely suboptimal, it is *broken*: the tokens do not
    apply, so the panel renders unstyled, and the theme flashes. The failure is
    visible, which is good, but it happens in production rather than at check time,
    which is not.
    """
    from django.conf import settings as django_settings

    if not panel_is_mounted():
        return []

    policies = [
        getattr(django_settings, "SECURE_CSP", None),
        getattr(django_settings, "SECURE_CSP_REPORT_ONLY", None),
    ]
    uses_nonce = any(_policy_uses_nonce(policy) for policy in policies)
    if not uses_nonce:
        return []

    if _has_context_processor("vantage.security.csp_context"):
        return []

    return [
        Warning(
            "A nonce-based CSP is configured but Vantage's nonce context processor "
            "is not installed.",
            hint=(
                "Add 'vantage.security.csp_context' to the context_processors of your "
                "DjangoTemplates backend. Without it the panel's inline theme tokens "
                "and no-flash script carry no nonce, so the policy blocks them and the "
                "panel renders unstyled. See docs/security.md."
            ),
            id="vantage.W056",
        )
    ]


def check_deploy_csp_inline(app_configs, **kwargs) -> list[Warning]:
    """Inline constructs a configured policy would block. (``vantage.W056``)

    Shares an id with :func:`check_deploy_csp` because it is the same decision seen from
    the other side — "will this panel work under the policy you wrote" — and silencing
    one without the other leaves you half informed about one question.

    A nonce fixes ``<script>`` and ``<style>`` *elements*. It cannot be attached to a
    ``style="…"`` attribute: those are governed by ``style-src-attr``, which takes only
    ``'unsafe-inline'`` or a hash. The panel emits a handful of them and they are not
    removable — a tree row's indent and a progress bar's width are per-row values, and
    moving them to a stylesheet would mean generating one stylesheet per page.

    Silent unless a policy is actually configured. Telling a project with no CSP which
    directives it is not enforcing is a lecture, not a check; the findings only become
    actionable once someone has written a policy for them to contradict.
    """
    from vantage.security import scan_panel_templates

    if not panel_is_mounted():
        return []

    policy = _configured_policy()
    if policy is None:
        return []

    findings = scan_panel_templates()
    if not findings:
        return []

    # Grouped by directive rather than listed by file: the reader's next action is to
    # edit one policy line, and a list of seven template paths does not tell them which.
    needed: dict[str, list[str]] = {}
    for finding in findings:
        needed.setdefault(finding.directive, []).append(
            f"{finding.template}:{finding.line}"
        )

    unsatisfied = {
        directive: sites
        for directive, sites in needed.items()
        if not _policy_allows_inline(policy, directive)
    }
    if not unsatisfied:
        return []

    lines = [
        f"{directive} — {len(sites)} place(s): {', '.join(sorted(sites)[:4])}"
        + ("…" if len(sites) > 4 else "")
        for directive, sites in sorted(unsatisfied.items())
    ]
    return [
        Warning(
            "The panel emits inline markup your Content-Security-Policy would block: "
            + "; ".join(lines)
            + ".",
            hint=(
                "A nonce cannot authorise an attribute, only an element. Add "
                "\"style-src-attr\": [\"'unsafe-inline'\"] (and script-src-attr if it "
                "is listed above) to the policy for the panel's URLs, or accept that "
                "tree indentation and progress bars will not render. See "
                "docs/security.md for a policy that works."
            ),
            id="vantage.W056",
        )
    ]


def check_deploy_panel_middleware(app_configs, **kwargs) -> list[Warning]:
    """A mounted panel with no rate limits and no response headers. (``vantage.W058``)

    ``PanelSecurityMiddleware`` is the only thing that sets ``X-Frame-Options``,
    ``Referrer-Policy`` and ``X-Content-Type-Options`` on panel responses, and the only
    thing that budgets autocomplete, export and import. Without it the panel is
    frameable, its URLs leak through ``Referer``, and one signed-in account can page
    an entire table through the autocomplete endpoint as fast as the database answers.

    Django's ``XFrameOptionsMiddleware`` is *not* accepted as a substitute. Its default
    is ``SAMEORIGIN``, which still allows the panel to be framed by the site it is part
    of, and framing an admin panel from anywhere is not something worth allowing.
    """
    if not panel_is_mounted():
        return []
    if any("vantage.middleware.PanelSecurityMiddleware" in m for m in _middleware()):
        return []
    return [
        Warning(
            "The Vantage panel is mounted without PanelSecurityMiddleware.",
            hint=(
                "Add 'vantage.middleware.PanelSecurityMiddleware' after "
                "AuthenticationMiddleware. Without it nothing sets X-Frame-Options, "
                "Referrer-Policy or X-Content-Type-Options on panel responses, and "
                "autocomplete, export and import have no request budget. See "
                "docs/security.md."
            ),
            id="vantage.W058",
        )
    ]


def check_deploy_media_origin(app_configs, **kwargs) -> list[Warning]:
    """User uploads served from the panel's own origin. (``vantage.W059``)

    The panel accepts a file for every ``FileField`` and ``ImageField`` on every model
    it has registered, plus a logo and a favicon on the settings screen. Uploads are
    screened — content is sniffed rather than trusted, and an SVG containing script is
    refused — but screening is a filter, and a filter is a list of the attacks somebody
    thought of.

    The structural fix is an origin boundary. A file served from
    ``media.example.com`` that turns out to be HTML runs as ``media.example.com`` and
    can read nothing; the same file served from the panel's origin runs with the
    session cookie of a person who can edit every table in the database.

    A relative ``MEDIA_URL`` is the signal, because that is what "same origin" looks
    like in settings. Silent when ``MEDIA_URL`` names a host, and silent when nothing
    registered can hold a file.
    """
    from django.conf import settings as django_settings

    if not panel_is_mounted():
        return []

    media_url = str(getattr(django_settings, "MEDIA_URL", "") or "")
    if "//" in media_url:  # an absolute URL, or a protocol-relative one
        return []

    holders = _models_with_file_fields()
    if not holders:
        return []

    listed = ", ".join(holders[:5]) + ("…" if len(holders) > 5 else "")
    return [
        Warning(
            f"Uploads are served from the panel's own origin (MEDIA_URL={media_url!r}), "
            f"and {len(holders)} registered model(s) accept files: {listed}.",
            hint=(
                "A file that reaches a browser as HTML from this origin runs with the "
                "session cookie of whoever opens it — and the people who open panel "
                "links are the people who can edit every table. Vantage sniffs content "
                "rather than trusting the extension and refuses SVGs containing "
                "script, but that is a filter, not a boundary. Serve MEDIA_URL from a "
                "separate host, or from storage that sets Content-Disposition: "
                "attachment. See docs/security.md."
            ),
            id="vantage.W059",
        )
    ]


def _models_with_file_fields() -> list[str]:
    """``"app.Model"`` for every registered model that can hold an uploaded file."""
    from django.db import models as django_models

    from vantage.sites import all_sites

    found: set[str] = set()
    for site in all_sites:
        for panel in site.registry.panels():
            model = panel.model
            for field in model._meta.get_fields():
                if isinstance(field, django_models.FileField):
                    found.add(f"{model._meta.app_label}.{model.__name__}")
                    break
    return sorted(found)


def _configured_policy() -> dict | None:
    """The project's CSP as a directives dict, from whichever mechanism set it.

    Django 6.0's ``SECURE_CSP``, its report-only twin, and django-csp's
    ``CONTENT_SECURITY_POLICY`` are all recognised, because a project on any of the
    three has answered the same question and should not be asked it again.
    """
    from django.conf import settings as django_settings

    for name in ("SECURE_CSP", "SECURE_CSP_REPORT_ONLY", "CONTENT_SECURITY_POLICY"):
        value = getattr(django_settings, name, None)
        if isinstance(value, dict) and value:
            directives = value.get("DIRECTIVES")
            return directives if isinstance(directives, dict) else value
    return None


def _policy_allows_inline(policy: dict, directive: str) -> bool:
    """Whether ``policy`` permits inline content for ``directive``.

    Falls back the way a browser does: ``style-src-attr`` inherits from ``style-src``,
    which inherits from ``default-src``. Getting the fallback wrong in either direction
    makes the check useless — too strict and it cries wolf at a policy that works, too
    loose and it stays quiet about the one that does not.
    """
    fallbacks = {
        "style-src-attr": ("style-src-attr", "style-src", "default-src"),
        "script-src-attr": ("script-src-attr", "script-src", "default-src"),
        "style-src": ("style-src", "default-src"),
        "script-src": ("script-src", "default-src"),
    }
    for name in fallbacks.get(directive, (directive, "default-src")):
        if name not in policy:
            continue
        values = policy[name]
        if isinstance(values, str | bytes):
            values = [values]
        return any("unsafe-inline" in str(item).lower() for item in values or [])
    # The directive is not covered at all, so nothing is enforced for it.
    return True


def _policy_uses_nonce(policy) -> bool:
    """Whether a ``SECURE_CSP``-shaped dict asks for a nonce anywhere."""
    if not isinstance(policy, dict):
        return False
    for value in (policy.get("DIRECTIVES") or policy).values():
        if isinstance(value, str | bytes):
            values = [value]
        elif isinstance(value, list | tuple | set):
            values = list(value)
        else:
            continue
        for item in values:
            # Django spells it with a sentinel object whose repr contains "nonce";
            # django-csp and hand-written policies use the literal string.
            if "nonce" in str(item).lower():
                return True
    return False


def _has_context_processor(dotted: str) -> bool:
    from django.conf import settings as django_settings

    for engine in getattr(django_settings, "TEMPLATES", None) or []:
        options = engine.get("OPTIONS") or {}
        if dotted in (options.get("context_processors") or []):
            return True
    return False


def check_deploy_login_throttle(app_configs, **kwargs) -> list[Warning]:
    """No rate limiting in front of the sign-in form. (``vantage.W057``)

    Silent when the panel is not mounted, and silent when the project has *any* other
    middleware whose name suggests throttling — a project already running
    django-axes or django-ratelimit has solved this, and a checks framework that
    cannot be satisfied gets switched off.
    """
    if not panel_is_mounted():
        return []

    middleware = _middleware()
    if any("vantage.middleware.LoginThrottleMiddleware" in m for m in middleware):
        return []
    hints = ("axes", "ratelimit", "rate_limit", "throttle", "defender", "brute")
    if any(any(h in m.lower() for h in hints) for m in middleware):
        return []

    return [
        Warning(
            "The Vantage panel is mounted with no rate limiting on sign-in.",
            hint=(
                "Add 'vantage.middleware.LoginThrottleMiddleware' after "
                "AuthenticationMiddleware. It counts failures per username *and* "
                "client address, so it does not lock out an office sharing one IP and "
                "cannot be used to hold a known account shut. See docs/security.md."
            ),
            id="vantage.W057",
        )
    ]


#: Every deployment check, so ``apps.py`` registers them in one loop and adding one
#: here is not also an edit there.
DEPLOYMENT_CHECKS = (
    check_deploy_debug,
    check_deploy_cache,
    check_deploy_transport,
    check_deploy_session_cookie,
    check_deploy_import_limit,
    check_deploy_audit_log,
    check_deploy_csp,
    check_deploy_csp_inline,
    check_deploy_login_throttle,
    check_deploy_panel_middleware,
    check_deploy_media_origin,
)
