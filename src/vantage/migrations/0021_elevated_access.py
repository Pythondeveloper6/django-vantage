import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("vantage", "0020_field_permissions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ElevatedAccess",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("user_repr", models.CharField(blank=True, max_length=150)),
                ("role_repr", models.CharField(blank=True, max_length=150)),
                ("permissions", models.JSONField(blank=True, default=list)),
                ("reason", models.TextField()),
                ("minutes", models.PositiveIntegerField()),
                ("requested_by_repr", models.CharField(blank=True, max_length=150)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("granted_by_repr", models.CharField(blank=True, max_length=150)),
                ("granted_at", models.DateTimeField(blank=True, null=True)),
                ("starts_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_by_repr", models.CharField(blank=True, max_length=150)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revoke_reason", models.TextField(blank=True)),
                ("used", models.JSONField(blank=True, default=list)),
                ("logged_state", models.CharField(blank=True, max_length=10)),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="auth.group",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "elevated access",
                "verbose_name_plural": "elevated access grants",
                "ordering": ["-requested_at", "-pk"],
                "default_permissions": (),
                "indexes": [
                    models.Index(fields=["user", "expires_at"], name="vantage_elevated_user_idx")
                ],
            },
        ),
    ]
