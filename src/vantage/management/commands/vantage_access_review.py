"""``manage.py vantage_access_review`` — who can see and change what, as CSV or JSON.

The same report the panel's access review screen downloads, for a scheduled export to
an evidence store. See :mod:`vantage.accessreview`.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Export who can view, add, change and delete each model, and its guarded fields."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("csv", "json"), default="csv")
        parser.add_argument(
            "--output", default="-", help="File to write. Default: standard output."
        )
        parser.add_argument(
            "--model",
            action="append",
            default=[],
            help="Only this model, as app_label.model. Repeat for several.",
        )
        parser.add_argument("--by", choices=("user", "role"), default="user")

    def handle(self, *args, **options):
        from vantage import accessreview
        from vantage.sites import site

        known = {p.model._meta.label_lower for p in site.panels()}
        unknown = [m for m in options["model"] if m.lower() not in known]
        if unknown:
            raise CommandError(f"Not a model this panel registers: {', '.join(unknown)}")
        report = accessreview.build_report(site, by=options["by"], models=options["model"] or None)
        text = (
            accessreview.to_json(report)
            if options["format"] == "json"
            else accessreview.to_csv(report)
        )
        if options["output"] in ("", "-"):
            self.stdout.write(text, ending="" if text.endswith("\n") else "\n")
            return
        with open(options["output"], "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        self.stderr.write(f"Wrote {len(report.subjects)} subjects to {options['output']}.")
