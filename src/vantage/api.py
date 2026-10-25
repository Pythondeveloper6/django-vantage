"""A read/write JSON API over every registered panel.

The promise is the one in the module docstring of :mod:`vantage.serialize`: the HTML
page and the JSON endpoint describe the same record by calling the same functions,
rather than by two implementations agreeing to. Nothing here re-decides who may see or
change a row, which columns a list page shows, how a page is filtered or sorted, or how
a value is rendered — those questions already have one answer each, in
:mod:`vantage.permissions`, :mod:`vantage.listquery`, :mod:`vantage.filters`,
:mod:`vantage.pagination` and :mod:`vantage.serialize`, and this module is a thin HTTP
skin over all five. A second implementation of any of them would be a second set of
bugs, and for permissions specifically it would be a security hole.

**Reachable models.** A route only exists for a model :func:`vantage.sites.site.register`
has actually registered — see :func:`build_urls`. The one endpoint that still takes a
model name as *input* rather than as part of the URL, :class:`SchemaView`, resolves it
the same way :class:`vantage.autocomplete.AutocompleteView` does: through the registry,
never through ``django.apps.apps.get_model`` alone. That is what stops a client naming
``auth.Permission`` or a model from an unrelated app.

**Authentication.** Session, exactly like the panel itself — the same cookie, the same
``request.user``, no separate login. A script authenticates by POSTing credentials to
the panel's own login view and keeping the session cookie, precisely as a browser does;
see ``docs/api.md`` for the two-line recipe. CSRF is never exempted: every unsafe verb
is protected the same way an HTML form post is, by ``CsrfViewMiddleware``, and a script
carries the token in ``X-CSRFToken`` because a JSON body has no hidden input to hide it
in.

**Errors are data.** Every refusal in this module is a caught exception turned into a
:class:`django.http.JsonResponse` — never an uncaught one left for Django's own
exception middleware, which would hand a JSON client an HTML page it cannot parse. The
shapes and status codes are documented in ``docs/api.md``; the short version is
``401`` unauthenticated, ``403`` forbidden, ``404`` not found, ``422`` your data does
not validate, ``409`` somebody already changed the row (or it cannot be deleted without
taking other rows with it).

**Pagination is cursor-only.** ``LIMIT 20 OFFSET 100000`` makes the database produce
and discard a hundred thousand rows to hand back twenty; :mod:`vantage.pagination`
built :class:`~vantage.pagination.KeysetPaginator` for exactly this reason, and it had
no caller until this module. There is no page-number mode here to fall back to — a
sort this endpoint cannot express as a keyset window is refused with ``400`` rather
than silently paged by offset, which would reintroduce the cost this whole design
exists to avoid.

**Query cost does not grow with row count.** The list endpoint derives
``select_related``/``prefetch_related`` from the columns actually requested — the
panel's own inferred list joins, plus :func:`vantage.optimizer.plan_related` over
whatever ``?fields=`` asked for beyond them — so asking for more columns costs more
joins and nothing else scales with the size of the table.
"""

from __future__ import annotations

import json
from typing import Any

from django.apps import apps as django_apps
from django.core.exceptions import PermissionDenied
from django.db import router, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.urls import NoReverseMatch, path, reverse
from django.views import View

from vantage import concurrency, formviews, listquery
from vantage.inline import form_errors, json_to_form_data
from vantage.optimizer import plan_related
from vantage.pagination import (
    InvalidCursor,
    KeysetPaginator,
    KeysetUnsupported,
    with_total_ordering,
)
from vantage.permissions import get_permissions
from vantage.serialize import (
    field_schema,
    filter_payload,
    json_value,
    object_fields,
    record_fields,
    row,
)
from vantage.widgets import build_form_class

__all__ = [
    "ApiError",
    "ListCreateView",
    "RecordDetailView",
    "SchemaView",
    "build_urls",
    "csrf_failure",
]

#: The model-level permission each HTTP verb answers to. Matches the four actions
#: ``vantage.permissions.ACTIONS`` defines; there is no fifth for "list" versus
#: "retrieve" because the queryset-level scoping — not the model-level gate — is what
#: tells those two apart, exactly as it does for the HTML list and detail pages.
ACTION_FOR_METHOD: dict[str, str] = {
    "GET": "view",
    "HEAD": "view",
    "POST": "add",
    "PUT": "change",
    "PATCH": "change",
    "DELETE": "delete",
}

#: Bounds on ``?page_size=``. The floor keeps a client from turning a table into a
#: million single-row requests; the ceiling is the same reasoning the autocomplete
#: endpoint's ``PAGE_SIZE`` cap uses — a page is for paging, not for a client that
#: would rather not.
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 200


# ── errors as data ──────────────────────────────────────────────────────────


def json_response(payload: Any, *, status: int = 200) -> JsonResponse:
    """Every success response goes through here, for the one header they all need.

    ``no-store`` because every payload is permission-scoped to the caller — the same
    reasoning :class:`vantage.autocomplete.AutocompleteView` and
    :class:`vantage.search.SearchJSONView` already apply to their own responses. A
    shared cache handing one user's rows to the next would undo every check above it.
    """
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def error_response(status: int, detail: str, **extra: Any) -> JsonResponse:
    """``{"detail": "..."}``, plus whatever the caller needs to say more precisely.

    The one shape every error in this module takes. ``detail`` is always a sentence a
    person could read; ``extra`` carries the structured part — ``errors`` for a
    validation failure, ``conflict`` for a stale write — documented field by field in
    ``docs/api.md``.
    """
    return json_response({"detail": detail, **extra}, status=status)


class ApiError(Exception):
    """A refusal with a status code attached, caught by :class:`ApiView.dispatch`.

    Raised from anywhere below a view's ``dispatch`` — resolving the model, checking a
    permission, parsing the body — so that every one of those call sites reports itself
    the same way rather than each inventing its own ``JsonResponse``.
    """

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra

    def response(self) -> JsonResponse:
        return error_response(self.status, self.detail, **self.extra)


class BadRequest(ApiError):
    """A malformed request body. Always a 400 — this is about shape, not content."""

    def __init__(self, detail: str) -> None:
        super().__init__(400, detail)


def csrf_failure(request, reason: str = "") -> JsonResponse:
    """A JSON-shaped CSRF rejection, for a project that points ``CSRF_FAILURE_VIEW`` here.

    ``CsrfViewMiddleware`` rejects a forged or missing token from ``process_view``,
    entirely before any view — this one included — is called, so there is no
    ``dispatch()`` here to catch it. Django's own ``django.views.csrf.csrf_failure``
    renders an HTML page there instead, which is exactly the thing this module's
    docstring says a JSON client cannot read. Point the *whole project's*
    ``CSRF_FAILURE_VIEW`` at this to make that one failure JSON too; see
    ``docs/api.md`` for the trade-off of doing that project-wide.
    """
    if reason:
        detail = f"CSRF verification failed: {reason}"
    else:
        detail = (
            "CSRF verification failed. Send the token from the 'csrftoken' cookie "
            "back in an 'X-CSRFToken' header."
        )
    return error_response(403, detail)


def _with_field_defaults(
    form_class, editable: list[str], submitted: dict[str, Any]
) -> dict[str, Any]:
    """Fill in a create payload's gaps with each field's own model default.

    An HTML create page can never actually omit a required field with a default: the
    widget renders pre-filled with it — ``Field.formfield()`` sets exactly that as the
    form field's ``initial`` — and an untouched control still submits that value. A
    JSON client that reasonably leaves out a field it does not care about would
    otherwise see "This field is required" for a field the HTML form never asks about.
    Only fills what the client left out; anything submitted, including an explicit
    ``null``, is never overridden by this.
    """
    filled = dict(submitted)
    for name in editable:
        if name in filled:
            continue
        field = form_class.base_fields.get(name)
        if field is None:
            continue
        initial = field.initial() if callable(field.initial) else field.initial
        if initial is not None:
            filled[name] = json_value(initial)
    return filled


def _parse_json_body(request) -> dict[str, Any]:
    """The request body as a JSON object, or a :class:`BadRequest`."""
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BadRequest("The request body was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise BadRequest("The request body must be a JSON object.")
    return payload


# ── the registry guard ──────────────────────────────────────────────────────


def _resolve_model(site, app_label: str, model_name: str):
    """``("shop", "Product")`` to the model, refusing anything not registered.

    Restated from :meth:`vantage.autocomplete.AutocompleteView._resolve_model` rather
    than imported, because the two answer slightly different questions: that endpoint
    takes a model as a *query parameter* on one shared URL and so must guard against a
    client naming any model at all, on every call. Every route this module builds
    already names its model in the *path*, at mount time, from ``site.panels()`` — see
    :func:`build_urls` — so ``SchemaView`` aside, the guard below is never the only
    thing standing between a request and an unregistered model; it is defence in depth
    for the one endpoint that still resolves a model from caller input.

    404 rather than the autocomplete endpoint's 403: a bad model name here is a URL
    that does not name a resource, which is what 404 means, and there is no shared
    endpoint here whose 403 has to keep meaning "wrong model" specifically.
    """
    try:
        model = django_apps.get_model(app_label, model_name)
    except (LookupError, ValueError, TypeError):
        model = None
    if model is None or not site.is_registered(model):
        raise ApiError(404, "No such model.")
    return model


# ── URLs for one record / one list ──────────────────────────────────────────


def _list_url(site, panel) -> str | None:
    try:
        return reverse(
            "vantage:api-list",
            kwargs={"app_label": panel.opts.app_label, "model_name": panel.opts.model_name},
        )
    except NoReverseMatch:  # pragma: no cover - only when the API is not mounted
        return None


def _detail_url(panel, obj) -> str | None:
    try:
        return reverse(
            "vantage:api-detail",
            kwargs={
                "app_label": panel.opts.app_label,
                "model_name": panel.opts.model_name,
                "pk": obj.pk,
            },
        )
    except NoReverseMatch:  # pragma: no cover - only when the API is not mounted
        return None


def _html_detail_url(panel, obj) -> str | None:
    try:
        return reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
    except NoReverseMatch:  # pragma: no cover - only when the HTML detail page is not mounted
        return None


def _record_payload(panel, obj) -> dict[str, Any]:
    """One record, fully — the shape both retrieve and every write echo back."""
    return {
        "pk": json_value(obj.pk),
        "display": str(obj),
        "version": concurrency.token_for(obj),
        "url": _detail_url(panel, obj),
        "html_url": _html_detail_url(panel, obj),
        "fields": object_fields(panel, obj),
    }


# ── the base views ──────────────────────────────────────────────────────────


class ApiView(View):
    """Base for every endpoint in this module: authenticate, dispatch, catch.

    Deliberately does not build on :class:`vantage.views.SiteView`. That base redirects
    an anonymous caller to the HTML login page and lets ``PermissionDenied`` fall
    through to Django's own exception handling — both entirely correct for a page a
    browser is about to render, and both wrong here: a script gets a ``302`` to a login
    *page* it cannot follow usefully, or an HTML ``403``. This class answers both
    itself, in JSON, and — the reason ``dispatch`` is overridden rather than
    ``get``/``post`` wrapped individually — it is the *one* place every handler's
    exceptions are caught, so no handler below has to remember to.
    """

    site = None

    def dispatch(self, request, *args, **kwargs):
        try:
            self.authenticate(request)
            self.resolve(request, *args, **kwargs)
            method = request.method.lower()
            handler = getattr(self, method, None) if method in self.http_method_names else None
            if handler is None:
                return self.http_method_not_allowed(request, *args, **kwargs)
            return handler(request, *args, **kwargs)
        except ApiError as exc:
            return exc.response()
        except Http404 as exc:
            return error_response(404, str(exc) or "Not found.")
        except PermissionDenied as exc:
            # Catches vantage.tenancy.TenantRequired/TenantMismatch too - both are
            # PermissionDenied subclasses, and both belong on the same wire shape as
            # every other refusal a write can meet.
            return error_response(403, str(exc) or "You do not have permission to do that.")
        except (ValueError, TypeError):
            # get_object_or_404 - which vantage.formviews.get_record uses - only ever
            # catches the model's own DoesNotExist; a primary key of the wrong *shape*
            # (a letter in an integer pk, for instance) raises ValueError/TypeError
            # straight out of the database layer instead, uncaught, on the HTML pages
            # too. There it becomes Django's own 500 page; here it must not become
            # anything but the answer a bad identifier deserves: this row is not found.
            return error_response(404, "Not found.")

    def authenticate(self, request) -> None:
        """Session auth, matching the panel: signed in, and staff.

        The same bar :class:`vantage.views.SiteView.dispatch` sets for every HTML page
        in the panel — reached by requiring ``is_staff`` here rather than by importing
        that class, because the *response* on failure has to differ (JSON here, a
        login redirect there) even though the *rule* is identical.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            raise ApiError(401, "Authentication required.")
        if not user.is_staff:
            raise ApiError(403, "Staff access is required.")
        # Primes the CSRF cookie on every authenticated response, GET included, so a
        # script that only ever talks to this API (and never renders the login page's
        # own <form>) still has something to send back in X-CSRFToken on its first
        # write. Cheap: get_token() is idempotent once the cookie already matches.
        get_token(request)

    def resolve(self, request, *args, **kwargs) -> None:
        """Hook: a subclass resolves whatever its URL named — a model, nothing, ..."""

    def http_method_not_allowed(self, request, *args, **kwargs) -> JsonResponse:
        response = error_response(405, "Method not allowed.")
        response["Allow"] = ", ".join(self._allowed_methods())
        return response


def _model_denied_message(panel, action: str) -> str:
    """Why this verb was refused at the model level, in the panel's own words.

    ``vantage.views.PanelView.DENIED_MESSAGES`` is the sentence the HTML page shows for
    the same refusal; read from there rather than restated; so the two can never drift
    apart.
    """
    from vantage.views import PanelView

    records = panel.get_label_plural().lower()
    message = PanelView.DENIED_MESSAGES.get(action)
    if message is None:  # pragma: no cover - every action in ACTION_FOR_METHOD has one
        return f"You do not have permission to {action} {records}."
    return str(message) % {"records": records}


class ModelApiView(ApiView):
    """Base for the endpoints that act on one registered model.

    Resolves the model and the model-level permission for the verb once, in
    :meth:`resolve` — which :class:`ApiView.dispatch` already runs inside its
    try/except, so a refusal here reports itself exactly like any other.
    """

    panel = None
    model = None
    permissions = None

    def resolve(self, request, *args, **kwargs) -> None:
        self.model = _resolve_model(
            self.site, self.kwargs.get("app_label", ""), self.kwargs.get("model_name", "")
        )
        self.panel = self.site.get_panel(self.model)
        self.permissions = get_permissions()

        action = ACTION_FOR_METHOD.get(request.method.upper(), "view")
        if not self.permissions.has_model_perm(request, self.model, action):
            raise ApiError(403, _model_denied_message(self.panel, action))


# ── list + create ────────────────────────────────────────────────────────────


class ListCreateView(ModelApiView):
    """``GET`` the filtered, searched, sorted, cursor-paged list. ``POST`` a new row."""

    def get(self, request, *args, **kwargs):
        return self._list(request)

    def post(self, request, *args, **kwargs):
        return self._create(request)

    # ── list ────────────────────────────────────────────────────────────────

    def _list(self, request):
        resolved = listquery.build(self.panel, request, self.permissions, allow_tree=False)
        queryset = resolved.queryset

        columns = self._requested_fields(request)
        select, prefetch = plan_related(self.panel.model, columns)
        if select:
            queryset = queryset.select_related(*sorted(select))
        if prefetch:
            queryset = queryset.prefetch_related(*sorted(prefetch))

        queryset = with_total_ordering(queryset)
        try:
            paginator = KeysetPaginator(queryset, self._page_size(request))
        except KeysetUnsupported as exc:
            raise ApiError(
                400,
                "This list cannot be paged by cursor with its current sort: "
                f"{exc} Choose a plain, non-relation ?sort= field, or drop it to use "
                "the model's default ordering.",
            ) from exc

        try:
            page = paginator.page(request.GET.get("cursor") or None)
        except InvalidCursor as exc:
            raise ApiError(
                400, "The cursor is invalid, or does not match the current filters and sort."
            ) from exc

        rows = [
            row(
                self.panel,
                obj,
                columns,
                url=_detail_url(self.panel, obj),
                html_url=_html_detail_url(self.panel, obj),
            )
            for obj in page.object_list
        ]
        return json_response(
            {
                "results": rows,
                "next": page.next_cursor,
                "previous": page.previous_cursor,
                "query": resolved.query,
                "sort": resolved.sort,
                "filters": [filter_payload(f) for f in resolved.filters],
            }
        )

    def _requested_fields(self, request) -> list[str]:
        """The columns to serialise, refused unless the panel already offers them.

        ``?fields=`` used to be split on commas and handed straight to the serialiser,
        which walked it with ``resolve_path`` — and ``resolve_path`` knows about
        Django's ``_meta`` graph, not about this panel's registry or its permissions.
        So the parameter reached anything the ORM could join to: a field the
        ``SENSITIVE`` name filter deliberately hides on the detail endpoint, or a
        ``__``-chain into a related model the panel never registered and the caller has
        no permission for. ``?fields=number,customer__api_token`` returned the token.

        The allowlist is the union of two answers that already exist: what the list
        page would render for this request, and what the detail endpoint would return
        for one record. Neither is widened here — asking is now bounded by showing.

        Unknown names are refused rather than dropped. A client that quietly gets back
        fewer columns than it asked for reads the absence as "no value", and the bug
        surfaces later as missing data rather than here as a mistake.
        """
        raw = request.GET.get("fields", "").strip()
        if not raw:
            return self.panel.get_list_display(request)

        wanted = [name.strip() for name in raw.split(",") if name.strip()]
        allowed = {
            *self.panel.get_list_display(request),
            *(field.name for field in record_fields(self.panel.model)),
        }
        unknown = [name for name in wanted if name not in allowed]
        if unknown:
            raise ApiError(
                400,
                f"Unknown field(s): {', '.join(sorted(unknown))}. "
                "Ask the schema endpoint which fields this model offers.",
            )
        return wanted

    def _page_size(self, request) -> int:
        raw = request.GET.get("page_size")
        default = self.panel.get_per_page(request)
        if not raw:
            return default
        try:
            size = int(raw)
        except ValueError:
            return default
        return max(MIN_PAGE_SIZE, min(size, MAX_PAGE_SIZE))

    # ── create ──────────────────────────────────────────────────────────────

    def _create(self, request):
        payload = _parse_json_body(request)
        submitted = payload.get("fields")
        if not isinstance(submitted, dict):
            raise BadRequest("The request body must include a 'fields' object.")

        editable = formviews.form_editable_names(self.panel)
        unknown = sorted(set(submitted) - set(editable))
        if unknown:
            raise ApiError(400, f"Unknown or read-only field(s): {', '.join(unknown)}.")

        form_class = build_form_class(self.panel, editable)
        filled = _with_field_defaults(form_class, editable, submitted)
        form = form_class(data=json_to_form_data(form_class, filled))
        if not form.is_valid():
            return error_response(422, "Validation failed.", errors=form_errors(form))

        obj = formviews.save_form(request, self.panel, form, change=False)
        return json_response(_record_payload(self.panel, obj), status=201)


# ── one record ───────────────────────────────────────────────────────────────


class RecordDetailView(ModelApiView):
    """``GET`` one record. ``PUT``/``PATCH`` it. ``DELETE`` it."""

    def get(self, request, *args, **kwargs):
        obj = formviews.get_record(request, self.panel, self.permissions, self.kwargs["pk"], "view")
        return json_response(_record_payload(self.panel, obj))

    def put(self, request, *args, **kwargs):
        return self._write(request, partial=False)

    def patch(self, request, *args, **kwargs):
        return self._write(request, partial=True)

    def delete(self, request, *args, **kwargs):
        obj = formviews.get_record(
            request, self.panel, self.permissions, self.kwargs["pk"], "delete"
        )
        cascade = formviews.collect_cascade(obj)
        if cascade.is_blocked:
            # Same status the HTML confirm page answers with on the same refusal - see
            # vantage.formviews.DeleteView.post - so a client can tell "this delete is
            # impossible" apart from every other 4xx without parsing English.
            raise ApiError(409, cascade.blocked_by)

        from vantage import audit

        # Before the delete: Model.delete() blanks the pk, so an entry written
        # afterwards would snapshot a record with no identity - the same ordering
        # vantage.formviews.DeleteView.post uses.
        audit.log_delete(request, obj)
        with transaction.atomic(using=router.db_for_write(self.panel.model)):
            obj.delete()
        return HttpResponse(status=204)

    # ── update / partial update ────────────────────────────────────────────

    def _write(self, request, *, partial: bool):
        obj = formviews.get_record(
            request, self.panel, self.permissions, self.kwargs["pk"], "change"
        )
        payload = _parse_json_body(request)
        submitted = payload.get("fields")
        if not isinstance(submitted, dict):
            raise BadRequest("The request body must include a 'fields' object.")

        editable = formviews.form_editable_names(self.panel)
        unknown = sorted(set(submitted) - set(editable))
        if unknown:
            raise ApiError(400, f"Unknown or read-only field(s): {', '.join(unknown)}.")

        # PATCH touches only the names it was sent, exactly like a bound ModelForm
        # built over a subset of fields - vantage.inline.ChangeSet.build_form does the
        # same narrowing for the same reason: a partial write must not demand a value
        # for a required field nobody is touching.
        names = list(submitted) if partial else editable
        values = submitted if partial else {name: submitted.get(name) for name in editable}

        form_class = build_form_class(self.panel, names)
        form = form_class(data=json_to_form_data(form_class, values), instance=obj)
        if not form.is_valid():
            return error_response(422, "Validation failed.", errors=form_errors(form))

        version = payload.get("version")
        try:
            saved = formviews.save_form(
                request,
                self.panel,
                form,
                change=True,
                version=version if isinstance(version, str) else None,
            )
        except concurrency.StaleWrite as stale:
            return error_response(
                409,
                "This record was changed by someone else since you read it.",
                conflict={
                    "pk": json_value(stale.obj.pk),
                    "version": stale.token,
                    "label": str(stale.obj),
                    "fields": concurrency.field_diff(stale.obj, submitted),
                },
            )
        return json_response(_record_payload(self.panel, saved))


# ── schema ───────────────────────────────────────────────────────────────────


class SchemaView(ApiView):
    """Every model this caller may view, described well enough to build a client cold.

    Not a :class:`ModelApiView`: there is no one model to gate on, and the visibility
    rule is the one :class:`vantage.search.GlobalSearch.targets` already applies —
    ``visible_panels`` is the *only* way a panel enters the response, so nothing below
    has to remember to check again.
    """

    def get(self, request, *args, **kwargs):
        models = {
            panel.opts.label_lower: self._model_schema(request, panel)
            for panel in self.permissions.visible_panels(request, list(self.site.panels()))
        }
        return json_response({"models": models})

    def resolve(self, request, *args, **kwargs) -> None:
        self.permissions = get_permissions()

    def _model_schema(self, request, panel) -> dict[str, Any]:
        editable = set(formviews.form_editable_names(panel))
        form_class = build_form_class(panel, sorted(editable)) if editable else None
        bound = form_class.base_fields if form_class is not None else {}

        fields = [
            field_schema(
                panel.model,
                field.name,
                form_field=bound.get(field.name),
                readonly=field.name not in editable,
                options_url=self._options_url(field),
            )
            for field in record_fields(panel.model)
        ]

        return {
            "model": panel.opts.label_lower,
            "label": panel.get_label(),
            "label_plural": panel.get_label_plural(),
            "actions": sorted(self.permissions.allowed_actions(request, panel.model)),
            "list_url": _list_url(self.site, panel),
            "list_display": panel.get_list_display(request),
            "list_filter": panel.get_list_filter(request),
            "list_search": panel.get_list_search(request),
            "fields": fields,
        }

    @staticmethod
    def _options_url(field: Any) -> str | None:
        related = getattr(field, "related_model", None)
        if related is None:
            return None
        try:
            base = reverse("vantage:autocomplete")
        except NoReverseMatch:  # pragma: no cover - only when autocomplete is not mounted
            return None
        return f"{base}?model={related._meta.label}"


# ── mounting ────────────────────────────────────────────────────────────────


def build_urls(site) -> list:
    """Routes for ``site``: one schema endpoint, and one list/detail pair per model.

    The list and detail routes are parameterised by ``app_label``/``model_name`` rather
    than looped per panel the way :mod:`vantage.recordviews` mounts its own — a shorter
    urlconf, and it is what lets :func:`_resolve_model` be the single, testable guard
    against an unregistered model, in the same shape
    :class:`vantage.autocomplete.AutocompleteView` already uses for the same reason.
    Nothing this resolves can name a model outside ``site.panels()``: the guard runs on
    every request, not only once at mount time.
    """
    return [
        path("_/api/schema/", SchemaView.as_view(site=site), name="api-schema"),
        path(
            "_/api/<str:app_label>/<str:model_name>/",
            ListCreateView.as_view(site=site),
            name="api-list",
        ),
        path(
            "_/api/<str:app_label>/<str:model_name>/<path:pk>/",
            RecordDetailView.as_view(site=site),
            name="api-detail",
        ),
    ]
