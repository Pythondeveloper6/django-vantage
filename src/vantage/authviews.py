"""Sign in, sign out, change password — and the three error pages.

A panel that sends anonymous visitors to ``LOGIN_URL`` and then hands them an unstyled
Django form has already broken its own promise: the first screen a user ever sees is
the one screen that does not look like the product. Every project ends up writing the
same login template, badly, so the package writes it once instead. Mount it with::

    path("accounts/", include("vantage.authviews")),

The route *names* deliberately match ``django.contrib.auth.urls`` (``login``,
``logout``, ``password_change``), and no ``app_name`` is declared, so this is a
drop-in replacement: ``LOGIN_URL``, ``redirect_to_login`` and every
``{% url 'login' %}`` in a project keep working unchanged.

The error handlers live here rather than in ``views.py`` because they are not panel
views. They are bound to no site, run no permission check, and — in the 500 case —
have to work when the panel's own machinery is precisely what has failed.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.http import (
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseServerError,
)
from django.template import loader
from django.urls import path, reverse_lazy
from django.utils.functional import Promise
from django.utils.translation import get_language, get_language_bidi
from django.utils.translation import gettext_lazy as _

from vantage.conf import vantage_settings
from vantage.nav import build_nav, resolve_theme
from vantage.theme import NO_FLASH_SCRIPT, render_theme_css

__all__ = [
    "LoginView",
    "LogoutView",
    "PasswordChangeView",
    "branding_context",
    "page_not_found",
    "permission_denied",
    "server_error",
    "shell_context",
    "urlpatterns",
]


# ── context ─────────────────────────────────────────────────────────────────


def branding_context(request) -> dict:
    """The handful of values a page needs to look like the panel without being in it.

    Rendered per request rather than captured at import time, so changing
    ``BRAND_COLOR`` — in settings or from the settings screen — restyles the sign-in
    page along with everything else instead of leaving one stale screen behind.
    """
    return {
        "settings": vantage_settings,
        "theme": resolve_theme(request),
        "theme_css": render_theme_css(),
        "no_flash_script": NO_FLASH_SCRIPT,
        "language_code": get_language() or "en",
        "is_rtl": get_language_bidi(),
    }


def _safe_nav(site, request) -> list:
    """The sidebar, or nothing at all — never an exception.

    Navigation is built from the registry and the permission layer, and both can hit
    the database. On an error page that is exactly what may be broken, and a 404 that
    turns into a 500 because its sidebar failed is a strictly worse answer than a 404
    with no sidebar.
    """
    try:
        return build_nav(site, request)
    except Exception:  # pragma: no cover - defensive; the sidebar is never the point
        return []


def shell_context(request, *, site=None, page_title: str = "", breadcrumbs=()) -> dict:
    """Everything ``vantage/base.html`` reads, built without a :class:`SiteView`.

    Duplicating a little of ``SiteView.get_context_data`` is deliberate: the pages
    that need this — password change, 403, 404 — must not inherit that view's
    authentication redirect, and an error handler has no view instance to borrow.
    """
    from vantage.safety import impersonating, read_only_reason, real_user

    if site is None:
        from vantage.sites import site as default_site

        site = default_site

    context = branding_context(request)
    context.update(
        site=site,
        nav=_safe_nav(site, request),
        path=getattr(request, "path", ""),
        read_only=read_only_reason(),
        impersonating=impersonating(request),
        real_user=real_user(request) if hasattr(request, "user") else None,
        page_title=page_title,
        page_description="",
        breadcrumbs=list(breadcrumbs),
    )
    context.update(_safe_topbar(site, request))
    return context


def _safe_topbar(site, request) -> dict:
    """The header's controls, or an empty bar — never an exception.

    Same reasoning as :func:`_safe_nav`, and the same failure it guards: the top bar
    reads a stored arrangement out of the database, and on an error page the database
    is exactly what may have failed. A 404 that turns into a 500 because its header
    could not be arranged is a strictly worse answer than a 404 with a bare header.
    """
    from vantage.topbar import Topbar, topbar_context

    try:
        return topbar_context(site, request)
    except Exception:  # pragma: no cover - defensive; the header is never the point
        return {"topbar": Topbar(), "account_name": "", "account_initials": ""}


#: The id of the ``role="alert"`` box ``login.html`` renders for a non-field error.
#: Named here rather than only in the template because the widget attributes that point
#: at it are built in this module, and an ``aria-describedby`` naming an element that
#: does not exist is worse than none: a screen reader announces nothing and there is no
#: warning anywhere that the reference is dangling.
SUMMARY_ID = "v-auth-summary"


def _style_form(form):
    """Give Django's own auth forms the panel's input styling and error wiring.

    Done in Python rather than with a ``|addclass`` filter in the template so that a
    project swapping in its own ``authentication_form`` inherits both for free.
    ``aria-invalid`` and ``aria-describedby`` matter more than the class: without them
    a screen reader reaches a red-bordered field and is told nothing at all.
    """
    # "Please enter a correct username and password" is the *common* failure, and
    # Django puts it in non_field_errors — so a per-field loop alone leaves the one
    # error people actually hit attached to nothing. A screen-reader user tabs through
    # two fields that announce no problem at all, and the alert is only found by
    # exploring the page. Both credential fields point at the summary instead: the
    # message does not say which of the two was wrong, deliberately, so neither field
    # can honestly claim it alone.
    summary_id = SUMMARY_ID if (form.is_bound and form.non_field_errors()) else None

    for name, field in form.fields.items():
        bound = form[name]
        attrs = field.widget.attrs
        attrs.setdefault("class", "v-input")

        described = []
        if field.help_text:
            described.append(f"{bound.auto_id}_help")
        if form.is_bound and bound.errors:
            attrs["aria-invalid"] = "true"
            described.append(f"{bound.auto_id}_error")
        elif summary_id:
            attrs["aria-invalid"] = "true"
            described.append(summary_id)
        if described:
            # Both, in reading order. Pointing only at the error would silently drop
            # the password rules at the exact moment the user needs to read them.
            attrs["aria-describedby"] = " ".join(described)
    return form


# ── views ───────────────────────────────────────────────────────────────────


class LoginView(auth_views.LoginView):
    """Django's login view wearing the panel's branding."""

    template_name = "vantage/login.html"
    #: Someone already signed in who lands on /login/ wanted the panel, not the form.
    redirect_authenticated_user = True

    def get_form(self, form_class=None):
        return _style_form(super().get_form(form_class))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(branding_context(self.request))
        return context


class LogoutView(auth_views.LogoutView):
    """Signed-out confirmation, rather than a bounce to an anonymous panel URL.

    Renders only when the project has set no ``LOGOUT_REDIRECT_URL``; if it has, that
    setting wins, which is the behaviour a project that configured it expects.
    """

    template_name = "vantage/logged_out.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(branding_context(self.request))
        return context


class PasswordChangeView(auth_views.PasswordChangeView):
    """Change your own password, inside the panel shell.

    Redirects back to itself with a message rather than to a separate "done" page:
    one fewer template for a project to override, and the confirmation lands in the
    same toast region as every other confirmation in the panel.
    """

    template_name = "vantage/password_change.html"
    success_url = reverse_lazy("password_change")

    def get_form(self, form_class=None):
        return _style_form(super().get_form(form_class))

    def form_valid(self, form):
        response = super().form_valid(form)
        # fail_silently: the messages framework is optional, and a project without it
        # should get a working password change rather than a 500 on success.
        messages.add_message(
            self.request,
            messages.SUCCESS,
            _("Your password has been changed."),
            fail_silently=True,
        )
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            shell_context(
                self.request,
                page_title=_("Change password"),
                breadcrumbs=[(_("Change password"), None)],
            )
        )
        return context


#: Mount with ``path("accounts/", include("vantage.authviews"))``. No ``app_name``,
#: because these names are Django's and every project already reverses them unqualified.
urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password_change/", PasswordChangeView.as_view(), name="password_change"),
]


# ── error handlers ──────────────────────────────────────────────────────────


def _in_shell(request, template_name: str, status: int, response_class, **extra):
    """Render an error page inside the panel shell, or fall back to the bare one.

    A 403 or a 404 is an ordinary request that happens to end badly, so it can afford
    the sidebar and the branding. It can also fail to render them — an unmounted
    panel makes ``{% url 'vantage:dashboard' %}`` raise — and an error page that
    errors is the one failure mode with no floor under it. Hence the fallback.
    """
    try:
        context = shell_context(request)
        context.update(extra)
        body = loader.render_to_string(template_name, context, request=request)
    except Exception:
        return _bare(status)
    return response_class(body)


def _bare(status: int) -> HttpResponse:
    """The self-contained error page, at whatever status.

    The dict is copied and finished here rather than being complete at import time,
    because the active language is a per-request fact: a module-level dict of *rendered*
    strings would freeze whichever language happened to be active when this module was
    first imported. The values themselves are lazy, so they resolve as the template
    renders them; only the language code and the writing direction have to be read now.
    """
    context: dict[str, str | Promise] = dict(_BARE_TEXT.get(status, _BARE_TEXT[500]))
    context.setdefault("error_link", _("Return to the start"))
    # ``vantage/500.html`` is the one template with no ``{% load i18n %}`` — a load tag
    # is precisely what may have failed — so it cannot call ``get_current_language``
    # itself and is handed both values instead.
    context["language_code"] = get_language() or "en"
    context["language_dir"] = "rtl" if get_language_bidi() else "ltr"
    return HttpResponse(
        loader.get_template("vantage/500.html").render(context),
        status=status,
    )


#: Lazy, never eager: this is evaluated at import, long before any request has said
#: which language it wants.
_BARE_TEXT: dict[int, dict[str, str | Promise]] = {
    403: {
        "error_code": _("Error 403"),
        "error_title": _("You do not have access to this"),
        "error_body": _("This page exists, but your account is not permitted to open it."),
    },
    404: {
        "error_code": _("Error 404"),
        "error_title": _("We could not find that page"),
        "error_body": _("The address may be mistyped, or what it pointed at may be gone."),
    },
    # Spelled out rather than left to the template's own defaults: those defaults are
    # hard-coded English that no translation can reach, and they exist only for the
    # no-context smoke test that proves this page needs nothing at all to render.
    500: {
        "error_code": _("Error 500"),
        "error_title": _("Something went wrong"),
        "error_body": _(
            "The server hit an error handling that request. It has been logged; "
            "nothing you did caused it and trying again is safe."
        ),
    },
}


def permission_denied(request, exception=None, template_name="vantage/403.html"):
    """``handler403``. Signature matches Django's own so it is a drop-in.

    The exception is passed through because ``PanelView`` raises ``PermissionDenied``
    with a sentence written for the user ("You do not have permission to view
    orders."), and discarding it in favour of generic wording loses the only part of
    the page that says anything specific.
    """
    return _in_shell(request, template_name, 403, HttpResponseForbidden, exception=exception)


def page_not_found(request, exception=None, template_name="vantage/404.html"):
    """``handler404``. Signature matches Django's own so it is a drop-in."""
    return _in_shell(request, template_name, 404, HttpResponseNotFound)


def server_error(request, template_name="vantage/500.html"):
    """``handler500``.

    Rendered with ``Template.render()`` and no arguments on purpose: no request, so no
    context processors run, and nothing here touches the database, the settings cache
    or the registry. By the time this handler is reached any of those may be the thing
    that is broken, and a 500 page that raises is the one error with nowhere to go.
    """
    return HttpResponseServerError(loader.get_template(template_name).render())
