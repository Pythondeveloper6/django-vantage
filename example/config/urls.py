"""URLs for the demo.

The panel is mounted at ``/admin/``; the auth pages come from the package itself, so
this file shows the whole integration a real project needs.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.views.generic import RedirectView

from vantage import site

handler403 = "vantage.authviews.permission_denied"
handler404 = "vantage.authviews.page_not_found"
handler500 = "vantage.authviews.server_error"

urlpatterns = [
    # Vantage ships branded login, logout and password-change pages, so a project
    # does not have to write them. There is no app_name and the route names are
    # Django's own, which makes this a drop-in for
    # include("django.contrib.auth.urls") — LOGIN_URL and redirect_to_login keep
    # working untouched.
    path("accounts/", include("vantage.authviews")),
    path("admin/", site.urls),
    path("", RedirectView.as_view(url="/admin/")),
]

if settings.DEBUG:
    # Uploaded branding assets. Django serves these only with DEBUG on;
    # a real deployment puts them behind its own web server.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
