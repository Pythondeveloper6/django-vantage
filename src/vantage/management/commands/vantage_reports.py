"""``manage.py vantage_reports`` — send the scheduled reports that are due.

Run it from cron every minute. It queues each due report through the job backend (or
sends it right here when there is none), so it returns quickly where a queue exists and
takes as long as the emails do where it does not. Running it twice in the same minute
sends nothing twice: a report is claimed before it is queued.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send scheduled reports that are due, and delete old send logs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-sweep",
            action="store_true",
            help="Skip deleting send logs older than REPORTS_RETENTION_DAYS.",
        )

    def handle(self, *args, **options):
        from vantage import reports

        if not reports.enabled():
            self.stdout.write("Scheduled reports are switched off (VANTAGE['REPORTS']).")
            return
        queued = reports.run_due()
        self.stdout.write(f"Queued {queued} due report{'' if queued == 1 else 's'}.")
        again = reports.requeue_stale()
        if again:
            self.stdout.write(f"Queued {again} report{'' if again == 1 else 's'} again.")
        if not options["no_sweep"]:
            removed = reports.sweep()
            self.stdout.write(f"Removed {removed} old send log{'' if removed == 1 else 's'}.")
