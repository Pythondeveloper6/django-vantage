"""Inline cell editing on the list page: the allowlist, the staged changeset, the writes.

Airtable autosaves every keystroke because the grid *is* the product. Retool and
``django.contrib.admin`` stage a changeset because the grid is a view onto **someone
else's production database**, and Vantage is the second case. So nothing here writes
when a cell changes. Edits accumulate in the browser, a persistent bar says how many
are pending, and one request commits them: one transaction, one permission pass, one
review step before production moves.

Filament's own documentation admits that its ``TextInputColumn`` **does not check model
policies before saving**, and recommends full edit pages "where policy enforcement is
essential". That is the failure this module is written against, and it is why the
allowlist below is computed twice — once to decide which cells to draw, and again,
from scratch, on the way in. A field that the change form would not have offered is
dropped from the payload without comment, exactly as ``construct_instance`` drops it:
*hiding a control is a UI affordance; removing the field is the security boundary.*

Four rules shape the rest.

**The same form, not a second validation path.** Every write goes through the same
``ModelForm`` :mod:`vantage.widgets` builds for the change page, so coercion, choice
validation, ``max_length``, unique checks and ``_post_clean`` behave identically. A
second implementation of "is this value acceptable" is a second set of bugs, and the
one that runs on the quick path is the one nobody tests.

**The same permission answers, including the shape of the refusal.** Model-level
``change`` gates the endpoint; every row is fetched through ``filter_queryset`` first,
so a row outside your scope answers 404 rather than confirming it exists, and only then
does the object-level check decide the verb — which is a 403, because by that point you
already know the record is there. That is
:class:`~vantage.formviews.RecordView`'s order, restated rather than imported because
the batch resolves many rows and the view class resolves exactly one.

**Not every field, and not by taste.** Only kinds a single cell can express honestly:
boolean, choice, short text, number, decimal, date. Never a file, a long text, JSON, a
relation, or — the subtle one — any field caught up in a multi-column unique
constraint. Django silently *skips* a ``unique_together`` check when one of its fields
is missing from the form, so a one-field inline write turns a validation error into an
``IntegrityError`` five hundred milliseconds later. A model with a custom ``clean()`` is
excluded wholesale for the same reason: cross-field validation cannot be answered a
cell at a time.

**A rejected cell is never silently reverted.** The response says which row and which
field failed and nothing else changes; the browser keeps what the user typed. Quietly
restoring the old value loses typed work in a way nobody notices until much later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, router, transaction
from django.http import Http404, JsonResponse
from django.urls import NoReverseMatch, path, reverse

from vantage import concurrency
from vantage.sensitive import safe_str
from vantage.views import PanelView
from vantage.widgets import FieldKind, build_form_class, classify, infer_form_fields

__all__ = [
    "INLINE_KINDS",
    "InlineConfig",
    "InlineField",
    "InlineValidateView",
    "InlineWriteView",
    "bound_form",
    "build_urls",
    "form_errors",
    "get_config",
    "inline_fields",
    "json_to_form_data",
]

#: The kinds a table cell can hold without lying about what it is. Everything absent
#: from this set needs room, context or a picker, and the change form is where those
#: live. Widening it is a one-line change *and* a design decision — a relation belongs
#: here only once there is a search picker to put in the cell.
INLINE_KINDS: frozenset[FieldKind] = frozenset(
    {
        FieldKind.BOOLEAN,
        FieldKind.CHOICE,
        FieldKind.TEXT,
        FieldKind.NUMBER,
        FieldKind.DECIMAL,
        FieldKind.DATE,
    }
)

#: Longest ``max_length`` still considered a "short" string. A 2000-character
#: ``CharField`` is prose with a length limit, and prose in a table cell is a scroll
#: bar, not an editor. A ``CharField`` with no limit at all is treated the same way.
MAX_TEXT_LENGTH = 255

#: How many rows one commit may carry. The bar can only accumulate what is on screen,
#: so anything past a large page is a hand-written payload, and an unbounded batch is
#: an unbounded transaction on somebody's production database.
MAX_ROWS_PER_SAVE = 500


@dataclass(frozen=True)
class InlineField:
    """One column that may be edited in place."""

    name: str
    kind: FieldKind
    label: str


@dataclass(frozen=True)
class InlineConfig:
    """Everything the list template and the browser need, resolved once per request."""

    panel: Any
    fields: tuple[InlineField, ...]
    save_url: str
    validate_url: str

    @property
    def enabled(self) -> bool:
        return bool(self.fields and self.save_url)

    @property
    def names(self) -> list[str]:
        return [field.name for field in self.fields]

    def field(self, name: str) -> InlineField | None:
        for candidate in self.fields:
            if candidate.name == name:
                return candidate
        return None


# ── the allowlist ───────────────────────────────────────────────────────────


def _change_form_names(panel) -> list[str]:
    """The names the change form would actually let you write.

    Restated from :meth:`~vantage.formviews.PanelFormView.get_editable_field_names`
    rather than imported: the two must agree, and the way to make sure they agree is
    for a divergence to fail a test named after the rule, not for one to import the
    other's view class into a template tag.
    """
    names = list(panel.fields) if panel.fields else infer_form_fields(panel.model)
    readonly = set(panel.readonly_fields or ())
    excluded = set(panel.exclude or ())
    return [name for name in names if name not in readonly and name not in excluded]


def _cross_field_names(model: type[models.Model]) -> set[str]:
    """Fields whose validity depends on another field's value.

    ``unique_together`` and multi-column ``UniqueConstraint``s are the dangerous case
    and the reason this function exists. ``Model._get_unique_checks`` drops any check
    whose fields are not all present on the form, so a one-field write does not fail
    validation — it passes, and then the database raises. An error the user cannot
    read, on a write they were told was fine.
    """
    names: set[str] = set()
    for group in model._meta.unique_together:
        if len(group) > 1:
            names.update(group)
    for constraint in model._meta.constraints:
        fields = getattr(constraint, "fields", None) or ()
        if isinstance(constraint, models.UniqueConstraint) and len(fields) > 1:
            names.update(fields)
    return names


def _has_custom_clean(model: type[models.Model]) -> bool:
    """Whether the model validates across its own fields in ``clean()``.

    When it does, no single cell can be validated or saved in isolation without
    running a rule against values the user never saw. The whole model drops out of
    inline editing, which is the conservative answer: the change form still works,
    and it is the page that shows every field the rule is about.
    """
    return model.clean is not models.Model.clean


def _is_short(field: models.Field, kind: FieldKind) -> bool:
    if kind is not FieldKind.TEXT:
        return True
    length = getattr(field, "max_length", None)
    return bool(length) and length <= MAX_TEXT_LENGTH


def inline_fields(panel, request=None) -> tuple[InlineField, ...]:
    """The columns this panel may edit in place, in ``list_editable`` order.

    Every filter here is a refusal, and each one is applied server-side on the way in
    as well as on the way out. Nothing about the rendered page is trusted to have
    limited the payload.
    """
    declared = list(panel.list_editable or ())
    if not declared or _has_custom_clean(panel.model):
        return ()

    columns = set(panel.get_list_display(request))
    writable = set(_change_form_names(panel))
    entangled = _cross_field_names(panel.model)
    link = panel.get_list_link(request)

    resolved: list[InlineField] = []
    for name in declared:
        if name not in columns or name not in writable or name in entangled:
            continue
        if name == link:
            # A cell is a link or an input, never both. ``panel_checks`` reports this
            # as E016; dropping it here as well keeps a misconfigured panel usable
            # rather than turning one bad line into an unopenable page.
            continue
        try:
            field = panel.model._meta.get_field(name)
        except Exception:  # pragma: no cover - E015 already reports this
            continue
        if field.is_relation or not getattr(field, "editable", False):
            continue
        if name in panel.get_sensitive_fields():
            # A cell editor round-trips the value it edits, through the control and
            # back in the save response. A secret has no business in either.
            continue
        kind = classify(field)
        if kind not in INLINE_KINDS or not _is_short(field, kind):
            continue
        resolved.append(
            InlineField(
                name=name,
                kind=kind,
                label=str(getattr(field, "verbose_name", name)).capitalize(),
            )
        )
    return tuple(resolved)


def get_config(panel, request=None) -> InlineConfig:
    """The resolved configuration, memoised on the request.

    The list template asks for this once per page and then once per cell; recomputing
    the allowlist fifty times a render would be the sort of quiet cost that only
    shows up on the model with thirty columns.
    """
    cache = getattr(request, "_vantage_inline", None) if request is not None else None
    if cache is None:
        cache = {}
        if request is not None:
            request._vantage_inline = cache
    if panel.slug in cache:
        return cache[panel.slug]

    fields = inline_fields(panel, request)
    save_url = _url(panel, "inline")
    config = InlineConfig(
        panel=panel,
        fields=fields,
        save_url=save_url,
        validate_url=_url(panel, "inline-validate"),
    )
    cache[panel.slug] = config
    return config


def _url(panel, suffix: str) -> str:
    """Resolve a route, or return ``""`` when it is not mounted.

    The feature has to degrade to a plain read-only table rather than raising: a
    project that has not mounted the inline routes should get the list page it
    always had, not a ``NoReverseMatch`` in the middle of a template.
    """
    try:
        return reverse(f"vantage:{panel.slug}-{suffix}")
    except NoReverseMatch:
        return ""


# ── rendering ───────────────────────────────────────────────────────────────


def bound_form(config: InlineConfig, obj) -> forms.ModelForm:
    """A form bound to one row, built once and reused across that row's cells.

    Cached on the instance because the template renders one cell at a time and a
    fresh ``ModelForm`` per cell would build the same widgets three times per row.
    The form is never saved from here; it exists to produce a control that agrees
    with the change page down to the ``step`` attribute.
    """
    cached = getattr(obj, "_vantage_inline_form", None)
    if cached is not None:
        return cached
    form_class = build_form_class(config.panel, config.names)
    form = form_class(instance=obj)
    obj._vantage_inline_form = form
    return form


def control_attrs(field: InlineField, obj) -> dict[str, str]:
    """Attributes added to the widget the change form would have rendered.

    ``tabindex="-1"`` is the APG grid contract — the table has exactly one tab stop
    and the arrow keys reach everything inside it. ``form`` re-parents the control
    away from the bulk-action form it is physically nested in: without it, Enter in a
    cell submits a bulk action, and every cell value rides along with it.
    """
    return {
        "class": "v-inline-input",
        "tabindex": "-1",
        "form": "v-inline-form",
        "data-inline-input": "",
        "data-inline-field": field.name,
        "aria-label": f"{field.label}: {safe_str(obj)}",
        "aria-describedby": error_id(obj, field),
    }


def error_id(obj, field: InlineField) -> str:
    return f"v-inline-err-{obj.pk}-{field.name}"


# ── the write path ──────────────────────────────────────────────────────────


class InlinePanelView(PanelView):
    """Shared plumbing: JSON in, JSON out, and the row lookup both endpoints share."""

    #: Model-level gate, identical to the change form's. A user without it never
    #: reaches a handler, and gets the same 403 the edit page would give them.
    required_action = "change"

    def parse_body(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.request.body.decode() or "{}")
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("The request body was not valid JSON.") from None
        if not isinstance(payload, dict):
            raise BadRequest("The request body must be a JSON object.")
        return payload

    def get_row(self, pk):
        """Scope, then authorise — and answer 404 before 403, in that order.

        Straight out of :class:`~vantage.formviews.RecordView`. ``filter_queryset``
        runs with ``"view"`` because scoping answers "does this row exist for you",
        which has one answer per user rather than one per verb. Only an admissible
        row reaches the object check, and only that check produces a 403 — by then
        the user already knows the record is there, so there is nothing left to leak.
        """
        queryset = self.permissions.filter_queryset(
            self.request, self.panel.get_queryset(self.request), "view"
        )
        try:
            obj = queryset.filter(pk=pk).first()
        except (TypeError, ValueError, ValidationError):
            # A primary key of the wrong shape is a row that does not exist.
            obj = None
        if obj is None:
            raise Http404(f"No {self.panel.get_label().lower()} matches {pk!r}.")
        if not self.permissions.has_object_perm(self.request, self.panel.model, "change", obj):
            raise PermissionDenied(
                f"You do not have permission to change this {self.panel.get_label().lower()}."
            )
        return obj

    def allowed_fields(self, submitted: dict[str, Any]) -> dict[str, Any]:
        """Drop everything the change form would not have rendered.

        Silently, and that is the point: there is no legitimate client that can send
        one of these, so there is no error worth composing. Reporting "``is_staff``
        is not editable" would answer a question only an attacker asks.
        """
        config = get_config(self.panel, self.request)
        return {name: value for name, value in submitted.items() if config.field(name) is not None}


class BadRequest(Exception):
    """A malformed payload. Distinct from a rejected one, which is a 422."""


class InlineWriteView(InlinePanelView):
    """Commit a staged changeset. ``PATCH``, JSON, all-or-nothing.

    ``PATCH`` rather than ``POST`` because that is what this is: a partial update of
    rows that already exist. Practically it also keeps the endpoint out of reach of a
    plain HTML form post, which is a small extra wall around a write that never has a
    page of its own.
    """

    http_method_names = ["patch"]

    def patch(self, request, *args, **kwargs):
        try:
            payload = self.parse_body()
            rows = self.read_changes(payload)
        except BadRequest as exc:
            body = {"ok": False, "error": "bad_request", "detail": str(exc)}
            return JsonResponse(body, status=400)

        if not rows:
            return JsonResponse({"ok": True, "saved": 0, "rows": []})

        from vantage import undo

        undo.arm(request, self.panel)
        self.capture = (
            undo.FieldCapture(
                self.panel.model, get_config(self.panel, request).names, strategy="instances"
            )
            if undo.enabled_for(self.panel)
            else None
        )
        try:
            saved = self.commit(rows)
        except Rejected as exc:
            return JsonResponse(exc.payload, status=exc.status)

        # Outside the transaction, deliberately: an audit failure caught inside an
        # atomic block still marks it for rollback, so logging the write there could
        # destroy the write it was recording.
        from vantage import audit

        for obj, form in saved:
            audit.log_form_save(request, obj, form, change=True)

        body: dict[str, Any] = {
            "ok": True,
            "saved": len(saved),
            "rows": [self.row_result(obj) for obj, _ in saved],
        }
        if self.capture is not None:
            plan = self.capture.plan(self.panel, verb="change", what="inline", origin="inline")
            offered = undo.offer(request, self.panel, plan)
            if offered is not None:
                body["undo"] = offered.as_json(plan.summary if plan else "")
        return JsonResponse(body)

    # ── reading the payload ─────────────────────────────────────────────────

    def read_changes(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        changes = payload.get("changes")
        if not isinstance(changes, list):
            raise BadRequest("'changes' must be a list.")
        if len(changes) > MAX_ROWS_PER_SAVE:
            raise BadRequest(f"A changeset may carry at most {MAX_ROWS_PER_SAVE} rows.")

        rows = []
        for entry in changes:
            if not isinstance(entry, dict) or "pk" not in entry:
                raise BadRequest("Every change needs a 'pk'.")
            fields = entry.get("fields")
            if not isinstance(fields, dict):
                raise BadRequest("Every change needs a 'fields' object.")
            allowed = self.allowed_fields(fields)
            if not allowed:
                # Every field in this row was dropped by the allowlist. Nothing to
                # write, and nothing to report: the row simply does not participate.
                continue
            version = entry.get("version")
            rows.append(
                {
                    "pk": entry["pk"],
                    "version": version if isinstance(version, str) else None,
                    "fields": allowed,
                }
            )
        return rows

    # ── writing ─────────────────────────────────────────────────────────────

    def commit(self, rows: list[dict[str, Any]]):
        """One transaction for the whole changeset.

        Atomic because a half-applied changeset is worse than a refused one: the user
        reviewed twelve edits as a unit and pressed a button that said "Save 12
        changes". Ten of them landing is not a partial success, it is a state nobody
        asked for and nobody can see.

        Conflicts and validation errors are gathered across *all* rows before
        anything is reported, so a changeset with three bad cells takes one round trip
        to fix rather than three.
        """
        conflicts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        saved: list[tuple[Any, forms.ModelForm]] = []

        with transaction.atomic(using=router.db_for_write(self.panel.model)):
            capture = getattr(self, "capture", None)
            for row in rows:
                obj = self.get_row(row["pk"])
                if capture is not None:
                    # Before the form is bound: validating it writes the new values
                    # onto ``obj``, and the "before" would already be the "after".
                    capture.note(obj, [obj._meta.get_field(name).attname for name in row["fields"]])
                form = self.build_form(obj, row["fields"])
                if not form.is_valid():
                    errors.append({"pk": str(obj.pk), "fields": form_errors(form)})
                    continue
                try:
                    concurrency.claim(obj, row["version"])
                except concurrency.StaleWrite as stale:
                    conflicts.append(self.conflict(stale, row["fields"]))
                    continue
                saved.append((self.save_row(obj, form), form))
                if capture is not None:
                    capture.read_row(obj)

            if conflicts or errors:
                # Raised inside the block on purpose: this is what rolls back the rows
                # that did succeed, which is the atomicity the bar promised.
                raise Rejected(conflicts, errors)

        return saved

    def build_form(self, obj, values: dict[str, Any]) -> forms.ModelForm:
        """A ModelForm over exactly the fields this row is changing.

        Narrowed to the submitted fields rather than to every editable column, so a
        row that only changes ``status`` is not also required to satisfy a blank
        ``rating`` that nobody touched.
        """
        form_class = build_form_class(self.panel, list(values))
        return form_class(data=json_to_form_data(form_class, values), instance=obj)

    def save_row(self, obj, form: forms.ModelForm):
        """Save one row through the panel's own hook, exactly as the change form does."""
        from vantage.tenancy import enforce as enforce_tenant

        hook = getattr(self.panel, "save_model", None)
        saved = form.save(commit=False)
        # The grid posts arbitrary columns, so it can post the tenant column too.
        # A no-op unless tenancy is on; a 403 when the value would cross a boundary.
        enforce_tenant(self.request, saved)
        if callable(hook):
            hook(self.request, saved, form, True)
        else:
            saved.save()
        form.save_m2m()
        return saved

    def conflict(self, stale: concurrency.StaleWrite, values: dict[str, Any]) -> dict[str, Any]:
        return {
            "pk": str(stale.obj.pk),
            "version": stale.token,
            "label": safe_str(stale.obj),
            "fields": concurrency.field_diff(stale.obj, values),
        }

    def row_result(self, obj) -> dict[str, Any]:
        """What the row looks like now, so the grid shows the saved state, not the typed one.

        A value the server normalised — ``3.5`` stored as ``3.50``, a date reformatted
        — has to go back to the control, or the cell keeps showing something that is
        not what is in the database and the next save reports a spurious change.
        """
        config = get_config(self.panel, self.request)
        form_class = build_form_class(self.panel, config.names)
        form = form_class(instance=obj)
        return {
            "pk": str(obj.pk),
            "version": concurrency.token_for(obj),
            "label": safe_str(obj),
            "values": {name: _widget_value(form, name) for name in config.names},
        }


class Rejected(Exception):
    """Nothing was written. Carries the whole story so one response can tell it."""

    def __init__(self, conflicts: list[dict], errors: list[dict]) -> None:
        super().__init__("The changeset was refused.")
        self.conflicts = conflicts
        self.errors = errors

    @property
    def status(self) -> int:
        # A conflict outranks a validation error: it is the one the user cannot fix by
        # retyping, and it is the one that needs the other side of the story shown.
        return 409 if self.conflicts else 422

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "saved": 0,
            "error": "conflict" if self.conflicts else "invalid",
            "conflicts": self.conflicts,
            "errors": self.errors,
        }


class InlineValidateView(InlinePanelView):
    """Validate one field against the real form, and save nothing.

    Reusing the ``ModelForm`` is the whole design. A second "is this a valid status"
    written in JavaScript disagrees with the server the first time somebody adds a
    choice, and it disagrees in the direction that lets bad data through.

    This is an affordance, not a gate. The browser debounces it, aborts the previous
    one, and — the part that matters — treats a failed *request* as no answer at all
    rather than as a failed validation. A network blip must never be able to stop
    somebody saving their work.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        try:
            payload = self.parse_body()
        except BadRequest as exc:
            return JsonResponse({"error": "bad_request", "detail": str(exc)}, status=400)

        name = payload.get("field")
        if not isinstance(name, str) or not self.allowed_fields({name: None}):
            # Same silence as the write path: naming the editable fields to someone
            # probing for them is the answer, not the refusal.
            return JsonResponse({"error": "not_editable"}, status=400)

        obj = self.get_row(payload.get("pk"))
        form_class = build_form_class(self.panel, [name])
        value = payload.get("value")
        form = form_class(data=json_to_form_data(form_class, {name: value}), instance=obj)
        form.is_valid()

        return JsonResponse(
            {
                "pk": str(obj.pk),
                "field": name,
                "valid": not form.errors.get(name),
                "errors": [str(message) for message in form.errors.get(name, [])],
            }
        )


# ── helpers ─────────────────────────────────────────────────────────────────


def form_errors(form: forms.ModelForm) -> dict[str, list[str]]:
    """``{"rating": ["Ensure that there are no more than 3 digits."]}``.

    Plain strings keyed by field, including ``__all__``. Django's ``as_json`` carries
    error codes too, but the browser has nothing to do with a code, and a payload the
    client has to unwrap before it can show a sentence is a payload that will
    eventually be shown unwrapped. Public — :mod:`vantage.api` reports validation
    errors in exactly this shape, and a second implementation would be the one place
    the two payloads could quietly disagree.
    """
    return {name: [str(message) for message in messages] for name, messages in form.errors.items()}


def json_to_form_data(form_class, values: dict[str, Any]) -> dict[str, Any]:
    """Turn a JSON object into something a ``ModelForm`` can bind.

    JSON carries real booleans and numbers; an HTML form carries strings and the
    *absence* of a key for an unchecked box. ``CheckboxInput.value_from_datadict``
    reads presence, so ``{"featured": false}`` would arrive as ``True`` — the classic
    way a JSON API quietly inverts every boolean it is given.

    ``forms.JSONField`` gets the same treatment for the reverse reason. It expects the
    *string* a ``<textarea>`` would have posted and calls ``json.loads`` on it itself;
    handed the already-decoded Python value instead, ``to_python`` checks it against
    Django's ``EMPTY_VALUES``, which includes a bare ``{}`` and ``[]`` — so an honestly
    empty object or array is read as "nothing was submitted" and silently becomes
    ``None``, which a ``NOT NULL`` JSON column then refuses. :data:`INLINE_KINDS`
    excludes JSON, so this module never met the bug; :mod:`vantage.api` is the first
    caller that hands a real JSON value through this function, and the fix belongs
    here rather than in ``vantage.api`` because it is a fact about ``forms.JSONField``,
    not about either caller.

    Public, and used by :mod:`vantage.api` as well as by this module: every JSON write
    in the package — one cell or a whole record — goes through this one conversion, so
    a boolean or a JSON value cannot behave differently depending on which endpoint set
    it.
    """
    write_only = set(getattr(form_class, "vantage_write_only", ()))
    data: dict[str, Any] = {}
    for name, value in values.items():
        field = form_class.base_fields.get(name)
        if name in write_only and value is None:
            # JSON null on a write-only secret means "clear it"; an empty string or an
            # omitted key means "keep it". See vantage.widgets.WriteOnlyModelForm.
            from vantage.widgets import CLEAR_SUFFIX

            data[name] = ""
            data[f"{name}{CLEAR_SUFFIX}"] = "1"
            continue
        if field is not None and isinstance(field.widget, forms.CheckboxInput):
            if _truthy(value):
                data[name] = "on"
            continue
        if field is not None and isinstance(field, forms.JSONField) and not isinstance(value, str):
            data[name] = "" if value is None else json.dumps(value)
            continue
        data[name] = "" if value is None else value
    return data


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "off", "no")
    return bool(value)


def _widget_value(form: forms.ModelForm, name: str) -> Any:
    """The value the control should show, in the format its widget expects."""
    bound = form[name]
    value = bound.value()
    if isinstance(bound.field.widget, forms.CheckboxInput):
        return bool(value)
    formatted = bound.field.widget.format_value(value)
    return "" if formatted is None else formatted


def build_urls(site) -> list:
    """The two inline routes for every panel.

    Mounted from :mod:`vantage.urls` **before** the per-panel loop, because the detail
    route's ``<path:pk>`` converter is greedy and would otherwise swallow ``_inline``
    as a primary key — the same reason the import routes are added there.
    """
    urlpatterns = []
    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/_inline/"
        urlpatterns.extend(
            [
                path(
                    prefix,
                    InlineWriteView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-inline",
                ),
                path(
                    f"{prefix}validate/",
                    InlineValidateView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-inline-validate",
                ),
            ]
        )
    return urlpatterns
