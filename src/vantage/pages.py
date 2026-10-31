"""Custom pages: anything that is not a model.

A reporting screen, an operations console, a settings form, a health check. The bar
this API has to clear is that adding one should feel no heavier than writing a
``ModelPanel`` — a class, a title, and a method that returns some data:

    @site.register_page
    class RevenuePage(Page):
        title = "Revenue"
        icon = "trending-up"

        def get_context(self, request):
            return {"total": Order.objects.total_revenue()}

Everything else has a default. The URL comes from the class name, the template falls
back to a generic renderer that displays whatever ``get_context`` returned, and the
nav entry appears on its own. A page with no template at all still produces a
presentable screen, which is what keeps the first one cheap to write.
"""

from __future__ import annotations

import re
from typing import Any

from django.utils.translation import gettext_lazy as _

__all__ = [
    "AccessReviewPage",
    "ApprovalsPage",
    "BreakGlassPage",
    "DataQualityPage",
    "HealthPage",
    "MyReportsPage",
    "Page",
    "ReportsPage",
    "RolesPage",
    "SchemaPage",
    "WebhooksPage",
    "page_slug",
]

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def page_slug(cls: type) -> str:
    """``RevenueReportPage`` -> ``revenue-report``.

    The trailing ``Page`` is dropped because every class has it and repeating it in
    every URL would be noise.
    """
    name = cls.__name__
    if name.endswith("Page") and len(name) > 4:
        name = name[:-4]
    return _CAMEL.sub("-", name).lower().replace("_", "-")


class Page:
    """Base class for a custom panel page."""

    #: Shown in the nav and as the page heading. Required in practice; defaults to
    #: the slug so a page that forgets it is still usable rather than broken.
    title: str = ""
    description: str = ""
    icon: str | None = None
    group: str | None = None
    order: int = 0

    #: URL segment, relative to where the site is mounted. Defaults to the slug.
    path: str | None = None

    #: An explicit slug, if the derived one is not what you want.
    slug_override: str | None = None

    #: A Django permission string, or a callable taking the request. ``None`` means
    #: any staff user who can reach the panel can open the page.
    permission: str | Any | None = None

    #: Falls back to ``vantage/pages/<slug>.html``, then to the generic renderer.
    template_name: str | None = None

    def __init__(self, site=None) -> None:
        self.site = site

    # ── identity ────────────────────────────────────────────────────────────

    @classmethod
    def get_slug(cls) -> str:
        return cls.slug_override or page_slug(cls)

    @classmethod
    def get_path(cls) -> str:
        path = cls.path if cls.path is not None else f"{cls.get_slug()}/"
        return path.lstrip("/")

    @classmethod
    def get_title(cls) -> str:
        return cls.title or cls.get_slug().replace("-", " ").capitalize()

    # ── access ──────────────────────────────────────────────────────────────

    def has_permission(self, request) -> bool:
        """Whether this user may open the page.

        A callable receives the request, so a page can be gated on anything — a
        feature flag, group membership, the time of day — without inventing a
        permission just to express it.
        """
        if self.permission is None:
            return True
        if callable(self.permission):
            return bool(self.permission(request))
        if request.user.has_perm(str(self.permission)):
            return True
        from vantage.breakglass import holds

        return holds(request, str(self.permission))

    # ── content ─────────────────────────────────────────────────────────────

    def get_context(self, request) -> dict:
        """Everything the page needs. Override this; it is the whole point."""
        return {}

    def get_badge(self, request) -> str | None:
        """A count shown beside the page in the navigation, or ``None`` for none.

        Asked once per page load for every page the user can see, so it has to be cheap
        — a count, not a computation.
        """
        return None

    def get_template_names(self) -> list[str]:
        if self.template_name:
            return [self.template_name]
        # The project's own template wins if it exists; otherwise the generic
        # renderer displays whatever get_context returned.
        return [f"vantage/pages/{self.get_slug()}.html", "vantage/page.html"]

    def get_breadcrumbs(self) -> list[tuple[str, str | None]]:
        return [(self.get_title(), None)]


class SchemaPage(Page):
    """The relationship map, shipped as a built-in page.

    Registered by the app config rather than left for projects to add, because a
    schema view is only useful if it is always there — one a user has to switch on is
    one they never remember exists.
    """

    title = _("Schema")
    icon = "share-2"
    group = _("System")
    order = 90
    path = "_/schema/"
    template_name = "vantage/pages/schema.html"

    def get_context(self, request):
        from vantage.erd import build_graph

        return {"graph": build_graph(self.site, request)}


class HealthPage(Page):
    """System diagnostics. Superusers only — the detail is deployment information."""

    title = _("System")
    icon = "activity"
    group = _("System")
    order = 95
    path = "_/health/"
    template_name = "vantage/pages/health.html"
    permission = staticmethod(lambda request: request.user.is_superuser)

    def get_context(self, request):
        from vantage.health import run_checks

        checks = run_checks()
        return {
            "checks": checks,
            "worst": next(
                (s for s in ("fail", "warn") if any(c.status == s for c in checks)), "ok"
            ),
        }


class DataQualityPage(Page):
    """A read-only health check of the data in every registered model.

    Superusers by default; ``VANTAGE["DATA_QUALITY"]["PERMISSION"]`` widens it. The
    page only ever shows models the user may view, through the same scoped queryset
    the list page uses. Results come from the cache; the "re-run" button posts here.
    """

    title = _("Data quality")
    icon = "database"
    group = _("System")
    order = 96
    path = "_/data-quality/"
    template_name = "vantage/pages/data-quality.html"

    def has_permission(self, request) -> bool:
        from vantage.dataquality import config

        cfg = config()
        if not cfg["ENABLED"]:
            return False
        rule = cfg["PERMISSION"]
        if rule is None:
            return bool(request.user.is_superuser)
        if callable(rule):
            return bool(rule(request))
        return request.user.has_perm(str(rule))

    def get_breadcrumbs(self):
        return [(self.get_title(), None)]

    def get_context(self, request):
        from vantage.dataquality import SEVERITIES, config, get_checks, run_audit

        cfg = config()
        audit = run_audit(self.site, request, page_budget=float(cfg["PAGE_TIME_BUDGET"]))
        selected = request.GET.get("model", "")
        detail = next((r for r in audit.reports if r.model == selected), None)
        issues = [f for r in audit.reports for f in r.issues]
        return {
            "audit": audit,
            "detail": detail,
            "selected": selected,
            "totals": {
                "models": len(audit.reports),
                "issues": len(issues),
                "failures": sum(1 for f in issues if f.severity == "fail"),
                "pending": len(audit.pending),
            },
            "checks": [c for c in get_checks() if c.name not in cfg["CHECKS_EXCLUDE"]],
            "severities": SEVERITIES,
            "cfg": cfg,
        }

    def post(self, request):
        """The re-run button: audit again, bypassing the cache, then show the result."""
        from django.contrib import messages
        from django.shortcuts import redirect

        from vantage.dataquality import config, run_audit

        label = request.POST.get("model") or None
        audit = run_audit(
            self.site,
            request,
            labels=[label] if label else None,
            force=True,
            page_budget=float(config()["PAGE_TIME_BUDGET"]),
        )
        if audit.pending:
            messages.warning(
                request,
                _("Re-ran part of the audit before its time budget ran out. Run it again."),
            )
        else:
            messages.success(request, _("Data quality audit re-run."))
        target = request.path
        if label and request.POST.get("stay"):
            target += f"?model={label}"
        return redirect(target)


def _may_manage_roles(request) -> bool:
    from vantage.roles import may_manage_roles

    return may_manage_roles(request)


class RolesPage(Page):
    """Roles and permissions: ``auth.Group`` managed as a matrix, not a 400-row list.

    Built in for the same reason as the schema page — the alternative is Django's
    filter-horizontal widget over every permission in the project, which is where
    permission hygiene goes to die. Superusers only unless
    ``VANTAGE["ROLES_PERMISSION"]`` widens it. The editor, members and per-user screens
    hang off this page's path; see :mod:`vantage.rolesviews`.
    """

    title = _("Roles")
    description = _("Who can do what: permissions bundled into roles, and who holds them.")
    icon = "shield"
    group = _("System")
    order = 92
    path = "_/roles/"
    template_name = "vantage/roles/list.html"
    permission = staticmethod(_may_manage_roles)

    def get_context(self, request):
        from vantage.rolesviews import list_context

        return list_context(request)


def _may_review_access(request) -> bool:
    from vantage.accessreview import may_review

    return may_review(request)


class AccessReviewPage(Page):
    """Who can see and change which model and which guarded field, for audit evidence.

    Superusers only unless ``VANTAGE["ACCESS_REVIEW_PERMISSION"]`` widens it. Every
    answer comes from the panel's own permission resolver; see
    :mod:`vantage.accessreview`. The download hangs off this page's path.
    """

    title = _("Access review")
    description = _(
        "Who can view, add, change and delete each model, and its sensitive or restricted fields."
    )
    icon = "users"
    group = _("System")
    order = 93
    path = "_/access-review/"
    template_name = "vantage/access/review.html"
    permission = staticmethod(_may_review_access)

    def get_context(self, request):
        from vantage.accessviews import review_context

        return review_context(self.site, request)


class BreakGlassPage(Page):
    """Time-boxed elevated access: ask for it, grant it, see it, end it.

    Open to every staff member, because anyone may *ask* for elevation with a reason;
    granting and approving are gated by ``VANTAGE["BREAK_GLASS_PERMISSION"]``. The
    request, grant, approve, revoke and drop routes hang off this page's path; see
    :mod:`vantage.accessviews` and :mod:`vantage.breakglass`.
    """

    title = _("Elevated access")
    description = _("Hold more than your usual access for a few minutes, with a reason.")
    icon = "shield"
    group = _("System")
    order = 93
    path = "_/elevated-access/"
    template_name = "vantage/access/break_glass.html"

    def get_context(self, request):
        from vantage.accessviews import break_glass_context

        return break_glass_context(self.site, request)


def _may_manage_webhooks(request) -> bool:
    from vantage import webhooks

    return webhooks.enabled() and webhooks.may_manage(request)


class WebhooksPage(Page):
    """Outbound webhooks and their delivery log.

    Superusers, or anyone holding ``vantage.manage_webhooks``; ``VANTAGE["WEBHOOKS"] =
    False`` removes the page. The edit, test and redeliver screens hang off this page's
    path; see :mod:`vantage.webhookviews`.
    """

    title = _("Webhooks")
    description = _("Tell another system, with a signed POST, when a record changes.")
    icon = "send"
    group = _("System")
    order = 97
    path = "_/webhooks/"
    template_name = "vantage/webhooks/list.html"
    permission = staticmethod(_may_manage_webhooks)

    def get_context(self, request):
        from vantage.webhookviews import list_context

        return list_context(request)


def _may_see_reports(request) -> bool:
    from vantage import reports

    return reports.enabled()


def _may_manage_reports(request) -> bool:
    from vantage import reports

    return reports.enabled() and reports.may_manage(request)


class MyReportsPage(Page):
    """The scheduled reports you subscribed to, and what became of each send.

    Open to anyone who can reach the panel: what a report contains is decided by its
    owner's own permissions when it is sent, so listing your own reveals nothing.
    ``VANTAGE["REPORTS"] = False`` removes it. The routes that act on a report hang off
    the same path; see :mod:`vantage.reportviews`.
    """

    title = _("My reports")
    description = _("Saved views emailed to you as a CSV, and what became of each send.")
    icon = "mail"
    group = _("System")
    order = 98
    path = "_/reports/"
    template_name = "vantage/reports/list.html"
    permission = staticmethod(_may_see_reports)

    def get_context(self, request):
        from vantage.reportviews import list_context

        return list_context(request, everyone=False)


class ReportsPage(Page):
    """Every scheduled report, for the people who run the panel."""

    title = _("Scheduled reports")
    description = _("Everyone's scheduled reports: pause, resume, delete or send one now.")
    icon = "mail"
    group = _("System")
    order = 99
    path = "_/reports/all/"
    template_name = "vantage/reports/list.html"
    permission = staticmethod(_may_manage_reports)

    def get_context(self, request):
        from vantage.reportviews import list_context

        return list_context(request, everyone=True)


class ApprovalsPage(Page):
    """Changes to ``requires_approval`` fields, waiting for someone to approve them.

    In the navigation for anyone who can view a model that has such fields: approvers
    work the queue, and everyone else finds their own requests there. The count beside
    it is what is waiting for *you*. The review screen hangs off this page's path; see
    :mod:`vantage.approvalviews`.
    """

    title = _("Approvals")
    description = _("Changes that wait for a second pair of eyes before they are saved.")
    icon = "check"
    group = _("System")
    order = 94
    path = "_/approvals/"
    template_name = "vantage/approvals/list.html"

    def has_permission(self, request) -> bool:
        from vantage.approvalviews import may_use

        return self.site is not None and may_use(self.site, request)

    def get_badge(self, request) -> str | None:
        from vantage.approvalviews import waiting_count

        count = waiting_count(self.site, request)
        return str(count) if count else None

    def get_context(self, request):
        from vantage.approvalviews import list_context

        return list_context(self.site, request)
