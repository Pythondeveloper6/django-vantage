"""``manage.py vantage_scaffold <app>`` — write inferred config out as real Python.

With ``--from-db`` it starts one step earlier, at the database: unmanaged models from
``inspectdb`` plus read-only panels. With ``--preset`` it adds a starter dashboard.
"""

from __future__ import annotations

from pathlib import Path

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from vantage import scaffold_db
from vantage.scaffold import render_panel_module


class Command(BaseCommand):
    help = (
        "Write the configuration Vantage infers for an app out as a real panel.py, "
        "so you can see it, change it, and own it. --from-db starts from an existing "
        "database; --preset adds a starter dashboard."
    )

    def add_arguments(self, parser):
        parser.add_argument("app_label", help="The app to scaffold, e.g. 'blog'.")
        parser.add_argument(
            "--write",
            action="store_true",
            help="Write panel.py into the app directory instead of printing it.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite an existing panel.py.",
        )
        parser.add_argument(
            "--from-db",
            action="store_true",
            help="Generate unmanaged models and read-only panels from the database.",
        )
        parser.add_argument(
            "--database", default="default", help="Database alias for --from-db (default: default)."
        )
        parser.add_argument(
            "--table",
            action="append",
            default=[],
            help="With --from-db: only these tables (shell pattern, repeatable).",
        )
        parser.add_argument(
            "--exclude-table",
            action="append",
            default=[],
            help="With --from-db: skip these tables (shell pattern, repeatable).",
        )
        parser.add_argument(
            "--include-views", action="store_true", help="With --from-db: include views."
        )
        parser.add_argument(
            "--preset",
            choices=sorted(scaffold_db.PRESETS),
            help="Also write a starter dashboard (and panel groups) for this kind of app.",
        )
        parser.add_argument(
            "--map",
            action="append",
            default=[],
            metavar="ROLE=MODEL",
            help="Name the model that plays a preset role, e.g. --map deal=Opportunity.",
        )
        parser.add_argument(
            "--output-dir",
            help="With --write: write here instead of into the app directory.",
        )

    def handle(self, app_label: str, write: bool, force: bool, **options):
        if options["from_db"] or options["preset"]:
            return self.handle_extended(app_label, write, force, options)
        if options["map"] or options["table"] or options["exclude_table"]:
            raise CommandError("--map, --table and --exclude-table need --from-db or --preset.")

        try:
            app_config = django_apps.get_app_config(app_label)
        except LookupError as exc:
            raise CommandError(str(exc)) from None

        source = render_panel_module(app_label)

        if not write:
            self.stdout.write(source)
            return

        if app_config.path is None:
            raise CommandError(f"App {app_label!r} has no filesystem path to write to.")

        target = Path(app_config.path) / "panel.py"
        if target.exists() and not force:
            raise CommandError(
                f"{target} already exists. Pass --force to overwrite it, or drop "
                f"--write to print the file instead."
            )

        target.write_text(source, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {target}"))

    # ── --from-db / --preset ────────────────────────────────────────────────

    def handle_extended(self, app_label: str, write: bool, force: bool, options: dict):
        preset = options["preset"]
        try:
            mapping = scaffold_db.parse_mapping(options["map"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None

        installed = any(c.label == app_label for c in django_apps.get_app_configs())
        files: dict[str, str] = {}
        try:
            if options["from_db"]:
                if options["database"] not in connections:
                    raise CommandError(f"Unknown database alias {options['database']!r}.")
                module = (
                    f"{django_apps.get_app_config(app_label).name}.models"
                    if installed
                    else f"{app_label}.models"
                )
                source = scaffold_db.inspect_models(
                    options["database"],
                    options["table"],
                    options["exclude_table"],
                    options["include_views"],
                )
                models = scaffold_db.models_from_source(source, module)
                files["models.py"] = source
                files["panel.py"] = scaffold_db.render_readonly_panels(
                    models, module, app_label, preset, mapping
                )
            else:
                if not installed:
                    raise CommandError(f"No installed app with label {app_label!r}.")
                models = scaffold_db.models_from_app(app_label)
            if preset:
                files["dashboard.py"] = scaffold_db.render_dashboard(preset, models, mapping)
        except ValueError as exc:
            raise CommandError(str(exc)) from None

        if not write:
            for name, text in files.items():
                self.stdout.write(f"# {'=' * 8} {name} {'=' * 8}")
                self.stdout.write(text)
            return

        target_dir = self._target_dir(app_label, installed, options["output_dir"])
        for name in files:
            target = target_dir / name
            if target.exists() and not force:
                raise CommandError(
                    f"{target} already exists. Pass --force to overwrite it, or drop --write."
                )
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (target_dir / name).write_text(text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote {target_dir / name}"))

    def _target_dir(self, app_label: str, installed: bool, output_dir: str | None) -> Path:
        if output_dir:
            root = Path(output_dir).resolve()
            target = root / app_label
            if not target.resolve().is_relative_to(root):
                raise CommandError(f"{target} resolves outside {root}; refusing to write.")
            return target
        if not installed:
            raise CommandError(
                f"App {app_label!r} is not installed, so there is no directory to write "
                f"to. Pass --output-dir."
            )
        path = django_apps.get_app_config(app_label).path
        if path is None:
            raise CommandError(f"App {app_label!r} has no filesystem path to write to.")
        return Path(path)
