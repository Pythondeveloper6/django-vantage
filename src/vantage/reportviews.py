"""The screens for scheduled reports: subscribe, "My reports", the System page, and the
unsubscribe link.

Four rules shape them, and each is a place a quieter design would have gone wrong.

**You act on your own subscriptions; managers act on everyone's, but only to stop or
start them.** Pause, resume, delete and send-now are open to the owner and to anyone who
may manage reports. Changing what is sent and to whom is the owner's alone, because the
export runs as them: letting a manager redirect somebody else's report would be a way to
read data through that person's permissions.

**Other addresses are a permission.** Anybody who can see a view may mail it to
themselves; naming someone else needs ``vantage.send_reports_to_others``, held by the
person *editing*, and checked again by the sender for the person it runs as.

**Subscribing needs the same rights as opening the view.** The model's ``view``
permission and access to the saved view, checked on this request. They are checked
again, from the database, every time it sends.

**The unsubscribe link is the one route with no sign-in.** It carries a signed token for
a single subscription and can only pause that one. A GET shows a button instead of
acting, because mail scanners follow every link in a message and an unsubscribe that
fires on GET would be triggered by the scanner; the POST is what mail clients send for
one-click unsubscribe.
"""

from __future__ import annotations

import datetime as dt
from functools import cached_property

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dates import WEEKDAYS
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from vantage import reports, twofactor
from vantage.models import ReportRun, ReportSubscription, SavedView, VantageLogEntry
from vantage.safety import read_only_reason
from vantage.views import SiteView

__all__ = [
    "BASE",
    "ReportDeleteView",
    "ReportEditView",
    "ReportForm",
    "ReportStateView",
    "SubscribeView",
    "UnsubscribeView",
    "build_urls",
    "form_for",
    "list_context",
    "subscribe_context",
]

#: Where every report route lives, under the ``_/`` prefix that is reserved for panel
#: machinery.
BASE = "_/reports/"

#: Sends per page of the log.
PER_PAGE = 25


# ── the form ────────────────────────────────────────────────────────────────


class ReportForm(forms.ModelForm):
    """Frequency, time and recipients. The same form is the popover on a list page and
    the edit screen, so the two cannot disagree about what is valid."""

    weekday = forms.TypedChoiceField(
        label=_("Day of the week"),
        required=False,
        coerce=int,
        empty_value=None,
        choices=[(day, str(name)) for day, name in WEEKDAYS.items()],
        initial=0,
    )
    day_of_month = forms.IntegerField(
        label=_("Day of the month"),
        required=False,
        min_value=1,
        max_value=31,
        initial=1,
        help_text=_("Months that are shorter send on their last day."),
    )
    send_time = forms.TimeField(
        label=_("Time"),
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        initial=dt.time(8, 0),
    )
    to_self = forms.BooleanField(label=_("Send to me"), required=False, initial=True)
    recipients = forms.CharField(
        label=_("Also send to"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Email addresses, separated by commas."),
    )

    class Meta:
        model = ReportSubscription
        fields = ["frequency", "weekday", "day_of_month", "send_time", "to_self", "recipients"]
        labels = {"frequency": _("How often")}

    def __init__(self, *args, owner, editor, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.editor = editor
        self.fields["frequency"].choices = ReportSubscription.Frequency.choices
        if not reports.may_send_to_others(editor):
            # Absent rather than disabled: an address typed into a field the person is
            # not allowed to use must not be able to reach the model instance.
            del self.fields["recipients"]
        else:
            self.initial["recipients"] = ", ".join(self.instance.recipients or [])
        for name, css in (
            ("frequency", "v-select"),
            ("weekday", "v-select"),
            ("day_of_month", "v-input"),
            ("send_time", "v-input"),
            ("recipients", "v-input"),
        ):
            if name in self.fields:
                self.fields[name].widget.attrs["class"] = css
        self.fields["to_self"].help_text = (
            _("Goes to %(address)s.") % {"address": owner.email}
            if owner.email
            else _("Your account has no email address, so there is nothing to send to.")
        )

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients") or ""
        found = reports.parse_recipients(raw)
        if len(found) > reports.MAX_RECIPIENTS:
            raise forms.ValidationError(
                gettext("At most %(limit)s other addresses.") % {"limit": reports.MAX_RECIPIENTS}
            )
        return found

    def clean(self):
        data = super().clean()
        frequency = data.get("frequency")
        if frequency == ReportSubscription.Frequency.WEEKLY:
            if data.get("weekday") is None:
                self.add_error("weekday", gettext("Choose a day of the week."))
        else:
            data["weekday"] = None
        if frequency == ReportSubscription.Frequency.MONTHLY:
            if not data.get("day_of_month"):
                self.add_error("day_of_month", gettext("Choose a day of the month."))
        else:
            data["day_of_month"] = None
        if data.get("to_self") and not self.owner.email:
            self.add_error("to_self", gettext("Your account has no email address."))
        elif not data.get("to_self") and not data.get("recipients", self.instance.recipients):
            self.add_error("to_self", gettext("Choose at least one recipient."))
        return data

    def save(self, commit=True):
        sub = super().save(commit=False)
        # ``recipients`` is a model field but not always a form field; the cleaned list
        # is only applied when this editor was allowed to submit it.
        if "recipients" in self.fields:
            sub.recipients = self.cleaned_data["recipients"]
        sub.next_run_at = reports.next_run_for(sub) if sub.active else None
        if commit:
            sub.save()
        return sub


def form_for(request, data=None, *, instance=None, owner=None):
    return ReportForm(
        data,
        instance=instance or ReportSubscription(),
        owner=owner or request.user,
        editor=request.user,
    )


# ── gates ───────────────────────────────────────────────────────────────────


def _safe_next(request, fallback: str) -> str:
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return fallback


def _audit(request, action, sub, changes=None) -> None:
    from vantage import audit

    audit.record(request, action, obj=sub, changes=changes or {})


@method_decorator(csrf_protect, name="dispatch")
class ReportView(SiteView):
    """Shared gate and lookups."""

    def check_permission(self, request):
        if reports.enabled():
            return None
        raise PermissionDenied(gettext("Scheduled reports are switched off for this site."))

    def require_writable(self) -> None:
        reason = read_only_reason()
        if reason:
            raise PermissionDenied(reason)

    @cached_property
    def sub(self) -> ReportSubscription:
        return get_object_or_404(
            ReportSubscription.objects.select_related("view", "user"), pk=self.kwargs["pk"]
        )

    def mine(self) -> bool:
        return self.sub.user_id == self.request.user.pk

    def require_owner_or_manager(self) -> None:
        # A 404, not a 403: whether someone else's subscription exists is not for this
        # person to learn.
        if not (self.mine() or reports.may_manage(self.request)):
            from django.http import Http404

            raise Http404

    def home_url(self) -> str:
        return reverse("vantage:page-my-reports")

    def get_breadcrumbs(self):
        return [(gettext("My reports"), self.home_url()), (self.page_title or "", None)]


class SubscribeView(ReportView):
    """``POST /_/reports/subscribe/`` — from the popover on a saved view's list page."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        from vantage import savedviews
        from vantage.permissions import get_permissions
        from vantage.tenancy import snapshot

        self.require_writable()
        back = _safe_next(request, self.home_url())
        view = SavedView.objects.filter(pk=_int(request.POST.get("view"))).first()
        panel = savedviews._panel_for(self.site, view.model_label) if view else None
        # The same two questions the sender asks again on every run. A 404 for a view
        # the person cannot use, so subscribing cannot probe for which views exist.
        if (
            view is None
            or panel is None
            or not get_permissions().has_model_perm(request, panel.model, "view")
            or not savedviews.can_use(view, request)
        ):
            from django.http import Http404

            raise Http404

        if ReportSubscription.objects.filter(user=request.user).count() >= reports.max_per_user():
            messages.error(
                request,
                gettext("You can have at most %(limit)s scheduled reports.")
                % {"limit": reports.max_per_user()},
            )
            return HttpResponseRedirect(back)

        form = form_for(request, request.POST)
        if not form.is_valid():
            problems = "; ".join(
                f"{form.fields[name].label or name}: {' '.join(errors)}"
                for name, errors in form.errors.items()
                if name in form.fields
            ) or "; ".join(form.non_field_errors())
            messages.error(request, gettext("The report was not scheduled. %s") % problems)
            return HttpResponseRedirect(back)

        sub = form.save(commit=False)
        sub.view = view
        sub.user = request.user
        sub.tenant = snapshot(request) or ""
        sub.origin = request.build_absolute_uri("/")[:-1][:200]
        sub.save()
        _audit(request, VantageLogEntry.Action.CREATE, sub, {"view": {"to": view.name}})
        messages.success(
            request,
            gettext("You will get “%(view)s” — %(when)s.")
            % {"view": view.name, "when": reports.schedule_label(sub).lower()},
        )
        return HttpResponseRedirect(back)


class ReportEditView(ReportView):
    template_name = "vantage/reports/form.html"

    def check_permission(self, request):
        denied = super().check_permission(request)
        if denied is not None:
            return denied
        # Only the owner. A manager can stop a report, not change where it goes.
        if self.sub.user_id != request.user.pk:
            from django.http import Http404

            raise Http404
        return None

    def get_form(self, data=None):
        return form_for(self.request, data, instance=self.sub, owner=self.sub.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        self.page_title = self.sub.view.name
        context.update(
            form=kwargs.get("form") or self.get_form(),
            sub=self.sub,
            page_title=self.sub.view.name,
            page_description=gettext("When this saved view is emailed, and to whom."),
            list_url=self.home_url(),
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require_writable()
        form = self.get_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        sub = form.save()
        _audit(
            request,
            VantageLogEntry.Action.UPDATE,
            sub,
            {name: {"redacted": True} for name in form.changed_data},
        )
        messages.success(request, gettext("Report saved."))
        return HttpResponseRedirect(self.home_url())


class ReportStateView(ReportView):
    """``POST /_/reports/<pk>/<pause|resume|send>/`` — the three that never change a report's
    contents, so a manager may use them on anyone's."""

    http_method_names = ["post"]
    action = ""

    def post(self, request, *args, **kwargs):
        self.require_owner_or_manager()
        sub = self.sub
        back = _safe_next(request, self.home_url())
        self.require_writable()
        if self.action == "send":
            wait = reports.manual_wait(sub)
            if wait:
                messages.error(
                    request,
                    ngettext(
                        "This report was sent a moment ago. Try again in %(count)s second.",
                        "This report was sent a moment ago. Try again in %(count)s seconds.",
                        wait,
                    )
                    % {"count": wait},
                )
                return HttpResponseRedirect(back)
            reports.enqueue(sub, trigger=ReportRun.Trigger.MANUAL)
            messages.success(request, gettext("Queued. The result will appear in the log."))
            return HttpResponseRedirect(back)
        if self.action == "pause":
            sub.active = False
            sub.next_run_at = None
            sub.paused_reason = ""
        else:
            sub.active = True
            sub.paused_reason = ""
            sub.next_run_at = reports.next_run_for(sub)
        sub.save(update_fields=["active", "paused_reason", "next_run_at", "updated_at"])
        _audit(request, VantageLogEntry.Action.UPDATE, sub, {"active": {"to": sub.active}})
        messages.success(
            request,
            gettext("Report resumed.") if sub.active else gettext("Report paused."),
        )
        return HttpResponseRedirect(back)


class ReportDeleteView(ReportView):
    template_name = "vantage/reports/confirm_delete.html"

    def get_context_data(self, **kwargs):
        self.require_owner_or_manager()
        context = super().get_context_data(**kwargs)
        context.update(
            sub=self.sub,
            runs=self.sub.runs.count(),
            page_title=gettext("Delete report"),
            list_url=self.home_url(),
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require_owner_or_manager()
        self.require_writable()
        sub = self.sub
        _audit(request, VantageLogEntry.Action.DELETE, sub, {"view": {"from": sub.view.name}})
        sub.delete()
        messages.success(request, gettext("Report deleted."))
        return HttpResponseRedirect(self.home_url())


# ── the pages ───────────────────────────────────────────────────────────────


def _int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def list_context(request, *, everyone: bool) -> dict:
    """What the page renders: subscriptions, and a page of the send log.

    ``everyone`` is the System page. The caller has already checked ``may_manage``; this
    function only decides *which rows*, so the My reports page can never list somebody
    else's.
    """
    subs = ReportSubscription.objects.select_related("view", "user").annotate(
        failing=Count("runs", filter=Q(runs__status="failed"))
    )
    if not everyone:
        subs = subs.filter(user=request.user)
    subs = list(subs.order_by("view__name", "pk"))

    runs = ReportRun.objects.select_related("subscription__view", "subscription__user")
    if not everyone:
        runs = runs.filter(subscription__user=request.user)
    selected = request.GET.get("report", "")
    if selected.isdigit():
        runs = runs.filter(subscription_id=int(selected))
    status = request.GET.get("status", "")
    if status in ReportRun.Status.values:
        runs = runs.filter(status=status)
    page = Paginator(runs, PER_PAGE).get_page(request.GET.get("page"))

    manager = reports.may_manage(request)
    for sub in subs:
        sub.schedule = reports.schedule_label(sub)
        sub.can_edit = sub.user_id == request.user.pk
        sub.can_act = sub.can_edit or manager
    from vantage.jobs import detect_backend

    return {
        "subs": subs,
        "page_obj": page,
        "selected": selected,
        "status": status,
        "statuses": ReportRun.Status.choices,
        "everyone": everyone,
        "writable": read_only_reason() is None,
        "backend": detect_backend(),
        "now": timezone.now(),
    }


def subscribe_context(request, panel, view) -> dict:
    """What the Subscribe control on a saved view's list page needs. Empty when there is
    nothing to subscribe to, or the feature is off, so the page pays nothing."""
    if not reports.enabled() or view is None or not request.user.is_authenticated:
        return {}
    try:
        url = reverse("vantage:report-subscribe")
    except Exception:
        return {}
    mine = ReportSubscription.objects.filter(user=request.user, view=view).first()
    return {
        "report_subscribe_url": url,
        "report_form": form_for(request),
        "report_existing": mine,
        "report_home_url": reverse("vantage:page-my-reports"),
    }


# ── unsubscribe ─────────────────────────────────────────────────────────────


@method_decorator([never_cache, csrf_exempt], name="dispatch")
class UnsubscribeView(View):
    """The link at the bottom of every report email. No sign-in, one subscription.

    ``csrf_exempt`` because mail clients send the one-click POST (RFC 8058) with no
    cookie and no CSRF token; the signed token in the URL is the credential, it names
    exactly one subscription, and the only thing the POST can do is pause it.

    Pausing, not deleting: the person who clicked may be a recipient rather than the
    owner, and the owner can still see the subscription, resume it, or remove it.
    """

    site = None
    template_name = "vantage/reports/unsubscribe.html"
    #: Reachable by a session that still owes the second sign-in step: it does not read
    #: or write anything the step protects.
    two_factor_allows = frozenset({twofactor.VERIFY, twofactor.ENROL})

    def dispatch(self, request, *args, **kwargs):
        pk = reports.read_unsubscribe_token(kwargs["token"])
        self.sub = (
            ReportSubscription.objects.select_related("view").filter(pk=pk).first()
            if pk is not None
            else None
        )
        self.valid = pk is not None
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return self._render(done=False)

    def post(self, request, *args, **kwargs):
        done = False
        if self.sub is not None:
            if self.sub.active:
                ReportSubscription.objects.filter(pk=self.sub.pk).update(
                    active=False,
                    next_run_at=None,
                    paused_reason=gettext("Someone used the unsubscribe link in an email."),
                )
            done = True
        return self._render(done=done)

    def _render(self, *, done: bool) -> HttpResponse:
        from vantage.authviews import branding_context

        context = branding_context(self.request)
        context.update(
            sub=self.sub,
            valid=self.valid and self.sub is not None,
            done=done,
        )
        response = TemplateResponse(
            self.request,
            self.template_name,
            context,
            status=200 if context["valid"] else 404,
        )
        response["Referrer-Policy"] = "no-referrer"
        return response


def build_urls(site) -> list:
    """Every report route.

    Always built, the unsubscribe link included, whatever pages are registered: an email
    that went out last month still links here.
    """
    return [
        path(
            f"{BASE}subscribe/",
            SubscribeView.as_view(site=site),
            name="report-subscribe",
        ),
        path(
            f"{BASE}unsubscribe/<str:token>/",
            UnsubscribeView.as_view(site=site),
            name="report-unsubscribe",
        ),
        path(f"{BASE}<int:pk>/", ReportEditView.as_view(site=site), name="report-edit"),
        path(f"{BASE}<int:pk>/delete/", ReportDeleteView.as_view(site=site), name="report-delete"),
        path(
            f"{BASE}<int:pk>/pause/",
            ReportStateView.as_view(site=site, action="pause"),
            name="report-pause",
        ),
        path(
            f"{BASE}<int:pk>/resume/",
            ReportStateView.as_view(site=site, action="resume"),
            name="report-resume",
        ),
        path(
            f"{BASE}<int:pk>/send/",
            ReportStateView.as_view(site=site, action="send"),
            name="report-send",
        ),
    ]
