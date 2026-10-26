from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0011_navconfiguration_known_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="ui_theme",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="ui_theme_toggle",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
