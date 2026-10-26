"""``manage.py vantage_purge_undo`` — delete undo entries whose window has passed.

Expired entries are already purged on every write to the undo table, so this is only
needed by a deployment that wants a guarantee about *when* a hard delete's snapshot —
a copy of the deleted rows — is gone, rather than "by the next delete". Run it from
cron as often as that guarantee needs.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete expired undo entries (and the snapshots of deleted rows they hold)."

    def handle(self, *args, **options):
        from vantage.undo import purge_expired

        purged = purge_expired()
        self.stdout.write(f"Purged {purged} expired undo entr{'y' if purged == 1 else 'ies'}.")
