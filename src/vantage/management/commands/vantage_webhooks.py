"""``manage.py vantage_webhooks`` — send the retries that are due, and tidy the log.

Where a task backend can delay a task, retries are scheduled when an attempt fails and
this command is only a safety net. Where it cannot — no backend at all, or one without
delayed tasks — it is how a ``retrying`` delivery is ever tried again. Run it from cron
every minute.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retry webhook deliveries that are due, and delete old finished ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-sweep",
            action="store_true",
            help="Skip deleting finished deliveries older than WEBHOOKS_RETENTION_DAYS.",
        )

    def handle(self, *args, **options):
        from vantage import webhooks

        tried = webhooks.retry_due()
        self.stdout.write(f"Tried {tried} due deliver{'y' if tried == 1 else 'ies'}.")
        if not options["no_sweep"]:
            removed = webhooks.sweep()
            self.stdout.write(f"Removed {removed} old deliver{'y' if removed == 1 else 'ies'}.")
