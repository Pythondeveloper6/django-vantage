"""The table: the default mode, and the one that is not implemented here.

It stays where it always was — :class:`vantage.views.ListView` and ``list.html`` — and
this class exists only so the switcher, the URL and the remembered preference can name
it like any other mode. Moving the grid into this module would have been tidier and
would have rewritten the most heavily tested page in the package for no change in
behaviour.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from vantage.listviews.base import ListMode

__all__ = ["TableMode"]


class TableMode(ListMode):
    name = "table"
    label = _("Table")
    icon = "table"

    @property
    def is_table(self) -> bool:
        return True

    @classmethod
    def supports(cls, panel) -> bool:
        return True

    def context(self, request, queryset, *, permissions) -> dict:  # pragma: no cover
        return {}
