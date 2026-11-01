"""Revealing one sensitive value, and setting a password instead of revealing a hash.

The request half of :mod:`vantage.sensitive`. Two views, both small, both deliberately
strict.

:class:`RevealView` answers one ``POST`` with one value. Every property of it is a
refusal of something a GET-based "show" link would allow:

* **POST only.** A GET is prefetched by browsers, followed by link checkers, kept in
  history and logged in full by every proxy between the user and the server. None of
  those should ever hold a secret.
* **The record is fetched the way every other record is** — scoped by
  ``filter_queryset`` (404 when it is out of reach) and then checked at object level for
  ``view`` — before the reveal permission is even asked, so revealing can never reach a
  row the user could not open.
* **A dedicated permission.** Viewing a customer is not the same grant as reading their
  API key. :meth:`~vantage.panels.ModelPanel.can_reveal` decides, defaulting to
  superusers and ``vantage.reveal_sensitive``.
* **Rate-limited per account** through the same fixed-window machinery as export and
  import, enforced here rather than by the middleware so it holds in a project that has
  not installed :class:`~vantage.middleware.PanelSecurityMiddleware`. A loop revealing
  every row is a bulk export with extra steps.
* **Audited** — "admin revealed api_token on Acme Ltd" — and written *before* the value
  leaves the server, so there is no response carrying a secret without a log entry
  behind it.
* **Never cached.** ``Cache-Control: no-store`` plus ``Pragma: no-cache`` for the old
  proxies that still read it.
* **Never a hash.** Refused for Django's ``User.password`` and anything else
  :func:`~vantage.sensitive.is_hashed` recognises, and refused again if the stored value
  turns out to be in Django's hash format whatever the column is called.

:class:`SetPasswordView` is what the detail page offers next to a masked password hash:
Django's own ``AdminPasswordChangeForm``, gated on ``change`` for that record, audited
with the value redacted.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.urls import NoReverseMatch, path, reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from vantage import sensitive
from vantage.views import PanelView

__all__ = ["RevealView", "SetPasswordView", "build_urls"]

logger = logging.getLogger("vantage.sensitive")


def _json(payload: dict, *, status: int = 200) -> JsonResponse:
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store, max-age=0"
    response["Pragma"] = "no-cache"
    return response


class RevealView(PanelView):
    """``POST <model>/<pk>/reveal/<field>/`` → ``{"value": ..., "expires_in": 30}``."""

    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        # The JSON caller gets JSON for every refusal, not the HTML 403 page.
        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied as exc:
            return _json({"error": str(exc) or _("Not allowed.")}, status=403)
        except Http404:
            return _json({"error": _("Not found.")}, status=404)

    def post(self, request, *args, **kwargs):
        from vantage.formviews import get_record
        from vantage.security import actor_for, rate_limit_for

        panel = self.panel
        name = self.kwargs["field"]

        # Before the lookup, so a refused name costs the same as an allowed one and the
        # endpoint cannot be used to probe which fields exist. A field hidden from this
        # person gets the same answer as one that is not sensitive at all.
        from vantage.fieldperms import is_hidden_path

        if name not in panel.get_sensitive_fields() or is_hidden_path(request, panel, name):
            return _json({"error": _("That field cannot be revealed.")}, status=404)
        try:
            field = panel.model._meta.get_field(name)
        except FieldDoesNotExist:
            return _json({"error": _("That field cannot be revealed.")}, status=404)
        if not getattr(field, "concrete", False):
            return _json({"error": _("That field cannot be revealed.")}, status=404)

        # The budget is spent before any database work, for the same reason the
        # middleware spends it before the view runs: the refusal path must not be the
        # cheap, unlimited one.
        limit = rate_limit_for("reveal")
        if limit is not None:
            state = limit.spend(actor_for(request))
            if state.blocked:
                response = _json(
                    {"error": _("Too many values revealed. Wait a moment and try again.")},
                    status=429,
                )
                if state.retry_after:
                    response["Retry-After"] = str(int(state.retry_after))
                return response

        obj = get_record(request, panel, self.permissions, self.kwargs["pk"], "view")

        if sensitive.is_hashed(panel.model, name):
            return self._hashed(panel, obj, name)
        if not sensitive.can_reveal(request, panel, obj, name):
            logger.warning(
                "Refused to reveal %s.%s on %s for user %s.",
                panel.opts.label,
                name,
                obj.pk,
                getattr(request.user, "pk", None),
            )
            return _json(
                {"error": _("You do not have permission to reveal this value.")}, status=403
            )

        value = getattr(obj, field.attname, None)
        if sensitive.looks_like_password_hash(value):
            return self._hashed(panel, obj, name)

        from vantage import audit
        from vantage.models import VantageLogEntry

        audit.record(
            request,
            VantageLogEntry.Action.ACTION,
            obj=obj,
            action_name=f"revealed {name} on",
            object_repr=sensitive.safe_str(obj, panel.site),
            changes={},
        )
        return _json(
            {
                "field": name,
                "value": sensitive.reveal_value(value),
                "expires_in": sensitive.REVEAL_SECONDS,
            }
        )

    @staticmethod
    def _hashed(panel, obj, name):
        return _json(
            {
                "error": _("This value is stored as a hash and cannot be revealed."),
                "set_password_url": sensitive.set_password_url(panel, obj, name),
            },
            status=403,
        )


class SetPasswordView(PanelView):
    """Set a user's password from their detail page, with Django's own admin form."""

    template_name = "vantage/set_password.html"
    required_action = "change"

    def get_object(self):
        from vantage.fieldperms import access
        from vantage.formviews import get_record
        from vantage.safety import real_user

        obj = get_record(self.request, self.panel, self.permissions, self.kwargs["pk"], "change")
        if sensitive.set_password_url(self.panel, obj, "password") is None:
            raise Http404(_("This record has no password to set."))
        if not access(self.request, self.panel).can_change("password"):
            # Setting a password is a write to the field, whichever form does it.
            raise PermissionDenied(_("You cannot change this field."))
        if getattr(obj, "is_superuser", False) and not getattr(
            real_user(self.request), "is_superuser", False
        ):
            # Choosing a superuser's password is signing in as them: a staff account
            # allowed to edit users would otherwise make itself a superuser in one step.
            raise PermissionDenied(_("Only a superuser can set a superuser's password."))
        return obj

    def get_form(self, obj):
        from django.contrib.auth.forms import AdminPasswordChangeForm

        data = self.request.POST if self.request.method == "POST" else None
        form = AdminPasswordChangeForm(obj, data)
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "v-input")
        return form

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(self.get_context_data(form=self.get_form(self.object)))

    def post(self, request, *args, **kwargs):
        self.object = obj = self.get_object()
        form = self.get_form(obj)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        form.save()
        if isinstance(request.user, type(obj)) and request.user.pk == obj.pk:
            # Changing your own password must not sign you out mid-task, as Django's
            # admin does not either.
            from django.contrib.auth import update_session_auth_hash

            update_session_auth_hash(request, obj)

        from vantage import audit
        from vantage.models import VantageLogEntry

        audit.record(
            request,
            VantageLogEntry.Action.UPDATE,
            obj=obj,
            object_repr=sensitive.safe_str(obj, self.panel.site),
            changes={"password": {"redacted": True}},
        )
        messages.success(request, _("The password was changed."))
        return HttpResponseRedirect(self._detail_url(obj))

    def _detail_url(self, obj) -> str:
        try:
            return reverse(f"vantage:{self.panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
            return reverse(self.panel.list_url_name)

    def get_breadcrumbs(self):
        obj = getattr(self, "object", None)
        crumbs = [(self.panel.get_label_plural(), reverse(self.panel.list_url_name))]
        if obj is not None:
            crumbs.append((sensitive.safe_str(obj, self.panel.site), self._detail_url(obj)))
        crumbs.append((gettext_lazy("Set password"), None))
        return crumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            panel=self.panel,
            object=self.object,
            object_label=sensitive.safe_str(self.object, self.panel.site),
            cancel_url=self._detail_url(self.object),
            page_title=_("Set password"),
        )
        return context


def build_urls(site) -> list:
    """The two routes per panel, mounted before the greedy ``<path:pk>/`` detail route."""
    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/"
        urlpatterns.append(
            path(
                f"{prefix}<path:pk>/reveal/<str:field>/",
                RevealView.as_view(site=site, panel=panel),
                name=f"{panel.slug}-reveal",
            )
        )
        if hasattr(panel.model, "set_password"):
            urlpatterns.append(
                path(
                    f"{prefix}<path:pk>/set-password/",
                    SetPasswordView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-set-password",
                )
            )
    return urlpatterns
