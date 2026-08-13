"""Panel views.

Every view is bound to a :class:`~vantage.sites.VantageSite` at URL-build time via
``as_view(site=...)``, so two mounted panels never share view state.
"""

from __future__ import annotations

from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.utils.translation import get_language, get_language_bidi
from django.views.generic import TemplateView

from vantage.conf import vantage_settings
from vantage.nav import build_nav, resolve_theme
from vantage.theme import NO_FLASH_SCRIPT, render_theme_css


class SiteView(TemplateView):
    """Base for every view belonging to a panel."""

    site = None
    page_title = ""
    page_description = ""

    def dispatch(self, request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_staff:
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            site=self.site,
            settings=vantage_settings,
            nav=build_nav(self.site, self.request),
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

    def get_breadcrumbs(self):
        return [(self.panel.get_label_plural(), None)]


class ListView(PanelView):
    template_name = "vantage/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        panel = self.panel
        request = self.request

        queryset = panel.get_list_queryset(request)

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
        from vantage.render import render_cell

        return {
            "obj": obj,
            "pk": obj.pk,
            "cells": [
                {
                    "value": render_cell(panel, obj, name),
                    "is_link": name == link,
                }
                for name in columns
            ],
        }


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
