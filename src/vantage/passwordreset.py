"""An opt-in "Forgot password?" flow for the sign-in page.

Off unless ``VANTAGE["PASSWORD_RESET"]`` is ``True``: until then every route here is a
404 and the sign-in page renders no link. It is built from Django's own parts — a
:class:`~django.contrib.auth.tokens.PasswordResetTokenGenerator` and the project's
email backend — so there is no table, no migration and no second token scheme.

What it is careful about, because a reset flow is an account-takeover path with a form
in front of it:

* **No enumeration.** The request form answers identically whether or not an address
  has an account, and the email is handed to a background thread so a slow mail server
  does not make a real address slower to answer than an unknown one. Rate-limited and
  ineligible requests look the same too.
* **Only people who can sign in here.** Active, staff, with a usable password. An
  account that signs in only through an identity provider has no password to reset.
* **Links are single use and short lived.** The token hashes the password and
  ``last_login``, so using it, or signing in, voids it. Its lifetime is
  ``PASSWORD_RESET_TIMEOUT_MINUTES`` on this flow's own clock; Django's global
  ``PASSWORD_RESET_TIMEOUT`` is not touched.
* **The token leaves the address bar.** The first visit moves it into the session and
  redirects (``Referrer-Policy: no-referrer``) to a token-free URL, as Django's own views
  do; every page here sends ``Referrer-Policy: same-origin`` and ``Cache-Control: no-store``.
* **Links point where the project says.** ``PASSWORD_RESET_BASE_URL`` if set, else the
  request's host as Django validated it against ``ALLOWED_HOSTS``. The host is read
  before any account lookup, so a disallowed ``Host`` fails the same way for every
  address.
* **Two-factor is untouched.** The reset sets a password and nothing else: nobody is
  signed in by it and no authenticator is disabled, so the next sign-in still asks for
  a code. Changing the password also signs every other session of that account out.
"""

from __future__ import annotations

import logging
import threading
import time

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import EmailMessage
from django.http import Http404, HttpResponseRedirect
from django.template import loader
from django.urls import NoReverseMatch, path, reverse
from django.utils.cache import patch_cache_control
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import base36_to_int, urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView, TemplateView

from vantage.conf import vantage_settings

__all__ = [
    "ResetTokenGenerator",
    "build_urls",
    "eligible_users",
    "enabled",
    "request_url",
    "wait_for_mail",
]

logger = logging.getLogger("vantage.passwordreset")

#: Where the token waits in the session between the emailed link and the form.
SESSION_TOKEN = "vantage-password-reset-token"  # noqa: S105
#: Stands in for the token in the form's URL, exactly as Django's own views do. A real
#: token is ``<base36>-<hash>`` and cannot equal this.
INTERNAL_TOKEN = "set-password"  # noqa: S105


def enabled() -> bool:
    return vantage_settings.PASSWORD_RESET is True


# ── the token ───────────────────────────────────────────────────────────────


class ResetTokenGenerator(PasswordResetTokenGenerator):
    """Django's generator with a lifetime of its own and a salt of its own.

    The salt means a token minted for Django's ``password_reset`` views is not accepted
    here, or the other way round. The lifetime is checked here rather than by changing
    ``settings.PASSWORD_RESET_TIMEOUT``, which every other reset flow in the project
    shares. The comparison is Django's constant-time one.
    """

    key_salt = "vantage.passwordreset.ResetTokenGenerator"

    @staticmethod
    def timeout_seconds() -> int:
        return int(vantage_settings.PASSWORD_RESET_TIMEOUT_MINUTES) * 60

    def check_token(self, user, token) -> bool:
        if not (user and token):
            return False
        try:
            ts_b36, _hash = str(token).split("-")
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False
        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(self._make_token_with_timestamp(user, ts, secret), token):
                break
        else:
            return False
        return (self._num_seconds(self._now()) - ts) <= self.timeout_seconds()


token_generator = ResetTokenGenerator()


# ── who may be reset ────────────────────────────────────────────────────────


def is_eligible(user) -> bool:
    """Someone who can sign in to the panel with a password."""
    return bool(
        user is not None
        and user.is_active
        and user.is_staff
        and user.has_usable_password()
        and user.email
    )


def eligible_users(email: str):
    """Users with this address who may be sent a link. Case-insensitive."""
    model = get_user_model()
    field = model.get_email_field_name()
    candidates = model._default_manager.filter(**{f"{field}__iexact": email.strip()})
    return [u for u in candidates if is_eligible(u)]


def _user_for(uidb64):
    model = get_user_model()
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        return model._default_manager.get(pk=model._meta.pk.to_python(pk))
    except (TypeError, ValueError, OverflowError, model.DoesNotExist, forms.ValidationError):
        return None


# ── links and mail ──────────────────────────────────────────────────────────


def _base(request) -> str:
    """The scheme and host links point at.

    ``request.get_host()`` is Django's own and raises ``DisallowedHost`` for anything
    ``ALLOWED_HOSTS`` does not name, honouring ``X-Forwarded-Host`` only if the project
    set ``USE_X_FORWARDED_HOST``. An explicit ``PASSWORD_RESET_BASE_URL`` wins.
    """
    explicit = vantage_settings.PASSWORD_RESET_BASE_URL
    if explicit:
        return explicit.rstrip("/")
    return f"{request.scheme}://{request.get_host()}"


def request_url() -> str | None:
    """The path of the request form, or ``None`` when the flow is off or unmounted."""
    if not enabled():
        return None
    try:
        return reverse("vantage:password_reset")
    except NoReverseMatch:
        return None


def _link(base: str, user) -> str:
    path_ = reverse(
        "vantage:password_reset_confirm",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(user.pk)),
            "token": token_generator.make_token(user),
        },
    )
    return base + path_


def _message(user, link: str) -> EmailMessage:
    context = {
        "user": user,
        "username": user.get_username(),
        "link": link,
        "site_title": vantage_settings.SITE_TITLE,
        "valid_minutes": int(vantage_settings.PASSWORD_RESET_TIMEOUT_MINUTES),
    }
    subject = loader.render_to_string("vantage/password_reset_subject.txt", context)
    body = loader.render_to_string("vantage/password_reset_email.txt", context)
    # Header injection is Django's to refuse; collapsing the subject to one line keeps a
    # project's overriding template from tripping it with a stray newline.
    return EmailMessage(" ".join(subject.split()), body, to=[user.email])


_threads: list[threading.Thread] = []
_threads_lock = threading.Lock()


def _deliver(message: EmailMessage) -> None:
    try:
        message.send()
    except Exception:
        # Never surfaced: the page's answer must not depend on whether this worked.
        logger.exception("Could not send a password reset email.")


def _dispatch(message: EmailMessage) -> None:
    """Send off the request path, so a slow mail server tells a caller nothing."""
    worker = threading.Thread(target=_deliver, args=(message,), daemon=True)
    with _threads_lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
        _threads.append(worker)
    worker.start()


def wait_for_mail(timeout: float = 10.0) -> None:
    """Block until queued emails have been handed to the backend. For tests and shutdown."""
    deadline = time.monotonic() + timeout
    with _threads_lock:
        pending = list(_threads)
    for worker in pending:
        worker.join(max(0.0, deadline - time.monotonic()))


def _audit(request, user, name: str) -> None:
    from vantage.audit import record
    from vantage.models import VantageLogEntry

    # Who and where, never the token or the password: the entry carries the account as
    # its object and the caller's address from the request.
    record(request, VantageLogEntry.Action.UPDATE, obj=user, action_name=name)


# ── rate limits ─────────────────────────────────────────────────────────────


def _spend(surface: str, actor: str) -> bool:
    """Spend one request from a budget. ``True`` if it was already exhausted."""
    from vantage.security import rate_limit_for

    limit = rate_limit_for(surface)
    return limit is not None and limit.spend(actor).blocked


def _caller(request) -> str:
    from vantage.security import client_ip

    return f"addr:{client_ip(request) or 'unknown'}"


# ── views ───────────────────────────────────────────────────────────────────


class _ResetMixin:
    """Shared behaviour: off means 404, and nothing here is cached or leaked."""

    site = None

    def dispatch(self, request, *args, **kwargs):
        if not enabled():
            raise Http404
        response = super().dispatch(request, *args, **kwargs)
        if "Referrer-Policy" in response:
            return response
        return harden(response)

    def get_context_data(self, **kwargs):
        from vantage.authviews import branding_context

        context = super().get_context_data(**kwargs)
        context.update(branding_context(self.request))
        try:
            context["login_url"] = reverse("login")
        except NoReverseMatch:
            from django.conf import settings
            from django.shortcuts import resolve_url

            context["login_url"] = resolve_url(settings.LOGIN_URL)
        return context


def harden(response, policy: str = "same-origin"):
    """Never leak a referrer cross-site, never cache.

    ``same-origin`` rather than ``no-referrer`` on the pages that post a form: a
    browser sends ``Origin: null`` on a form post under ``no-referrer``, which Django's
    CSRF check rightly refuses. The one response that carries the token, the redirect
    that moves it out of the URL, does use ``no-referrer``.
    """
    response["Referrer-Policy"] = policy
    patch_cache_control(response, no_store=True, no_cache=True, private=True, max_age=0)
    response["Pragma"] = "no-cache"
    return response


class RequestForm(forms.Form):
    email = forms.EmailField(
        label=_("Email address"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


@method_decorator([csrf_protect, never_cache], name="dispatch")
class RequestView(_ResetMixin, FormView):
    template_name = "vantage/password_reset.html"
    form_class = RequestForm

    def get_form(self, form_class=None):
        from vantage.authviews import _style_form

        return _style_form(super().get_form(form_class))

    def form_valid(self, form):
        # Before any lookup: a Host that ALLOWED_HOSTS refuses fails identically for
        # every address, so it cannot be used to tell which have accounts.
        base = _base(self.request)
        email = form.cleaned_data["email"].strip().lower()

        ip_blocked = _spend("password_reset", _caller(self.request))
        address_blocked = _spend("password_reset_address", f"email:{email}")
        if ip_blocked or address_blocked:
            logger.warning("Password reset request throttled.")
        else:
            for user in eligible_users(email):
                _audit(self.request, user, "Password reset requested")
                _dispatch(_message(user, _link(base, user)))
        return HttpResponseRedirect(reverse("vantage:password_reset_done"))


@method_decorator(never_cache, name="dispatch")
class DoneView(_ResetMixin, TemplateView):
    template_name = "vantage/password_reset_done.html"


@method_decorator([csrf_protect, never_cache, sensitive_post_parameters()], name="dispatch")
class ConfirmView(_ResetMixin, TemplateView):
    template_name = "vantage/password_reset_confirm.html"

    user = None
    valid_link = False

    def _check(self, request, uidb64, token):
        """Throttle, find the account and judge the token.

        Returns a response to stop with (a throttle refusal, or the redirect that takes
        the token out of the URL), or ``None`` with ``self.user`` / ``self.valid_link``
        set for the caller.
        """
        if _spend("password_reset_confirm", _caller(request)):
            response = self.render_to_response(self.get_context_data(throttled=True))
            response.status_code = 429
            response["Retry-After"] = "3600"
            return response
        user = _user_for(uidb64)
        if user is None or not is_eligible(user):
            return None
        if token == INTERNAL_TOKEN:
            if token_generator.check_token(user, request.session.get(SESSION_TOKEN, "")):
                self.user, self.valid_link = user, True
            return None
        if token_generator.check_token(user, token):
            # Move the token out of the URL before anything renders, so it cannot leak
            # through a Referer header, a script or the browser's history.
            request.session[SESSION_TOKEN] = token
            return harden(
                HttpResponseRedirect(
                    reverse(
                        "vantage:password_reset_confirm",
                        kwargs={"uidb64": uidb64, "token": INTERNAL_TOKEN},
                    )
                ),
                "no-referrer",
            )
        return None

    def _page(self, form=None, **extra):
        form = (
            form if form is not None else (SetPasswordForm(self.user) if self.valid_link else None)
        )
        if form is not None:
            from vantage.authviews import _style_form

            _style_form(form)
        return self.render_to_response(
            self.get_context_data(
                form=form,
                valid_link=self.valid_link,
                request_url=reverse("vantage:password_reset"),
                **extra,
            )
        )

    def get(self, request, uidb64, token):
        return self._check(request, uidb64, token) or self._page()

    def post(self, request, uidb64, token):
        stopped = self._check(request, uidb64, token)
        if stopped is not None:
            return stopped
        if not self.valid_link:
            return self._page()
        form = SetPasswordForm(self.user, request.POST)
        if not form.is_valid():
            return self._page(form)
        form.save()
        request.session.pop(SESSION_TOKEN, None)
        _audit(request, self.user, "Password reset completed")
        return HttpResponseRedirect(reverse("vantage:password_reset_complete"))


@method_decorator(never_cache, name="dispatch")
class CompleteView(_ResetMixin, TemplateView):
    template_name = "vantage/password_reset_complete.html"


# ── routes ──────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    """Under the panel's reserved ``_/`` prefix. Always mounted; each 404s while off."""
    return [
        path("_/password-reset/", RequestView.as_view(site=site), name="password_reset"),
        path(
            "_/password-reset/done/",
            DoneView.as_view(site=site),
            name="password_reset_done",
        ),
        path(
            "_/password-reset/confirm/<uidb64>/<token>/",
            ConfirmView.as_view(site=site),
            name="password_reset_confirm",
        ),
        path(
            "_/password-reset/complete/",
            CompleteView.as_view(site=site),
            name="password_reset_complete",
        ),
    ]
