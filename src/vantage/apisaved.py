"""Saved views over the JSON API, under the HTML feature's own rules.

Every decision is :mod:`vantage.savedviews`': which views a caller can see
(:func:`~vantage.savedviews.visible_views`), whether one may be overwritten
(:func:`~vantage.savedviews.can_edit`), and what a view is allowed to remember
(:func:`~vantage.savedviews.capture`, an allowlist). As on the HTML page the stored state
is never taken from the body verbatim: it is re-derived from a query string or a state
object through ``capture``, so a client cannot store a parameter the filter layer does
not own.

A view the caller cannot see is a 404, not a 403: a 403 would confirm that someone else's
private view with that id exists.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpResponse, QueryDict
from django.urls import NoReverseMatch, reverse

from vantage import savedviews
from vantage.api import ApiError, BadRequest, ModelApiView, _parse_json_body, json_response
from vantage.models import SavedView, SavedViewUserState

__all__ = ["SavedViewDetailView", "SavedViewListView", "view_payload"]

NAME_MAX = SavedView._meta.get_field("name").max_length
DESCRIPTION_MAX = SavedView._meta.get_field("description").max_length


def view_payload(view: SavedView, request, panel, *, default_ids: set[int]) -> dict[str, Any]:
    hydrated = savedviews.hydrate(panel, view.state)
    try:
        apply_url = reverse("vantage:saved-view-apply", args=[view.pk])
    except NoReverseMatch:  # pragma: no cover
        apply_url = None
    return {
        "id": view.pk,
        "name": view.name,
        "description": view.description,
        "model": view.model_label,
        "visibility": view.visibility,
        "edit_scope": view.edit_scope,
        "locked": view.locked,
        "version": view.version,
        "owner": view.owner.get_username() if view.owner_id else None,
        "is_owner": view.owner_id == request.user.pk,
        "can_edit": savedviews.can_edit(view, request),
        "is_default": view.pk in default_ids,
        "state": view.state,
        "query": hydrated.query,
        "broken": [
            {"parameter": b.parameter, "values": b.values, "reason": str(b.reason)}
            for b in hydrated.broken
        ],
        "apply_url": apply_url,
    }


class _SavedBase(ModelApiView):
    def required_action(self, request) -> str:
        # Saved views write no model row; the floor for all of them is "may view".
        return "view"

    def resolve(self, request, *args, **kwargs) -> None:
        super().resolve(request, *args, **kwargs)
        if not savedviews.enabled():
            raise ApiError(403, "Saved views are switched off.")

    def default_ids(self, request) -> set[int]:
        return set(
            SavedViewUserState.objects.filter(user=request.user, is_default=True).values_list(
                "view_id", flat=True
            )
        )

    def payload(self, request, view: SavedView) -> dict[str, Any]:
        return view_payload(view, request, self.panel, default_ids=self.default_ids(request))

    def params(self, body: dict[str, Any]) -> QueryDict | None:
        """The query a body names, or ``None`` when it names none."""
        if "query" in body:
            query = body["query"]
            if not isinstance(query, str):
                raise BadRequest("'query' must be a query string.")
            return QueryDict(query.lstrip("?"))
        if "state" in body:
            state = body["state"]
            if not isinstance(state, dict):
                raise BadRequest("'state' must be an object.")
            return QueryDict(savedviews.hydrate(self.panel, state).query)
        return None

    @staticmethod
    def choice(body: dict[str, Any], key: str, choices, fallback):
        if key not in body:
            return fallback
        valid = {value for value, _label in choices.choices}
        if body[key] not in valid:
            raise BadRequest(f"'{key}' must be one of: {', '.join(sorted(valid))}.")
        return body[key]

    @staticmethod
    def clean_text(body: dict[str, Any], key: str, limit: int) -> str | None:
        if key not in body:
            return None
        value = body[key]
        if not isinstance(value, str):
            raise BadRequest(f"'{key}' must be a string.")
        value = value.strip()
        if len(value) > limit:
            raise BadRequest(f"'{key}' is limited to {limit} characters.")
        return value


class SavedViewListView(_SavedBase):
    """``GET`` the views this caller can open. ``POST`` to save one (Save as)."""

    def get(self, request, *args, **kwargs):
        views = savedviews.visible_views(request, self.panel).select_related("owner")
        defaults = self.default_ids(request)
        return json_response(
            {"views": [view_payload(v, request, self.panel, default_ids=defaults) for v in views]}
        )

    def post(self, request, *args, **kwargs):
        body = _parse_json_body(request)
        name = self.clean_text(body, "name", NAME_MAX)
        if not name:
            raise BadRequest("A saved view needs a name.")
        params = self.params(body) or QueryDict("")
        visibility = self.choice(body, "visibility", SavedView.Visibility, "private")
        edit_scope = self.choice(body, "edit_scope", SavedView.EditScope, "owner")
        description = self.clean_text(body, "description", DESCRIPTION_MAX) or ""

        state = savedviews.capture(self.panel, params)
        view, created = SavedView.objects.get_or_create(
            owner=request.user,
            model_label=self.panel.model._meta.label_lower,
            name=name,
            defaults={
                "state": state,
                "visibility": visibility,
                "edit_scope": edit_scope,
                "description": description,
                "updated_by": request.user,
            },
        )
        if not created:
            # As on the HTML page: the unique constraint is per owner, so this is the
            # caller's own view, and replacing it is what saving under its name means.
            if view.locked:
                raise ApiError(403, "That view is locked.")
            savedviews.write_state(view, request, state, None)
        return json_response(self.payload(request, view), status=201 if created else 200)


class SavedViewDetailView(_SavedBase):
    """``GET`` / ``PATCH`` / ``DELETE`` one view."""

    def get_view(self, request) -> SavedView:
        view = (
            savedviews.visible_views(request, self.panel)
            .select_related("owner")
            .filter(pk=self.kwargs["view_pk"])
            .first()
        )
        if view is None:
            raise ApiError(404, "No such saved view.")
        return view

    def get(self, request, *args, **kwargs):
        return json_response(self.payload(request, self.get_view(request)))

    def patch(self, request, *args, **kwargs):
        view = self.get_view(request)
        body = _parse_json_body(request)
        is_owner = view.owner_id == request.user.pk
        params = self.params(body)

        metadata = {"name", "description", "visibility", "edit_scope", "locked"} & set(body)
        if metadata and not is_owner:
            raise ApiError(403, "Only the owner can change a saved view's settings.")
        # A locked view freezes its configuration - the owner's too - so the one thing the
        # owner may still do is unlock it.
        frozen = view.locked and (params is not None or (metadata - {"locked"}))
        if frozen:
            raise ApiError(403, "That view is locked.")
        if params is not None and not savedviews.can_edit(view, request):
            raise ApiError(403, "You cannot change that view. Save a copy instead.")

        try:
            with transaction.atomic():
                changed = []
                name = self.clean_text(body, "name", NAME_MAX)
                if name is not None:
                    if not name:
                        raise BadRequest("A saved view needs a name.")
                    view.name = name
                    changed.append("name")
                description = self.clean_text(body, "description", DESCRIPTION_MAX)
                if description is not None:
                    view.description = description
                    changed.append("description")
                if "visibility" in body:
                    view.visibility = self.choice(body, "visibility", SavedView.Visibility, None)
                    changed.append("visibility")
                if "edit_scope" in body:
                    view.edit_scope = self.choice(body, "edit_scope", SavedView.EditScope, None)
                    changed.append("edit_scope")
                if "locked" in body:
                    view.locked = bool(body["locked"])
                    changed.append("locked")
                if changed:
                    view.updated_by = request.user
                    view.save(update_fields=[*changed, "updated_by", "updated_at"])
                if params is not None:
                    expected = body.get("version")
                    if view.is_shared and not isinstance(expected, int):
                        raise BadRequest("Updating a shared view requires its current 'version'.")
                    savedviews.write_state(
                        view,
                        request,
                        savedviews.capture(self.panel, params),
                        expected if view.is_shared else None,
                    )
        except savedviews.VersionConflict as exc:
            raise ApiError(
                409, str(exc), version=SavedView.objects.get(pk=view.pk).version
            ) from exc
        except IntegrityError as exc:
            raise ApiError(409, "You already have a saved view with that name.") from exc

        if "default" in body:
            if body["default"]:
                savedviews.set_default(request, view)
            else:
                SavedViewUserState.objects.filter(view=view, user=request.user).update(
                    is_default=False
                )
        view.refresh_from_db()
        return json_response(self.payload(request, view))

    def delete(self, request, *args, **kwargs):
        view = self.get_view(request)
        if not (view.owner_id == request.user.pk or savedviews.can_edit(view, request)):
            raise ApiError(403, "You cannot delete that view.")
        view.delete()
        return HttpResponse(status=204)
