"""``manage.py vantage_adopt`` — a mapping report for moving off ``django.contrib.admin``."""

from __future__ import annotations

import sysconfig
from pathlib import Path

from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from vantage import adopt


def _site_packages() -> list[Path]:
    paths = sysconfig.get_paths()
    return [Path(paths[k]).resolve() for k in ("purelib", "platlib") if k in paths]


class Command(BaseCommand):
    help = (
        "Read every ModelAdmin registered on django.contrib.admin, translate it, and report "
        "per model and per attribute what was translated, what needs panel code and what "
        "is unsupported. --write also writes a starter panel.py per app."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--admin-site",
            default=None,
            help="Dotted path to an AdminSite instance (default: django.contrib.admin.site).",
        )
        parser.add_argument("--app", dest="app_label", help="Only report on this app label.")
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument("--write", action="store_true", help="Write starter panel.py files.")
        parser.add_argument("--force", action="store_true", help="Overwrite existing files.")
        parser.add_argument(
            "--output-dir",
            help="Write <dir>/<app_label>/panel.py instead of into each app's directory.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit with status 1 when anything is unsupported (for CI).",
        )

    def handle(self, *args, **options):
        admin_site = self._admin_site(options["admin_site"])
        app_label = options["app_label"]
        if app_label:
            try:
                django_apps.get_app_config(app_label)
            except LookupError as exc:
                raise CommandError(str(exc)) from None

        reports = adopt.build_report(admin_site, app_label)

        # Report on stderr when writing, so ``--format json`` stays parseable on stdout.
        if options["format"] == "json":
            self.stdout.write(adopt.report_to_json(reports))
        else:
            self.stdout.write(adopt.report_to_text(reports))

        if options["write"]:
            self._write(reports, options)

        if options["strict"] and adopt.summarise(reports)[adopt.UNSUPPORTED]:
            raise SystemExit(1)

    def _admin_site(self, dotted: str | None):
        if dotted:
            try:
                return import_string(dotted)
            except ImportError as exc:
                raise CommandError(f"Cannot import admin site {dotted!r}: {exc}") from None
        if not django_apps.is_installed("django.contrib.admin"):
            raise CommandError(
                "django.contrib.admin is not in INSTALLED_APPS; pass --admin-site to name one."
            )
        from django.contrib import admin

        return admin.site

    def _write(self, reports, options):
        by_app: dict[str, list] = {}
        for report in reports:
            by_app.setdefault(report.app_label, []).append(report)

        output_dir = Path(options["output_dir"]).resolve() if options["output_dir"] else None
        for app_label, app_reports in sorted(by_app.items()):
            try:
                adopt.check_label(app_label)
            except ImproperlyConfigured as exc:
                raise CommandError(str(exc)) from None
            target = self._target(app_label, output_dir)
            if target is None:
                continue
            if target.exists() or target.is_symlink():
                if not options["force"]:
                    self.stderr.write(f"Skipped {target}: it exists (pass --force to overwrite).")
                    continue
                if target.is_symlink():
                    raise CommandError(f"Refusing to write through the symlink {target}.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(adopt.render_app_module(app_label, app_reports), encoding="utf-8")
            self.stderr.write(self.style.SUCCESS(f"Wrote {target}"))

    def _target(self, app_label: str, output_dir: Path | None) -> Path | None:
        if output_dir is not None:
            target = output_dir / app_label / "panel.py"
            # Resolve first: a symlinked app directory inside the output directory must
            # not carry the write somewhere else.
            if not target.parent.resolve().is_relative_to(output_dir):
                raise CommandError(f"{target} resolves outside {output_dir}; refusing to write.")
            return target
        try:
            config = django_apps.get_app_config(app_label)
        except LookupError:
            return None
        if config.path is None:
            return None
        path = Path(config.path).resolve()
        if any(path.is_relative_to(sp) for sp in _site_packages()):
            self.stderr.write(
                f"Skipped {app_label}: it is an installed package. Pass --output-dir to write it."
            )
            return None
        return path / "panel.py"
