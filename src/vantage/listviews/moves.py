"""Moving a Kanban card: an inline edit of one field, and nothing more.

:class:`KanbanMoveView` *is* :class:`~vantage.inline.InlineWriteView` with a different
allowlist. Everything that makes the inline write safe is inherited rather than
restated, so it cannot drift:

* the model-level ``change`` gate, which is also how read-only mode refuses it;
* :meth:`~vantage.inline.InlinePanelView.get_row` — scope first, so a card outside your
  rows answers 404, then the object-level ``change`` check, which answers 403;
* the ``ModelForm`` narrowed to the one field, whose ``_post_clean`` runs the model's
  ``full_clean`` — choice validation, validators and ``Model.clean()`` all apply;
* :func:`vantage.concurrency.claim`, so a card somebody else moved a second ago is a
  409 with both sides of the story rather than a silent overwrite;
* the panel's ``save_model`` hook, the tenancy guard, and the audit entry.

The allowlist is the only thing that differs. Where the grid allows ``list_editable``,
a board allows exactly its own field, and only when the change form would let you
write it — a field in ``readonly_fields`` cannot be moved, however the card was
dragged.

The endpoint also accepts an ordinary form post, which is what the "Move to…" menu
submits when JavaScript is unavailable. Same checks, same write; it redirects back to
the board with a message instead of answering JSON.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from vantage import concurrency
from vantage.inline import BadRequest, InlineWriteView, Rejected
from vantage.listviews.kanban import KanbanMode
from vantage.sensitive import safe_str

__all__ = ["KanbanMoveView"]


class KanbanMoveView(InlineWriteView):
    """``POST <app>/<model>/_kanban/move/`` — ``{"pk", "value", "version", "field"}``.

    ``field`` is the column the board was grouped by when it was drawn (its "Group by"
    picker). It is optional — absent means the board's default — and it is checked
    against the fields the board may be grouped by, so it can only ever name one of
    those, never an arbitrary column.
    """

    http_method_names = ["post"]

    #: The field the move asked for, set by :meth:`read_move` before the allowlist runs.
    _move_field: str | None = None

    def board(self) -> KanbanMode:
        # ``field=`` rather than the request: a move must write the column the card was
        # drawn in, not whatever ``?by=`` or the stored preference says now.
        return KanbanMode(self.panel, self.request, field=self._move_field or "")

    def allowed_fields(self, submitted: dict[str, Any]) -> dict[str, Any]:
        """The board's own field, if this panel offers a board and may write it."""
        mode = self.board()
        if mode.field is None:
            return {}
        if "kanban" not in self.panel.get_list_views(self.request):
            return {}
        if not mode.is_writable(self.request):
            return {}
        name = mode.field.name
        return {key: value for key, value in submitted.items() if key == name}

    def post(self, request, *args, **kwargs):
        as_form = not (request.content_type or "").startswith("application/json")
        try:
            payload = self.read_form() if as_form else self.parse_body()
            row = self.read_move(payload)
        except BadRequest as exc:
            if as_form:
                messages.error(request, str(exc))
                return self.back()
            body = {"ok": False, "error": "bad_request", "detail": str(exc)}
            return JsonResponse(body, status=400)

        if row is None:
            # Same silence as the inline allowlist: there is no legitimate client that
            # can move a card on a field it was never offered.
            if as_form:
                messages.error(request, _("That card cannot be moved."))
                return self.back()
            return JsonResponse({"ok": False, "error": "not_movable"}, status=400)

        try:
            saved = self.commit([row])
        except Rejected as exc:
            if as_form:
                messages.error(request, self.describe(exc))
                return self.back()
            return JsonResponse(exc.payload, status=exc.status)

        from vantage import approvals, audit

        # Outside the transaction, for the reason InlineWriteView.patch gives.
        for obj, form in saved:
            audit.log_form_save(request, obj, form, change=True)

        obj, form = saved[0]
        name = next(iter(row["fields"]))
        pending = getattr(form, "vantage_pending", None)
        if pending is not None:
            # The card stays where it was: the column it was dragged to is a request,
            # not the record's state. ``ok`` because nothing failed.
            approvals.announce(request, obj, pending)
            message = approvals.pending_message(self.panel, pending)
            if as_form:
                messages.warning(request, message)
                return self.back()
            return JsonResponse(
                {
                    "ok": True,
                    "pending": True,
                    "detail": message,
                    "pk": str(obj.pk),
                    "label": safe_str(obj),
                    "version": concurrency.token_for(obj) or "",
                    "value": concurrency._jsonify(getattr(obj, name)),
                }
            )
        if as_form:
            messages.success(request, _("Moved “%(record)s”.") % {"record": safe_str(obj)})
            return self.back()
        return JsonResponse(
            {
                "ok": True,
                "pk": str(obj.pk),
                "label": safe_str(obj),
                "version": concurrency.token_for(obj) or "",
                "value": concurrency._jsonify(getattr(obj, name)),
            }
        )

    # ── reading ─────────────────────────────────────────────────────────────

    def read_form(self) -> dict[str, Any]:
        """The no-JavaScript menu posts the target column as the JSON it was drawn with."""
        raw = self.request.POST.get("value", "")
        try:
            value = json.loads(raw)
        except ValueError:
            raise BadRequest(_("That is not a column on this board.")) from None
        return {
            "pk": self.request.POST.get("pk"),
            "value": value,
            "version": self.request.POST.get("version") or None,
            "field": self.request.POST.get("field") or None,
        }

    def read_move(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("pk") in (None, "") or "value" not in payload:
            raise BadRequest("A move needs a 'pk' and a 'value'.")
        field = payload.get("field")
        self._move_field = field if isinstance(field, str) and field else None
        name = self.board().field
        fields = self.allowed_fields({name.name: payload["value"]}) if name is not None else {}
        if not fields:
            return None
        version = payload.get("version")
        return {
            "pk": payload["pk"],
            "version": version if isinstance(version, str) and version else None,
            "fields": fields,
        }

    # ── answering a form post ───────────────────────────────────────────────

    def back(self):
        target = self.request.POST.get("next") or ""
        if not url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        ):
            from django.urls import reverse

            target = reverse(self.panel.list_url_name) + "?view=kanban"
        return HttpResponseRedirect(target)

    @staticmethod
    def describe(exc: Rejected) -> str:
        if exc.conflicts:
            return _("Somebody else changed that record a moment ago. Reload and try again.")
        messages_ = [
            message
            for error in exc.errors
            for field_messages in error["fields"].values()
            for message in field_messages
        ]
        return " ".join(messages_) or _("That move was refused.")
