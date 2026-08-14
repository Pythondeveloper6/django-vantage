"""The search endpoint behind relation pickers.

A ``<select>`` over fifty thousand rows is not a control, it is an outage: the browser
renders every option into the DOM and the page stops responding. Above a threshold
the widget layer already flags a relation as ``needs_search``; this is what it talks
to.

The whole surface is one endpoint returning one shape, deliberately. A per-model
endpoint would multiply the places that have to get the permission check right, and
the permission check is the entire risk here — an autocomplete is a read API over
arbitrary models, and the naive version happily enumerates a table the caller cannot
open.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _

__all__ = ["AutocompleteView", "build_urls"]

#: Options per response. Enough to scroll, few enough that the payload stays small
#: and the user is nudged towards typing rather than browsing.
PAGE_SIZE = 20


class AutocompleteView:
    """Options for a relation field, scoped to what the caller may see."""

    site = None

    @classmethod
    def as_view(cls, site=None):
        def view(request, *args, **kwargs):
            return cls(site).handle(request)

        return view

    def __init__(self, site) -> None:
        self.site = site

    def handle(self, request):
        from vantage.permissions import get_permissions

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            raise PermissionDenied(_("Sign in required."))

        model = self._resolve_model(request.GET.get("model", ""))
        panel = self._resolve_panel(model)

        permissions = get_permissions()
        # The whole point of the endpoint's single-entry design: one place where the
        # permission is checked, and it is checked against the *target* model rather
        # than the page the request came from.
        if not permissions.has_model_perm(request, model, "view"):
            raise PermissionDenied(_("You cannot view those records."))

        queryset = permissions.filter_queryset(request, panel.get_queryset(request), "view")

        term = request.GET.get("q", "").strip()
        if term:
            queryset = queryset.filter(self._search_q(panel, term))

        # One extra row answers "is there more" without a COUNT over a table that may
        # be enormous, which is the thing this endpoint exists to avoid touching.
        rows = list(queryset[: PAGE_SIZE + 1])
        more = len(rows) > PAGE_SIZE

        return JsonResponse(
            {
                "results": [{"id": str(obj.pk), "text": str(obj)} for obj in rows[:PAGE_SIZE]],
                "more": more,
                "term": term,
            },
            headers={"Cache-Control": "no-store"},
        )

    # ── resolution ──────────────────────────────────────────────────────────

    def _resolve_model(self, label: str):
        """``"shop.Product"`` to the model, refusing anything not registered.

        Going through the registry rather than ``get_model`` alone is the guard that
        matters: it means the endpoint can only ever reach models this panel already
        exposes, so an attacker cannot name ``auth.Permission`` or a model from an
        unrelated app and enumerate it.
        """
        try:
            model = django_apps.get_model(label)
        except (LookupError, ValueError):
            raise PermissionDenied(_("Unknown model.")) from None
        if model is None or not self.site.is_registered(model):
            raise PermissionDenied(_("Unknown model."))
        return model

    def _resolve_panel(self, model):
        return self.site.get_panel(model)

    @staticmethod
    def _search_q(panel, term: str) -> Q:
        combined = Q()
        paths = panel.get_list_search() or []
        for path in paths:
            combined |= Q(**{f"{path.lstrip('^=@')}__icontains": term})
        if not paths:
            # Nothing declared or inferred as searchable: fall back to the label
            # column so the picker is still usable rather than silently inert.
            from vantage.inference import infer_list_link

            combined = Q(**{f"{infer_list_link(panel.model)}__icontains": term})
        return combined


def build_urls(site) -> list:
    from django.urls import path

    return [
        path("_/autocomplete/", AutocompleteView.as_view(site=site), name="autocomplete"),
    ]
