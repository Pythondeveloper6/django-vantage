"""The screens behind :class:`vantage.pages.AgentActivityPage`: the download and one run.

Read-only. Nothing here changes an identity; revoking and rotating stay in the
``vantage_agent`` command. Every route is a 404 while ``VANTAGE["AGENTS"]`` is off, and a
403 for anyone who may not read the audit log, because what an agent did *is* audit data.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from vantage import agentactivity, agents
from vantage.audit import may_view_audit
from vantage.fieldperms import hide_entry_fields
from vantage.models import AgentRun, VantageLogEntry
from vantage.views import SiteView

__all__ = ["AgentActivityDownloadView", "AgentRunView", "build_urls", "list_context", "may_use"]

RUNS_SHOWN = 100
PER_PAGE = 100

_CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
    "ndjson": "application/x-ndjson",
}


def may_use(request) -> bool:
    """Agents switched on, and the reader may read the audit log."""
    return agents.enabled() and may_view_audit(request)


def _check(request) -> None:
    if not agents.enabled():
        raise Http404
    if not may_view_audit(request):
        raise PermissionDenied(_("You do not have permission to read agent activity."))


def list_context(site, request) -> dict:
    agent = (request.GET.get("agent") or "").strip()
    table = agentactivity.agent_table()
    runs = AgentRun.objects.select_related("identity").order_by("-started_at", "-pk")
    if agent:
        runs = runs.filter(identity__name=agent)
    query = f"?agent={agent}" if agent else ""
    sep = "&" if agent else "?"
    download = reverse("vantage:agent-activity-download")
    return {
        "agents": table,
        "agent_names": [row["identity"].name for row in table],
        "selected_agent": agent,
        "runs": [
            {"run": run, "url": reverse("vantage:agent-activity-run", args=[run.pk])}
            for run in runs[:RUNS_SHOWN]
        ],
        "runs_shown": RUNS_SHOWN,
        "download_csv_url": f"{download}{query}{sep}format=csv",
        "download_ndjson_url": f"{download}{query}{sep}format=ndjson",
        "download_runs_url": f"{download}{query}{sep}format=csv&runs=1",
    }


class AgentActivityDownloadView(SiteView):
    """The command's rows as a file, from the same functions."""

    http_method_names = ["get", "head"]

    def check_permission(self, request):
        _check(request)

    def get(self, request, *args, **kwargs):
        fmt = request.GET.get("format", "csv")
        fmt = fmt if fmt in agentactivity.FORMATS else "csv"
        names = [name for name in request.GET.getlist("agent") if name]
        runs = request.GET.get("runs") == "1"
        try:
            since = (
                agentactivity.parse_moment(request.GET["since"])
                if request.GET.get("since")
                else None
            )
            common = {"since": since, "agents": names, "run": request.GET.get("run", "")}
            if runs:
                columns = agentactivity.RUN_COLUMNS
                rows = agentactivity.iter_run_rows(agentactivity.run_queryset(**common))
            else:
                columns = agentactivity.ENTRY_COLUMNS
                rows = agentactivity.iter_entry_rows(
                    agentactivity.entry_queryset(**common), request=request
                )
        except agentactivity.ActivityError:
            raise Http404 from None
        from vantage.audit import record

        record(
            request,
            VantageLogEntry.Action.ACTION,
            changes={
                gettext("Format"): {"to": fmt},
                gettext("Rows"): {"to": gettext("Runs") if runs else gettext("Entries")},
            },
            action_name="agent-activity.export",
            object_repr=gettext("Agent activity"),
        )
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        name = "agent-runs" if runs else "agent-activity"
        response = StreamingHttpResponse(
            agentactivity.render(columns, rows, fmt), content_type=_CONTENT_TYPES[fmt]
        )
        response["Content-Disposition"] = f'attachment; filename="{name}-{stamp}.{fmt}"'
        response["Cache-Control"] = "no-store"
        return response


class AgentRunView(SiteView):
    """One run and the audit entries it made."""

    template_name = "vantage/agents/run.html"
    page_title = _("Agent run")

    def check_permission(self, request):
        _check(request)

    def get_breadcrumbs(self):
        return [(_("Agent activity"), reverse("vantage:page-agent-activity")), (_("Run"), None)]

    def get_context_data(self, **kwargs):
        from vantage.auditviews import _row

        context = super().get_context_data(**kwargs)
        run = get_object_or_404(AgentRun.objects.select_related("identity"), pk=self.kwargs["pk"])
        queryset = (
            VantageLogEntry.objects.filter(agent_run=run)
            .select_related("content_type")
            .order_by("created_at", "id")
        )
        paginator = Paginator(queryset, PER_PAGE)
        page = paginator.get_page(self.request.GET.get("page"))
        rows = []
        for entry in hide_entry_fields(self.request, page.object_list):
            row = _row(entry)
            row["summary"] = agentactivity.summarise(entry)
            rows.append(row)
        context.update(
            run=run,
            entries=rows,
            page=page,
            paginator=paginator,
            page_title=gettext("Run %(key)s of %(agent)s")
            % {"key": run.run_key, "agent": run.identity.name},
            page_description=gettext("The audit entries this run made, oldest first."),
            back_url=reverse("vantage:page-agent-activity"),
        )
        return context


def build_urls(site) -> list:
    """Routes under the page's own path, and only while the page is registered."""
    from vantage.pages import AgentActivityPage

    page = next((p for p in site.pages() if issubclass(p, AgentActivityPage)), None)
    if page is None:
        return []
    base = page.get_path()
    return [
        path(
            f"{base}download/",
            AgentActivityDownloadView.as_view(site=site),
            name="agent-activity-download",
        ),
        path(f"{base}run/<int:pk>/", AgentRunView.as_view(site=site), name="agent-activity-run"),
    ]
