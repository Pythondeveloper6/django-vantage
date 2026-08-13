"""Panel views.

Every view is bound to a :class:`~vantage.sites.VantageSite` at URL-build time via
``as_view(site=...)``, so two mounted panels never share view state.
"""

from __future__ import annotations

from django.views.generic import TemplateView


class SiteView(TemplateView):
    """Base for every view belonging to a panel."""

    site = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["site"] = self.site
        return context


class DashboardView(SiteView):
    template_name = "vantage/dashboard.html"
