"""The audit log and saved views: four tables, and the reason there are only four.

``VantageLogEntry`` is one generic-FK table in *this* app rather than a history model
in the user's — that is the whole reason ``django-simple-history`` is soft-detected
instead of required, and it is why installing Vantage still costs a project exactly
zero migrations of its own.

The three saved-view tables are one row per view, one per grant and one per person who
uses it. The third exists because "make this my default" has to be a personal act on a
shared view; folding it into ``SavedView`` would make one person's preference change
everybody's screen, which is the same failure the no-autosave rule exists to prevent.

Depends on ``auth`` for ``Group`` (a grant can name a group) and on ``contenttypes``
for the log's generic foreign key.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("vantage", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SavedView",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("model_label", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=120)),
                ("description", models.CharField(blank=True, max_length=300)),
                (
                    "visibility",
                    models.CharField(
                        choices=[
                            ("private", "Only me"),
                            ("shared", "Everyone who can see this model"),
                        ],
                        default="private",
                        max_length=10,
                    ),
                ),
                (
                    "edit_scope",
                    models.CharField(
                        choices=[
                            ("owner", "Only the owner"),
                            ("editors", "The owner and named editors"),
                            ("everyone", "Anyone who can see it"),
                        ],
                        default="owner",
                        max_length=10,
                    ),
                ),
                ("locked", models.BooleanField(default=False)),
                ("state", models.JSONField(blank=True, default=dict)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vantage_saved_views",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
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
            ],
            options={
                "verbose_name": "saved view",
                "verbose_name_plural": "saved views",
                "ordering": ["name", "pk"],
            },
        ),
        migrations.CreateModel(
            name="SavedViewGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("viewer", "Can use"), ("editor", "Can use and change")],
                        default="viewer",
                        max_length=10,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="auth.group",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "view",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="grants",
                        to="vantage.savedview",
                    ),
                ),
            ],
            options={
                "verbose_name": "saved view grant",
                "verbose_name_plural": "saved view grants",
            },
        ),
        migrations.CreateModel(
            name="SavedViewUserState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("pinned", models.BooleanField(default=False)),
                ("is_default", models.BooleanField(default=False)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "view",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user_states",
                        to="vantage.savedview",
                    ),
                ),
            ],
            options={
                "verbose_name": "saved view state",
                "verbose_name_plural": "saved view states",
            },
        ),
        migrations.CreateModel(
            name="VantageLogEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("actor_repr", models.CharField(blank=True, max_length=150)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("create", "Created"),
                            ("update", "Updated"),
                            ("delete", "Deleted"),
                            ("action", "Ran an action"),
                        ],
                        max_length=10,
                    ),
                ),
                ("action_name", models.CharField(blank=True, max_length=100)),
                ("object_id", models.CharField(blank=True, max_length=255)),
                ("object_repr", models.CharField(blank=True, max_length=200)),
                ("changes", models.JSONField(blank=True, default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=400)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
            ],
            options={
                "verbose_name": "log entry",
                "verbose_name_plural": "log entries",
                "ordering": ["-created_at", "-pk"],
            },
        ),
        migrations.AddIndex(
            model_name="savedview",
            index=models.Index(fields=["model_label"], name="vantage_view_model_idx"),
        ),
        migrations.AddIndex(
            model_name="savedview",
            index=models.Index(fields=["owner", "model_label"], name="vantage_view_owner_idx"),
        ),
        migrations.AddConstraint(
            model_name="savedview",
            constraint=models.UniqueConstraint(
                fields=("owner", "model_label", "name"), name="vantage_view_unique_name_per_owner"
            ),
        ),
        migrations.AddConstraint(
            model_name="savedviewgrant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("group__isnull", True), ("user__isnull", False)),
                    models.Q(("group__isnull", False), ("user__isnull", True)),
                    _connector="OR",
                ),
                name="vantage_grant_user_xor_group",
            ),
        ),
        migrations.AddConstraint(
            model_name="savedviewgrant",
            constraint=models.UniqueConstraint(
                condition=models.Q(("user__isnull", False)),
                fields=("view", "user"),
                name="vantage_grant_unique_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="savedviewgrant",
            constraint=models.UniqueConstraint(
                condition=models.Q(("group__isnull", False)),
                fields=("view", "group"),
                name="vantage_grant_unique_group",
            ),
        ),
        migrations.AddConstraint(
            model_name="savedviewuserstate",
            constraint=models.UniqueConstraint(
                fields=("view", "user"), name="vantage_view_state_unique"
            ),
        ),
        migrations.AddIndex(
            model_name="vantagelogentry",
            index=models.Index(fields=["content_type", "object_id"], name="vantage_log_object_idx"),
        ),
        migrations.AddIndex(
            model_name="vantagelogentry",
            index=models.Index(fields=["-created_at"], name="vantage_log_recent_idx"),
        ),
        migrations.AddIndex(
            model_name="vantagelogentry",
            index=models.Index(fields=["actor", "-created_at"], name="vantage_log_actor_idx"),
        ),
    ]
