"""Bulk actions over the JSON API.

A thin skin over the same pieces the HTML list page's bulk bar uses:
:func:`vantage.actions.resolve_actions` decides which actions this caller may run,
:class:`vantage.actions.Selection` carries "these ids" or "everything matching the
filters, minus these", :func:`vantage.listquery.build` resolves the filters (so tenancy
and permission scoping apply before the selection does), and :mod:`vantage.jobs` decides
whether the work leaves the request. Nothing here re-decides any of them.

The filter selection travels in the **query string**, exactly as it does for the list
endpoint, because a background job stores that query string to re-resolve "select all
matching" later; the selection itself (``ids`` / ``select_all`` / ``excluded``) and the
two safety switches (``confirmed``, ``dry_run``) are the JSON body.
"""

from __future__ import annotations

from typing import Any

from django.urls import NoReverseMatch, reverse

from vantage import audit, jobs, listprefs, listquery
from vantage.actions import Action, Selection, resolve_actions
from vantage.api import (
    ApiError,
    BadRequest,
    ModelApiView,
    _parse_json_body,
    json_response,
)

__all__ = ["ActionListView", "ActionRunView", "action_payload"]


def action_payload(act: Action) -> dict[str, Any]:
    return {
        "name": act.name,
        "label": str(act.label),
        "icon": act.icon,
        "permission": act.permission,
        "destructive": act.destructive,
        "requires_confirmation": act.needs_confirmation(),
        "max_rows": act.max_rows,
        "bulk_strategy": act.bulk_strategy,
        "keywords": list(act.keywords),
    }


class ActionListView(ModelApiView):
    """``GET`` the actions this caller may run on this model."""

    def get(self, request, *args, **kwargs):
        actions = resolve_actions(self.panel, request)
        run_url = None
        try:
            run_url = reverse(
                "vantage:api-action-run",
                kwargs={
                    "app_label": self.panel.opts.app_label,
                    "model_name": self.panel.opts.model_name,
                    "action_name": "ACTION",
                },
            ).replace("ACTION", "{name}")
        except NoReverseMatch:  # pragma: no cover - only when the API is not mounted
            pass
        return json_response({"actions": [action_payload(a) for a in actions], "run_url": run_url})


class ActionRunView(ModelApiView):
    """``POST`` one action over an explicit or filtered selection."""

    def required_action(self, request) -> str:
        # The model-level floor is "may see these rows"; the action's own permission is
        # re-checked below by resolve_actions, on this request, never trusted from a list
        # fetched earlier.
        return "view"

    def post(self, request, *args, **kwargs):
        available = {a.name: a for a in resolve_actions(self.panel, request)}
        chosen = available.get(self.kwargs["action_name"])
        if chosen is None:
            # Unknown and not-permitted share one answer, as on the HTML page, so the
            # response cannot be used to enumerate actions the caller may not run.
            raise ApiError(403, "That action is not available.")

        body = _parse_json_body(request)
        selection = self._selection(body)
        if selection.is_empty:
            raise BadRequest("Nothing was selected. Send 'ids' or 'select_all'.")

        listprefs.resolve(self.panel, request)
        queryset = selection.apply(listquery.build(self.panel, request, self.permissions).queryset)
        count = queryset.count()
        dry_run = _flag(body.get("dry_run"))

        if dry_run:
            return json_response(
                {
                    "dry_run": True,
                    "action": chosen.name,
                    "count": count,
                    "scope": selection.describe(count),
                    "requires_confirmation": chosen.needs_confirmation(),
                    "max_rows": chosen.max_rows,
                    "exceeds_max_rows": chosen.max_rows is not None and count > chosen.max_rows,
                    "background": jobs.should_enqueue(chosen, count) if count else False,
                }
            )

        if count == 0:
            raise ApiError(422, "Those records no longer match.", count=0)
        if chosen.max_rows is not None and count > chosen.max_rows:
            raise ApiError(
                422,
                f"{chosen.label} is limited to {chosen.max_rows} records at a time; "
                f"{count} were selected.",
                count=count,
                max_rows=chosen.max_rows,
            )
        if chosen.needs_confirmation() and not _flag(body.get("confirmed")):
            # 428: the request is fine but must be repeated with "confirmed": true. The
            # affected count is what the HTML confirm step shows.
            raise ApiError(
                428,
                str(chosen.confirmation_text(count)),
                confirmation_required=True,
                count=count,
                scope=selection.describe(count),
            )

        if jobs.should_enqueue(chosen, count):
            job = jobs.enqueue(
                request=request,
                panel=self.panel,
                action=chosen,
                selection=selection,
                count=count,
            )
            status_url = reverse("vantage:job-status", args=[job.pk])
            payload = {
                "action": chosen.name,
                "job": {
                    "id": job.pk,
                    "status": job.status,
                    "total": job.total,
                    "summary": job.summary,
                    "status_url": status_url,
                },
                "scope": selection.describe(job.total),
            }
            # No worker: the job ran inside the request and is already finished.
            return json_response(payload, status=200 if job.is_terminal else 202)

        result = chosen.run(self.panel, request, queryset)
        audit.log_action(request, self.panel.model, chosen, count)
        payload = {
            "action": chosen.name,
            "message": str(result.message),
            "level": result.level,
            "affected": count,
            "scope": selection.describe(count),
        }
        return json_response(payload, status=409 if result.level == "error" else 200)

    @staticmethod
    def _selection(body: dict[str, Any]) -> Selection:
        ids = body.get("ids") or []
        excluded = body.get("excluded") or []
        if not isinstance(ids, list) or not isinstance(excluded, list):
            raise BadRequest("'ids' and 'excluded' must be arrays.")
        return Selection(
            select_all=_flag(body.get("select_all")),
            ids=[str(i) for i in ids],
            excluded=[str(i) for i in excluded],
        )


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)
