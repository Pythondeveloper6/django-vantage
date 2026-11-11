"""``POST /_/api/_agents/runs/<run-key>/undo/``: revert an agent run.

For a person signed in with a session. An agent's bearer token is refused outright: an
agent must never undo (or approve) anything, its own work included. Only a superuser or
the identity's owner may undo a run; see :func:`vantage.agentundo.undo_run`.
"""

from __future__ import annotations

from vantage import agents
from vantage.api import ApiError, ApiView, json_response

__all__ = ["RunUndoView"]


class RunUndoView(ApiView):
    def authenticate(self, request) -> None:
        # Before anything else: a request carrying a bearer header is an agent's, and an
        # agent has no business here whether or not its token is valid.
        if agents.has_bearer(request):
            raise ApiError(403, "Agent tokens cannot undo runs.", code="agent_forbidden")
        super().authenticate(request)

    def post(self, request, *args, **kwargs):
        from vantage.models import AgentRun

        key = self.kwargs["run_key"]
        runs = AgentRun.objects.select_related("identity", "identity__owner").filter(run_key=key)
        name = request.GET.get("agent", "").strip()
        if name:
            runs = runs.filter(identity__name=name)
        found = list(runs[:2])
        if not found:
            raise ApiError(404, "No such run.")
        if len(found) > 1:
            raise ApiError(
                400, "More than one agent used that run key. Add ?agent=<name>.", code="ambiguous"
            )
        run = found[0]
        try:
            report = agents.undo_run(run, request.user, dry_run=agents.is_dry_run(request))
        except agents.UndoNotAllowed as exc:
            raise ApiError(403, str(exc)) from exc
        return json_response(report)
