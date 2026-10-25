"""The dashboard, as arranged from the dashboard itself.

One row per person per mounted panel, plus a row with a ``NULL`` user that is the
arrangement everybody else opens on — the same two-row cascade ``ListPreference``
already uses one level down, and the same meaning for ``NULL``: the code decides.

The two partial unique indexes are not interchangeable with one plain one. SQL treats
NULLs as distinct, so a single unique index over ``(site_name, user)`` would police the
per-user rows and quietly let site-wide rows multiply.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0006_nav_configuration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DashboardLayout",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("site_name", models.CharField(default="vantage", max_length=100)),
                ("cards", models.JSONField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vantage_dashboard_layouts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "dashboard layout",
                "verbose_name_plural": "dashboard layouts",
                "ordering": ["site_name", "pk"],
            },
        ),
        migrations.AddIndex(
            model_name="dashboardlayout",
            index=models.Index(fields=["site_name", "user"], name="vantage_dashlayout_idx"),
        ),
        migrations.AddConstraint(
            model_name="dashboardlayout",
            constraint=models.UniqueConstraint(
                condition=models.Q(("user__isnull", False)),
                fields=("site_name", "user"),
                name="vantage_dashlayout_unique_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="dashboardlayout",
            constraint=models.UniqueConstraint(
                condition=models.Q(("user__isnull", True)),
                fields=("site_name",),
                name="vantage_dashlayout_unique_default",
            ),
        ),
    ]
