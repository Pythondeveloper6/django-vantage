"""The screens behind :class:`vantage.pages.WebhooksPage`.

The page itself (hooks and their delivery log) is a plain :class:`~vantage.pages.Page`;
this module owns the writes. Every one is a POST that redirects, wrapped in
``csrf_protect`` for the reason :mod:`vantage.rolesviews` gives: this screen decides where
the project's data is sent, and a forged request here is an exfiltration route.

**The signing secret is shown once.** It is generated on create (or when "new secret" is
ticked) and put in a flash message, which is read once and gone. The edit form never
prints it, so a browser extension, a screenshot of the page or a colleague with
``manage_webhooks`` reading over a shoulder cannot lift it later.
"""

from __future__ import annotations

from functools import cached_property

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_protect

from vantage import webhooks
from vantage.models import VantageLogEntry, Webhook, WebhookDelivery
from vantage.safety import read_only_reason
from vantage.views import SiteView

__all__ = [
    "DeliveryView",
    "RedeliverView",
    "TestWebhookView",
    "WebhookDeleteView",
    "WebhookEditView",
    "build_urls",
    "list_context",
]

#: Session key for the secret that was just generated, until the list page shows it.
SECRET_KEY = "vantage_webhook_secret"

#: Deliveries per page of the log.
PER_PAGE = 25


def _resource_choices(site, current=()) -> list[tuple[str, str]]:
    """Every registered model, plus any already subscribed to that no longer is, so
    re-saving a hook never silently drops a subscription."""
    choices = {
        panel.model._meta.label_lower: str(panel.model._meta.verbose_name_plural).capitalize()
        for panel in site.panels()
        if panel.model._meta.app_label != "vantage"
    }
    for key in current:
        choices.setdefault(key, key)
    return sorted(choices.items(), key=lambda item: item[1].lower())


class WebhookForm(forms.ModelForm):
    resources = forms.MultipleChoiceField(
        label=_("Models"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Leave all unticked to be told about every model."),
    )
    events = forms.MultipleChoiceField(
        label=_("Events"),
        choices=[(key, label) for key, label in webhooks.EVENTS],
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": _("Choose at least one event.")},
    )
    rotate_secret = forms.BooleanField(
        label=_("Generate a new secret"),
        required=False,
        help_text=_("The old one stops working at once. The new one is shown once, after saving."),
    )

    class Meta:
        model = Webhook
        fields = ["name", "url", "resources", "events", "active"]
        labels = {"name": _("Name"), "url": _("URL"), "active": _("Active")}
        help_texts = {
            "url": _(
                "Where the POST is sent. Public addresses only, unless the project allows more."
            ),
        }

    def __init__(self, *args, site, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["resources"].choices = _resource_choices(site, self.instance.resources or ())
        if not self.instance.pk:
            self.fields["events"].initial = [key for key, _label in webhooks.EVENTS]
            self.fields["active"].initial = True
            del self.fields["rotate_secret"]

    def clean_url(self):
        url = self.cleaned_data["url"].strip()
        problem = webhooks.url_problem(url)
        if problem:
            raise forms.ValidationError(problem)
        return url


@method_decorator(csrf_protect, name="dispatch")
class WebhookView(SiteView):
    """Shared gate, lookups and the read-only refusal."""

    def check_permission(self, request):
        if webhooks.enabled() and webhooks.may_manage(request):
            return None
        raise PermissionDenied(_("You do not have permission to manage webhooks."))

    @cached_property
    def hook(self) -> Webhook:
        return get_object_or_404(Webhook, pk=self.kwargs["pk"])

    def require_writable(self) -> None:
        reason = read_only_reason()
        if reason:
            raise PermissionDenied(reason)

    def list_url(self) -> str:
        return reverse("vantage:page-webhooks")

    def get_breadcrumbs(self):
        return [(gettext("Webhooks"), self.list_url()), (self.page_title or "", None)]

    def audit(self, action, hook, changes=None):
        from vantage import audit

        audit.record(self.request, action, obj=hook, changes=changes or {})


class WebhookEditView(WebhookView):
    """Create (no ``pk``) or edit one webhook."""

    template_name = "vantage/webhooks/form.html"

    @cached_property
    def instance(self) -> Webhook | None:
        return self.hook if "pk" in self.kwargs else None

    def get_form(self, data=None):
        return WebhookForm(data, instance=self.instance or Webhook(), site=self.site)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        hook = self.instance
        context.update(
            form=kwargs.get("form") or self.get_form(),
            hook=hook,
            page_title=hook.name if hook else _("New webhook"),
            page_description=_("A signed POST to this address whenever a record changes."),
            list_url=self.list_url(),
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require_writable()
        form = self.get_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        hook = form.save(commit=False)
        creating = hook.pk is None
        changed = [name for name in form.changed_data if name != "rotate_secret"]
        secret = ""
        if creating or form.cleaned_data.get("rotate_secret"):
            secret = hook.secret = webhooks.new_secret()
            changed.append("secret")
        if creating:
            hook.created_by = request.user
        hook.save()
        # The secret is recorded as having changed, never with its value.
        self.audit(
            VantageLogEntry.Action.CREATE if creating else VantageLogEntry.Action.UPDATE,
            hook,
            {name: {"redacted": True} for name in changed},
        )
        messages.success(request, gettext("Webhook saved."))
        if secret:
            # Held in the session until the list page has shown it once. Not a message:
            # a toast fades, and this is the only chance to copy the value.
            request.session[SECRET_KEY] = {"name": hook.name, "secret": secret}
        return HttpResponseRedirect(self.list_url())


class WebhookDeleteView(WebhookView):
    template_name = "vantage/webhooks/confirm_delete.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            hook=self.hook,
            deliveries=self.hook.deliveries.count(),
            page_title=_("Delete webhook"),
            list_url=self.list_url(),
        )
        return context

    def post(self, request, *args, **kwargs):
        self.require_writable()
        hook = self.hook
        self.audit(VantageLogEntry.Action.DELETE, hook, {"name": {"from": hook.name}})
        hook.delete()
        messages.success(request, gettext("Webhook deleted."))
        return HttpResponseRedirect(self.list_url())


class TestWebhookView(WebhookView):
    """Send a ``test`` event now and report what the receiver said."""

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        delivery = webhooks.send_test(request, self.hook)
        if delivery.status == WebhookDelivery.Status.SUCCEEDED:
            messages.success(
                request,
                gettext("Test delivered: the receiver answered %(code)s.")
                % {"code": delivery.response_code},
            )
        else:
            messages.error(
                request,
                gettext("Test failed: %(error)s")
                % {"error": delivery.error or gettext("no answer")},
            )
        return HttpResponseRedirect(reverse("vantage:webhook-delivery", args=[delivery.pk]))


class DeliveryView(WebhookView):
    """One delivery: the body that was sent and everything the receiver sent back."""

    template_name = "vantage/webhooks/delivery.html"

    @cached_property
    def delivery(self) -> WebhookDelivery:
        return get_object_or_404(
            WebhookDelivery.objects.select_related("webhook"), pk=self.kwargs["pk"]
        )

    def get_context_data(self, **kwargs):
        import json

        context = super().get_context_data(**kwargs)
        context.update(
            delivery=self.delivery,
            body=json.dumps(self.delivery.payload, indent=2, sort_keys=True),
            page_title=_("Delivery"),
            page_description=self.delivery.webhook.name,
            list_url=self.list_url(),
        )
        return context


class RedeliverView(WebhookView):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        original = get_object_or_404(
            WebhookDelivery.objects.select_related("webhook"), pk=kwargs["pk"]
        )
        copy = webhooks.redeliver(original)
        messages.success(request, gettext("Queued again as a new delivery."))
        return HttpResponseRedirect(reverse("vantage:webhook-delivery", args=[copy.pk]))


def list_context(request) -> dict:
    """What the page renders: the hooks, and a page of the delivery log."""
    from django.db.models import Count, Q

    hooks = list(
        Webhook.objects.annotate(
            failing=Count("deliveries", filter=Q(deliveries__status="failed")),
            total=Count("deliveries"),
        )
    )
    deliveries = WebhookDelivery.objects.select_related("webhook")
    selected = request.GET.get("webhook", "")
    if selected.isdigit():
        deliveries = deliveries.filter(webhook_id=int(selected))
    status = request.GET.get("status", "")
    if status in WebhookDelivery.Status.values:
        deliveries = deliveries.filter(status=status)
    page = Paginator(deliveries, PER_PAGE).get_page(request.GET.get("page"))
    return {
        "hooks": hooks,
        "page_obj": page,
        "selected": selected,
        "status": status,
        "statuses": WebhookDelivery.Status.choices,
        "new_secret": request.session.pop(SECRET_KEY, None)
        if hasattr(request, "session")
        else None,
        "writable": read_only_reason() is None,
        "backend": _backend(),
    }


def _backend():
    from vantage.jobs import detect_backend

    return detect_backend()


def build_urls(site) -> list:
    """The edit, delete, test and delivery routes, under the webhooks page's own path.

    Empty when no :class:`~vantage.pages.WebhooksPage` is registered, so removing the page
    removes every write route with it."""
    from vantage.pages import WebhooksPage

    page = next((p for p in site.pages() if issubclass(p, WebhooksPage)), None)
    if page is None:
        return []
    base = page.get_path()
    return [
        path(f"{base}new/", WebhookEditView.as_view(site=site), name="webhook-create"),
        path(f"{base}<int:pk>/", WebhookEditView.as_view(site=site), name="webhook-edit"),
        path(
            f"{base}<int:pk>/delete/", WebhookDeleteView.as_view(site=site), name="webhook-delete"
        ),
        path(f"{base}<int:pk>/test/", TestWebhookView.as_view(site=site), name="webhook-test"),
        path(
            f"{base}deliveries/<int:pk>/", DeliveryView.as_view(site=site), name="webhook-delivery"
        ),
        path(
            f"{base}deliveries/<int:pk>/redeliver/",
            RedeliverView.as_view(site=site),
            name="webhook-redeliver",
        ),
    ]
