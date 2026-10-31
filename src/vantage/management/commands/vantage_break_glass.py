"""``manage.py vantage_break_glass`` — grant, revoke and list elevated access from a shell.

For the emergency in which nobody who could grant it can reach the panel. Shell access
is already beyond anything the panel restricts, so *who* may grant is not checked here;
the reason, the target and the duration cap are, and every grant and revocation is
written to the audit log like one made on the screen. See :mod:`vantage.breakglass`.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


def _user(value: str):
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    user = user_model._default_manager.filter(**{user_model.USERNAME_FIELD: value}).first()
    if user is None:
        raise CommandError(f"No user called {value!r}.")
    return user


def _request(actor):
    from django.http import HttpRequest

    request = HttpRequest()
    request.user = actor
    request.session = {}
    return request


class Command(BaseCommand):
    help = "Grant, revoke or list time-boxed elevated access."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="command", required=True)

        grant = sub.add_parser("grant", help="Grant elevated access now.")
        grant.add_argument("username")
        grant.add_argument("--role", help="Name of the role to grant.")
        grant.add_argument(
            "--perm",
            action="append",
            default=[],
            help="A permission as app_label.codename. Repeat for several.",
        )
        grant.add_argument("--minutes", type=int, required=True)
        grant.add_argument("--reason", required=True)
        grant.add_argument("--by", help="Username to record as the granter.")

        revoke = sub.add_parser("revoke", help="End a grant now.")
        revoke.add_argument("id", type=int)
        revoke.add_argument("--reason", default="")
        revoke.add_argument("--by", help="Username to record as the revoker.")

        listing = sub.add_parser("list", help="Active and pending grants.")
        listing.add_argument("--all", action="store_true", help="Include ended grants.")

    def handle(self, *args, **options):
        return getattr(self, f"_{options['command']}")(options)

    def _grant(self, options):
        from django.contrib.auth.models import Group

        from vantage import breakglass

        role = None
        if options["role"]:
            role = Group.objects.filter(name=options["role"]).first()
            if role is None:
                raise CommandError(f"No role called {options['role']!r}.")
        actor = _user(options["by"]) if options["by"] else None
        try:
            grant = breakglass.grant(
                _request(actor) if actor is not None else None,
                user=_user(options["username"]),
                role=role,
                permissions=options["perm"],
                minutes=options["minutes"],
                reason=options["reason"],
                enforce=False,
            )
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from None
        self.stdout.write(
            f"Granted #{grant.pk} to {grant.user_repr} until "
            f"{grant.expires_at.isoformat(timespec='seconds')}."
        )

    def _revoke(self, options):
        from vantage import breakglass
        from vantage.models import ElevatedAccess

        grant = ElevatedAccess.objects.filter(pk=options["id"]).first()
        if grant is None:
            raise CommandError(f"No grant #{options['id']}.")
        if grant.revoked_at is not None:
            raise CommandError(f"Grant #{grant.pk} was already revoked.")
        actor = _user(options["by"]) if options["by"] else None
        breakglass.revoke(
            _request(actor) if actor is not None else None,
            grant,
            reason=options["reason"],
            enforce=False,
        )
        self.stdout.write(f"Revoked #{grant.pk}.")

    def _list(self, options):
        from vantage import breakglass
        from vantage.models import ElevatedAccess

        breakglass.observe_all()
        queryset = ElevatedAccess.objects.order_by("-requested_at", "-pk")
        rows = [g for g in queryset[:500] if options["all"] or g.status in _OPEN]
        if not rows:
            self.stdout.write("No grants.")
            return
        for g in rows:
            until = g.expires_at.isoformat(timespec="seconds") if g.expires_at else "-"
            what = g.role_repr or ", ".join(g.permissions or [])
            self.stdout.write(
                f"#{g.pk}\t{g.status}\t{g.user_repr}\t{what}\tuntil {until}\t{g.reason}"
            )


_OPEN = ("pending", "scheduled", "active")
