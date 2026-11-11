"""``manage.py vantage_agent`` - create, list, revoke and rotate agent identities.

The token is printed once, by ``create`` and ``rotate``, and cannot be shown again.
See :mod:`vantage.agents`.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from vantage import agents
from vantage.models import AgentIdentity

WARNING = "Copy this token now. It is shown once and cannot be recovered."


class Command(BaseCommand):
    help = "Create, list, revoke or rotate agent identities."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="command", required=True)
        create = sub.add_parser("create", help="Create an identity and print its token.")
        create.add_argument("--name", required=True)
        create.add_argument("--user", required=True, help="Service user (created if missing).")
        create.add_argument("--owner", help="Username the agent acts on behalf of.")
        create.add_argument("--access", choices=["read", "write"], default="read")
        create.add_argument("--models", nargs="*", default=[], metavar="app.model")
        create.add_argument("--allow-delete", action="store_true")
        create.add_argument("--require-approval", action="store_true")
        create.add_argument("--expires-in", type=int, metavar="DAYS")
        create.add_argument("--rate", type=int, metavar="N", help="Requests per minute.")
        create.add_argument("--max-writes", type=int, default=0, metavar="N")
        sub.add_parser("list", help="List identities.")
        revoke = sub.add_parser("revoke", help="Revoke an identity's token.")
        revoke.add_argument("name")
        rotate = sub.add_parser("rotate", help="Issue a new token; the old one dies at once.")
        rotate.add_argument("name")

    def handle(self, *args, **options):
        return getattr(self, f"_{options['command']}")(options)

    def _find(self, name):
        try:
            return AgentIdentity.objects.get(name=name)
        except AgentIdentity.DoesNotExist:
            raise CommandError(f"No agent called {name!r}.") from None

    def _create(self, options):
        User = get_user_model()
        field = User.USERNAME_FIELD
        if AgentIdentity.objects.filter(name=options["name"]).exists():
            raise CommandError(f"An agent called {options['name']!r} already exists.")
        user = User._default_manager.filter(**{field: options["user"]}).first()
        if user is None:
            user = User(**{field: options["user"]}, is_staff=True, is_superuser=False)
            user.set_unusable_password()
            user.save()
        owner = None
        if options["owner"]:
            owner = User._default_manager.filter(**{field: options["owner"]}).first()
            if owner is None:
                raise CommandError(f"No user called {options['owner']!r}.")
        expires = (
            timezone.now() + timedelta(days=options["expires_in"])
            if options["expires_in"]
            else None
        )
        try:
            identity, token = agents.create_identity(
                options["name"],
                user,
                owner,
                access=options["access"],
                allowed_models=options["models"],
                allow_delete=options["allow_delete"],
                require_approval=options["require_approval"],
                expires_at=expires,
                rate_limit_per_minute=options["rate"],
                max_writes_per_run=options["max_writes"],
            )
        except agents.AgentError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"Created agent {identity.name!r} running as {user.get_username()}.")
        self.stdout.write(self.style.WARNING(WARNING))
        self.stdout.write(token)

    def _list(self, options):
        for identity in AgentIdentity.objects.select_related("user", "owner"):
            owner = identity.owner.get_username() if identity.owner else "-"
            self.stdout.write(
                f"{identity.name}\t{identity.status}\t{identity.access}\t"
                f"user={identity.user.get_username()}\towner={owner}\t"
                f"prefix={identity.token_prefix}\t"
                f"last_used={identity.last_used_at.isoformat() if identity.last_used_at else '-'}"
            )

    def _revoke(self, options):
        identity = self._find(options["name"])
        identity.revoked_at = identity.revoked_at or timezone.now()
        identity.save(update_fields=["revoked_at"])
        self.stdout.write(f"Revoked agent {identity.name!r}.")

    def _rotate(self, options):
        identity = self._find(options["name"])
        if identity.revoked_at is not None:
            raise CommandError("That agent is revoked. Create a new one.")
        token = agents.rotate_token(identity)
        self.stdout.write(f"Rotated the token of {identity.name!r}; the old one no longer works.")
        self.stdout.write(self.style.WARNING(WARNING))
        self.stdout.write(token)
