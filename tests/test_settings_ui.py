"""The settings screen.

The URL is declared here rather than assumed: the view is mounted by the project's
urlconf, and these tests should not depend on where. ``path`` below is the same line
the package's own :mod:`vantage.urls` uses.
"""

import json

import pytest
from django.contrib.auth.models import Permission, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import path, reverse

from vantage import site
from vantage.conf import vantage_settings
from vantage.models import MAX_UPLOAD_BYTES, SiteConfiguration
from vantage.settingsview import SECTIONS, SiteSettingsForm, SiteSettingsView, brand_report
from vantage.siteconfig import UI_EDITABLE, invalidate

urlpatterns = [
    path("admin/_/settings/", SiteSettingsView.as_view(site=site), name="settings"),
    path("admin/", site.urls),
]

pytestmark = pytest.mark.urls("tests.test_settings_ui")

EVERY_FIELD = [name for _, _, _, names in SECTIONS for name in names]


@pytest.fixture(autouse=True)
def clean_cache():
    invalidate()
    yield
    invalidate()


@pytest.fixture(autouse=True)
def media(tmp_path, settings):
    """Uploads must never land in the working tree, even when a test fails."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    return tmp_path


@pytest.fixture
def admin(db):
    return User.objects.create_superuser("boss", "boss@example.com", "x")


@pytest.fixture
def client_in(client, admin):
    client.force_login(admin)
    return client


def url() -> str:
    return reverse("settings")


def payload(**overrides) -> dict:
    """A full form post. The real form always submits every control, so tests do too."""
    data = dict.fromkeys(EVERY_FIELD, "")
    data.update(overrides)
    return data


def svg(body: str = "") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">{body}</svg>'.encode()


class TestAccess:
    def test_anonymous_is_sent_to_the_login_page(self, client, db):
        response = client.get(url())
        assert response.status_code == 302
        assert "/login" in response["Location"]

    def test_a_non_superuser_is_refused(self, client, db):
        """Staff is enough to reach the panel. It is not enough to rebrand it."""
        User.objects.create_user("editor", password="x", is_staff=True)
        client.login(username="editor", password="x")
        assert client.get(url()).status_code == 403

    def test_a_superuser_gets_in(self, client_in):
        assert client_in.get(url()).status_code == 200

    def test_a_non_superuser_cannot_post_either(self, client, db):
        """The gate is on dispatch, so it covers the write as well as the read."""
        User.objects.create_user("editor", password="x", is_staff=True)
        client.login(username="editor", password="x")
        assert client.post(url(), payload(site_title="Sneaky")).status_code == 403
        assert SiteConfiguration.objects.count() == 0

    def test_a_permission_can_be_named_instead(self, client, db):
        perm = Permission.objects.get(codename="change_siteconfiguration")
        user = User.objects.create_user("ops", password="x", is_staff=True)
        user.user_permissions.add(perm)
        client.login(username="ops", password="x")

        with override_settings(VANTAGE={"SETTINGS_PERMISSION": "vantage.change_siteconfiguration"}):
            assert client.get(url()).status_code == 200

    def test_a_callable_can_decide_instead(self, client, db):
        User.objects.create_user("ops", password="x", is_staff=True)
        client.login(username="ops", password="x")
        with override_settings(
            VANTAGE={"SETTINGS_PERMISSION": lambda request: request.user.username == "ops"}
        ):
            assert client.get(url()).status_code == 200

    def test_the_named_permission_is_actually_required(self, client, db):
        User.objects.create_user("ops", password="x", is_staff=True)
        client.login(username="ops", password="x")
        with override_settings(VANTAGE={"SETTINGS_PERMISSION": "vantage.change_siteconfiguration"}):
            assert client.get(url()).status_code == 403


class TestScreen:
    def test_every_section_is_rendered(self, client_in):
        body = client_in.get(url()).content.decode()
        for _, title, _, _ in SECTIONS:
            assert title in body

    def test_the_inherited_value_is_shown_next_to_each_field(self, client_in):
        """"Clear this field" is only safe advice if the fallback is visible."""
        with override_settings(VANTAGE={"SITE_TITLE": "Northwind"}):
            body = client_in.get(url()).content.decode()
        assert "Northwind" in body
        assert "settings.py" in body

    def test_the_code_only_settings_are_named_and_not_offered(self, client_in):
        body = client_in.get(url()).content.decode()
        assert "AUTO_REGISTER" in body
        assert 'name="auto_register"' not in body

    def test_the_form_offers_exactly_the_allowlist(self):
        form = SiteSettingsForm()
        assert set(form.fields) == set(UI_EDITABLE.values())

    def test_an_upload_field_is_restricted_in_the_markup_too(self, client_in):
        body = client_in.get(url()).content.decode()
        assert ".svg,.png,.jpg,.jpeg,.webp" in body


class TestSaving:
    def test_saving_a_title_changes_every_page(self, client_in):
        client_in.post(url(), payload(site_title="Acme Admin"))
        body = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert "Acme Admin" in body

    def test_saving_a_colour_changes_the_rendered_theme(self, client_in):
        """The point of the whole feature, asserted on the bytes the browser receives."""
        before = client_in.get(reverse("vantage:dashboard")).content.decode()
        primary_before = _primary_token(before)

        client_in.post(url(), payload(brand_color="#ef4444"))

        after = client_in.get(reverse("vantage:dashboard")).content.decode()
        assert _primary_token(after) != primary_before

    def test_a_saved_value_survives_the_redirect(self, client_in):
        response = client_in.post(url(), payload(site_title="Acme"), follow=True)
        assert response.status_code == 200
        assert "Settings saved." in response.content.decode()

    def test_the_post_redirects_rather_than_rendering(self, client_in):
        """Otherwise a refresh re-submits the upload."""
        response = client_in.post(url(), payload(site_title="Acme"))
        assert response.status_code == 302
        assert response["Location"] == url()

    def test_who_changed_it_is_recorded(self, client_in, admin):
        client_in.post(url(), payload(site_title="Acme"))
        assert SiteConfiguration.objects.get().updated_by == admin

    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py"})
    def test_clearing_a_field_hands_it_back_to_the_code(self, client_in):
        client_in.post(url(), payload(site_title="From the panel"))
        assert vantage_settings.SITE_TITLE == "From the panel"

        client_in.post(url(), payload(site_title=""))
        assert SiteConfiguration.objects.get().site_title is None
        assert vantage_settings.SITE_TITLE == "From settings.py"

    @override_settings(VANTAGE={"THEME_TOGGLE": True})
    def test_a_boolean_can_be_set_to_false_and_back_to_inherited(self, client_in):
        client_in.post(url(), payload(theme_toggle="false"))
        assert vantage_settings.THEME_TOGGLE is False

        client_in.post(url(), payload(theme_toggle=""))
        assert SiteConfiguration.objects.get().theme_toggle is None
        assert vantage_settings.THEME_TOGGLE is True

    def test_an_invalid_colour_is_refused_and_nothing_is_saved(self, client_in):
        response = client_in.post(url(), payload(brand_color="chartreuse"))
        assert response.status_code == 200
        assert "hex colour" in response.content.decode()
        assert SiteConfiguration.objects.count() == 0

    def test_an_invalid_radius_cannot_reach_the_stylesheet(self, client_in):
        response = client_in.post(url(), payload(brand_radius="1rem; } html { display:none"))
        assert response.status_code == 200
        assert SiteConfiguration.objects.count() == 0


class TestContrastGuard:
    def test_a_colour_that_had_to_move_is_accepted_and_explained(self, client_in):
        """Accept the colour — the derivation copes — but never quietly change it."""
        response = client_in.post(url(), payload(brand_color="#0d9488"), follow=True)
        body = response.content.decode()

        assert SiteConfiguration.objects.get().brand_color == "#0d9488"
        assert "WCAG AA" in body
        assert "darkened" in body or "lightened" in body

    def test_a_colour_that_did_not_move_says_nothing(self, client_in):
        body = client_in.post(url(), payload(brand_color="#6366f1"), follow=True).content.decode()
        assert "WCAG AA" not in body

    def test_the_report_agrees_with_the_palette_that_will_be_rendered(self):
        report = brand_report("#0d9488")
        assert report.adjusted is True
        assert report.ratio >= 4.5
        assert report.requested_ratio < 4.5


class TestPreview:
    def test_the_preview_returns_the_derived_palette(self, client_in):
        response = client_in.get(url(), {"preview": "#ef4444"})
        data = json.loads(response.content)
        assert data["ok"] is True
        assert data["light"][0]["name"] == "primary"
        assert data["light"][0]["value"].startswith("oklch(")

    def test_the_preview_reports_the_contrast_warning(self, client_in):
        data = json.loads(client_in.get(url(), {"preview": "#0d9488"}).content)
        assert data["adjusted"] is True
        assert "WCAG AA" in data["warning"]

    def test_junk_is_rejected_rather_than_crashing(self, client_in):
        response = client_in.get(url(), {"preview": "chartreuse"})
        assert response.status_code == 400
        assert json.loads(response.content)["ok"] is False

    def test_the_preview_needs_the_same_permission_as_the_screen(self, client, db):
        User.objects.create_user("editor", password="x", is_staff=True)
        client.login(username="editor", password="x")
        assert client.get(url(), {"preview": "#ef4444"}).status_code == 403


class TestUploads:
    def test_an_svg_containing_a_script_tag_is_rejected(self, client_in):
        """Stored XSS. The logo is on every page of the panel, so the payload would run
        in every user's session — this is the reason ``FileField`` is validated by
        hand rather than trusted."""
        upload = SimpleUploadedFile(
            "logo.svg", svg('<script>fetch("/steal?c="+document.cookie)</script>'), "image/svg+xml"
        )
        response = client_in.post(url(), payload(logo=upload))

        assert response.status_code == 200
        assert "script" in response.content.decode()
        assert SiteConfiguration.objects.count() == 0

    def test_an_svg_with_an_event_handler_is_rejected(self, client_in):
        upload = SimpleUploadedFile(
            "logo.svg", svg('<rect width="8" height="8" onmouseover="alert(1)"/>')
        )
        assert client_in.post(url(), payload(logo=upload)).status_code == 200
        assert SiteConfiguration.objects.count() == 0

    def test_a_disguised_executable_is_rejected(self, client_in):
        upload = SimpleUploadedFile("logo.html", b"<h1>hello</h1>")
        assert client_in.post(url(), payload(logo=upload)).status_code == 200
        assert SiteConfiguration.objects.count() == 0

    def test_an_oversized_upload_is_rejected(self, client_in):
        upload = SimpleUploadedFile("logo.png", b"\x89PNG" + b"0" * MAX_UPLOAD_BYTES)
        assert client_in.post(url(), payload(logo=upload)).status_code == 200
        assert SiteConfiguration.objects.count() == 0

    def test_a_clean_logo_is_stored_and_reaches_the_settings(self, client_in):
        upload = SimpleUploadedFile("logo.svg", svg('<circle cx="4" cy="4" r="3"/>'))
        client_in.post(url(), payload(logo=upload))

        assert vantage_settings.LOGO.startswith("/media/vantage/branding/logo")
        body = client_in.get(url()).content.decode()
        assert "vantage/branding/logo" in body


class TestReset:
    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py", "BRAND_COLOR": "#ef4444"})
    def test_a_section_resets_only_its_own_fields(self, client_in):
        client_in.post(url(), payload(site_title="Overridden", brand_color="#123456"))

        client_in.post(url(), payload(reset="branding"))

        config = SiteConfiguration.objects.get()
        assert config.site_title is None
        assert config.brand_color == "#123456"
        assert vantage_settings.SITE_TITLE == "From settings.py"

    def test_resetting_removes_the_uploaded_bytes_too(self, client_in, media):
        upload = SimpleUploadedFile("logo.svg", svg('<circle cx="4" cy="4" r="3"/>'))
        client_in.post(url(), payload(logo=upload))
        stored = media / SiteConfiguration.objects.get().logo.name
        assert stored.exists()

        client_in.post(url(), payload(reset="branding"))

        assert not stored.exists()
        assert SiteConfiguration.objects.get().logo.name in ("", None)

    def test_resetting_with_no_row_is_harmless(self, client_in):
        assert client_in.post(url(), payload(reset="branding")).status_code == 302
        assert SiteConfiguration.objects.count() == 0

    def test_an_unknown_section_changes_nothing(self, client_in):
        client_in.post(url(), payload(site_title="Acme"))
        client_in.post(url(), payload(reset="nonsense"))
        assert SiteConfiguration.objects.get().site_title == "Acme"


def _primary_token(html: str) -> str:
    for line in html.splitlines():
        if line.strip().startswith("--primary:"):
            return line.strip()
    raise AssertionError("the rendered page carries no --primary token")
