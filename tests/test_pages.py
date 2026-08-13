"""Custom pages: anything that is not a model.

The bar this API has to clear is that the *first* page someone writes is cheap — a
class, a title, a method. Most of these tests are about what you get for free.
"""

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import override_settings
from django.urls import reverse

from tests.testapp.pages import (
    BareMinimumPage,
    CallableGatedPage,
    CustomPathPage,
    GatedPage,
    RevenuePage,
)
from vantage.pages import Page, page_slug
from vantage.sites import VantageSite


class TestSlugAndPath:
    def test_slug_drops_the_page_suffix(self):
        assert page_slug(RevenuePage) == "revenue"

    def test_camel_case_becomes_hyphenated(self):
        class QuarterlyRevenueReportPage(Page):
            pass

        assert page_slug(QuarterlyRevenueReportPage) == "quarterly-revenue-report"

    def test_a_class_not_ending_in_page_keeps_its_whole_name(self):
        class Console(Page):
            pass

        assert page_slug(Console) == "console"

    def test_path_defaults_to_the_slug(self):
        assert RevenuePage.get_path() == "revenue/"

    def test_an_explicit_path_wins(self):
        assert CustomPathPage.get_path() == "ops/console/"

    def test_a_slug_override_wins(self):
        class Weird(Page):
            slug_override = "my-thing"

        assert Weird.get_slug() == "my-thing"
        assert Weird.get_path() == "my-thing/"

    def test_title_falls_back_to_the_slug(self):
        assert BareMinimumPage.get_title() == "Bare minimum"


class TestPermission:
    def test_open_by_default(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_user("plain", password="x", is_staff=True)
        assert RevenuePage().has_permission(request) is True

    def test_a_permission_string_is_checked(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_user("plain", password="x", is_staff=True)
        assert GatedPage().has_permission(request) is False

    def test_a_superuser_passes_a_permission_string(self, rf, db):
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        assert GatedPage().has_permission(request) is True

    def test_a_callable_can_gate_on_anything(self, rf, db):
        """So a page can depend on a feature flag without inventing a permission."""
        request = rf.get("/")
        request.user = User.objects.create_superuser("boss", "b@example.com", "x")
        assert CallableGatedPage().has_permission(request) is True
        request.user = User.objects.create_user("other", password="x", is_staff=True)
        assert CallableGatedPage().has_permission(request) is False


class TestTemplateResolution:
    def test_prefers_a_project_template_then_the_generic_one(self):
        assert RevenuePage().get_template_names() == [
            "vantage/pages/revenue.html",
            "vantage/page.html",
        ]

    def test_an_explicit_template_wins(self):
        class Templated(Page):
            template_name = "custom/thing.html"

        assert Templated().get_template_names() == ["custom/thing.html"]


class TestRegistration:
    def test_register_page_returns_the_class_unchanged(self):
        site = VantageSite("scratch")

        @site.register_page
        class Thing(Page):
            title = "Thing"

        assert Thing.title == "Thing"
        assert Thing in site.pages()


# ── integration: a page mounted on the real site ────────────────────────────


@pytest.fixture
def boss(client, db):
    client.force_login(User.objects.create_superuser("boss", "b@example.com", "x"))
    return client


class TestMountedPages:
    def test_a_page_gets_a_url_without_touching_a_urlconf(self, boss):
        assert boss.get(reverse("vantage:page-revenue")).status_code == 200

    def test_the_custom_path_is_honoured(self, boss):
        """The URL *name* still comes from the slug; only the segment is overridden."""
        url = reverse("vantage:page-custom-path")
        assert url.endswith("/ops/console/")
        assert boss.get(url).status_code == 200

    def test_context_reaches_the_template(self, boss):
        body = boss.get(reverse("vantage:page-revenue")).content.decode()
        assert "1234" in body
        assert "Total" in body

    def test_the_page_appears_in_the_sidebar(self, boss):
        body = boss.get(reverse("vantage:dashboard")).content.decode()
        assert "Revenue" in body
        assert reverse("vantage:page-revenue") in body

    def test_a_page_with_no_template_still_renders_usefully(self, boss):
        """The whole point: the first page you write costs a class and a method."""
        response = boss.get(reverse("vantage:page-bare-minimum"))
        assert response.status_code == 200
        assert "Bare minimum" in response.content.decode()

    def test_a_page_with_no_data_says_so_rather_than_rendering_blank(self, boss):
        body = boss.get(reverse("vantage:page-bare-minimum")).content.decode()
        assert "get_context" in body

    def test_the_page_uses_the_panel_shell(self, boss):
        body = boss.get(reverse("vantage:page-revenue")).content.decode()
        assert "v-sidebar" in body
        assert "--background:" in body

    def test_a_gated_page_is_refused(self, client, db):
        client.force_login(User.objects.create_user("plain", password="x", is_staff=True))
        assert client.get(reverse("vantage:page-gated")).status_code == 403

    def test_an_anonymous_user_is_redirected(self, client, db):
        assert client.get(reverse("vantage:page-revenue")).status_code == 302

    @override_settings(
        VANTAGE={
            "NAV": [{"group": "Reports", "items": [{"page": "revenue", "label": "Money"}]}],
            "NAV_UNLISTED": "hide",
        }
    )
    def test_a_page_can_be_placed_and_renamed_from_settings(self, boss):
        body = boss.get(reverse("vantage:page-revenue")).content.decode()
        assert "Money" in body


def test_permission_denied_message_names_the_page(rf, db):
    from vantage.views import PageView

    request = rf.get("/")
    request.user = User.objects.create_user("plain", password="x", is_staff=True)
    view = PageView(site=None, page_class=GatedPage)
    view.request = request
    with pytest.raises(PermissionDenied, match="Gated"):
        view.check_permission(request)
