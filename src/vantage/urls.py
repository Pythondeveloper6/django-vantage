"""URL construction for a :class:`~vantage.sites.VantageSite`.

Routes are built from the registry at mount time, so a newly registered model is
reachable without touching a urlconf.
"""

from __future__ import annotations

from django.urls import path
from django.views.decorators.cache import cache_page
from django.views.i18n import JavaScriptCatalog

from vantage import (
    api,
    auditviews,
    autocomplete,
    formviews,
    importviews,
    inline,
    jobviews,
    listprefsview,
    recordviews,
    savedviews,
    settingsview,
    topbarviews,
    views,
)
from vantage.palette import build_urls


def build(site) -> list:
    """Return the urlpatterns for ``site``."""
    urlpatterns = [
        path("", views.DashboardView.as_view(site=site), name="dashboard"),
        path("_/theme/", views.ThemeView.as_view(site=site), name="theme"),
        path(
            "_/jsi18n/",
            # `packages` scopes this to vantage's own catalogue — never the whole
            # project's, which is a project's own view to add if it wants one.
            #
            # Cached: the response is identical for every request in one language, and
            # generating it means walking every .mo file's plural forms on every hit.
            # Safe across languages without any Vary header of our own — Django's
            # _i18n_cache_key_suffix appends the active language to the cache key
            # whenever USE_I18N is on, reading request.LANGUAGE_CODE if a middleware
            # set one and falling back to get_language() otherwise. So a project that
            # negotiates language without LocaleMiddleware still gets its own entry
            # rather than whichever language happened to warm the cache first.
            cache_page(86400)(JavaScriptCatalog.as_view(packages=["vantage"])),
            name="jsi18n",
        ),
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
        path("_/undo/", views.UndoView.as_view(site=site), name="undo"),
    ]

    # The account page and the top-bar arrangement screen. Before the page loop for
    # the same reason as the block above: both live under the reserved "_/" prefix.
    urlpatterns += topbarviews.build_urls(site)

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
    # Same reason, same place: '_inline' is not a primary key.
    urlpatterns.extend(inline.build_urls(site))
    # And again: 'duplicate' and 'action' are suffixes on a record route, so they must
    # be declared before the bare '<path:pk>' that would otherwise swallow them.
    urlpatterns.extend(recordviews.build_urls(site))

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
                # Also before "<path:pk>", and for the same reason.
                path(
                    f"{prefix}_prefs/",
                    listprefsview.ListPrefsView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-prefs",
                ),
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
                    f"{prefix}<path:pk>/restore/",
                    formviews.RestoreView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-restore",
                ),
                path(
                    f"{prefix}<path:pk>/",
                    views.DetailView.as_view(site=site, panel=panel),
                    name=f"{panel.slug}-detail",
                ),
            ]
        )

    urlpatterns += build_urls(site)
    urlpatterns += autocomplete.build_urls(site)
    # Imported here rather than at the top: `vantage.dashboards` imports `vantage.views`
    # for its arrange view, and a project reaching `vantage.Dashboard` from settings
    # should not drag the whole view layer in behind it at import time.
    from vantage import dashboards

    urlpatterns += dashboards.build_urls(site)
    urlpatterns += auditviews.build_urls(site)
    urlpatterns += jobviews.build_urls(site)
    urlpatterns += savedviews.build_urls(site)
    # The JSON API. Its own routes are parameterised by model rather than looped per
    # panel, so where it is appended relative to the per-panel loop above does not
    # matter the way the "_prefs"/"_inline" routes' ordering does - see api.build_urls.
    urlpatterns += api.build_urls(site)
    return urlpatterns
