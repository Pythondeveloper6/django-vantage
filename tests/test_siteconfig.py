"""The database layer of the settings cascade.

The cascade is ``SiteConfiguration row > settings.VANTAGE > DEFAULTS``, and the whole
value of this feature is that no consumer knows the first step exists. So every
assertion here goes through ``vantage_settings.X`` — the same attribute access the rest
of the package uses — rather than through the resolution functions directly.
"""

import io
from contextlib import contextmanager

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings

from tests.conftest import sqlite_only
from vantage.conf import DEFAULTS, vantage_settings
from vantage.models import (
    MAX_UPLOAD_BYTES,
    SiteConfiguration,
    validate_brand_color,
    validate_branding_upload,
    validate_css_length,
)
from vantage.siteconfig import UI_EDITABLE, code_defaults, database_overrides, invalidate


@pytest.fixture(autouse=True)
def clean_cache():
    """No test may inherit another's resolved settings.

    The overrides are cached for the process between requests, and a test that saves a
    row would otherwise leave that row's values visible to the next test — after the
    transaction that created it had already rolled the row away.
    """
    invalidate()
    yield
    invalidate()


@pytest.fixture
def media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.MEDIA_URL = "/media/"
    return tmp_path


def svg(body: str = "") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">{body}</svg>'.encode()


class TestResolutionOrder:
    def test_nothing_stored_leaves_the_defaults_alone(self, db):
        assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]

    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py"})
    def test_the_file_wins_over_the_defaults(self, db):
        assert vantage_settings.SITE_TITLE == "From settings.py"

    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py"})
    def test_the_database_wins_over_the_file(self, db):
        SiteConfiguration(site_title="From the database").save()
        assert vantage_settings.SITE_TITLE == "From the database"

    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py"})
    def test_an_unset_column_falls_through_to_the_file(self, db):
        """A row that overrides one setting must not blank the other thirteen."""
        SiteConfiguration(brand_color="#ef4444").save()
        assert vantage_settings.SITE_TITLE == "From settings.py"
        assert vantage_settings.BRAND_COLOR == "#ef4444"

    @override_settings(VANTAGE={"ENVIRONMENT": "Production"})
    def test_an_empty_string_is_an_override_and_null_is_not(self, db):
        """The distinction the whole schema is built on.

        Blanking the banner is a thing a site owner can want; it must not be confused
        with never having touched the field.
        """
        SiteConfiguration(environment="").save()
        assert vantage_settings.ENVIRONMENT == ""

        SiteConfiguration(environment=None).save()
        assert vantage_settings.ENVIRONMENT == "Production"

    def test_stored_booleans_can_be_false(self, db):
        """False and NULL are different answers, and ``if value:`` conflates them."""
        SiteConfiguration(theme_toggle=False).save()
        assert vantage_settings.THEME_TOGGLE is False

    def test_derived_values_are_computed_from_the_stored_ones(self, db):
        SiteConfiguration(site_title="Acme Corporation").save()
        assert vantage_settings.SITE_ACRONYM == "AC"

    def test_a_stored_logo_supplies_the_dark_logo_too(self, db, media):
        SiteConfiguration(logo=SimpleUploadedFile("logo.png", b"\x89PNG")).save()
        assert vantage_settings.LOGO_DARK == vantage_settings.LOGO

    @sqlite_only
    def test_a_nonsense_stored_choice_is_ignored_rather_than_served(self, db):
        """A hand-edited row must not be able to emit an invalid theme attribute."""
        SiteConfiguration.objects.create()
        SiteConfiguration.objects.filter(pk=1).update(theme="chartreuse")
        invalidate()
        assert vantage_settings.THEME == DEFAULTS["THEME"]

    def test_a_nonsense_stored_page_size_is_ignored(self, db):
        SiteConfiguration.objects.create()
        SiteConfiguration.objects.filter(pk=1).update(default_per_page=0)
        invalidate()
        assert vantage_settings.DEFAULT_PER_PAGE == DEFAULTS["DEFAULT_PER_PAGE"]


class TestCaching:
    def test_saving_the_row_is_visible_immediately(self, db):
        """No restart, no cache timeout: the next read sees it."""
        assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]
        SiteConfiguration(site_title="Acme").save()
        assert vantage_settings.SITE_TITLE == "Acme"

    def test_changing_the_row_again_is_visible_immediately(self, db):
        config = SiteConfiguration(site_title="First")
        config.save()
        assert vantage_settings.SITE_TITLE == "First"
        config.site_title = "Second"
        config.save()
        assert vantage_settings.SITE_TITLE == "Second"

    def test_deleting_the_row_returns_everything_to_the_file(self, db):
        config = SiteConfiguration(site_title="Acme")
        config.save()
        assert vantage_settings.SITE_TITLE == "Acme"
        config.delete()
        assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]

    def test_the_row_is_read_once_and_then_held(self, db, django_assert_num_queries):
        SiteConfiguration(site_title="Acme").save()
        with django_assert_num_queries(1):
            for _ in range(5):
                assert vantage_settings.SITE_TITLE == "Acme"


@contextmanager
def hidden_table():
    """The pre-``migrate`` state, reproduced inside a test.

    Renamed rather than dropped so the suite gets the table back even if an assertion
    fails, and by raw SQL rather than the schema editor because SQLite refuses schema
    edits inside the transaction every test already runs in.
    """
    quoted = connection.ops.quote_name(SiteConfiguration._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE {quoted} RENAME TO vantage_siteconfiguration_hidden")
    invalidate()
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE vantage_siteconfiguration_hidden RENAME TO {quoted}")


@sqlite_only
class TestDatabaseUnavailable:
    def test_a_missing_table_falls_through_silently(self, db):
        """``manage.py migrate`` on an empty database reads settings before this table
        exists. Raising here makes the migration that would create it unrunnable."""
        with hidden_table():
            assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]
            assert database_overrides() == {}

    @override_settings(VANTAGE={"SITE_TITLE": "From settings.py", "BRAND_COLOR": "#ef4444"})
    def test_the_file_settings_still_apply_with_no_table(self, db):
        with hidden_table():
            assert vantage_settings.SITE_TITLE == "From settings.py"
            assert vantage_settings.BRAND_COLOR == "#ef4444"

    def test_the_miss_is_not_cached(self, db):
        """A process that started before ``migrate`` must notice the table appearing.

        Nothing is invalidated on the way out of the block: if the unavailable read had
        been cached, the save below would be invisible.
        """
        with hidden_table():
            assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]

        SiteConfiguration(site_title="Acme").save()
        assert vantage_settings.SITE_TITLE == "Acme"

    def test_a_test_without_a_database_still_resolves_settings(self):
        """No ``db`` fixture: the connection is blocked outright, and that is fine.

        Hundreds of tests in this suite read settings without asking for a database.
        A settings layer that needs one would fail every single one of them.
        """
        assert vantage_settings.SITE_TITLE == DEFAULTS["SITE_TITLE"]
        assert database_overrides() == {}


class TestAllowlist:
    def test_every_editable_key_is_a_real_setting(self):
        assert set(UI_EDITABLE) <= set(DEFAULTS)

    def test_every_editable_key_has_a_column(self):
        columns = {field.name for field in SiteConfiguration._meta.get_fields()}
        assert set(UI_EDITABLE.values()) <= columns

    @pytest.mark.parametrize(
        "key",
        [
            "AUTO_REGISTER",
            "EXCLUDE",
            "INCLUDE_ONLY",
            "NAV",
            "NAV_UNLISTED",
            "DASHBOARD",
            "PERMISSIONS",
            "SETTINGS_PERMISSION",
            "AUDIT_LOG",
            "CUSTOM_CSS",
        ],
    )
    def test_code_only_settings_have_no_column(self, key):
        """These decide what exists and who may reach it. A web form is the wrong place.

        ``SETTINGS_PERMISSION`` most of all: editable, it would let whoever holds it
        hand the same power to anyone else.
        """
        assert key in DEFAULTS
        assert key not in UI_EDITABLE


class TestCodeDefaults:
    @override_settings(VANTAGE={"SITE_TITLE": "Acme Corporation"})
    def test_reports_the_cascade_without_the_database(self, db):
        SiteConfiguration(site_title="Overridden").save()
        assert vantage_settings.SITE_TITLE == "Overridden"
        assert code_defaults()["SITE_TITLE"] == "Acme Corporation"

    @override_settings(VANTAGE={"SITE_TITLE": "Acme Corporation"})
    def test_derives_the_same_values_the_panel_would(self):
        assert code_defaults()["SITE_ACRONYM"] == "AC"


class TestUploadValidation:
    def test_an_svg_containing_a_script_tag_is_rejected(self):
        """Stored XSS, and the logo is on every page — so it would fire for everyone.

        The file is served from the panel's own origin, which means a script inside it
        runs with the viewer's session.
        """
        upload = SimpleUploadedFile(
            "logo.svg", svg('<script>fetch("/steal")</script>'), "image/svg+xml"
        )
        with pytest.raises(ValidationError, match="script"):
            validate_branding_upload(upload)

    def test_an_svg_with_an_inline_event_handler_is_rejected(self):
        upload = SimpleUploadedFile(
            "logo.svg", svg('<rect onload="alert(1)" width="8" height="8"/>'), "image/svg+xml"
        )
        with pytest.raises(ValidationError, match="event handler"):
            validate_branding_upload(upload)

    def test_an_svg_animating_an_event_handler_into_place_is_rejected(self):
        upload = SimpleUploadedFile(
            "logo.svg", svg('<set attributeName="onload" to="alert(1)"/>'), "image/svg+xml"
        )
        with pytest.raises(ValidationError):
            validate_branding_upload(upload)

    def test_an_svg_with_a_javascript_url_is_rejected(self):
        upload = SimpleUploadedFile(
            "logo.svg", svg('<a href="javascript:alert(1)"><rect/></a>'), "image/svg+xml"
        )
        with pytest.raises(ValidationError, match="javascript"):
            validate_branding_upload(upload)

    def test_an_ordinary_svg_is_accepted(self):
        upload = SimpleUploadedFile("logo.svg", svg('<circle cx="4" cy="4" r="3"/>'))
        validate_branding_upload(upload)

    def test_the_file_is_left_rewound_for_whoever_stores_it(self):
        """Reading it to scan it must not consume it."""
        payload = svg('<circle cx="4" cy="4" r="3"/>')
        upload = SimpleUploadedFile("logo.svg", payload)
        validate_branding_upload(upload)
        assert upload.read() == payload

    @pytest.mark.parametrize("name", ["logo.exe", "logo.html", "logo.php", "logo", "logo.gif"])
    def test_an_extension_off_the_allowlist_is_rejected(self, name):
        with pytest.raises(ValidationError):
            validate_branding_upload(SimpleUploadedFile(name, b"whatever"))

    def test_an_oversized_file_is_rejected(self):
        payload = b"\x89PNG" + b"0" * MAX_UPLOAD_BYTES
        with pytest.raises(ValidationError, match="limit"):
            validate_branding_upload(SimpleUploadedFile("logo.png", payload))

    def test_a_stored_file_is_not_re_read_on_every_clean(self, db, media):
        """Validation runs on arrival. Re-running it would download the file to
        validate it, on every form render."""
        config = SiteConfiguration(logo=SimpleUploadedFile("logo.png", b"\x89PNG"))
        config.save()
        config.refresh_from_db()
        config.full_clean()

    def test_a_shell_save_is_validated_too(self, db, media):
        """The check is on the field, so it is not something the form can be routed
        around."""
        config = SiteConfiguration(
            logo=SimpleUploadedFile("logo.svg", svg("<script>alert(1)</script>"))
        )
        with pytest.raises(ValidationError):
            config.full_clean()


class TestValueValidation:
    def test_a_brand_colour_must_be_one_the_palette_can_read(self):
        with pytest.raises(ValidationError, match="hex colour"):
            validate_brand_color("chartreuse")

    def test_a_hex_brand_colour_passes(self):
        validate_brand_color("#0d9488")

    @pytest.mark.parametrize("value", ["0.5rem", "8px", "0", "1.25em", "50%"])
    def test_a_css_length_passes(self, value):
        validate_css_length(value)

    @pytest.mark.parametrize("value", ["0.5rem; } body { display: none", "huge", "10"])
    def test_anything_that_is_not_a_css_length_is_rejected(self, value):
        """The radius is interpolated into a ``<style>`` block, not into text."""
        with pytest.raises(ValidationError):
            validate_css_length(value)


class TestSingleton:
    def test_there_is_only_ever_one_row(self, db):
        SiteConfiguration(site_title="First").save()
        SiteConfiguration(site_title="Second").save()
        assert SiteConfiguration.objects.count() == 1
        assert SiteConfiguration.objects.get().site_title == "Second"

    def test_loading_does_not_create_a_row(self, db):
        """Opening the settings screen is a read."""
        SiteConfiguration.load()
        assert SiteConfiguration.objects.count() == 0

    def test_loading_returns_the_row_when_there_is_one(self, db):
        SiteConfiguration(site_title="Acme").save()
        assert SiteConfiguration.load().site_title == "Acme"


class TestStoredFiles:
    def test_a_stored_logo_resolves_to_its_url(self, db, media):
        SiteConfiguration(logo=SimpleUploadedFile("logo.svg", svg())).save()
        assert vantage_settings.LOGO.startswith("/media/vantage/branding/logo")

    def test_no_upload_falls_through_to_the_file_setting(self, db, media):
        with override_settings(VANTAGE={"LOGO": "img/logo.svg"}):
            SiteConfiguration(site_title="Acme").save()
            assert vantage_settings.LOGO == "img/logo.svg"

    def test_the_bytes_survive_the_round_trip(self, db, media):
        payload = svg('<circle cx="4" cy="4" r="3"/>')
        config = SiteConfiguration(logo=SimpleUploadedFile("logo.svg", payload))
        config.save()
        with config.logo.open("rb") as stored:
            assert stored.read() == payload

    def test_a_file_object_without_a_name_is_rejected(self):
        upload = io.BytesIO(b"nope")
        upload.name = ""
        with pytest.raises(ValidationError):
            validate_branding_upload(upload)
