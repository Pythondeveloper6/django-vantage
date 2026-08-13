"""URLs for the demo.

The panel is mounted at ``/admin/``. Everything else here exists only to give it a
login page to redirect anonymous visitors to.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from vantage import site
from vantage.conf import vantage_settings
from vantage.theme import NO_FLASH_SCRIPT, render_theme_css


class DemoLoginView(auth_views.LoginView):
    """Django's login view, handed the few context values the panel's stylesheet wants.

    The token stylesheet is rendered per request rather than captured at import time,
    so changing ``BRAND_COLOR`` in settings restyles the login page on the next
    runserver reload along with the rest of the panel.
    """

    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            vantage=vantage_settings,
            theme_css=render_theme_css(),
            no_flash_script=NO_FLASH_SCRIPT,
        )
        return context


urlpatterns = [
    path("", RedirectView.as_view(url="/admin/")),
    path("accounts/login/", DemoLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("admin/", site.urls),
]

if settings.DEBUG:
    # Uploaded branding assets. Django serves these only with DEBUG on;
    # a real deployment puts them behind its own web server.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
