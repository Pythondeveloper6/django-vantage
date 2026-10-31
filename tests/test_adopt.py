"""``manage.py vantage_adopt``: the mapping report and the starter panel files."""

import ast
import json
import subprocess
import sys
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

import vantage
from tests.testapp.models import Author, Order, Post
from vantage import ModelPanel, adopt
from vantage.contrib import django_admin
from vantage.sites import VantageSite

SITE = "tests.adopt_admins.site"


@pytest.fixture(autouse=True)
def clean_adoptions():
    django_admin.reset()
    yield
    django_admin.reset()


@pytest.fixture
def reports():
    from tests.adopt_admins import site

    return {r.label: r for r in adopt.build_report(site)}


def status_of(report, attr):
    return next(i.status for i in report.items if i.attr == attr)


def run(*args):
    out, err = StringIO(), StringIO()
    call_command("vantage_adopt", f"--admin-site={SITE}", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class TestReport:
    def test_plain_settings_are_translated(self, reports):
        post = reports["testapp.Post"]
        for attr in ("list_display", "list_filter", "search_fields", "ordering", "list_per_page"):
            assert status_of(post, attr) == adopt.TRANSLATED

    def test_formfield_overrides_is_translated(self, reports):
        assert status_of(reports["testapp.Post"], "formfield_overrides") == adopt.TRANSLATED

    def test_list_display_links_names_list_link(self, reports):
        item = next(i for i in reports["testapp.Post"].items if i.attr == "list_display_links")
        assert item.equivalent == "ModelPanel.list_link"

    def test_date_hierarchy_is_a_filter_with_a_caveat(self, reports):
        item = next(i for i in reports["testapp.Post"].items if i.attr == "date_hierarchy")
        assert item.status == adopt.TRANSLATED
        assert "not the drill-down" in item.detail

    def test_raw_id_fields_is_built_in(self, reports):
        item = next(i for i in reports["testapp.Post"].items if i.attr == "raw_id_fields")
        assert item.status == adopt.TRANSLATED
        assert "picker" in item.equivalent

    def test_code_needing_items(self, reports):
        post = reports["testapp.Post"]
        assert status_of(post, "list_display[word_count]") == adopt.NEEDS_CODE
        assert status_of(post, "actions[feature]") == adopt.NEEDS_CODE
        assert status_of(post, "get_queryset()") == adopt.NEEDS_CODE
        assert status_of(post, "save_model()") == adopt.NEEDS_CODE
        assert status_of(post, "has_delete_permission") == adopt.NEEDS_CODE
        assert status_of(post, "inlines") == adopt.NEEDS_CODE

    def test_unsupported_item_has_reason_and_equivalent(self, reports):
        item = next(i for i in reports["testapp.Post"].items if i.attr == "change_list_template")
        assert item.status == adopt.UNSUPPORTED
        assert item.detail
        assert "slots" in item.equivalent

    def test_unknown_third_party_base_is_flagged(self, reports):
        assert status_of(reports["testapp.Post"], "mixin FancyMixin") == adopt.NEEDS_CODE

    def test_known_mixin_maps_to_vantage_history(self, reports):
        item = next(i for i in reports["testapp.Author"].items if i.attr.startswith("mixin"))
        assert item.attr == "mixin SimpleHistoryAdmin"
        assert item.status == adopt.TRANSLATED
        assert "history" in item.equivalent

    def test_an_admin_that_declares_nothing_has_no_rows(self, reports):
        assert reports["testapp.Tag"].items == []

    def test_str_column_is_not_flagged(self, reports):
        assert not [i for i in reports["testapp.Order"].items if i.attr.startswith("list_display[")]

    def test_building_a_report_leaves_the_adapter_registry_alone(self):
        from tests.adopt_admins import site

        adopt.build_report(site)
        assert django_admin.adoptions() == []


class TestCommand:
    def test_text_report(self):
        out, _ = run()
        assert "testapp.Post  (PostAdmin)" in out
        assert "needs panel code" in out
        assert "[Vantage: ModelPanel.list_link]" in out

    def test_json_report(self):
        out, _ = run("--format=json")
        data = json.loads(out)
        assert data["summary"]["models"] == 4
        assert {m["model"] for m in data["models"]} >= {"testapp.Post", "testapp.Tag"}

    def test_app_filter(self):
        out, _ = run("--format=json", "--app=testapp")
        assert json.loads(out)["summary"]["models"] == 4
        out, _ = run("--format=json", "--app=auth")
        assert json.loads(out)["summary"]["models"] == 0

    def test_unknown_app_is_a_clean_error(self):
        with pytest.raises(CommandError):
            run("--app=nosuchapp")

    def test_bad_admin_site_is_a_clean_error(self):
        with pytest.raises(CommandError):
            call_command("vantage_adopt", "--admin-site=nope.nothing", stdout=StringIO())

    def test_no_contrib_admin_and_no_site_is_a_clean_error(self):
        with pytest.raises(CommandError, match="admin-site"):
            call_command("vantage_adopt", stdout=StringIO())

    def test_strict_exits_one_when_something_is_unsupported(self):
        with pytest.raises(SystemExit) as exc:
            run("--strict")
        assert exc.value.code == 1

    def test_strict_passes_when_nothing_is_unsupported(self):
        run("--strict", "--app=auth")  # no admins on that app


@pytest.fixture
def capture(monkeypatch):
    """Exec a generated module with ``register`` captured instead of applied."""
    registered = {}

    def register(model):
        def wrap(cls):
            registered[model] = cls
            return cls

        return wrap

    monkeypatch.setattr(vantage, "register", register)

    def load(source):
        registered.clear()
        exec(compile(source, "panel.py", "exec"), {"__name__": "generated_panel"})
        return dict(registered)

    return load


class TestGeneratedModule:
    @pytest.fixture
    def source(self, reports):
        return adopt.render_app_module("testapp", list(reports.values()))

    def test_is_valid_python(self, source):
        ast.parse(source)

    def test_registers_a_panel_per_model(self, source, capture):
        assert set(capture(source)) == {Post, Order, Author, vantage_tag()}

    def test_panels_match_the_adapter(self, source, capture, reports):
        generated = capture(source)
        for model, cls in generated.items():
            mine = cls(model)
            theirs = reports[model._meta.label].adoption.panel_class(model)
            assert mine.get_list_search() == theirs.get_list_search()
            assert mine.get_ordering() == theirs.get_ordering()
            assert mine.get_per_page() == theirs.get_per_page()
            assert mine.readonly_fields == theirs.readonly_fields
            assert mine.get_list_filter() == theirs.get_list_filter()
            assert mine.layout == theirs.layout or repr(mine.layout) == repr(theirs.layout)

    def test_columns_needing_code_are_left_as_todos(self, source, capture):
        assert "TODO(vantage_adopt) column word_count" in source
        assert capture(source)[Post](Post).get_list_display() == ["title", "status"]

    def test_str_column_is_written_out(self, source, capture):
        panel = capture(source)[Order](Order)
        assert panel.get_list_display()[0] == "order_display"
        assert panel.order_display(Order(number="A-1")) == "A-1"

    def test_formfield_overrides_written_out(self, source, capture):
        overrides = capture(source)[Post].formfield_overrides
        from django.db import models

        assert overrides[models.TextField]["widget"].attrs["rows"] == 3

    def test_every_panel_is_a_model_panel(self, source, capture):
        assert all(issubclass(c, ModelPanel) for c in capture(source).values())

    def test_is_ruff_formatted(self, source, tmp_path):
        target = tmp_path / "panel.py"
        target.write_text(source)
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", "--line-length=100", str(target)],
            capture_output=True,
            text=True,
        )
        if "No module named ruff" in result.stderr:
            pytest.skip("ruff is not installed")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_registers_on_a_real_site(self, reports):
        site = VantageSite(name="adopt-check")
        panel = reports["testapp.Post"].adoption.panel_class
        site.register(Post, panel)
        assert site.is_registered(Post)


def vantage_tag():
    from tests.testapp.models import Tag

    return Tag


class TestWrite:
    def test_writes_per_app_under_output_dir(self, tmp_path):
        _, err = run("--write", f"--output-dir={tmp_path}")
        target = tmp_path / "testapp" / "panel.py"
        assert target.exists()
        assert "Wrote" in err
        ast.parse(target.read_text())

    def test_never_overwrites_without_force(self, tmp_path):
        target = tmp_path / "testapp" / "panel.py"
        target.parent.mkdir()
        target.write_text("# mine\n")
        _, err = run("--write", f"--output-dir={tmp_path}")
        assert target.read_text() == "# mine\n"
        assert "Skipped" in err

    def test_force_overwrites(self, tmp_path):
        target = tmp_path / "testapp" / "panel.py"
        target.parent.mkdir()
        target.write_text("# mine\n")
        run("--write", "--force", f"--output-dir={tmp_path}")
        assert "PostPanel" in target.read_text()

    def test_symlinked_app_dir_cannot_escape_output_dir(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        (out / "testapp").symlink_to(outside, target_is_directory=True)
        with pytest.raises(CommandError, match="outside"):
            run("--write", f"--output-dir={out}")
        assert not list(outside.iterdir())

    def test_symlinked_target_is_refused(self, tmp_path):
        out = tmp_path / "out"
        (out / "testapp").mkdir(parents=True)
        victim = tmp_path / "victim.py"
        victim.write_text("keep")
        (out / "testapp" / "panel.py").symlink_to(victim)
        with pytest.raises(CommandError, match="symlink"):
            run("--write", "--force", f"--output-dir={out}")
        assert victim.read_text() == "keep"

    def test_unsafe_app_label_is_refused(self):
        with pytest.raises(Exception, match="Refusing"):
            adopt.check_label("../evil")

    def test_installed_packages_are_not_written_into(self, monkeypatch, tmp_path):
        import sysconfig

        from django.apps import apps as django_apps

        config = django_apps.get_app_config("testapp")
        fake = tmp_path / "site-packages"
        (fake / "testapp").mkdir(parents=True)
        monkeypatch.setattr(config, "path", str(fake / "testapp"))
        monkeypatch.setattr(
            sysconfig, "get_paths", lambda *a, **k: {"purelib": str(fake), "platlib": str(fake)}
        )
        _, err = run("--write")
        assert "installed package" in err
        assert not (fake / "testapp" / "panel.py").exists()

    def test_writes_into_the_app_directory_by_default(self, monkeypatch, tmp_path):
        from django.apps import apps as django_apps

        config = django_apps.get_app_config("testapp")
        monkeypatch.setattr(config, "path", str(tmp_path))
        run("--write")
        assert (tmp_path / "panel.py").exists()
