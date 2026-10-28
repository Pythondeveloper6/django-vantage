import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("vantage", "0019_approvals"),
    ]

    operations = [
        migrations.CreateModel(
            name="FieldPermission",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("field_name", models.CharField(max_length=255)),
                (
                    "level",
                    models.CharField(
                        choices=[("readonly", "Read-only"), ("hidden", "Hidden")], max_length=16
                    ),
                ),
                (
                    "content_type",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="contenttypes.contenttype",
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vantage_field_permissions",
                        to="auth.group",
                    ),
                ),
            ],
            options={
                "verbose_name": "field permission",
                "verbose_name_plural": "field permissions",
                "default_permissions": (),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("group", "content_type", "field_name"),
                        name="vantage_field_perm_unique",
                    )
                ],
            },
        ),
    ]
