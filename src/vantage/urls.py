"""URL construction for a :class:`~vantage.sites.VantageSite`.

Routes are built from the registry at mount time, so a newly registered model is
reachable without touching a urlconf.
"""

from __future__ import annotations

from django.urls import path

from vantage import views


def build(site) -> list:
    """Return the urlpatterns for ``site``."""
    urlpatterns = [
        path("", views.DashboardView.as_view(site=site), name="dashboard"),
        path("_/theme/", views.ThemeView.as_view(site=site), name="theme"),
    ]

    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/"
        urlpatterns.append(
            path(
                prefix,
                views.ListView.as_view(site=site, panel=panel),
                name=f"{panel.slug}-list",
            )
        )

    return urlpatterns
