"""The screens behind :class:`~vantage.pages.AccessReviewPage` and
:class:`~vantage.pages.BreakGlassPage`.

Thin, like :mod:`vantage.rolesviews`: what a grant may confer and who may give it is
decided in :mod:`vantage.breakglass`, and what the report says in
:mod:`vantage.accessreview`. Every write is a CSRF-protected POST that redirects —
``csrf_protect`` is applied here rather than assumed from middleware, for the reason
the roles screens give: the failure it guards against is a forged grant.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_protect

from vantage import accessreview, breakglass
from vantage.views import SiteView

__all__ = [
    "AccessReviewExportView",
    "BreakGlassApproveView",
    "BreakGlassDropView",
    "BreakGlassGrantView",
    "BreakGlassRequestView",
    "BreakGlassRevokeView",
    "break_glass_context",
    "build_urls",
    "review_context",
]

#: Ended grants listed under the open ones. Older ones are in the audit log.
RECENT_SHOWN = 50


# ── the access review ───────────────────────────────────────────────────────


def _review_options(site, request) -> tuple[str, str | None, list[tuple[str, str]]]:
    by = request.GET.get("by", "user")
    by = by if by in accessreview.BY else "user"
    choices = sorted(
        ((p.model._meta.label_lower, str(p.get_label_plural())) for p in site.panels()),
        key=lambda item: item[0],
    )
    model = request.GET.get("model", "") or None
    if model is not None and model not in {key for key, _label in choices}:
        model = None
    return by, model, choices


def review_context(site, request) -> dict:
    by, model, choices = _review_options(site, request)
    report = accessreview.build_report(site, by=by, models=[model] if model else None)
    query = f"by={by}" + (f"&model={model}" if model else "")
    export = reverse("vantage:access-review-export")
    return {
        "report": report,
        "by": by,
        "model": model or "",
        "model_choices": choices,
        "export_csv_url": f"{export}?format=csv&{query}",
        "export_json_url": f"{export}?format=json&{query}",
    }


@method_decorator(csrf_protect, name="dispatch")
class AccessReviewExportView(SiteView):
    """The report as a file. Each download is written to the audit log: evidence of
    who can see what is itself sensitive, and who took a copy is part of the story."""

    http_method_names = ["get", "head"]

    def check_permission(self, request):
        if accessreview.may_review(request):
            return None
        raise PermissionDenied(_("You do not have permission to review access."))

    def get(self, request, *args, **kwargs):
        from vantage.audit import record
        from vantage.models import VantageLogEntry

        fmt = request.GET.get("format", "csv")
        fmt = fmt if fmt in accessreview.FORMATS else "csv"
        by, model, _choices = _review_options(self.site, request)
        report = accessreview.build_report(self.site, by=by, models=[model] if model else None)
        if fmt == "json":
            body, content_type = accessreview.to_json(report), "application/json"
        else:
            body, content_type = accessreview.to_csv(report), "text/csv; charset=utf-8"
        record(
            request,
            VantageLogEntry.Action.ACTION,
            changes={
                gettext("Format"): {"to": fmt},
                gettext("By"): {"to": by},
                gettext("Model"): {"to": model or gettext("All")},
                gettext("Subjects"): {"to": str(len(report.subjects))},
            },
            action_name="access-review.export",
            object_repr=gettext("Access review"),
        )
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        response = HttpResponse(body, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="access-review-{by}-{stamp}.{fmt}"'
        response["Cache-Control"] = "no-store"
        return response


# ── break glass ─────────────────────────────────────────────────────────────


_STATUS_LABELS = {
    "pending": _("Waiting for approval"),
    "scheduled": _("Scheduled"),
    "active": _("Active"),
    "expired": _("Expired"),
    "revoked": _("Revoked"),
    "rejected": _("Rejected"),
}


def _row(request, grant) -> dict:
    return {
        "grant": grant,
        "status": grant.status,
        "status_label": _STATUS_LABELS.get(grant.status, grant.status),
        "what": grant.role_repr or ", ".join(grant.permissions or []),
        "may_approve": breakglass.may_approve(request, grant),
        "may_revoke": breakglass.may_revoke(request, grant) and grant.status != "expired",
    }


def break_glass_context(site, request) -> dict:
    from vantage.models import ElevatedAccess

    # Records expiries nobody has observed yet, so the list and the log agree.
    breakglass.observe_all()
    can_grant = breakglass.may_grant(request)
    queryset = ElevatedAccess.objects.select_related("role").order_by("-requested_at", "-pk")
    if not can_grant:
        queryset = queryset.filter(user_id=request.user.pk)
    rows = [_row(request, g) for g in queryset[:500]]
    open_rows = [r for r in rows if r["status"] in ("pending", "scheduled", "active")]
    ended = [r for r in rows if r["status"] not in ("pending", "scheduled", "active")]
    return {
        "can_grant": can_grant,
        "self_service": breakglass.self_service(),
        "max_minutes": breakglass.max_minutes(),
        "min_reason": breakglass.MIN_REASON_LENGTH,
        "roles": list(Group.objects.order_by("name")),
        "open_grants": open_rows,
        "recent_grants": ended[:RECENT_SHOWN],
        "request_url": reverse("vantage:break-glass-request"),
        "grant_url": reverse("vantage:break-glass-grant"),
        "is_superuser": bool(getattr(request.user, "is_superuser", False)),
    }


@method_decorator(csrf_protect, name="dispatch")
class _BreakGlassView(SiteView):
    http_method_names = ["post"]

    def page_url(self) -> str:
        return reverse("vantage:page-break-glass")

    def done(self) -> HttpResponseRedirect:
        return HttpResponseRedirect(self.page_url())

    def fail(self, exc: ValidationError) -> HttpResponseRedirect:
        messages.error(self.request, " ".join(exc.messages))
        return self.done()

    @staticmethod
    def chosen_role(request) -> Group | None:
        value = request.POST.get("role", "").strip()
        if not value:
            return None
        if not value.isdigit():
            raise ValidationError(gettext("That role does not exist."))
        role = Group.objects.filter(pk=int(value)).first()
        if role is None:
            raise ValidationError(gettext("That role does not exist."))
        return role

    def grant_row(self):
        from vantage.models import ElevatedAccess

        return get_object_or_404(ElevatedAccess, pk=self.kwargs["pk"])


class BreakGlassRequestView(_BreakGlassView):
    """Anyone in the panel asks for elevation for themselves, with a reason."""

    def post(self, request, *args, **kwargs):
        try:
            grant = breakglass.request_elevation(
                request,
                role=self.chosen_role(request),
                permissions=request.POST.getlist("permissions"),
                minutes=request.POST.get("minutes", ""),
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as exc:
            return self.fail(exc)
        if grant.granted_at is not None:
            messages.success(request, _("Elevated access is active."))
        else:
            messages.success(
                request, _("Request sent. Someone allowed to grant access has to approve it.")
            )
        return self.done()


class BreakGlassGrantView(_BreakGlassView):
    def check_permission(self, request):
        if breakglass.may_grant(request):
            return None
        raise PermissionDenied(_("You may not grant elevated access."))

    def post(self, request, *args, **kwargs):
        user_model = get_user_model()
        username = request.POST.get("username", "").strip()
        user = user_model._default_manager.filter(**{user_model.USERNAME_FIELD: username}).first()
        try:
            if user is None:
                raise ValidationError(gettext("No account with that username."))
            grant = breakglass.grant(
                request,
                user=user,
                role=self.chosen_role(request),
                permissions=request.POST.getlist("permissions"),
                minutes=request.POST.get("minutes", ""),
                reason=request.POST.get("reason", ""),
            )
        except ValidationError as exc:
            return self.fail(exc)
        messages.success(
            request,
            _("Granted to %(user)s until %(time)s.")
            % {
                "user": grant.user_repr,
                "time": timezone.localtime(grant.expires_at).strftime("%H:%M"),
            },
        )
        return self.done()


class BreakGlassApproveView(_BreakGlassView):
    def post(self, request, *args, **kwargs):
        try:
            grant = breakglass.approve(request, self.grant_row())
        except ValidationError as exc:
            return self.fail(exc)
        messages.success(request, _("Approved for %(user)s.") % {"user": grant.user_repr})
        return self.done()


class BreakGlassRevokeView(_BreakGlassView):
    def post(self, request, *args, **kwargs):
        grant = self.grant_row()
        breakglass.revoke(request, grant, reason=request.POST.get("reason", ""))
        messages.success(request, _("Ended."))
        return self.done()


class BreakGlassDropView(_BreakGlassView):
    """The banner's button: end every active grant of the signed-in person, now."""

    def post(self, request, *args, **kwargs):
        from vantage.models import ElevatedAccess
        from vantage.safety import real_user

        # The account actually signed in: impersonating someone must not let a
        # superuser end that person's grants from their banner.
        actor = real_user(request)
        if actor.pk != request.user.pk:
            raise PermissionDenied(_("Stop impersonating to change elevated access."))
        now = timezone.now()
        grants = ElevatedAccess.objects.filter(
            user_id=actor.pk,
            granted_at__isnull=False,
            revoked_at__isnull=True,
            expires_at__gt=now,
        )
        count = 0
        for grant in grants:
            breakglass.revoke(request, grant, reason=gettext("Dropped by the holder."))
            count += 1
        messages.success(
            request,
            ngettext(
                "Dropped %(count)d elevated grant.",
                "Dropped %(count)d elevated grants.",
                count,
            )
            % {"count": count},
        )
        target = request.POST.get("next", "")
        from django.utils.http import url_has_allowed_host_and_scheme

        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return HttpResponseRedirect(target)
        return self.done()


# ── routes ──────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    """Routes under each page's own path, and only while that page is registered."""
    from vantage.pages import AccessReviewPage, BreakGlassPage

    pages = site.pages()
    urls = []
    review = next((p for p in pages if issubclass(p, AccessReviewPage)), None)
    if review is not None:
        urls.append(
            path(
                f"{review.get_path()}export/",
                AccessReviewExportView.as_view(site=site),
                name="access-review-export",
            )
        )
    glass = next((p for p in pages if issubclass(p, BreakGlassPage)), None)
    if glass is not None:
        base = glass.get_path()
        urls += [
            path(
                f"{base}request/",
                BreakGlassRequestView.as_view(site=site),
                name="break-glass-request",
            ),
            path(f"{base}grant/", BreakGlassGrantView.as_view(site=site), name="break-glass-grant"),
            path(f"{base}drop/", BreakGlassDropView.as_view(site=site), name="break-glass-drop"),
            path(
                f"{base}<int:pk>/approve/",
                BreakGlassApproveView.as_view(site=site),
                name="break-glass-approve",
            ),
            path(
                f"{base}<int:pk>/revoke/",
                BreakGlassRevokeView.as_view(site=site),
                name="break-glass-revoke",
            ),
        ]
    return urls
