from django.urls import path

from vantage import site

urlpatterns = [
    path("admin/", site.urls),
]
