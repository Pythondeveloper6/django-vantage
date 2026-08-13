"""The settings layer: defaults, overrides, and loud failure on typos."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from vantage.conf import vantage_settings


def test_defaults_available_without_any_configuration():
    assert vantage_settings.SITE_TITLE == "Django Vantage"
    assert vantage_settings.THEME == "system"
    assert vantage_settings.AUTO_REGISTER is True
    assert vantage_settings.DEFAULT_PER_PAGE == 50


@override_settings(VANTAGE={"SITE_TITLE": "Acme"})
def test_override_replaces_only_the_named_key():
    assert vantage_settings.SITE_TITLE == "Acme"
    # Every other default must survive a partial override.
    assert vantage_settings.THEME == "system"
    assert vantage_settings.BRAND_COLOR == "#6366f1"


@override_settings(VANTAGE={"SITE_TITLE": "First"})
def test_settings_react_to_later_overrides():
    assert vantage_settings.SITE_TITLE == "First"
    with override_settings(VANTAGE={"SITE_TITLE": "Second"}):
        assert vantage_settings.SITE_TITLE == "Second"
    assert vantage_settings.SITE_TITLE == "First"


def test_unknown_key_raises_rather_than_being_ignored():
    """A typo in a setting name must fail loudly, not silently do nothing."""
    with override_settings(VANTAGE={"BRAND_COLOUR": "#fff"}):
        with pytest.raises(ImproperlyConfigured) as exc:
            _ = vantage_settings.SITE_TITLE
    assert "BRAND_COLOUR" in str(exc.value)


def test_unknown_key_error_suggests_the_intended_name():
    with override_settings(VANTAGE={"BRAND_COLOUR": "#fff"}):
        with pytest.raises(ImproperlyConfigured) as exc:
            _ = vantage_settings.SITE_TITLE
    assert "BRAND_COLOR" in str(exc.value)


def test_accessing_an_undefined_attribute_is_an_error():
    with pytest.raises(AttributeError):
        _ = vantage_settings.NOT_A_SETTING


@override_settings(VANTAGE={"SITE_TITLE": "Acme Corporation"})
def test_site_acronym_derives_from_title_when_unset():
    assert vantage_settings.SITE_ACRONYM == "AC"


@override_settings(VANTAGE={"SITE_TITLE": "Acme", "SITE_ACRONYM": "ACME"})
def test_explicit_site_acronym_wins():
    assert vantage_settings.SITE_ACRONYM == "ACME"


@override_settings(VANTAGE={"LOGO": "img/logo.svg"})
def test_dark_logo_falls_back_to_light_logo():
    assert vantage_settings.LOGO_DARK == "img/logo.svg"


@override_settings(VANTAGE={"LOGO": "img/logo.svg", "LOGO_DARK": "img/dark.svg"})
def test_explicit_dark_logo_wins():
    assert vantage_settings.LOGO_DARK == "img/dark.svg"
