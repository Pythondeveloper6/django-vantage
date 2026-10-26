import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0013_siteconfiguration_ui_theme"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UndoEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("session_nonce", models.CharField(max_length=64)),
                ("resource_key", models.CharField(max_length=100)),
                ("verb", models.CharField(max_length=20)),
                ("summary", models.CharField(max_length=200)),
                ("payload", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("result", models.CharField(blank=True, max_length=300)),
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
                "verbose_name": "undo entry",
                "verbose_name_plural": "undo entries",
                "indexes": [
                    models.Index(fields=["expires_at"], name="vantage_undo_expiry_idx"),
                    models.Index(fields=["user", "-created_at"], name="vantage_undo_user_idx"),
                ],
            },
        ),
    ]
