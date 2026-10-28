"""The screens behind :class:`vantage.pages.ApprovalsPage`, and the record banner.

The page is the queue: what is waiting for you, what you have asked for, and what was
decided lately. Each request opens on its own review screen — the field, what it is now,
what was asked — and every decision there is a POST that redirects, wrapped in
``csrf_protect`` for the reason :mod:`vantage.rolesviews` gives: a forged approval is a
write somebody else's permission paid for.

**A request is as visible as its record.** Every lookup goes through the panel's scoped
queryset — :meth:`~vantage.permissions.PanelPermissions.filter_queryset`, which is where
tenancy is applied too — so a request about a record you cannot see does not exist for
you, on the queue, in the count or by its id. The one exception is your own request
whose record has since been deleted or moved out of your reach: you can still read it
and withdraw it, though never approve it.
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import NoReverseMatch, path, reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_protect

from vantage import approvals
from vantage.models import ChangeRequest
from vantage.views import SiteView

__all__ = [
    "ReviewView",
    "banner_context",
    "build_urls",
    "list_context",
    "may_use",
    "waiting_count",
]

#: How many requests of each kind the page lists. It is a queue to work through, not
#: an archive: the audit log is where old decisions are looked up.
QUEUE_LIMIT = 100
RECENT_LIMIT = 20


# ── which requests this user may see ────────────────────────────────────────


def _panels(site) -> dict[str, Any]:
    return {panel.model._meta.label_lower: panel for panel in site.panels()}


def may_use(site, request) -> bool:
    """Whether the page is in this user's navigation at all.

    Anyone who may view a model with ``requires_approval``: approvers work the queue,
    and everybody else finds their own requests there.
    """
    from vantage.permissions import get_permissions

    permissions = get_permissions()
    return any(
        approvals.approvable_fields(panel)
        and permissions.has_model_perm(request, panel.model, "view")
        for panel in site.panels()
    )


def _visible(site, request, change_requests) -> list[tuple[ChangeRequest, Any, Any]]:
    """``(request, panel, record)`` for each one whose record this user can see.

    One scoped query per model, not per request. The record is ``None`` only for the
    user's own request when the record is out of reach; anyone else's is dropped.
    """
    from vantage.permissions import get_permissions

    permissions = get_permissions()
    panels = _panels(site)
    by_key: dict[str, list[ChangeRequest]] = {}
    for item in change_requests:
        by_key.setdefault(item.resource_key, []).append(item)

    records: dict[tuple[str, str], Any] = {}
    for key, items in by_key.items():
        panel = panels.get(key)
        if panel is None or not permissions.has_model_perm(request, panel.model, "view"):
            continue
        queryset = permissions.filter_queryset(request, panel.get_queryset(request), "view")
        ids = {item.object_id for item in items}
        try:
            found = queryset.filter(pk__in=ids)
            records.update({(key, str(obj.pk)): obj for obj in found})
        except (ValueError, TypeError):  # an id the key's type cannot parse
            continue

    out = []
    for item in change_requests:
        panel = panels.get(item.resource_key)
        if panel is None:
            continue
        obj = records.get((item.resource_key, item.object_id))
        if obj is None and not approvals.is_own(request, item):
            continue
        out.append((item, panel, obj))
    return out


def _approvable_keys(site, request) -> list[str]:
    """The models with ``requires_approval`` whose requests this user may decide on.

    Model-level only. Empty — and so no query anywhere downstream — on a site where no
    panel uses the feature.
    """
    return [
        key
        for key, panel in _panels(site).items()
        if approvals.approvable_fields(panel) and approvals.may_approve(request, panel)
    ]


def waiting_count(site, request) -> int:
    """The number in the navigation: pending requests this user could decide.

    Model-level permission only — the object-level check runs on the page, once per
    request shown. A count that asked it for every pending row on every page load would
    put the queue's cost on every screen in the panel.
    """
    keys = _approvable_keys(site, request)
    if not keys:
        return 0
    pending = ChangeRequest.objects.filter(
        status=ChangeRequest.Status.PENDING, resource_key__in=keys
    ).exclude(requested_by=request.user)[:QUEUE_LIMIT]
    return sum(1 for _item, _panel, obj in _visible(site, request, list(pending)) if obj)


# ── the page ────────────────────────────────────────────────────────────────


def _entry(request, item, panel, obj) -> dict[str, Any]:
    return {
        "request": item,
        "panel": panel,
        "object": obj,
        "record_url": _record_url(panel, obj),
        "review_url": reverse("vantage:approval-review", args=[item.pk]),
        "rows": approvals.rows(panel, item, obj),
        "may_decide": approvals.may_decide(request, panel, obj, item),
        "may_withdraw": approvals.may_withdraw(request, item),
    }


def _record_url(panel, obj) -> str | None:
    if obj is None:
        return None
    try:
        return reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
    except NoReverseMatch:  # pragma: no cover - only when detail is not mounted
        return None


def list_context(site, request) -> dict[str, Any]:
    """Context for ``vantage/approvals/list.html``."""
    keys = _approvable_keys(site, request)
    waiting = []
    if keys:
        pending = ChangeRequest.objects.filter(
            status=ChangeRequest.Status.PENDING, resource_key__in=keys
        ).order_by("requested_at", "pk")[:QUEUE_LIMIT]
        waiting = [
            _entry(request, item, panel, obj)
            for item, panel, obj in _visible(site, request, list(pending))
            if obj is not None and not approvals.is_own(request, item)
        ]

    mine_qs = ChangeRequest.objects.filter(requested_by=request.user).order_by(
        "-requested_at", "-pk"
    )[:QUEUE_LIMIT]
    mine = [_entry(request, *row) for row in _visible(site, request, list(mine_qs))]

    recent = []
    if keys:
        decided = (
            ChangeRequest.objects.filter(resource_key__in=keys)
            .exclude(status=ChangeRequest.Status.PENDING)
            .order_by("-decided_at", "-pk")[:RECENT_LIMIT]
        )
        recent = [_entry(request, *row) for row in _visible(site, request, list(decided))]

    return {
        "waiting": waiting,
        "mine": mine,
        "recent": recent,
        "is_approver": bool(keys),
    }


# ── the banner on a record ──────────────────────────────────────────────────


def banner_context(request, panel, obj) -> dict[str, Any]:
    """``approval_banner`` for the detail and edit pages: what is waiting on this record.

    Shown to anyone who can see the record, because "the price you are looking at is
    about to change" is worth knowing whoever you are. The review link only appears for
    someone who could act on it.
    """
    if not approvals.approvable_fields(panel):
        # No query at all for the panels that do not use the feature, which is most.
        return {"approval_banner": None}
    pending = approvals.pending_for(request, panel, obj)
    entries = []
    for item in pending:
        entries.append(
            {
                "request": item,
                "rows": approvals.rows(panel, item, obj),
                "review_url": reverse("vantage:approval-review", args=[item.pk])
                if approvals.may_decide(request, panel, obj, item)
                or approvals.may_withdraw(request, item)
                else None,
            }
        )
    held = approvals.guarded_for(request, panel, obj)
    return {
        "approval_banner": {
            "pending": entries,
            # Said on the edit page before anything is typed, rather than after.
            "held": [approvals.field_label(panel.model, name) for name in held],
            "held_names": list(held),
            "note_field": approvals.NOTE_FIELD,
        }
    }


# ── one request ─────────────────────────────────────────────────────────────


@method_decorator(csrf_protect, name="dispatch")
class ReviewView(SiteView):
    """One request: the review screen, and the decisions posted from it."""

    template_name = "vantage/approvals/review.html"

    def check_permission(self, request):
        # The lookup below is the real gate: it 404s anything this user may not see.
        return None

    def resolve(self):
        item = get_object_or_404(ChangeRequest, pk=self.kwargs["pk"])
        found = _visible(self.site, self.request, [item])
        if not found:
            raise Http404(_("No such change request."))
        mine = approvals.is_own(self.request, item)
        _item, panel, obj = found[0]
        if not mine and not approvals.may_approve(self.request, panel):
            raise Http404(_("No such change request."))
        return item, panel, obj

    def list_url(self) -> str:
        return reverse("vantage:page-approvals")

    def get(self, request, *args, **kwargs):
        item, panel, obj = self.resolve()
        entry = _entry(request, item, panel, obj)
        return self.render_to_response(self.get_context_data(entry=entry))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entry = kwargs["entry"]
        context.update(
            page_title=gettext("Change to %(record)s") % {"record": entry["request"].object_repr},
            page_description=entry["panel"].get_label(),
            list_url=self.list_url(),
        )
        context["breadcrumbs"] = [
            (gettext("Approvals"), self.list_url()),
            (context["page_title"], None),
        ]
        return context

    def post(self, request, *args, **kwargs):
        item, panel, obj = self.resolve()
        decision = request.POST.get("decision")
        back = self.next_url()
        if decision == "withdraw":
            if not approvals.may_withdraw(request, item):
                return self.refuse(request, item, panel, obj)
            if approvals.withdraw(request, obj, item):
                messages.success(request, gettext("Your request was withdrawn."))
            else:
                messages.error(request, gettext("This request has already been decided."))
            return HttpResponseRedirect(back)

        if decision not in ("approve", "reject"):
            messages.error(request, gettext("Choose approve or reject."))
            return HttpResponseRedirect(request.path)
        if not approvals.may_decide(request, panel, obj, item):
            return self.refuse(request, item, panel, obj)

        if decision == "reject":
            reason = request.POST.get("reason", "").strip()
            if not reason:
                messages.error(
                    request, gettext("Say why it is rejected, so they know what to change.")
                )
                return self.render_to_response(
                    self.get_context_data(
                        entry=_entry(request, item, panel, obj), reason_missing=True
                    ),
                    status=422,
                )
            if approvals.reject(request, obj, item, reason):
                messages.success(request, gettext("Rejected. The record was not changed."))
            else:
                messages.error(request, gettext("This request has already been decided."))
            return HttpResponseRedirect(back)

        outcome = approvals.approve(request, panel, obj, item)
        if outcome.ok:
            messages.success(request, outcome.message)
            return HttpResponseRedirect(back)
        messages.error(request, outcome.message)
        for name, errors in outcome.errors.items():
            for error in errors:
                messages.error(request, f"{approvals.field_label(panel.model, name)}: {error}")
        # Back to the review screen, which now marks the stale fields beside their
        # current values: "look again" is literally what it shows.
        return HttpResponseRedirect(request.path)

    def refuse(self, request, item, panel, obj):
        if not item.is_pending:
            messages.error(request, gettext("This request has already been decided."))
        elif approvals.is_own(request, item) and approvals.may_approve(request, panel, obj):
            messages.error(request, gettext("You cannot approve or reject your own request."))
        else:
            messages.error(request, gettext("You do not have permission to do that."))
        return HttpResponseRedirect(request.path)

    def next_url(self) -> str:
        target = self.request.POST.get("next") or ""
        if url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            return target
        return self.list_url()


def build_urls(site) -> list:
    """The review screen, under the approvals page's own path."""
    return [
        path(
            "_/approvals/<int:pk>/",
            ReviewView.as_view(site=site),
            name="approval-review",
        )
    ]
