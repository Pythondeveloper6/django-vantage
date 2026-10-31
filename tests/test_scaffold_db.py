"""``vantage_scaffold --from-db`` and ``--preset``, against real raw tables."""

import ast
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection

from vantage import scaffold_db

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def raw_tables():
    """Two tables Django knows nothing about, created with raw SQL."""
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE legacy_customer (id integer PRIMARY KEY, name varchar(100) NOT NULL, "
            "email varchar(100) NULL, signed_up datetime NOT NULL)"
        )
        cursor.execute(
            "CREATE TABLE legacy_order (id integer PRIMARY KEY, "
            "customer_id integer NOT NULL REFERENCES legacy_customer (id), "
            "total decimal NOT NULL, status varchar(20) NOT NULL, placed_at datetime NOT NULL)"
        )
        cursor.execute("CREATE TABLE scratch_tmp (id integer PRIMARY KEY, note text)")
    yield
    with connection.cursor() as cursor:
        for table in ("legacy_order", "legacy_customer", "scratch_tmp"):
            cursor.execute(f"DROP TABLE IF EXISTS {table}")


def scaffold(*args):
    out = StringIO()
    call_command("vantage_scaffold", "legacy", "--from-db", *args, stdout=out)
    return out.getvalue()


def sections(text):
    parts = {}
    current = None
    for line in text.splitlines():
        if line.startswith("# ========"):
            current = line.strip("# =")
            parts[current] = []
        elif current:
            parts[current].append(line)
    return {k: "\n".join(v) for k, v in parts.items()}


class TestInspect:
    def test_models_are_unmanaged(self, raw_tables):
        source = scaffold_db.inspect_models(tables=["legacy_*"])
        assert "class LegacyCustomer(models.Model):" in source
        assert source.count("        managed = False") == 2

    def test_table_and_exclude_filters(self, raw_tables):
        source = scaffold_db.inspect_models(tables=["legacy_*"], exclude=["legacy_order"])
        assert "LegacyCustomer" in source
        assert "LegacyOrder" not in source
        assert "ScratchTmp" not in source

    def test_unknown_database_alias(self):
        with pytest.raises(ValueError):
            scaffold_db.inspect_models(database="nope")


class TestFromDb:
    def test_prints_models_and_readonly_panels(self, raw_tables):
        parts = sections(scaffold("--table=legacy_*"))
        assert set(parts) == {"models.py", "panel.py"}
        for source in parts.values():
            ast.parse(source)
        panels = parts["panel.py"]
        assert "@register(LegacyCustomer)" in panels
        assert "from legacy.models import LegacyCustomer, LegacyOrder" in panels
        assert panels.count("return False") == 6

    def test_exclude_table(self, raw_tables):
        parts = sections(scaffold("--table=legacy_*", "--exclude-table=legacy_order"))
        assert "LegacyOrder" not in parts["panel.py"]

    def test_unknown_database(self):
        with pytest.raises(CommandError, match="database alias"):
            scaffold("--database=nope")

    def test_write_needs_somewhere_to_go(self, raw_tables):
        with pytest.raises(CommandError, match="--output-dir"):
            scaffold("--write")

    def test_write_to_output_dir_and_no_clobber(self, raw_tables, tmp_path):
        scaffold("--table=legacy_*", "--write", f"--output-dir={tmp_path}")
        assert (tmp_path / "legacy" / "models.py").exists()
        ast.parse((tmp_path / "legacy" / "panel.py").read_text())
        (tmp_path / "legacy" / "panel.py").write_text("# mine\n")
        with pytest.raises(CommandError, match="already exists"):
            scaffold("--table=legacy_*", "--write", f"--output-dir={tmp_path}")
        assert (tmp_path / "legacy" / "panel.py").read_text() == "# mine\n"
        scaffold("--table=legacy_*", "--write", "--force", f"--output-dir={tmp_path}")
        assert "LegacyCustomer" in (tmp_path / "legacy" / "panel.py").read_text()

    def test_output_dir_traversal_is_refused(self, raw_tables, tmp_path):
        out = StringIO()
        with pytest.raises(CommandError, match="outside"):
            call_command(
                "vantage_scaffold",
                "../escape",
                "--from-db",
                "--write",
                f"--output-dir={tmp_path}",
                stdout=out,
            )
        assert not (tmp_path.parent / "escape").exists()

    def test_generated_panels_register_read_only(self, raw_tables, tmp_path):
        """Models and panels, loaded for real, give a panel that refuses writes."""
        import sys

        scaffold("--table=legacy_*", "--write", f"--output-dir={tmp_path}")
        package = tmp_path / "legacy"
        (package / "__init__.py").write_text("")
        source = (package / "models.py").read_text()
        namespace = {"__name__": "legacy.models"}
        # Attach the models to an existing app so they can be built without installing one.
        source = source.replace("class Meta:", "class Meta:\n        app_label = 'testapp'")
        exec(compile(source, "models.py", "exec"), namespace)
        model = namespace["LegacyOrder"]
        assert model._meta.managed is False

        from vantage import ModelPanel

        panel_source = (package / "panel.py").read_text()
        body = panel_source.split("class LegacyOrderPanel(ModelPanel):")[1].split("@register")[0]
        local = {}
        exec("class P(ModelPanel):" + body, {"ModelPanel": ModelPanel}, local)
        panel = local["P"](model)
        assert panel.has_delete_permission(None) is False
        assert panel.has_add_permission(None) is False
        sys.modules.pop("legacy", None)


class TestPresets:
    def test_ecommerce_from_db(self, raw_tables):
        parts = sections(scaffold("--table=legacy_*", "--preset=ecommerce"))
        dash = parts["dashboard.py"]
        ast.parse(dash)
        assert 'Sum("total")' in dash
        assert 'group_by="status"' in dash
        assert "LegacyOrder.objects.all()" in dash
        assert "Skipped: Products" in dash
        # Only models that exist are imported.
        assert "from legacy.models import LegacyCustomer, LegacyOrder" in dash
        assert 'group = "Sales"' in parts["panel.py"]

    def test_map_overrides_the_guess(self, raw_tables):
        parts = sections(scaffold("--table=legacy_*", "--preset=crm", "--map=deal=LegacyOrder"))
        assert "LegacyOrder.objects.all()" in parts["dashboard.py"]
        assert "Pipeline value" in parts["dashboard.py"]

    def test_bad_map_is_a_clean_error(self, raw_tables):
        with pytest.raises(CommandError, match="role=Model"):
            scaffold("--preset=crm", "--map=oops")
        with pytest.raises(CommandError, match="Unknown role"):
            scaffold("--preset=crm", "--map=nothing=LegacyOrder", "--table=legacy_*")
        with pytest.raises(CommandError, match="no such model"):
            scaffold("--preset=crm", "--map=deal=Missing", "--table=legacy_*")

    def test_preset_over_an_existing_app(self):
        out = StringIO()
        call_command(
            "vantage_scaffold",
            "testapp",
            "--preset=ecommerce",
            "--map=order=Order",
            "--map=customer=Author",
            stdout=out,
        )
        dash = sections(out.getvalue())["dashboard.py"]
        ast.parse(dash)
        assert "from tests.testapp.models import Author, Order" in dash
        assert 'Sum("total")' in dash

    def test_dashboard_is_importable_and_builds_widgets(self, raw_tables, tmp_path):
        """Run the generated dashboard code over the real testapp models."""
        out = StringIO()
        call_command(
            "vantage_scaffold",
            "testapp",
            "--preset=ecommerce",
            "--map=order=Order",
            "--map=customer=Author",
            stdout=out,
        )
        dash = sections(out.getvalue())["dashboard.py"]
        namespace = {}
        exec(compile(dash, "dashboard.py", "exec"), namespace)
        widgets = namespace["StarterDashboard"]().get_widgets(None)
        assert widgets

    def test_unmatched_roles_contribute_nothing(self):
        infos = [scaffold_db.ModelInfo("Thing", "x.models", {"id": "int"})]
        source = scaffold_db.render_dashboard("saas", infos, {})
        ast.parse(source)
        assert "return []" in source

    def test_options_need_a_mode(self):
        with pytest.raises(CommandError, match="--from-db or --preset"):
            call_command("vantage_scaffold", "testapp", "--table=x", stdout=StringIO())
