"""Per-list customisation: one table, and the reason it is shaped like this.

One row per person per model, plus at most one row per model with a ``NULL`` user that
is the site-wide default. Every setting column is nullable because ``NULL`` is the
feature: it means "whatever ``panels.py`` says", which is what keeps changing the
columns of a list from also freezing its filters. See :mod:`vantage.listprefs`.

Two unique constraints rather than one, because SQL treats NULLs as distinct: a single
constraint over ``(resource_key, user)`` polices the per-user rows and would quietly
allow a second site-wide default.

Depends only on the user model. There is deliberately no ``contenttypes`` dependency —
the model is identified by its label as text, so a preference for a model that has
since been renamed or removed stays listable instead of cascading away.
"""

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0002_audit_log_and_saved_views"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ListPreference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("resource_key", models.CharField(max_length=100)),
                ("columns", models.JSONField(blank=True, null=True)),
                (
                    "per_page",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(500),
                        ],
                    ),
                ),
                ("search_enabled", models.BooleanField(blank=True, null=True)),
                ("search_fields", models.JSONField(blank=True, null=True)),
                ("filters_enabled", models.BooleanField(blank=True, null=True)),
                ("filter_fields", models.JSONField(blank=True, null=True)),
                ("facet_counts", models.BooleanField(blank=True, null=True)),
                ("stats_enabled", models.BooleanField(blank=True, null=True)),
                ("stat_keys", models.JSONField(blank=True, null=True)),
                ("extra_stats", models.JSONField(blank=True, null=True)),
                ("charts_enabled", models.BooleanField(blank=True, null=True)),
                ("chart_keys", models.JSONField(blank=True, null=True)),
                ("extra_charts", models.JSONField(blank=True, null=True)),
                ("summary_enabled", models.BooleanField(blank=True, null=True)),
                ("summary_aggregates", models.JSONField(blank=True, null=True)),
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
                        related_name="vantage_list_preferences",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "list preference",
                "verbose_name_plural": "list preferences",
                "ordering": ["resource_key", "pk"],
                "indexes": [
                    models.Index(fields=["resource_key", "user"], name="vantage_listpref_idx")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("user__isnull", False)),
                        fields=("resource_key", "user"),
                        name="vantage_listpref_unique_user",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("user__isnull", True)),
                        fields=("resource_key",),
                        name="vantage_listpref_unique_default",
                    ),
                ],
            },
        ),
    ]
