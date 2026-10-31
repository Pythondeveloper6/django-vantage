"""The API stability surface, deprecations, and ``manage.py vantage_upgrade_check``."""

import json
import re
import tomllib
import typing
import warnings
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from tests.testapp.models import Post
from vantage import deprecation, upgrade
from vantage.conf import DEFAULTS, vantage_settings
from vantage.deprecation import Deprecation, VantageDeprecationWarning, parse_version
from vantage.panels import ModelPanel
from vantage.sites import VantageSite

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    out = StringIO()
    try:
        call_command("vantage_upgrade_check", *args, stdout=out)
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue()


class TestTheSurfaceIsClassified:
    """Adding a setting or attribute without deciding whether it is stable fails here."""

    def test_every_setting_is_stable_or_provisional(self):
        classified = deprecation.STABLE_SETTINGS | deprecation.PROVISIONAL_SETTINGS
        assert set(DEFAULTS) - classified == set(), "classify new settings in deprecation.py"
        assert classified - set(DEFAULTS) == set(), "remove settings that no longer exist"

    def test_no_setting_is_both(self):
        assert deprecation.STABLE_SETTINGS & deprecation.PROVISIONAL_SETTINGS == set()

    def test_every_panel_attribute_is_stable_or_provisional(self):
        declared = set(typing.get_type_hints(ModelPanel, include_extras=False))
        declared -= {"model", "site", "opts"}
        classified = deprecation.STABLE_PANEL_ATTRIBUTES | deprecation.PROVISIONAL_PANEL_ATTRIBUTES
        assert declared - classified == set(), "classify new panel attributes in deprecation.py"
        assert classified - declared == set()

    def test_no_panel_attribute_is_both(self):
        assert (
            deprecation.STABLE_PANEL_ATTRIBUTES & deprecation.PROVISIONAL_PANEL_ATTRIBUTES == set()
        )

    def test_the_supported_django_range_matches_the_package_metadata(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        (django_spec,) = [d for d in project["project"]["dependencies"] if d.startswith("Django")]
        low, high = re.findall(r"(\d+)\.(\d+)", django_spec)
        assert upgrade.SUPPORTED_DJANGO == (tuple(map(int, low)), tuple(map(int, high)))


class TestVersions:
    @pytest.mark.parametrize(
        ("older", "newer"),
        [("0.2.0a1", "0.2.0a2"), ("0.2.0a2", "0.2.0b1"), ("0.2.0rc1", "0.2.0"), ("0.2", "0.10.0")],
    )
    def test_ordering(self, older, newer):
        assert parse_version(older) < parse_version(newer)

    def test_next_sorts_last(self):
        assert parse_version("99.0.0") < parse_version("next")

    def test_garbage_is_rejected(self):
        with pytest.raises(ValueError):
            parse_version("latest")

    def test_notes_are_in_order(self):
        names = [name for name, _ in deprecation.UPGRADE_NOTES]
        assert names == sorted(names, key=parse_version)


@pytest.fixture
def renamed_setting(monkeypatch):
    monkeypatch.setitem(
        deprecation.DEPRECATED_SETTINGS,
        "SITE_NAME",
        Deprecation("SITE_NAME", since="0.3.0", removed_in="1.0.0", replacement="SITE_TITLE"),
    )
    vantage_settings.reset()
    yield
    vantage_settings.reset()


class TestDeprecatedSettings:
    def test_a_renamed_setting_keeps_working_with_a_warning(self, renamed_setting):
        with override_settings(VANTAGE={"SITE_NAME": "Back office"}):
            with pytest.warns(VantageDeprecationWarning, match="SITE_NAME.*SITE_TITLE"):
                assert vantage_settings.SITE_TITLE == "Back office"

    def test_the_new_name_wins_when_both_are_set(self, renamed_setting):
        with override_settings(VANTAGE={"SITE_NAME": "Old", "SITE_TITLE": "New"}):
            with pytest.warns(VantageDeprecationWarning):
                assert vantage_settings.SITE_TITLE == "New"

    def test_the_warning_class_is_a_deprecation_warning(self):
        assert issubclass(VantageDeprecationWarning, DeprecationWarning)

    def test_the_command_reports_it(self, renamed_setting):
        with override_settings(VANTAGE={"SITE_NAME": "Back office"}):
            code, out = run("--skip-database", "--fail-level", "warning")
        assert code == 1
        assert "U002" in out
        assert "SITE_TITLE" in out


class TestDeprecatedPanelAttributes:
    def test_a_renamed_attribute_is_carried_over_with_a_warning(self, monkeypatch):
        monkeypatch.setitem(
            deprecation.DEPRECATED_PANEL_ATTRIBUTES,
            "search",
            Deprecation("search", since="0.3.0", removed_in="1.0.0", replacement="list_search"),
        )
        with pytest.warns(VantageDeprecationWarning, match="search"):
            cls = type("LegacyPanel", (ModelPanel,), {"search": ["title"]})
        assert cls(Post).get_list_search() == ["title"]

    def test_an_undeclared_attribute_does_not_warn(self, monkeypatch):
        monkeypatch.setitem(
            deprecation.DEPRECATED_PANEL_ATTRIBUTES,
            "search",
            Deprecation("search", since="0.3.0", removed_in="1.0.0"),
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            type("ModernPanel", (ModelPanel,), {"list_search": ["title"]})


class TestCommand:
    def test_a_clean_project_passes(self):
        code, out = run("--skip-database")
        assert code == 0
        assert "upgrade check" in out

    @override_settings(VANTAGE={"SITE_TITEL": "Typo"})
    def test_an_unknown_setting_fails_with_a_suggestion(self):
        code, out = run("--skip-database")
        assert code == 1
        assert "U001" in out
        assert "SITE_TITLE" in out

    @override_settings(VANTAGE={"WEBHOOKS": False})
    def test_a_provisional_setting_is_informational(self):
        code, out = run("--skip-database")
        assert code == 0
        assert "U008" in out
        code, _ = run("--skip-database", "--fail-level", "info")
        assert code == 1

    @override_settings(VANTAGE={"SITE_TITEL": "Typo"})
    def test_json_output(self):
        _, out = run("--skip-database", "--format", "json", "--from", "0.2.0a1")
        payload = json.loads(out)
        assert payload["findings"][0]["code"] == "U001"
        assert [note["version"] for note in payload["notes"]] == ["0.2.0a2", "next"]

    def test_notes_since_a_version(self):
        _, out = run("--skip-database", "--from", "0.2.0a2")
        assert "Upgrading to next" in out
        assert "Upgrading to 0.2.0a2" not in out

    def test_an_unparseable_from_version_is_a_command_error(self):
        with pytest.raises(CommandError):
            call_command("vantage_upgrade_check", "--skip-database", "--from", "latest")

    @pytest.mark.django_db
    def test_the_test_database_is_fully_migrated(self):
        assert upgrade.check_migrations("default") == []


class TestChecks:
    def test_an_unsupported_django_is_an_error(self):
        (finding,) = upgrade.check_django((5, 1))
        assert finding.code == "U006"
        assert upgrade.check_django((5, 2)) == []

    def test_a_removed_attribute_and_a_panel_error_are_reported(self):
        site = VantageSite(name="upgrade-check")

        class BrokenPanel(ModelPanel):
            permissions = ["view"]
            list_display = ["nope"]

        site.register(Post, BrokenPanel)
        codes = sorted(finding.code for finding in upgrade.check_panels(site))
        assert codes == ["U004", "U007"]

    def test_a_provisional_attribute_is_informational(self):
        site = VantageSite(name="upgrade-check-provisional")

        class HistoryPanel(ModelPanel):
            history = "view"

        site.register(Post, HistoryPanel)
        (finding,) = upgrade.check_panels(site)
        assert (finding.level, finding.code) == ("info", "U008")

    def test_exceeds(self):
        info = upgrade.Finding("info", "U008", "x")
        assert not upgrade.exceeds([info], "warning")
        assert upgrade.exceeds([info], "info")
