"""``manage.py vantage_two_factor_reset <username>`` — switch someone's second factor off.

The reset screen in the panel needs a superuser who can still sign in. This is for the
morning that the only superuser's phone is the one that went in the lake: whoever can
run ``manage.py`` on the server already holds every key there is, so it asks for
nothing more. The reset is written to the audit log with no actor, which is how the
log spells "from the command line".
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Switch off two-factor sign-in for one account, so it can sign in with its password."

    def add_arguments(self, parser):
        parser.add_argument("username", help="The account's username (its USERNAME_FIELD).")

    def handle(self, *args, **options):
        from vantage.twofactor import reset

        model = get_user_model()
        try:
            user = model._default_manager.get_by_natural_key(options["username"])
        except model.DoesNotExist as exc:
            raise CommandError(f"No account with the username {options['username']!r}.") from exc
        if reset(None, user):
            self.stdout.write(f"Two-factor sign-in is now off for {user.get_username()}.")
        else:
            self.stdout.write(f"{user.get_username()} did not have two-factor sign-in on.")
