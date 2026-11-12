"""``manage.py vantage_agent_activity`` - what agents did, as CSV, JSON or NDJSON.

One row per audit entry made through an agent identity, or with ``--runs`` one row per
run. The same rows the panel's "Agent activity" page downloads; see
:mod:`vantage.agentactivity`.
"""

from __future__ import annotations

import os
import sys

from django.core.management.base import BaseCommand, CommandError

from vantage import agentactivity


class Command(BaseCommand):
    help = "Export what agents did: one row per audit entry, or per run with --runs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since", help="7d, 24h, 2w, 30m, an ISO date or an ISO date-time. Default: all."
        )
        parser.add_argument(
            "--until",
            help="Same forms; a bare date includes that whole day. Default: now.",
        )
        parser.add_argument(
            "--agent", action="append", default=[], metavar="NAME", help="Repeat for several."
        )
        parser.add_argument("--run", default="", metavar="RUN_KEY")
        parser.add_argument("--model", default="", metavar="app.model")
        parser.add_argument(
            "--action", default="", help="create, update, delete, trash, restore or action."
        )
        parser.add_argument("--runs", action="store_true", help="One row per run instead.")
        parser.add_argument("--format", choices=agentactivity.FORMATS, default="ndjson")
        parser.add_argument("--output", default="-", help="File to write. Default: stdout.")
        parser.add_argument(
            "--force", action="store_true", help="Overwrite --output if it already exists."
        )

    def handle(self, *args, **options):
        runs = options["runs"]
        if runs and (options["model"] or options["action"]):
            raise CommandError("--model and --action do not apply with --runs.")
        try:
            since = agentactivity.parse_moment(options["since"]) if options["since"] else None
            until = (
                agentactivity.parse_moment(options["until"], end_of_day=True)
                if options["until"]
                else None
            )
            common = {
                "since": since,
                "until": until,
                "agents": options["agent"],
                "run": options["run"],
            }
            if runs:
                columns = agentactivity.RUN_COLUMNS
                rows = agentactivity.iter_run_rows(agentactivity.run_queryset(**common))
            else:
                columns = agentactivity.ENTRY_COLUMNS
                queryset = agentactivity.entry_queryset(
                    **common, model=options["model"], action=options["action"]
                )
                rows = agentactivity.iter_entry_rows(queryset)
        except agentactivity.ActivityError as exc:
            raise CommandError(str(exc)) from None

        chunks = agentactivity.render(columns, rows, options["format"])
        target = options["output"]
        if target in ("", "-"):
            for chunk in chunks:
                self.stdout.write(chunk, ending="")
            return
        if os.path.exists(target) and not options["force"]:
            raise CommandError(f"{target} already exists. Pass --force to overwrite it.")
        # "x" makes the no-overwrite promise atomic; --force opts into truncation.
        mode = "w" if options["force"] else "x"
        try:
            with open(target, mode, encoding="utf-8", newline="") as handle:
                for chunk in chunks:
                    handle.write(chunk)
        except FileExistsError:
            raise CommandError(f"{target} already exists. Pass --force to overwrite it.") from None
        sys.stderr.write(f"Wrote {target}.\n")
