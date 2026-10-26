"""Validation for user-written :class:`~vantage.panels.ModelPanel` subclasses.

Separated from the panel class itself so a project can subclass ``ModelPanelChecks``
and point its own panel base class at it — the same extension seam
``django.contrib.admin`` exposes through ``checks_class``.

A bad field path should fail ``manage.py check``, not raise ``FieldError`` deep inside
a query at request time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.checks import Error, Warning

from vantage.introspection import resolve_path

if TYPE_CHECKING:
    from vantage.panels import ModelPanel

#: Attributes removed from ``ModelPanel``, mapped to what replaced them. Setting one
#: is an error rather than a no-op — a panel that quietly ignores what you wrote is
#: precisely the problem these removals were addressing.
REMOVED_ATTRIBUTES: dict[str, str] = {
    "permissions": (
        "Access is resolved in one place. Define has_view_permission / "
        "has_add_permission / has_change_permission / has_delete_permission on the "
        "panel, or point VANTAGE['PERMISSIONS'] at a PanelPermissions subclass."
    ),
}


class ModelPanelChecks:
    """Validates the declarative attributes of one panel."""

    def check(self, panel: ModelPanel, **kwargs) -> list[Error]:
        return [
            *self._check_list_display(panel),
            *self._check_path_attr(panel, "list_filter", "vantage.E011"),
            *self._check_path_attr(panel, "list_search", "vantage.E012"),
            *self._check_ordering(panel),
            *self._check_list_link(panel),
            *self._check_list_editable(panel),
            *self._check_layout(panel),
            *self._check_list_select_related(panel),
            *self._check_related_tabs(panel),
            *self._check_data_quality(panel),
            *self._check_undo(panel),
            *self._check_removed(panel),
            *self._check_list_views(panel),
            *self._check_sensitive_fields(panel),
            *self._check_sensitive_lookups(panel),
        ]

    # ── helpers ─────────────────────────────────────────────────────────────

    def _resolves(self, panel: ModelPanel, path: str) -> bool:
        """Whether ``path`` names something the panel can actually render.

        A field path, a method on the panel, or an attribute on the model all count —
        only a name that matches none of those is an error.
        """
        if resolve_path(panel.model, path):
            return True
        if callable(getattr(panel, path, None)):
            return True
        return hasattr(panel.model, path)

    def _error(self, panel: ModelPanel, attr: str, value: str, ident: str) -> Error:
        return Error(
            f"{attr!r} refers to {value!r}, which is not a field on "
            f"{panel.opts.label}, an attribute of it, or a method on "
            f"{type(panel).__name__}.",
            obj=type(panel),
            id=ident,
        )

    # ── individual checks ───────────────────────────────────────────────────

    def _check_list_display(self, panel: ModelPanel) -> list[Error]:
        if panel.list_display is None:
            return []
        return [
            self._error(panel, "list_display", entry, "vantage.E010")
            for entry in panel.list_display
            if not self._resolves(panel, entry)
        ]

    def _check_path_attr(self, panel: ModelPanel, attr: str, ident: str) -> list[Error]:
        """Validate an attribute whose entries must be real model field paths.

        Unlike ``list_display``, these are fed to the ORM, so a panel method is not an
        acceptable value — it cannot be filtered or searched on in SQL.
        """
        value = getattr(panel, attr, None)
        if value is None:
            return []
        return [
            Error(
                f"{attr!r} refers to {entry!r}, which is not a field path on {panel.opts.label}.",
                hint="Only real model fields can be used here; they are resolved in SQL.",
                obj=type(panel),
                id=ident,
            )
            for entry in value
            if not resolve_path(panel.model, str(entry).lstrip("^=@"))
        ]

    def _check_ordering(self, panel: ModelPanel) -> list[Error]:
        if panel.ordering is None:
            return []
        errors = []
        for entry in panel.ordering:
            path = str(entry).lstrip("-")
            if path == "?":
                continue
            if not resolve_path(panel.model, path):
                errors.append(
                    Error(
                        f"'ordering' refers to {entry!r}, which is not a field path on "
                        f"{panel.opts.label}.",
                        obj=type(panel),
                        id="vantage.E013",
                    )
                )
        return errors

    def _check_list_link(self, panel: ModelPanel) -> list[Error]:
        """The linked column has to be a column that is actually displayed."""
        if panel.list_link is None or panel.list_display is None:
            return []
        if panel.list_link in panel.list_display:
            return []
        return [
            Error(
                f"'list_link' is {panel.list_link!r}, which is not in 'list_display'.",
                hint="The column that opens the record must be one of the shown columns.",
                obj=type(panel),
                id="vantage.E014",
            )
        ]

    def _check_layout(self, panel: ModelPanel) -> list[Error]:
        """A field named in a layout but absent from the model.

        Left unchecked this renders a form quietly missing a field, which is found
        weeks later by a confused user rather than at startup.
        """
        if panel.layout is None:
            return []
        from vantage.layout import check_layout
        from vantage.widgets import infer_form_fields

        try:
            available = infer_form_fields(panel.model)
        except Exception:
            return []

        return [
            Error(
                f"'layout' refers to {name!r}, which is not a field on {panel.opts.label}.",
                obj=type(panel),
                id="vantage.E017",
            )
            for name in check_layout(panel, available)
        ]

    def _check_list_editable(self, panel: ModelPanel) -> list[Error]:
        """A column cannot be both a link and an editable input.

        ``django.contrib.admin`` enforces the same rule, and for the same reason: the
        cell has to be one or the other.
        """
        if not panel.list_editable:
            return []
        errors = []
        for entry in panel.list_editable:
            if not resolve_path(panel.model, entry):
                errors.append(
                    Error(
                        f"'list_editable' refers to {entry!r}, which is not a field on "
                        f"{panel.opts.label}.",
                        obj=type(panel),
                        id="vantage.E015",
                    )
                )
            elif entry == panel.list_link:
                errors.append(
                    Error(
                        f"{entry!r} is in both 'list_editable' and 'list_link'.",
                        hint="A column can be an editable input or a link, not both.",
                        obj=type(panel),
                        id="vantage.E016",
                    )
                )
        return errors

    # ── attributes that were wired or withdrawn ─────────────────────────────

    def _check_list_select_related(self, panel: ModelPanel) -> list[Error]:
        """Every declared join must resolve, and must be joinable.

        The optimiser derives these from what the page renders, so anything set here
        is an addition on top. A path that cannot be joined would raise ``FieldError``
        deep inside the list query, which is exactly the class of failure the checks
        exist to move to startup.
        """
        declared = panel.list_select_related
        if not declared:
            return []

        errors: list[Error] = []
        for path in declared:
            hops = resolve_path(panel.model, str(path))
            if not hops:
                errors.append(
                    Error(
                        f"'list_select_related' refers to {path!r}, which is not a "
                        f"field path on {panel.opts.label}.",
                        obj=type(panel),
                        id="vantage.E018",
                    )
                )
            elif any(hop.is_multivalued for hop in hops):
                errors.append(
                    Error(
                        f"'list_select_related' refers to {path!r}, which crosses a "
                        f"multi-valued relation.",
                        hint="select_related cannot follow a reverse FK or M2M; those "
                        "are prefetched, and the optimiser already does it.",
                        obj=type(panel),
                        id="vantage.E018",
                    )
                )
        return errors

    def _check_related_tabs(self, panel: ModelPanel) -> list[Error]:
        """``related_tabs`` is tri-state: None decides by count, True/False assert."""
        value = panel.related_tabs
        if value is None or isinstance(value, bool):
            return []
        return [
            Error(
                f"'related_tabs' must be True, False or None; got {value!r}.",
                hint="None lets the panel decide from how many relations there are.",
                obj=type(panel),
                id="vantage.E019",
            )
        ]

    def _check_data_quality(self, panel: ModelPanel) -> list[Error]:
        """``data_quality`` is False, None, or a list of real check names."""
        value = panel.data_quality
        if value is None or isinstance(value, bool):
            return [] if value is not True else self._dq_error(panel, value)
        if not isinstance(value, list | tuple):
            return self._dq_error(panel, value)
        from vantage.dataquality import get_checks

        known = {c.name for c in get_checks()}
        unknown = [n for n in value if n not in known]
        if not unknown:
            return []
        return [
            Error(
                f"'data_quality' names unknown check(s) {unknown!r} on {panel.opts.label}.",
                hint=f"Known checks: {', '.join(sorted(known))}.",
                obj=type(panel),
                id="vantage.E063",
            )
        ]

    def _dq_error(self, panel: ModelPanel, value) -> list[Error]:
        return [
            Error(
                f"'data_quality' must be False, None or a list of check names; got {value!r}.",
                hint="False leaves the model out of the audit; a list skips those checks.",
                obj=type(panel),
                id="vantage.E063",
            )
        ]

    def _check_undo(self, panel: ModelPanel) -> list[Error]:
        """``undo`` is tri-state like ``related_tabs``; anything else is a typo."""
        value = getattr(panel, "undo", None)
        if value is None or isinstance(value, bool):
            return []
        return [
            Error(
                f"'undo' must be True, False or None; got {value!r}.",
                hint="None follows VANTAGE['UNDO_WINDOW']; False turns undo off for this model.",
                obj=type(panel),
                id="vantage.E077",
            )
        ]

    def _check_removed(self, panel: ModelPanel) -> list[Error]:
        """Fail loudly on an attribute that no longer exists.

        Silently ignoring a withdrawn attribute is the same bug the withdrawal was
        meant to fix: the author sets it, nothing happens, and nothing says so.
        """
        errors = []
        for name, replacement in REMOVED_ATTRIBUTES.items():
            if getattr(panel, name, None) is not None:
                errors.append(
                    Error(
                        f"{name!r} is no longer a Vantage panel attribute.",
                        hint=replacement,
                        obj=type(panel),
                        id="vantage.E020",
                    )
                )
        return errors

    # ── list views ──────────────────────────────────────────────────────────

    def _check_list_views(self, panel: ModelPanel) -> list[Error]:
        """``list_views`` and the fields each view is arranged by (E070 to E076).

        Run against what is *declared*, field by field, so a typo in ``kanban_field``
        is reported even when ``list_views`` is left to inference — where the panel
        would otherwise just quietly lose its board.
        """
        from django.db import models

        from vantage.listviews import MODES
        from vantage.listviews.inference import KANBAN_MAX_COLUMNS, is_point_field, kanban_field_ok
        from vantage.listviews.kanban import local_field

        errors: list[Error] = []

        def error(message: str, ident: str, hint: str | None = None) -> None:
            errors.append(Error(message, hint=hint, obj=type(panel), id=ident))

        if panel.kanban_field is not None:
            field = local_field(panel.model, panel.kanban_field)
            if field is None or not kanban_field_ok(field):
                error(
                    f"'kanban_field' is {panel.kanban_field!r}, which is not a field on "
                    f"{panel.opts.label} with between 2 and {KANBAN_MAX_COLUMNS} choices, "
                    "or a BooleanField.",
                    "vantage.E072",
                    "A board needs one column per value, so the values have to be few and known.",
                )
        if panel.calendar_field is not None:
            field = local_field(panel.model, panel.calendar_field)
            if not isinstance(field, models.DateField):
                error(
                    f"'calendar_field' is {panel.calendar_field!r}, which is not a DateField "
                    f"or DateTimeField on {panel.opts.label}.",
                    "vantage.E073",
                )
        if panel.gallery_field is not None:
            field = local_field(panel.model, panel.gallery_field)
            if not isinstance(field, models.FileField):
                error(
                    f"'gallery_field' is {panel.gallery_field!r}, which is not an ImageField "
                    f"or FileField on {panel.opts.label}.",
                    "vantage.E074",
                )
        if panel.map_fields is not None:
            value = panel.map_fields
            numbers = (models.FloatField, models.DecimalField)
            if isinstance(value, str):
                field = local_field(panel.model, value)
                ok = field is not None and is_point_field(field)
            else:
                pair = list(value) if isinstance(value, list | tuple) else []
                ok = len(pair) == 2 and all(
                    isinstance(local_field(panel.model, name), numbers) for name in pair
                )
            if not ok:
                error(
                    f"'map_fields' is {value!r}, which is neither a PointField nor a "
                    f"(latitude, longitude) pair of Float/Decimal fields on {panel.opts.label}.",
                    "vantage.E075",
                    'Write map_fields = ("latitude", "longitude") or name a PointField.',
                )
        for entry in panel.card_fields or ():
            if not self._resolves(panel, entry):
                errors.append(self._error(panel, "card_fields", entry, "vantage.E076"))

        # The pickers' choices: each must be something the view could be arranged by,
        # or the picker would offer a field that silently falls back to the default.
        pickers = (
            (
                "kanban_fields",
                "vantage.E078",
                kanban_field_ok,
                "a field with 2 to 12 choices, or a BooleanField",
            ),
            (
                "calendar_fields",
                "vantage.E079",
                lambda f: isinstance(f, models.DateField),
                "a DateField or DateTimeField",
            ),
        )
        for attr, ident, ok, wanted in pickers:
            declared = getattr(panel, attr)
            if declared is None:
                continue
            if isinstance(declared, str) or not isinstance(declared, list | tuple):
                error(f"'{attr}' must be a list of field names; got {declared!r}.", ident)
                continue
            for name in declared:
                field = local_field(panel.model, name)
                if field is None or not ok(field):
                    error(
                        f"'{attr}' includes {name!r}, which is not {wanted} on {panel.opts.label}.",
                        ident,
                    )

        if panel.list_views is None:
            return errors
        if isinstance(panel.list_views, str) or not panel.list_views:
            error(
                '\'list_views\' must be a non-empty list, such as ["table", "kanban"].',
                "vantage.E070",
            )
            return errors
        for name in panel.list_views:
            mode = MODES.get(name)
            if mode is None:
                error(
                    f"'list_views' refers to {name!r}, which is not a list view.",
                    "vantage.E070",
                    f"Choose from: {', '.join(MODES)}.",
                )
            elif not mode.supports(panel):
                error(
                    f"'list_views' includes {name!r}, but {panel.opts.label} has no field "
                    "that view can be arranged by.",
                    "vantage.E071",
                    f"Set '{_FIELD_ATTR.get(name, name)}' on the panel, or remove {name!r}.",
                )
        return errors

    # ── sensitive values ────────────────────────────────────────────────────

    def _check_sensitive_fields(self, panel: ModelPanel) -> list[Error]:
        """``sensitive_fields`` must be a list of names the model actually has.

        A typo here is the worst kind of silent: the field the author meant to mask is
        printed in full, and nothing on the page says the declaration did nothing.
        """
        declared = panel.sensitive_fields
        if declared is None:
            return []
        if isinstance(declared, str) or not isinstance(declared, list | tuple | set | frozenset):
            return [
                Error(
                    f"'sensitive_fields' must be a list of field names; got {declared!r}.",
                    hint="Use [] to switch masking off for this model.",
                    obj=type(panel),
                    id="vantage.E090",
                )
            ]
        return [
            Error(
                f"'sensitive_fields' refers to {name!r}, which is not a field or "
                f"attribute of {panel.opts.label}.",
                hint="An unknown name masks nothing, so the field you meant is shown.",
                obj=type(panel),
                id="vantage.E090",
            )
            for name in declared
            if not (resolve_path(panel.model, str(name)) or hasattr(panel.model, str(name)))
        ]

    def _check_sensitive_lookups(self, panel: ModelPanel) -> list[Warning]:
        """A sensitive field the panel *explicitly* asked to search, filter or sort by.

        Vantage drops it at request time either way — a search box over a secret is a
        guessing oracle, a filter's row count is one, and so is a sort order — so this
        is a warning that the declaration does nothing, not a vulnerability report.
        Inferred configuration never includes one, which is why only declared
        attributes are checked.
        """
        from vantage.sensitive import is_sensitive_path

        declared = {
            "list_search": panel.list_search,
            "list_filter": panel.list_filter,
            "ordering": panel.ordering,
        }
        if panel.search_fields is not None and panel.search_fields is panel.list_search:
            declared["search_fields"] = declared.pop("list_search")

        warnings: list[Warning] = []
        for attr, entries in declared.items():
            for entry in entries or ():
                path = str(entry).lstrip("-^=@")
                if path == "?" or not resolve_path(panel.model, path):
                    continue
                if is_sensitive_path(panel, path):
                    warnings.append(
                        Warning(
                            f"{attr!r} lists {entry!r}, which is a sensitive field on "
                            f"{panel.opts.label}; it is ignored there.",
                            hint="Searching, filtering or sorting by a masked value would "
                            "reveal it a guess at a time. Remove it, or take it out of "
                            "masking with sensitive_fields if it is not actually secret.",
                            obj=type(panel),
                            id="vantage.W090",
                        )
                    )
        return warnings


#: The attribute that tells each view which field to use, for E071's hint.
_FIELD_ATTR = {
    "kanban": "kanban_field",
    "calendar": "calendar_field",
    "gallery": "gallery_field",
    "map": "map_fields",
}
