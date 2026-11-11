from django.contrib.auth.models import Group, Permission, User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create demo logins: admin (superuser) and support (limited role)."

    def handle(self, *args, **options):
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@example.com", "admin12345")

        role, _ = Group.objects.get_or_create(name="Support")
        perms = Permission.objects.filter(
            content_type__app_label="shop",
            codename__in=[
                "view_order",
                "change_order",
                "view_customer",
                "view_product",
                "view_category",
                "view_orderitem",
            ],
        )
        role.permissions.set(perms)

        # Lesson 6: a role for an agent's service user. It can edit products and only read
        # customers; the agent identity can narrow this further, never widen it.
        agents, _ = Group.objects.get_or_create(name="Catalogue agent")
        agents.permissions.set(
            Permission.objects.filter(
                content_type__app_label="shop",
                codename__in=[
                    "view_product",
                    "change_product",
                    "view_category",
                    "view_customer",
                ],
            )
        )

        if not User.objects.filter(username="support").exists():
            user = User.objects.create_user("support", "support@example.com", "support12345")
            user.is_staff = True
            user.save()
            user.groups.add(role)
        self.stdout.write("Logins ready: admin / admin12345, support / support12345")
