"""The pages a project no longer has to write: sign in, sign out, password, errors.

This module doubles as its own urlconf. Mounting ``vantage.authviews`` is the thing
under test, so the test suite has to mount it the way a project would — and doing that
here rather than in ``tests/urls.py`` keeps the rest of the suite on a urlconf with no
auth routes, which is also a real configuration the package must survive.
"""

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.template import loader
from django.test import RequestFactory, override_settings
from django.urls import include, path, reverse

from vantage import authviews, site


def _denied(request):
    """Stands in for any view that refuses. Deterministic, unlike a permission fixture."""
    raise PermissionDenied("You do not have permission to view orders.")


urlpatterns = [
    path("accounts/", include("vantage.authviews")),
    path("denied/", _denied),
    path("admin/", site.urls),
]

handler403 = "vantage.authviews.permission_denied"
handler404 = "vantage.authviews.page_not_found"
handler500 = "vantage.authviews.server_error"

@pytest.fixture(autouse=True)
def _mounted(settings):
    """Every test in this module runs against the urlconf declared above."""
    settings.ROOT_URLCONF = "tests.test_auth_pages"


@pytest.fixture
def staff(db):
    return User.objects.create_superuser("staffer", "staff@example.com", "correct-horse")


class TestMounting:
    def test_the_routes_keep_djangos_own_names(self):
        """A drop-in replacement for django.contrib.auth.urls, or it is not one line."""
        assert reverse("login") == "/accounts/login/"
        assert reverse("logout") == "/accounts/logout/"
        assert reverse("password_change") == "/accounts/password_change/"

    def test_the_module_declares_no_app_name(self):
        """An app_name would namespace the routes and break every {% url 'login' %}."""
        assert not hasattr(authviews, "app_name")


class TestLogin:
    def test_renders(self, client, db):
        response = client.get("/accounts/login/")
        assert response.status_code == 200
        assert "vantage/login.html" in [t.name for t in response.templates]

    def test_is_branded_from_settings(self, client, db):
        with override_settings(VANTAGE={"SITE_TITLE": "Northwind", "BRAND_COLOR": "#0d9488"}):
            body = client.get("/accounts/login/").content.decode()
        assert "Northwind" in body
        # The theme stylesheet is inlined per request, so the brand really did reach it.
        assert "--primary:" in body

    def test_carries_the_no_flash_script(self, client, db):
        """Without it the sign-in page paints light and flips, before the panel does."""
        assert "vantage-theme" in client.get("/accounts/login/").content.decode()

    def test_shows_the_login_image_only_when_configured(self, client, db):
        assert "v-auth-art" not in client.get("/accounts/login/").content.decode()
        with override_settings(VANTAGE={"LOGIN_IMAGE": "/media/side.jpg"}):
            body = client.get("/accounts/login/").content.decode()
        assert "v-auth-art" in body
        assert "/media/side.jpg" in body

    def test_inputs_get_the_panel_class(self, client, db):
        assert 'class="v-input"' in client.get("/accounts/login/").content.decode()

    def test_a_bad_password_is_announced_not_merely_coloured(self, client, db):
        User.objects.create_user("someone", password="correct-horse")
        response = client.post(
            "/accounts/login/", {"username": "someone", "password": "wrong"}
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert 'role="alert"' in body
        assert "Sign-in failed" in body

    def test_a_failed_field_is_marked_invalid_for_a_screen_reader(self, client, db):
        response = client.post("/accounts/login/", {"username": "", "password": ""})
        body = response.content.decode()
        assert 'aria-invalid="true"' in body
        assert 'aria-describedby="id_username_error"' in body
        assert 'id="id_username_error"' in body

    def test_a_correct_password_gets_in(self, client, db):
        User.objects.create_user("someone", password="correct-horse", is_staff=True)
        response = client.post(
            "/accounts/login/",
            {"username": "someone", "password": "correct-horse", "next": "/admin/"},
        )
        assert response.status_code == 302
        assert response["Location"] == "/admin/"

    def test_an_authenticated_visitor_is_sent_on(self, client, staff):
        client.force_login(staff)
        assert client.get("/accounts/login/").status_code == 302

    def test_the_panel_redirects_anonymous_visitors_here(self, client, db):
        response = client.get("/admin/")
        assert response.status_code == 302
        assert response["Location"].startswith("/accounts/login/")


class TestLogout:
    def test_renders_a_confirmation(self, client, staff):
        client.force_login(staff)
        response = client.post("/accounts/logout/")
        assert response.status_code == 200
        assert "Signed out" in response.content.decode()

    def test_actually_signs_the_user_out(self, client, staff):
        client.force_login(staff)
        client.post("/accounts/logout/")
        assert client.get("/admin/").status_code == 302

    def test_offers_the_way_back_in(self, client, staff):
        client.force_login(staff)
        assert "/accounts/login/" in client.post("/accounts/logout/").content.decode()


class TestPasswordChange:
    def test_renders_inside_the_shell(self, client, staff):
        client.force_login(staff)
        response = client.get("/accounts/password_change/")
        assert response.status_code == 200
        names = [t.name for t in response.templates]
        assert "vantage/password_change.html" in names
        assert "vantage/base.html" in names

    def test_anonymous_visitors_are_sent_to_login(self, client, db):
        response = client.get("/accounts/password_change/")
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_changes_the_password(self, client, staff):
        client.force_login(staff)
        response = client.post(
            "/accounts/password_change/",
            {
                "old_password": "correct-horse",
                "new_password1": "battery-staple-99",
                "new_password2": "battery-staple-99",
            },
        )
        assert response.status_code == 302
        staff.refresh_from_db()
        assert staff.check_password("battery-staple-99")

    def test_confirms_with_a_message_rather_than_a_second_page(self, client, staff):
        client.force_login(staff)
        client.post(
            "/accounts/password_change/",
            {
                "old_password": "correct-horse",
                "new_password1": "battery-staple-99",
                "new_password2": "battery-staple-99",
            },
        )
        assert "Your password has been changed." in client.get("/admin/").content.decode()

    def test_a_mismatch_is_reported_on_the_field(self, client, staff):
        client.force_login(staff)
        response = client.post(
            "/accounts/password_change/",
            {
                "old_password": "correct-horse",
                "new_password1": "battery-staple-99",
                "new_password2": "something-else-11",
            },
        )
        assert response.status_code == 200
        assert 'aria-invalid="true"' in response.content.decode()


class TestErrorPages:
    def test_404_renders_in_the_shell(self, client, db):
        response = client.get("/no/such/page/")
        assert response.status_code == 404
        body = response.content.decode()
        assert "Error 404" in body
        assert "v-sidebar" in body  # it really is the panel shell

    def test_403_says_not_allowed_rather_than_not_found(self, client, db):
        response = client.get("/denied/")
        assert response.status_code == 403
        body = response.content.decode()
        assert "Error 403" in body
        # Spec §15: telling someone a thing does not exist when they merely cannot see
        # it is a lie that generates support tickets.
        assert "You do not have permission to view orders." in body

    def test_500_renders_with_no_context_at_all(self):
        """The point of the 500 page. No request, no context processors, no database.

        ``Template.render()`` with no argument builds a plain Context, so nothing in
        the file may reach for ``settings``, ``user``, ``request`` or a token.
        """
        html = loader.get_template("vantage/500.html").render()
        assert "Error 500" in html
        assert "Something went wrong" in html
        assert "<html" in html

    def test_500_inlines_its_own_styling(self):
        """No static tag: the file that renders when everything else is broken cannot
        depend on the static pipeline having been collected, on the token stylesheet
        having been generated, or on a urlconf still resolving."""
        source = loader.get_template("vantage/500.html").template.source
        assert "{% static" not in source
        assert "{% url" not in source
        assert "{% load" not in source
        assert "<style>" in source

    def test_the_500_handler_returns_a_500(self):
        response = authviews.server_error(RequestFactory().get("/boom/"))
        assert response.status_code == 500
        assert b"Something went wrong" in response.content

    def test_a_shell_that_cannot_render_still_produces_an_error_page(self, monkeypatch):
        """An unmounted panel, a dead database, a broken nav — any of them make the
        shell raise while rendering a 404.

        An error page that errors is the one failure with no floor under it, so the
        handler drops to the self-contained template rather than to a stack trace.
        """

        def boom(*args, **kwargs):
            raise RuntimeError("the database is gone")

        monkeypatch.setattr(authviews, "shell_context", boom)
        response = authviews.page_not_found(RequestFactory().get("/nope/"))
        assert response.status_code == 404
        assert b"We could not find that page" in response.content
        assert b"v-sidebar" not in response.content
