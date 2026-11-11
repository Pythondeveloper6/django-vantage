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
        undo = sub.add_parser("undo-run", help="Revert everything one run of an agent did.")
        undo.add_argument("name", help="The agent's name.")
        undo.add_argument("run_key")
        undo.add_argument("--dry-run", action="store_true", help="Report; change nothing.")
        undo.add_argument(
            "--as",
            dest="as_user",
            metavar="USERNAME",
            help="Act as this user (default: the agent's owner). Must be a superuser or the owner.",
        )

    def handle(self, *args, **options):
        return getattr(self, "_" + options["command"].replace("-", "_"))(options)

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

    def _undo_run(self, options):
        from vantage.models import AgentRun

        identity = self._find(options["name"])
        try:
            run = AgentRun.objects.select_related("identity__owner").get(
                identity=identity, run_key=options["run_key"]
            )
        except AgentRun.DoesNotExist:
            raise CommandError(
                f"Agent {identity.name!r} has no run {options['run_key']!r}."
            ) from None
        User = get_user_model()
        if options["as_user"]:
            user = User._default_manager.filter(**{User.USERNAME_FIELD: options["as_user"]}).first()
            if user is None:
                raise CommandError(f"No user called {options['as_user']!r}.")
        else:
            user = identity.owner
            if user is None:
                raise CommandError("This agent has no owner. Pass --as <superuser or owner>.")
        try:
            report = agents.undo_run(run, user, dry_run=options["dry_run"])
        except agents.UndoNotAllowed as exc:
            raise CommandError(str(exc)) from None
        mode = "Would revert" if options["dry_run"] else "Reverted"
        self.stdout.write(
            f"{mode} {len(report['reverted'])}, skipped {len(report['skipped'])}, "
            f"conflicts {len(report['conflicts'])}."
        )
        for bucket in ("reverted", "skipped", "conflicts"):
            for item in report[bucket]:
                detail = (
                    item.get("reason") or f"{item['action']} {', '.join(item['fields'])}".strip()
                )
                self.stdout.write(f"  {bucket}\t{item['object']}\t{detail}")
        return None
