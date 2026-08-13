"""Validation for user-written :class:`~vantage.panels.ModelPanel` subclasses.

Separated from the panel class itself so a project can subclass ``ModelPanelChecks``
and point its own panel base class at it — the same extension seam
``django.contrib.admin`` exposes through ``checks_class``.

A bad field path should fail ``manage.py check``, not raise ``FieldError`` deep inside
a query at request time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.checks import Error

from vantage.introspection import resolve_path

if TYPE_CHECKING:
    from vantage.panels import ModelPanel


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
                f"{attr!r} refers to {entry!r}, which is not a field path on "
                f"{panel.opts.label}.",
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
