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

    # Custom pages come before model routes so a page may deliberately claim a path
    # that would otherwise look like an app prefix.
    for page in site.pages():
        urlpatterns.append(
            path(
                page.get_path(),
                views.PageView.as_view(site=site, page_class=page),
                name=f"page-{page.get_slug()}",
            )
        )

    for panel in site.panels():
        opts = panel.opts
        prefix = f"{opts.app_label}/{opts.model_name}/"
        urlpatterns.extend(
            [
                path(
                    prefix,
                    views.ListView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-list",
                ),
                path(
                    f"{prefix}<path:pk>/",
                    views.DetailView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-detail",
                ),
            ]
        )

    return urlpatterns
