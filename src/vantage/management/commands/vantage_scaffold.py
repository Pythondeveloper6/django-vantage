"""``manage.py vantage_scaffold <app>`` — write inferred config out as real Python."""

from __future__ import annotations

from pathlib import Path

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError

from vantage.scaffold import render_panel_module


class Command(BaseCommand):
    help = (
        "Write the configuration Vantage infers for an app out as a real panel.py, "
        "so you can see it, change it, and own it."
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

    def handle(self, app_label: str, write: bool, force: bool, **options):
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
