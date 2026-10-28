"""The panel's two-factor screens: set it up, prove it, and reset it for someone else.

Thin on purpose — every decision is :mod:`vantage.twofactor`'s. What lives here is the
part only a view can know:

* **Which state a page may be reached in.** Each class names, in ``two_factor_allows``,
  what the gate may still be owed when it is opened. The set-up page allows "enrol" and
  nothing else, so a session that has the password but not the code cannot reach the
  form that turns two-factor sign-in *off*. The verify page allows both. Every other
  panel page allows neither, by not saying anything.
* **Nobody changes someone else's second factor by impersonating them.** All three
  refuse while impersonating: the impersonated account's authenticator is its owner's
  business, and the reset screen exists for the administrator's legitimate version.
* **Proof before anything that weakens it.** Turning it off, or replacing the recovery
  codes, takes a current code (or a recovery code) or the password, and a wrong one
  counts toward the same lockout as a wrong code at sign-in.

Every view is ``csrf_protect``-ed and ``never_cache``-d: these pages print secrets and
take them, and neither belongs in a shared cache or a forged form.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import TemplateView

from vantage import qr, twofactor
from vantage.safety import impersonating, real_user
from vantage.views import SiteView

__all__ = [
    "CodeForm",
    "TwoFactorUsersView",
    "TwoFactorVerifyView",
    "TwoFactorView",
    "build_urls",
    "safe_next",
]


class CodeForm(forms.Form):
    """One box for either kind of code. Which one it is, is decided by its shape.

    Not ``inputmode="numeric"``: that hides the letters a recovery code needs on a
    phone keyboard, and a recovery code is typed exactly when things have gone wrong.
    """

    code = forms.CharField(
        label=_("Authentication code"),
        max_length=64,
        help_text=_("The 6-digit code from your authenticator app, or one of your recovery codes."),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "autocapitalize": "off",
                "autocorrect": "off",
                "spellcheck": "false",
                "autofocus": True,
            }
        ),
    )


class ConfirmForm(CodeForm):
    """Enrolment's confirmation: an app code only, never a recovery code."""

    code = forms.CharField(
        label=_("Code from the app"),
        max_length=64,
        help_text=_("Type the 6-digit code your app now shows, to prove it is set up."),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "spellcheck": "false",
            }
        ),
    )


class ProofForm(forms.Form):
    """A current code or the password: either proves the request is the account owner."""

    code = forms.CharField(
        label=_("Authentication or recovery code"),
        max_length=64,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "spellcheck": "false"}),
    )
    password = forms.CharField(
        label=_("Or your password"),
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


def _style(form):
    from vantage.authviews import _style_form

    return _style_form(form)


def safe_next(request, fallback: str) -> str:
    """``?next=``/``next`` if it stays on this site, otherwise ``fallback``."""
    value = request.POST.get("next") or request.GET.get("next") or ""
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return fallback


def _reverse(request, name: str) -> str:
    """A route of *this* site, for a project that mounts more than one."""
    match = getattr(request, "resolver_match", None)
    return reverse(f"vantage:{name}", current_app=getattr(match, "namespace", None))


def _refuse_impersonation(request):
    if impersonating(request):
        raise PermissionDenied(
            gettext("Two-factor settings cannot be changed while impersonating someone.")
        )


def _no_store(response):
    response["Cache-Control"] = "no-store"
    return response


# ── your own ────────────────────────────────────────────────────────────────


@method_decorator(
    [csrf_protect, never_cache, sensitive_post_parameters("code", "password")], name="dispatch"
)
class TwoFactorView(SiteView):
    """Set up, look at, and switch off your own two-factor sign-in."""

    template_name = "vantage/two_factor.html"
    page_title = _("Two-factor sign-in")
    page_description = _("A code from an app on your phone, asked for after your password.")
    #: Reachable while the gate wants enrolment — this is where it sends people — and
    #: not while it wants a code: that would put "switch it off" in front of someone
    #: who has only the password.
    two_factor_allows = frozenset({twofactor.ENROL})

    def check_permission(self, request):
        if not twofactor.enabled():
            raise PermissionDenied(gettext("Two-factor sign-in is switched off for this site."))
        _refuse_impersonation(request)
        return None

    def get_breadcrumbs(self):
        return [(_("Your account"), _reverse(self.request, "account")), (self.page_title, None)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        device = twofactor.device_for(user)
        pending = None if device else twofactor.device_for(user, confirmed=False)
        context.update(
            device=device,
            remaining=twofactor.remaining_codes(device) if device else 0,
            required=twofactor.is_required(user),
            next=safe_next(self.request, ""),
            enrol=self._enrolment(user, pending) if pending else None,
            proof_form=kwargs.get("proof_form") or _style(ProofForm()),
            confirm_form=kwargs.get("confirm_form") or _style(ConfirmForm()),
        )
        return context

    @staticmethod
    def _enrolment(user, pending) -> dict | None:
        """What the set-up step shows. The only place the secret is ever rendered."""
        key = twofactor.device_key(pending)
        if key is None:
            return None
        uri = twofactor.provisioning_uri(user, key)
        try:
            code = qr.svg(qr.encode(uri), label=gettext("QR code for your authenticator app"))
        except qr.QRTooLong:
            code = ""
        secret = twofactor.b32(key)
        return {
            "uri": uri,
            # Built from booleans alone, plus an escaped label; nothing to escape again.
            "qr": mark_safe(code),
            "key": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            "issuer": twofactor.issuer(),
        }

    def get(self, request, *args, **kwargs):
        return _no_store(super().get(request, *args, **kwargs))

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "")
        handler = {
            "start": self._start,
            "cancel": self._cancel,
            "confirm": self._confirm,
            "disable": self._disable,
            "codes": self._codes,
        }.get(action)
        if handler is None:
            return self._back(request)
        return _no_store(handler(request))

    def _back(self, request):
        url = _reverse(self.request, "two_factor")
        next_url = safe_next(request, "")
        if next_url:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode({'next': next_url})}"
        return HttpResponseRedirect(url)

    def _start(self, request):
        try:
            twofactor.start_enrolment(request.user)
        except ValueError:
            messages.info(request, gettext("Two-factor sign-in is already on."), fail_silently=True)
        return self._back(request)

    def _cancel(self, request):
        pending = twofactor.device_for(request.user, confirmed=False)
        if pending is not None:
            pending.delete()
        return self._back(request)

    def _confirm(self, request):
        pending = twofactor.device_for(request.user, confirmed=False)
        if pending is None:
            return self._back(request)
        form = _style(ConfirmForm(request.POST))
        if form.is_valid():
            outcome, codes = twofactor.confirm_enrolment(
                request, pending, form.cleaned_data["code"]
            )
            if outcome:
                return self._show_codes(request, codes, enabled=True)
            form.add_error("code", outcome.message)
            _style(form)
        return self.render_to_response(self.get_context_data(confirm_form=form))

    def _proved(self, request, device, form) -> bool:
        """Whether the POST carries a right code or the right password.

        A wrong password is a failure against the same counter as a wrong code: the
        password is the cheaper thing to guess, and a lockout either could step around
        is not one.
        """
        if not form.is_valid():
            return False
        code = form.cleaned_data["code"]
        password = form.cleaned_data["password"]
        if code:
            outcome = twofactor.verify(device, code)
        elif password:
            outcome = twofactor.verify_password(device, request.user, password)
        else:
            form.add_error(None, gettext("Enter a current code or your password."))
            return False
        if outcome:
            return True
        if outcome.reason in ("locked", "reused"):
            form.add_error(None, outcome.message)
        else:
            form.add_error(None, gettext("That code or password is not right."))
        return False

    def _disable(self, request):
        device = twofactor.device_for(request.user)
        if device is None:
            return self._back(request)
        form = ProofForm(request.POST)
        if not self._proved(request, device, form):
            return self.render_to_response(self.get_context_data(proof_form=_style(form)))
        twofactor.disable(request, device)
        messages.success(request, gettext("Two-factor sign-in is now off."), fail_silently=True)
        return self._back(request)

    def _codes(self, request):
        device = twofactor.device_for(request.user)
        if device is None:
            return self._back(request)
        form = ProofForm(request.POST)
        if not self._proved(request, device, form):
            return self.render_to_response(self.get_context_data(proof_form=_style(form)))
        return self._show_codes(request, twofactor.regenerate_codes(request, device))

    def _show_codes(self, request, codes: list[str], *, enabled: bool = False):
        """Render the codes into this one response. They are not stored anywhere else."""
        context = self.get_context_data()
        context.update(new_codes=codes, just_enabled=enabled)
        return self.render_to_response(context)


# ── the second step, for a session that is already signed in ──────────────


@method_decorator([csrf_protect, never_cache, sensitive_post_parameters("code")], name="dispatch")
class TwoFactorVerifyView(TemplateView):
    """The code, asked of a session that signed in somewhere the panel did not see.

    The panel's own sign-in asks for the code *before* ``login()``. A project that
    signs people in through its own view, or through ``django.contrib.admin``'s, hands
    the panel a fully signed-in session that never answered — and this is where the
    gate sends it, before any panel page with data on it. With
    :class:`~vantage.twofactor.AdminTwoFactorMiddleware` installed, Django's admin
    sends it here too.

    Standalone rather than inside the shell: every link in a sidebar would lead
    straight back here.
    """

    site = None
    template_name = "vantage/two_factor_verify.html"
    two_factor_allows = frozenset({twofactor.VERIFY, twofactor.ENROL})

    def dispatch(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return redirect_to_login(request.get_full_path())
        self.person = real_user(request)
        self.device = twofactor.device_for(self.person)
        if twofactor.requirement(request) != twofactor.VERIFY or self.device is None:
            return HttpResponseRedirect(safe_next(request, _reverse(self.request, "dashboard")))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from vantage.authviews import branding_context

        context = super().get_context_data(**kwargs)
        context.update(branding_context(self.request))
        context.setdefault("form", _style(CodeForm()))
        context.update(next=safe_next(self.request, ""), signed_in=True)
        return context

    def post(self, request, *args, **kwargs):
        form = CodeForm(request.POST)
        if form.is_valid():
            outcome = twofactor.verify(self.device, form.cleaned_data["code"])
            if outcome:
                # Rotated here because this session is changing what it is allowed to
                # do, which is exactly the moment a fixed session id must stop working.
                request.session.cycle_key()
                twofactor.mark_verified(request, self.device)
                return HttpResponseRedirect(safe_next(request, _reverse(self.request, "dashboard")))
            form.add_error("code", outcome.message)
        return self.render_to_response(self.get_context_data(form=_style(form)))


# ── someone else's ──────────────────────────────────────────────────────────


@method_decorator([csrf_protect, never_cache], name="dispatch")
class TwoFactorUsersView(SiteView):
    """Who has two-factor sign-in on, and a reset for each — superusers only.

    For the colleague whose phone went in a lake. Superusers only, not whoever may
    manage roles: switching off a superuser's second factor is a step towards becoming
    one, so it is held by the people who already are. Your own row has no button —
    switching yours off goes through the page that asks for proof.
    """

    template_name = "vantage/two_factor_users.html"
    page_title = _("Two-factor sign-in for everyone")
    page_description = _("Who has it on, and a reset for anyone who has lost their phone.")

    def check_permission(self, request):
        if not twofactor.enabled():
            raise PermissionDenied(gettext("Two-factor sign-in is switched off for this site."))
        _refuse_impersonation(request)
        if not request.user.is_superuser:
            raise PermissionDenied(gettext("Only superusers can reset two-factor sign-in."))
        return None

    def get_breadcrumbs(self):
        return [(_("Your account"), _reverse(self.request, "account")), (self.page_title, None)]

    def get_context_data(self, **kwargs):
        from vantage.models import TwoFactorDevice

        context = super().get_context_data(**kwargs)
        devices = (
            TwoFactorDevice.objects.filter(confirmed_at__isnull=False)
            .select_related("user")
            .order_by("-confirmed_at")
        )
        context["devices"] = list(devices)
        return context

    def post(self, request, *args, **kwargs):
        model = get_user_model()
        try:
            pk = model._meta.pk.to_python(request.POST.get("user"))
        except ValidationError:
            pk = None
        target = get_object_or_404(model, pk=pk) if pk is not None else None
        if target is None or target.pk == request.user.pk:
            messages.error(
                request,
                gettext("Use your own two-factor page to switch yours off."),
                fail_silently=True,
            )
        elif twofactor.reset(request, target):
            messages.success(
                request,
                gettext("Two-factor sign-in was reset for %(name)s.")
                % {"name": target.get_username()},
                fail_silently=True,
            )
        return HttpResponseRedirect(_reverse(self.request, "two_factor_users"))


# ── routes ──────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    return [
        path("_/account/two-factor/", TwoFactorView.as_view(site=site), name="two_factor"),
        path(
            "_/account/two-factor/verify/",
            TwoFactorVerifyView.as_view(site=site),
            name="two_factor_verify",
        ),
        path(
            "_/account/two-factor/people/",
            TwoFactorUsersView.as_view(site=site),
            name="two_factor_users",
        ),
    ]
