"""URL construction for a :class:`~vantage.sites.VantageSite`.

Routes are built from the registry at mount time, so a newly registered model is
reachable without touching a urlconf.
"""

from __future__ import annotations

from django.urls import path

from vantage import auditviews, formviews, importviews, savedviews, settingsview, views
from vantage.palette import build_urls


def build(site) -> list:
    """Return the urlpatterns for ``site``."""
    urlpatterns = [
        path("", views.DashboardView.as_view(site=site), name="dashboard"),
        path("_/theme/", views.ThemeView.as_view(site=site), name="theme"),
        path(
            "_/settings/",
            settingsview.SiteSettingsView.as_view(site=site),
            name="settings",
        ),
        path(
            "_/impersonate/",
            views.ImpersonateView.as_view(site=site),
            name="impersonate",
        ),
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

    # Before the panel loop: the detail route's <path:pk> is greedy and would
    # otherwise swallow 'import/' as a primary key.
    urlpatterns.extend(importviews.build_import_urls(site))

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
                # "add" comes before "<path:pk>" or the literal would be swallowed
                # by the pk converter and every create page would 404.
                path(
                    f"{prefix}add/",
                    formviews.CreateView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-add",
                ),
                # The pk converter is `path:` so a composite key can be encoded with
                # separators. That makes it greedy, so every sub-route must be
                # declared BEFORE the bare detail route or it swallows them.
                path(
                    f"{prefix}<path:pk>/edit/",
                    formviews.UpdateView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-change",
                ),
                path(
                    f"{prefix}<path:pk>/delete/",
                    formviews.DeleteView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-delete",
                ),
                path(
                    f"{prefix}<path:pk>/",
                    views.DetailView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-detail",
                ),
            ]
        )

    urlpatterns += build_urls(site)
    urlpatterns += auditviews.build_urls(site)
    urlpatterns += savedviews.build_urls(site)
    return urlpatterns
