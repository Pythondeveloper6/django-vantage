"""Scaffolding: inference is only trustworthy if you can read what it decided."""

import ast
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from tests.testapp.models import Post, Tag
from vantage.scaffold import render_panel_class, render_panel_module


class TestRenderPanelClass:
    def test_renders_a_registered_panel(self):
        source = render_panel_class(Post)
        assert "@register(Post)" in source
        assert "class PostPanel(ModelPanel):" in source

    def test_includes_the_inferred_attributes(self):
        source = render_panel_class(Post)
        assert "list_display = " in source
        assert "list_filter = " in source
        assert "ordering = " in source

    def test_a_long_list_wraps_one_entry_per_line(self):
        source = render_panel_class(Post)
        assert max(len(line) for line in source.splitlines()) <= 90

    def test_a_short_list_stays_inline(self):
        assert 'list_display = ["name"]' in render_panel_class(Tag)

    def test_output_is_valid_python(self):
        ast.parse(render_panel_class(Post))


class TestRenderPanelModule:
    def test_output_is_valid_python(self):
        ast.parse(render_panel_module("testapp"))

    def test_imports_every_model_it_configures(self):
        source = render_panel_module("testapp")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        registered = {
            node.decorator_list[0].args[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.decorator_list
        }
        assert registered <= imported

    def test_says_how_to_get_inference_back(self):
        """A generated file has to explain its own escape hatch."""
        source = " ".join(render_panel_module("testapp").split())
        assert "Deleting an attribute returns it to inference" in source

    def test_a_named_subset_of_models(self):
        source = render_panel_module("testapp", models=[Tag])
        assert "TagPanel" in source
        assert "PostPanel" not in source


class TestCommand:
    def test_prints_by_default(self):
        out = StringIO()
        call_command("vantage_scaffold", "testapp", stdout=out)
        assert "class PostPanel(ModelPanel):" in out.getvalue()

    def test_printed_output_is_valid_python(self):
        out = StringIO()
        call_command("vantage_scaffold", "testapp", stdout=out)
        ast.parse(out.getvalue())

    def test_unknown_app_is_a_clean_error(self):
        with pytest.raises(CommandError):
            call_command("vantage_scaffold", "nosuchapp", stdout=StringIO())

    def test_write_creates_the_file(self, tmp_path, monkeypatch):
        from django.apps import apps as django_apps

        config = django_apps.get_app_config("testapp")
        monkeypatch.setattr(config, "path", str(tmp_path))

        call_command("vantage_scaffold", "testapp", write=True, stdout=StringIO())
        written = (tmp_path / "panel.py").read_text()
        assert "class PostPanel(ModelPanel):" in written
        ast.parse(written)

    def test_write_refuses_to_clobber_without_force(self, tmp_path, monkeypatch):
        from django.apps import apps as django_apps

        config = django_apps.get_app_config("testapp")
        monkeypatch.setattr(config, "path", str(tmp_path))
        (tmp_path / "panel.py").write_text("# mine\n")

        with pytest.raises(CommandError, match="already exists"):
            call_command("vantage_scaffold", "testapp", write=True, stdout=StringIO())
        assert (tmp_path / "panel.py").read_text() == "# mine\n"

    def test_force_overwrites(self, tmp_path, monkeypatch):
        from django.apps import apps as django_apps

        config = django_apps.get_app_config("testapp")
        monkeypatch.setattr(config, "path", str(tmp_path))
        (tmp_path / "panel.py").write_text("# mine\n")

        call_command("vantage_scaffold", "testapp", write=True, force=True, stdout=StringIO())
        assert "PostPanel" in (tmp_path / "panel.py").read_text()


def test_generated_config_passes_its_own_checks(tmp_path):
    """The strongest guarantee: what we generate is valid by our own validator.

    If inference can emit a configuration that fails `manage.py check`, the inference
    is wrong.
    """
    import textwrap

    from vantage.panels import ModelPanel

    namespace: dict = {}
    body = render_panel_class(Post)
    # Strip the decorator; we only want the class, not a registration side effect.
    class_source = "\n".join(line for line in body.splitlines() if not line.startswith("@register"))
    exec(textwrap.dedent(class_source), {"ModelPanel": ModelPanel}, namespace)
    panel = namespace["PostPanel"](Post)
    assert panel.check() == []
