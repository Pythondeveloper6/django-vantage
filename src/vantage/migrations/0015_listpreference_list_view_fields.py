from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0014_undo_entry"),
    ]

    operations = [
        migrations.AddField(
            model_name="listpreference",
            name="list_view_fields",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
