from django.urls import include, path

from vantage import site

urlpatterns = [
    path("admin/", site.urls),
    path("accounts/", include("vantage.authviews")),
]
