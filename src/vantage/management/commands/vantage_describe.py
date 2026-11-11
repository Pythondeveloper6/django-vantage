"""``manage.py vantage_describe`` — what this admin looks like, as JSON or Markdown.

Ground truth for a coding agent (or a person) about to write a panel: every registered
model with its columns, filters, fields, flags and actions, plus the site-level
settings and pages. It never reads a row. ``--undeclared`` lists only the models whose
panel is fully inferred and spells each inferred default out as the ``ModelPanel``
attributes that would reproduce it.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from vantage.describe import describe_site, render_markdown


class Command(BaseCommand):
    help = "Describe every registered model, page and setting as JSON or Markdown (reads no rows)."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=("json", "markdown"), default="json")
        parser.add_argument(
            "--model",
            metavar="APP.MODEL",
            help="Describe only this model, e.g. shop.order.",
        )
        parser.add_argument(
            "--undeclared",
            action="store_true",
            help="Only models whose panel is fully inferred, with the inferred defaults "
            "written out as ModelPanel attributes.",
        )

    def handle(self, *args, **options):
        description = describe_site(model=options["model"], undeclared=options["undeclared"])
        if options["model"] and not description["models"] and not description["errors"]:
            raise CommandError(
                f"{options['model']!r} is not a registered model (or is excluded by --undeclared)."
            )
        if options["format"] == "markdown":
            self.stdout.write(render_markdown(description), ending="")
        else:
            self.stdout.write(json.dumps(description, indent=2, sort_keys=True, default=str))
