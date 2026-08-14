"""Middleware. Two of them, and they are separate on purpose.

:class:`LoginThrottleMiddleware` is about *the project's* sign-in form, wherever it is —
it attaches to Django's ``LoginView`` and does nothing else. :class:`PanelSecurityMiddleware`
is about *the panel's* URLs and nothing outside them. A project that mounts the panel
behind a VPN and wants only the sign-in protection installs one; a project already
running django-axes installs the other. Merging them would mean neither could be
declined.

Install both after ``AuthenticationMiddleware``::

    MIDDLEWARE = [
        ...
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "vantage.middleware.LoginThrottleMiddleware",
        "vantage.middleware.PanelSecurityMiddleware",
        ...
    ]

After, not before: :class:`PanelSecurityMiddleware` keys its budgets on
``request.user``, and before ``AuthenticationMiddleware`` there is no such attribute —
it would silently fall back to keying every signed-in colleague on their shared office
address. It must also come *after* ``CsrfViewMiddleware``, so that a forged POST is
refused before this layer parses the file attached to it.

──────────────────────────────────────────────────────────────────────────────

:class:`LoginThrottleMiddleware` is deliberately middleware rather than a subclass of
:class:`vantage.authviews.LoginView`. Three reasons, in order of how much they matter:

1. **It covers the login view a project actually uses.** A panel that hardens only its
   own login view hardens nothing for the many projects that mount
   ``django.contrib.auth.urls``, or their own form, or both. This attaches to the view
   *class*, so anything deriving from ``django.contrib.auth.views.LoginView`` is
   covered without knowing Vantage exists.
2. **It is genuinely optional.** No middleware installed, no behaviour change and no
   signal receivers connected — not "a feature flag that defaults to off", which still
   leaves the code in the request path.
3. **It cannot be forgotten at the wrong layer.** Counting failures from the
   ``user_login_failed`` signal means the count comes from Django's own definition of
   a failed authentication rather than from this package guessing at a status code.
"""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.http import HttpResponse, JsonResponse

from vantage.security import (
    actor_for,
    apply_security_headers,
    client_ip,
    inspect_upload,
    rate_limit_for,
    throttle_from_settings,
)

__all__ = [
    "LoginThrottleMiddleware",
    "PanelSecurityMiddleware",
    "is_panel_request",
    "normalise_username",
    "surface_for",
]

logger = logging.getLogger("vantage.security")

#: Set on the request by :meth:`LoginThrottleMiddleware.process_view` and read by the
#: signal receivers. Its presence is what scopes counting to *login view* POSTs: a
#: project calling ``authenticate()`` from an API, a management command or a test
#: fixture also fires ``user_login_failed``, and none of those should spend a user's
#: budget on the sign-in form.
REQUEST_ATTR = "_vantage_login_throttle"

_connected = False


def normalise_username(value: object) -> str:
    """Fold a submitted username to the form the counter keys on.

    Case-folded, because otherwise ``admin``, ``Admin`` and ``ADMIN`` are three
    budgets and the throttle is trivially defeated by holding down shift. Folded
    rather than lowercased: ``casefold`` handles the non-ASCII pairs that ``lower``
    leaves distinct, and a username is not always ASCII.

    Whitespace is stripped for the same reason — ``" admin"`` and ``"admin"`` reach
    the same account through the auth backend, so they must reach the same counter.
    Length is capped because the value is attacker-supplied and ends up hashed; there
    is no reason to feed a megabyte of it to sha256 on every failed login.
    """
    return str(value or "").strip().casefold()[:320]


class LoginThrottleMiddleware:
    """Refuse a sign-in POST once one username has failed too often from one address.

    Subclass it to change the refusal:

    * :meth:`throttled_response` builds the 429.
    * :meth:`username_from` reads the identifier out of the POST for a custom form.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        _connect_signals()

    def __call__(self, request):
        return self.get_response(request)

    # ── the check ───────────────────────────────────────────────────────────

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Block before the view runs, so a blocked attempt costs no password hash.

        Running here rather than in ``__call__`` is what lets the decision be made on
        the resolved *view* instead of on a path pattern. Matching ``settings.LOGIN_URL``
        as a string would miss a project that mounts the auth views somewhere else, miss
        a second login view, and — worse — match a path that merely starts the same way.
        """
        if request.method != "POST":
            return None
        if not self.is_login_view(view_func):
            return None

        throttle = throttle_from_settings()
        if not throttle.enabled or throttle.limit <= 0:
            return None

        username = self.username_from(request)
        if not username:
            # Nothing to key on. Refusing here would turn a blank form submission into
            # a 429, and keying the whole deployment on the empty string would be the
            # IP-less, username-less counter that locks out everybody at once.
            return None

        address = client_ip(request)
        if not address:
            # No usable address means the pair key collapses to username-only, which is
            # precisely the denial-of-service this design refuses to build. Allowing the
            # attempt is the lesser failure: a throttle that is absent is better than one
            # an attacker can point at a named account.
            logger.debug("Vantage login throttle skipped: no usable client address.")
            return None

        # Recorded for the signal receivers whether or not this attempt is blocked, so
        # the pair the failure is counted against is the pair the check was made on.
        setattr(request, REQUEST_ATTR, (username, address))

        state = throttle.state(username, address)
        if state.blocked:
            logger.warning(
                "Vantage blocked a sign-in attempt: too many failures for this "
                "username from %s.",
                address,
            )
            return self.throttled_response(request, state)
        return None

    # ── hooks ───────────────────────────────────────────────────────────────

    @staticmethod
    def is_login_view(view_func) -> bool:
        """Whether this view is a Django login view (or anything derived from one)."""
        from django.contrib.auth.views import LoginView

        view_class = getattr(view_func, "view_class", None)
        return bool(view_class and issubclass(view_class, LoginView))

    @staticmethod
    def username_from(request) -> str:
        """The submitted identifier.

        ``username`` first because that is the *form field* name Django's
        ``AuthenticationForm`` uses whatever ``USERNAME_FIELD`` is set to — the common
        mistake here is to read ``USERNAME_FIELD`` and find nothing on a project whose
        users log in with an email address through the stock form.
        """
        from django.contrib.auth import get_user_model

        post = getattr(request, "POST", None) or {}
        value = post.get("username")
        if not value:
            field = getattr(get_user_model(), "USERNAME_FIELD", "username")
            value = post.get(field) or post.get("email")
        return normalise_username(value)

    @staticmethod
    def throttled_response(request, state) -> HttpResponse:
        """The refusal. 429 with ``Retry-After``.

        Plain text rather than a rendered page on purpose: this handler must work when
        the panel's own shell cannot be built, and it is reached by scripts far more
        often than by people. The wording says *too many attempts* and never whether
        the username exists — the entire value of the pair key would be undone by a
        message that confirms an account.
        """
        response = HttpResponse(
            "Too many sign-in attempts. Wait a few minutes and try again.",
            content_type="text/plain; charset=utf-8",
            status=429,
        )
        if state.retry_after:
            response["Retry-After"] = str(int(state.retry_after))
        # Never cached, and never counted as a page. A 429 stored by an intermediary
        # would keep refusing long after the window closed.
        response["Cache-Control"] = "no-store"
        return response


# ── signal receivers ────────────────────────────────────────────────────────


def _connect_signals() -> None:
    """Wire the counters up once, when the middleware is first instantiated.

    Connecting from here rather than from ``AppConfig.ready`` is what makes the
    feature opt-in: a project that has not added the middleware has no receivers,
    so a failed login anywhere in that project costs nothing at all.

    ``dispatch_uid`` makes this idempotent, which matters because middleware is
    instantiated once per process *per handler* — a project running WSGI and ASGI in
    one process, or a test that rebuilds the stack, would otherwise double-count every
    failure and halve the effective limit.
    """
    global _connected
    if _connected:
        return
    user_login_failed.connect(_on_login_failed, dispatch_uid="vantage-login-throttle")
    user_logged_in.connect(_on_login_succeeded, dispatch_uid="vantage-login-throttle")
    _connected = True


def _on_login_failed(sender, credentials=None, request=None, **kwargs) -> None:
    pair = getattr(request, REQUEST_ATTR, None)
    if not pair:
        return
    username, address = pair
    try:
        throttle_from_settings().record_failure(username, address)
    except Exception:  # pragma: no cover - the throttle already swallows cache errors
        logger.exception("Vantage could not record a failed sign-in.")


def _on_login_succeeded(sender, request=None, user=None, **kwargs) -> None:
    """Clear the pair's failures.

    Keyed on the *submitted* username from the request rather than on
    ``user.get_username()``. On a project where people sign in with an email address
    those two are different strings, and clearing the one the counter was never keyed
    on would leave the user locked out immediately after a successful sign-in — the
    kind of bug that only appears on the deployment that most needed the feature.
    """
    pair = getattr(request, REQUEST_ATTR, None)
    if not pair:
        return
    username, address = pair
    try:
        throttle_from_settings().clear(username, address)
    except Exception:  # pragma: no cover
        logger.exception("Vantage could not clear a sign-in counter.")


# ── the panel's own requests ────────────────────────────────────────────────


def is_panel_request(request) -> bool:
    """Whether this request resolved to a view belonging to a Vantage site.

    Asked of the resolver match rather than of the path, because the panel's mount
    point is a project's choice and a prefix comparison gets it wrong in both
    directions: ``/admin/`` may be ``django.contrib.admin``, and a project that mounts
    Vantage at ``/`` would match everything.

    The *application* namespace is used, not the instance namespace. A second site is
    constructed as ``VantageSite("partners")`` and mounts under its own instance
    namespace, so matching on that would protect the default panel and quietly skip
    every additional one — and an additional panel is usually the one exposed wider.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        return False
    return "vantage" in (getattr(match, "app_names", None) or [])


def surface_for(request) -> str | None:
    """Which budgeted surface this request is, or ``None`` for the rest of the panel.

    Only three surfaces have budgets, and each is here for a specific reason rather
    than because rate limiting is generally good:

    * ``autocomplete`` — a read API over every registered model, reachable by anyone
      with staff access, and cheap enough per call to be worth looping;
    * ``export`` — recognised by the ``?export=`` parameter on a list URL rather than by
      a route, because that is how the feature is spelled; one request walks a whole
      table and holds a database connection for the length of the download;
    * ``import`` — every step of the wizard, because the parse happens on upload and on
      preview, not only on commit.

    Ordinary list and detail views are deliberately unbudgeted. They are what the panel
    is *for*, they cost about what a page costs, and a limit on them would eventually
    fire on somebody working quickly — which is how a rate limit gets removed entirely.
    """
    match = getattr(request, "resolver_match", None)
    name = (getattr(match, "url_name", None) or "") if match else ""

    if name == "autocomplete":
        return "autocomplete"
    if name.endswith("-import") or "-import-" in name:
        return "import"
    if request.method in ("GET", "HEAD") and (request.GET.get("export") or ""):
        return "export"
    return None


class PanelSecurityMiddleware:
    """Rate limits, upload screening and response headers, on panel URLs only.

    Three jobs in one middleware because they share the one non-obvious thing —
    deciding what counts as a panel request — and because splitting them would mean a
    project could install the headers and forget the limits. They are separable in
    settings instead: ``VANTAGE["RATE_LIMIT"] = False`` leaves the headers in place.

    Everything here is scoped by :func:`is_panel_request`. A project mounting the panel
    inside a larger site keeps its own headers, its own caching and its own body parsing
    on every URL that is not the panel's.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Read after the response, when the resolver match has been set. A 404 never
        # resolved to a panel view and does not get panel headers — it is not a panel
        # response, and claiming otherwise would be a fingerprint of where the panel is.
        if is_panel_request(request):
            apply_security_headers(response)
        return response

    # ── before the view ─────────────────────────────────────────────────────

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Refuse over-budget and hostile requests before the view does any work.

        Order matters: the budget is spent *before* the upload is screened, so a
        hostile file costs its uploader the same as a good one. Screening first would
        make the cheap rejection path the unlimited one.
        """
        if not is_panel_request(request):
            return None

        refusal = self.check_rate_limit(request)
        if refusal is not None:
            return refusal
        return self.check_uploads(request)

    def check_rate_limit(self, request):
        surface = surface_for(request)
        if surface is None:
            return None
        limit = rate_limit_for(surface)
        if limit is None:
            return None

        state = limit.spend(actor_for(request))
        if not state.blocked:
            return None

        logger.warning(
            "Vantage refused a %s request: the budget for this account is spent.",
            surface,
        )
        return self.throttled_response(request, surface, state)

    def check_uploads(self, request):
        """Screen ``request.FILES`` on a panel POST.

        Reaching for ``request.FILES`` here parses the multipart body a few
        microseconds before the view would have parsed it anyway; Django caches the
        result, so the view sees exactly what it would have seen. A body that cannot be
        parsed is left alone — that is the view's error to report, with a form around
        it, not this layer's to turn into a bare 400.
        """
        if request.method not in ("POST", "PUT", "PATCH"):
            return None
        if not request.content_type or "multipart" not in request.content_type:
            return None

        try:
            files = request.FILES
        except Exception:  # pragma: no cover - a malformed or too-large body
            return None

        problems: list[str] = []
        for upload in files.values():
            problems.extend(inspect_upload(upload))
        if not problems:
            return None

        logger.warning("Vantage refused an upload: %s", "; ".join(problems))
        return self.rejected_upload_response(request, problems)

    # ── hooks ───────────────────────────────────────────────────────────────

    @staticmethod
    def throttled_response(request, surface: str, state):
        """429 with ``Retry-After``, in the content type the caller asked for.

        JSON for autocomplete because autocomplete is a JSON endpoint and its caller is
        a ``fetch``; a plain-text body there would be parsed as JSON, throw, and surface
        to the user as a picker that spins forever instead of one that says it is busy.
        """
        retry = str(int(state.retry_after or 0))
        message = "Too many requests. Wait a moment and try again."
        if surface == "autocomplete":
            response = JsonResponse(
                {"results": [], "more": False, "error": message}, status=429
            )
        else:
            response = HttpResponse(
                message, content_type="text/plain; charset=utf-8", status=429
            )
        if state.retry_after:
            response["Retry-After"] = retry
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def rejected_upload_response(request, problems: list[str]):
        """400, saying what was wrong with the file.

        The reasons are shown rather than hidden. They describe the *uploader's own*
        file back to them, so there is nothing here they did not already know, and a
        refusal without a reason is one that gets reported as a bug in the panel and
        retried until it is switched off.
        """
        body = "Upload refused.\n\n" + "\n".join(f"- {problem}" for problem in problems)
        response = HttpResponse(
            body, content_type="text/plain; charset=utf-8", status=400
        )
        response["Cache-Control"] = "no-store"
        return response
