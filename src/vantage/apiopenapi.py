"""An OpenAPI 3.1 description of the JSON API, generated from the registry.

Nothing is written by hand per model: each model's schema comes from
:func:`vantage.api.model_schema` (itself built on :func:`vantage.serialize.field_schema`),
the same function that answers ``/_/api/schema/``. So the document, the schema endpoint
and the endpoints themselves cannot disagree about which fields exist or which are
writable.

**Per caller.** :func:`build_document` takes the request it describes the API *for*. A
model the caller cannot view is absent entirely - no path, no schema, no tag - and a
write operation appears only if the caller holds the matching permission, so the
document never reveals that a model exists, nor what it looks like, to someone who could
not already open it. The management command without ``--user`` builds it for a synthetic
superuser, i.e. the full structure; treat that output as sensitive.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.urls import NoReverseMatch, reverse

import vantage
from vantage.api import ApiView, json_response, model_schema
from vantage.permissions import get_permissions

__all__ = ["OpenAPIView", "build_document", "synthetic_superuser_request"]

#: ``FieldKind`` value to a JSON Schema fragment for the *value* a client reads or sends.
_KIND_SCHEMAS: dict[str, dict[str, Any]] = {
    "text": {"type": "string"},
    "longtext": {"type": "string"},
    "richtext": {"type": "string"},
    "number": {"type": ["integer", "null"]},
    "decimal": {"type": "string", "description": "A decimal, sent as a string."},
    "boolean": {"type": "boolean"},
    "choice": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "time": {"type": "string", "format": "time"},
    "duration": {"type": "string", "description": "An ISO 8601 duration."},
    "email": {"type": "string", "format": "email"},
    "url": {"type": "string", "format": "uri"},
    "slug": {"type": "string"},
    "json": {},
    "file": {"type": "string", "format": "binary", "description": "Upload as multipart."},
    "image": {"type": "string", "format": "binary", "description": "Upload as multipart."},
    "color": {"type": "string"},
    "relation_one": {"description": "The related record's primary key."},
    "relation_many": {"type": "array", "items": {}, "description": "Related primary keys."},
    "uuid": {"type": "string", "format": "uuid"},
    "ip": {"type": "string"},
    "binary": {"type": "string"},
    "unknown": {},
}

_ERROR_STATUSES = {
    "401": "Authentication required.",
    "403": "Forbidden.",
    "404": "Not found.",
}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _value_schema(kind: str, *, nullable: bool = True) -> dict[str, Any]:
    schema = dict(_KIND_SCHEMAS.get(kind, {}))
    if nullable and "type" in schema and isinstance(schema["type"], str):
        schema["type"] = [schema["type"], "null"]
    return schema


def synthetic_superuser_request():
    """A request standing for "everyone and everything", for the unfiltered document."""
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = get_user_model()(username="openapi", is_active=True, is_staff=True)
    request.user.is_superuser = True
    return request


def _common_components() -> dict[str, Any]:
    error = {
        "type": "object",
        "required": ["detail"],
        "properties": {"detail": {"type": "string"}, "errors": {"type": "object"}},
        "additionalProperties": True,
    }
    return {
        "Error": error,
        "Action": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "label": {"type": "string"},
                "icon": {"type": ["string", "null"]},
                "permission": {"type": ["string", "null"]},
                "destructive": {"type": "boolean"},
                "requires_confirmation": {"type": "boolean"},
                "max_rows": {"type": ["integer", "null"]},
                "bulk_strategy": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
            },
        },
        "ActionRequest": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {}},
                "select_all": {"type": "boolean"},
                "excluded": {"type": "array", "items": {}},
                "confirmed": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
        },
        "ActionResult": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "message": {"type": "string"},
                "level": {"type": "string"},
                "affected": {"type": "integer"},
                "scope": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "count": {"type": "integer"},
                "job": {"type": "object"},
            },
        },
        "SavedView": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "model": {"type": "string"},
                "visibility": {"type": "string", "enum": ["private", "shared"]},
                "edit_scope": {"type": "string", "enum": ["owner", "editors", "everyone"]},
                "locked": {"type": "boolean"},
                "version": {"type": "integer"},
                "owner": {"type": ["string", "null"]},
                "is_owner": {"type": "boolean"},
                "can_edit": {"type": "boolean"},
                "is_default": {"type": "boolean"},
                "state": {"type": "object"},
                "query": {"type": "string"},
                "broken": {"type": "array", "items": {"type": "object"}},
                "apply_url": {"type": ["string", "null"]},
            },
        },
        "SavedViewWrite": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "query": {"type": "string"},
                "state": {"type": "object"},
                "visibility": {"type": "string", "enum": ["private", "shared"]},
                "edit_scope": {"type": "string", "enum": ["owner", "editors", "everyone"]},
                "locked": {"type": "boolean"},
                "default": {"type": "boolean"},
                "version": {"type": "integer"},
            },
        },
        "ImportRequest": {
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "format": "binary"},
                "mode": {"type": "string", "enum": ["create", "update", "upsert"]},
                "key_field": {"type": "string"},
                "mapping": {"type": "string", "description": "JSON object: header to field."},
                "dry_run": {"type": "boolean", "default": True},
            },
        },
        "ImportResult": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "summary": {"type": "string"},
                "rows": {"type": "integer"},
                "created": {"type": "integer"},
                "updated": {"type": "integer"},
                "unchanged": {"type": "integer"},
                "failed": {"type": "integer"},
                "queued_for_approval": {"type": "integer"},
                "errors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "row": {"type": "integer"},
                            "column": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                },
                "errors_truncated": {"type": "boolean"},
                "diffs": {"type": "array", "items": {"type": "object"}},
                "mapping": {"type": "object"},
            },
        },
        "FieldValue": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "kind": {"type": "string"},
                "value": {},
                "display": {"type": "string"},
                "masked": {"type": "boolean"},
            },
        },
    }


def _model_components(label: str, schema: dict[str, Any]) -> dict[str, Any]:
    """``Record``, ``Write`` and ``List`` schemas for one model."""
    read_props: dict[str, Any] = {}
    write_props: dict[str, Any] = {}
    required: list[str] = []
    for field in schema["fields"]:
        kind = field["kind"]
        read_props[field["name"]] = {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "kind": {"type": "string", "const": kind},
                "value": _value_schema(kind),
                "display": {"type": "string"},
                "masked": {"type": "boolean"},
            },
        }
        if field["readonly"]:
            continue
        prop = _value_schema(kind)
        choices = (field.get("widget") or {}).get("choices")
        if choices and all("value" in c for c in choices):
            prop = {**prop, "enum": [c["value"] for c in choices]}
        if field.get("help_text"):
            prop["description"] = field["help_text"]
        write_props[field["name"]] = prop
        if field["required"]:
            required.append(field["name"])

    write: dict[str, Any] = {"type": "object", "properties": write_props}
    if required:
        write["required"] = required
    return {
        f"{label}.Fields": {"type": "object", "properties": read_props},
        f"{label}.Write": write,
        f"{label}.Record": {
            "type": "object",
            "properties": {
                "pk": {},
                "display": {"type": "string"},
                "version": {"type": ["string", "null"]},
                "url": {"type": ["string", "null"]},
                "html_url": {"type": ["string", "null"]},
                "fields": _ref(f"{label}.Fields"),
            },
        },
        f"{label}.Envelope": {
            "type": "object",
            "required": ["fields"],
            "properties": {
                "fields": _ref(f"{label}.Write"),
                "version": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        f"{label}.List": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pk": {},
                            "display": {"type": "string"},
                            "url": {"type": ["string", "null"]},
                            "html_url": {"type": ["string", "null"]},
                            "version": {"type": ["string", "null"]},
                            "cells": {"type": "object"},
                        },
                    },
                },
                "next": {"type": ["string", "null"]},
                "previous": {"type": ["string", "null"]},
                "query": {"type": "string"},
                "sort": {"type": ["object", "null"]},
                "filters": {"type": "array", "items": {"type": "object"}},
            },
        },
    }


def _responses(success: dict[str, Any], *extra: str) -> dict[str, Any]:
    responses = dict(success)
    for code in ("401", "403", *extra):
        responses[code] = {
            "description": _ERROR_STATUSES.get(code, "Error."),
            "content": {"application/json": {"schema": _ref("Error")}},
        }
    return responses


def _json(schema: dict[str, Any], description: str = "OK") -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _model_paths(request, panel, label: str, allowed: set[str], base: str) -> dict[str, Any]:
    """Every path item for one model, limited to the verbs ``allowed`` grants."""
    tag = [panel.opts.label]
    ident = label.replace(".", "_")
    filters_note = (
        "Accepts the list page's own filter, search (q) and sort parameters, so a "
        "selection by filter matches what the list shows."
    )
    pk_param = {
        "name": "pk",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }
    body_json = {
        "required": True,
        "content": {"application/json": {"schema": _ref(f"{label}.Envelope")}},
    }
    body_multipart = {
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fields": {"type": "string", "description": "JSON object of fields."},
                        "version": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "additionalProperties": {"type": "string", "format": "binary"},
                }
            }
        }
    }
    request_body = {**body_json, "content": {**body_json["content"], **body_multipart["content"]}}
    record = _ref(f"{label}.Record")

    list_item: dict[str, Any] = {
        "get": {
            "operationId": f"list_{ident}",
            "tags": tag,
            "summary": f"List {panel.get_label_plural()}",
            "description": "Cursor-paginated. " + filters_note,
            "parameters": [
                {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                {"name": "page_size", "in": "query", "schema": {"type": "integer"}},
                {"name": "q", "in": "query", "schema": {"type": "string"}},
                {"name": "sort", "in": "query", "schema": {"type": "string"}},
                {"name": "fields", "in": "query", "schema": {"type": "string"}},
            ],
            "responses": _responses({"200": _json(_ref(f"{label}.List"))}, "400"),
        }
    }
    if "add" in allowed:
        list_item["post"] = {
            "operationId": f"create_{ident}",
            "tags": tag,
            "summary": f"Create a {panel.get_label()}",
            "requestBody": request_body,
            "security": [{"cookieAuth": [], "csrfToken": []}],
            "responses": _responses({"201": _json(record, "Created")}, "400", "422"),
        }

    detail: dict[str, Any] = {
        "get": {
            "operationId": f"retrieve_{ident}",
            "tags": tag,
            "parameters": [pk_param],
            "responses": _responses({"200": _json(record)}, "404"),
        }
    }
    if "change" in allowed:
        for verb in ("put", "patch"):
            detail[verb] = {
                "operationId": f"{'update' if verb == 'put' else 'partial_update'}_{ident}",
                "tags": tag,
                "parameters": [pk_param],
                "requestBody": request_body,
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses(
                    {
                        "200": _json(record),
                        "202": _json(record, "Saved; some fields await approval."),
                    },
                    "400",
                    "404",
                    "409",
                    "422",
                ),
            }
    if "delete" in allowed:
        detail["delete"] = {
            "operationId": f"delete_{ident}",
            "tags": tag,
            "parameters": [pk_param],
            "security": [{"cookieAuth": [], "csrfToken": []}],
            "responses": _responses({"204": {"description": "Deleted."}}, "404", "409"),
        }

    action_param = {
        "name": "action_name",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }
    paths: dict[str, Any] = {
        base: list_item,
        f"{base}{{pk}}/": detail,
        f"{base}_actions/": {
            "get": {
                "operationId": f"list_actions_{ident}",
                "tags": tag,
                "summary": "Bulk actions this caller may run.",
                "responses": _responses(
                    {
                        "200": _json(
                            {
                                "type": "object",
                                "properties": {
                                    "actions": {"type": "array", "items": _ref("Action")},
                                    "run_url": {"type": ["string", "null"]},
                                },
                            }
                        )
                    }
                ),
            }
        },
        f"{base}_actions/{{action_name}}/": {
            "post": {
                "operationId": f"run_action_{ident}",
                "tags": tag,
                "summary": "Run a bulk action, or preview its count with dry_run.",
                "description": filters_note,
                "parameters": [action_param],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _ref("ActionRequest")}},
                },
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses(
                    {
                        "200": _json(_ref("ActionResult")),
                        "202": _json(_ref("ActionResult"), "Running as a background job."),
                    },
                    "400",
                    "409",
                    "422",
                    "428",
                ),
            }
        },
        f"{base}_views/": {
            "get": {
                "operationId": f"list_views_{ident}",
                "tags": tag,
                "responses": _responses(
                    {
                        "200": _json(
                            {
                                "type": "object",
                                "properties": {
                                    "views": {"type": "array", "items": _ref("SavedView")}
                                },
                            }
                        )
                    }
                ),
            },
            "post": {
                "operationId": f"create_view_{ident}",
                "tags": tag,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _ref("SavedViewWrite")}},
                },
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses(
                    {
                        "200": _json(_ref("SavedView"), "Replaced a view of the same name."),
                        "201": _json(_ref("SavedView"), "Created."),
                    },
                    "400",
                ),
            },
        },
        f"{base}_views/{{view_pk}}/": {
            "get": {
                "operationId": f"retrieve_view_{ident}",
                "tags": tag,
                "parameters": [_view_param()],
                "responses": _responses({"200": _json(_ref("SavedView"))}, "404"),
            },
            "patch": {
                "operationId": f"update_view_{ident}",
                "tags": tag,
                "parameters": [_view_param()],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _ref("SavedViewWrite")}},
                },
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses({"200": _json(_ref("SavedView"))}, "400", "404", "409"),
            },
            "delete": {
                "operationId": f"delete_view_{ident}",
                "tags": tag,
                "parameters": [_view_param()],
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses({"204": {"description": "Deleted."}}, "404"),
            },
        },
        f"{base}_export/": {
            "get": {
                "operationId": f"export_{ident}",
                "tags": tag,
                "description": "Streams the filtered list. " + filters_note,
                "parameters": [
                    {
                        "name": "format",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["csv", "tsv", "json"]},
                    }
                ],
                "responses": _responses(
                    {
                        "200": {
                            "description": "The file.",
                            "content": {
                                "text/csv": {"schema": {"type": "string"}},
                                "text/tab-separated-values": {"schema": {"type": "string"}},
                                "application/x-ndjson": {"schema": {"type": "string"}},
                            },
                        }
                    },
                    "400",
                ),
            }
        },
    }
    if "add" in allowed:
        paths[f"{base}_import/"] = {
            "post": {
                "operationId": f"import_{ident}",
                "tags": tag,
                "summary": "Import a CSV; a dry run unless dry_run=false.",
                "requestBody": {
                    "required": True,
                    "content": {"multipart/form-data": {"schema": _ref("ImportRequest")}},
                },
                "security": [{"cookieAuth": [], "csrfToken": []}],
                "responses": _responses({"200": _json(_ref("ImportResult"))}, "400", "413", "422"),
            }
        }
    return paths


def _view_param() -> dict[str, Any]:
    return {"name": "view_pk", "in": "path", "required": True, "schema": {"type": "integer"}}


def build_document(site, request) -> dict[str, Any]:
    """The OpenAPI 3.1 document describing the API as ``request.user`` may use it."""
    permissions = get_permissions()
    schemas = _common_components()
    paths: dict[str, Any] = {}
    tags: list[dict[str, str]] = []

    for panel in permissions.visible_panels(request, list(site.panels())):
        label = panel.opts.label_lower
        try:
            base = reverse(
                "vantage:api-list",
                kwargs={"app_label": panel.opts.app_label, "model_name": panel.opts.model_name},
            )
        except NoReverseMatch:  # pragma: no cover - only when the API is not mounted
            continue
        schema = model_schema(request, panel, permissions)
        schemas.update(_model_components(label, schema))
        allowed = permissions.allowed_actions(request, panel.model)
        paths.update(_model_paths(request, panel, label, allowed, base))
        tags.append({"name": str(panel.opts.label)})

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Vantage JSON API",
            "version": getattr(vantage, "__version__", "0"),
            "description": (
                "Session-authenticated API over the registered panels. Unsafe requests "
                "must send the CSRF token in the X-CSRFToken header."
            ),
        },
        "tags": tags,
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": settings.SESSION_COOKIE_NAME,
                },
                "csrfToken": {"type": "apiKey", "in": "header", "name": "X-CSRFToken"},
            },
        },
        "security": [{"cookieAuth": []}],
    }


class OpenAPIView(ApiView):
    """``GET /_/api/openapi.json`` - staff only, filtered to what the caller can view."""

    def get(self, request, *args, **kwargs):
        return json_response(build_document(self.site, request))
