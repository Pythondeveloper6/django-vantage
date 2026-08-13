"""Panel views.

Every view is bound to a :class:`~vantage.sites.VantageSite` at URL-build time via
``as_view(site=...)``, so two mounted panels never share view state.
"""

from __future__ import annotations

from functools import cached_property

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.utils.translation import get_language, get_language_bidi
from django.views.generic import TemplateView

from vantage.conf import vantage_settings
from vantage.nav import build_nav, resolve_theme
from vantage.permissions import get_permissions
from vantage.theme import NO_FLASH_SCRIPT, render_theme_css


class SiteView(TemplateView):
    """Base for every view belonging to a panel."""

    site = None
    page_title = ""
    page_description = ""

    @cached_property
    def permissions(self):
        """Resolved lazily so a directly-constructed view still works in tests."""
        return get_permissions()

    def dispatch(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return redirect_to_login(request.get_full_path())
        denied = self.check_permission(request)
        if denied is not None:
            return denied
        return super().dispatch(request, *args, **kwargs)

    def check_permission(self, request):
        """Return a response to short-circuit with, or None to allow."""
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            site=self.site,
            settings=vantage_settings,
            nav=build_nav(self.site, self.request),
            permissions=self.permissions,
            path=self.request.path,
            theme=resolve_theme(self.request),
            language_code=get_language() or "en",
            is_rtl=get_language_bidi(),
            theme_css=render_theme_css(),
            no_flash_script=NO_FLASH_SCRIPT,
            page_title=self.page_title,
            page_description=self.page_description,
            breadcrumbs=self.get_breadcrumbs(),
        )
        return context

    def get_breadcrumbs(self) -> list[tuple[str, str | None]]:
        return []


class DashboardView(SiteView):
    template_name = "vantage/dashboard.html"
    page_title = "Dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["panels"] = list(self.site.panels())
        return context


class PanelView(SiteView):
    """Base for views scoped to one registered model."""

    panel = None

    #: The action a subclass needs permission for.
    required_action = "view"

    def check_permission(self, request):
        if self.permissions.has_model_perm(request, self.panel.model, self.required_action):
            return None
        raise PermissionDenied(
            f"You do not have permission to {self.required_action} "
            f"{self.panel.get_label_plural().lower()}."
        )

    def get_breadcrumbs(self):
        return [(self.panel.get_label_plural(), None)]


class ListView(PanelView):
    template_name = "vantage/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        panel = self.panel
        request = self.request

        queryset = self.permissions.filter_queryset(
            request, panel.get_list_queryset(request), "view"
        )

        query = request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(self._search_q(panel, query, request))

        columns = panel.get_list_display(request)
        link = panel.get_list_link(request)

        paginator = Paginator(queryset, panel.get_per_page(request))
        page = paginator.get_page(request.GET.get("page"))

        context.update(
            panel=panel,
            columns=[self._column_header(panel, name) for name in columns],
            rows=[self._row(panel, obj, columns, link) for obj in page.object_list],
            page=page,
            paginator=paginator,
            query=query,
            searchable=bool(panel.get_list_search(request)),
            page_title=panel.get_label_plural(),
        )
        return context

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _search_q(panel, query: str, request) -> Q:
        combined = Q()
        for path in panel.get_list_search(request):
            combined |= Q(**{f"{path.lstrip('^=@')}__icontains": query})
        return combined

    @staticmethod
    def _column_header(panel, name: str) -> dict:
        from vantage.introspection import resolve_path

        hops = resolve_path(panel.model, name)
        if hops:
            label = str(getattr(hops[-1].field, "verbose_name", name))
        else:
            label = name.replace("_", " ")
        return {"name": name, "label": label.capitalize()}

    @staticmethod
    def _row(panel, obj, columns: list[str], link: str) -> dict:
        from django.urls import NoReverseMatch, reverse

        from vantage.render import render_cell

        try:
            url = reverse(f"vantage:{panel.slug}-detail", args=[obj.pk])
        except NoReverseMatch:  # pragma: no cover
            url = None

        return {
            "obj": obj,
            "pk": obj.pk,
            "url": url,
            "cells": [
                {
                    "value": render_cell(panel, obj, name),
                    "is_link": name == link,
                }
                for name in columns
            ],
        }


class DetailView(PanelView):
    """One record, read-only for now: its fields, then what points at it."""

    template_name = "vantage/detail.html"

    def get_object(self):
        from django.shortcuts import get_object_or_404

        queryset = self.permissions.filter_queryset(
            self.request, self.panel.get_queryset(self.request), "view"
        )
        return get_object_or_404(queryset, pk=self.kwargs["pk"])

    def get_breadcrumbs(self):
        from django.urls import reverse

        return [
            (self.panel.get_label_plural(), reverse(self.panel.list_url_name)),
            (str(self.object), None),
        ]

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from vantage.introspection import displayable_fields, reverse_relations
        from vantage.render import render_cell

        context = super().get_context_data(**kwargs)
        obj = self.object
        panel = self.panel

        fields = [
            {
                "label": str(getattr(field, "verbose_name", field.name)).capitalize(),
                "value": render_cell(panel, obj, field.name),
            }
            for field in displayable_fields(panel.model)
            if not _is_hidden(field)
        ]

        # Counts only, not the rows themselves. A record with 50,000 children should
        # not make its detail page unopenable — the related list is a link, not a dump.
        related = []
        for rel in reverse_relations(panel.model):
            accessor = rel.accessor_name
            if accessor is None:
                continue
            manager = getattr(obj, accessor, None)
            if manager is None or not hasattr(manager, "count"):
                continue
            related.append(
                {
                    "label": str(rel.related_model._meta.verbose_name_plural).capitalize(),
                    "count": manager.count(),
                    "url": _related_url(self.site, rel, obj),
                }
            )

        context.update(
            object=obj,
            panel=panel,
            fields=fields,
            related=related,
            page_title=str(obj),
        )
        return context


def _is_hidden(field) -> bool:
    from vantage.inference import SENSITIVE

    return bool(SENSITIVE.search(field.name))


def _related_url(site, rel, obj) -> str | None:
    """A link through to the related model's list, pre-filtered to this record."""
    from django.urls import NoReverseMatch, reverse

    if not site.is_registered(rel.related_model):
        return None
    related_panel = site.get_panel(rel.related_model)
    try:
        base = reverse(related_panel.list_url_name)
    except NoReverseMatch:  # pragma: no cover
        return None
    return f"{base}?{rel.field.name}={obj.pk}"


class ThemeView(SiteView):
    """Persist a theme choice. Posted to by the toggle."""

    def dispatch(self, request, *args, **kwargs):
        if request.method != "POST":
            return HttpResponse(status=405)
        theme = request.POST.get("theme")
        response = HttpResponse(status=204)
        if theme in ("light", "dark"):
            response.set_cookie(
                "vantage-theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax"
            )
        else:
            response.delete_cookie("vantage-theme")
        return response
