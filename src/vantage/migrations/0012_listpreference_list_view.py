from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0011_navconfiguration_known_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="listpreference",
            name="list_view",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
    ]
