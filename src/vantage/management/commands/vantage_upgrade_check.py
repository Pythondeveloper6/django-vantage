"""``manage.py vantage_upgrade_check`` — what an upgrade of django-vantage will hit.

Run it after bumping the version in a branch, before deploying, or in CI on every
build: it lists unknown, deprecated and provisional settings, deprecated or removed
panel attributes, panel configuration errors, unapplied migrations and an unsupported
Django, and prints the upgrade notes for every release since ``--from``. Exit status 1
when anything at or above ``--fail-level`` (default ``error``) was found.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from vantage import __version__
from vantage.upgrade import LEVELS, exceeds, notes_since, run_checks


class Command(BaseCommand):
    help = "Report everything that needs attention before or after upgrading django-vantage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from",
            dest="from_version",
            metavar="VERSION",
            help="The version being upgraded from; prints the upgrade notes since then.",
        )
        parser.add_argument("--format", choices=("text", "json"), default="text")
        parser.add_argument(
            "--fail-level",
            choices=LEVELS,
            default="error",
            help="Exit with status 1 when a finding at or above this level exists.",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias whose migration state is checked.",
        )
        parser.add_argument(
            "--skip-database",
            action="store_true",
            help="Do not connect to a database (configuration checks only).",
        )

    def handle(self, *args, **options):
        findings = run_checks(database=None if options["skip_database"] else options["database"])
        try:
            notes = notes_since(options["from_version"]) if options["from_version"] else []
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options["format"] == "json":
            self.stdout.write(
                json.dumps(
                    {
                        "version": __version__,
                        "findings": [finding.as_dict() for finding in findings],
                        "notes": [{"version": name, "notes": list(lines)} for name, lines in notes],
                    },
                    indent=2,
                )
            )
        else:
            self._write_text(findings, notes)

        if exceeds(findings, options["fail_level"]):
            raise SystemExit(1)

    def _write_text(self, findings, notes):
        self.stdout.write(f"django-vantage {__version__} upgrade check")
        if not findings:
            self.stdout.write(self.style.SUCCESS("No problems found."))
        styles = {"error": self.style.ERROR, "warning": self.style.WARNING, "info": str}
        for finding in findings:
            line = f"[{finding.level}] {finding.code} {finding.message}"
            self.stdout.write(styles[finding.level](line))
            if finding.hint:
                self.stdout.write(f"    {finding.hint}")
        for name, lines in notes:
            self.stdout.write("")
            self.stdout.write(f"Upgrading to {name}:")
            for line in lines:
                self.stdout.write(f"  - {line}")
