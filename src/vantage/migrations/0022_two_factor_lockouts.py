from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0021_elevated_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="twofactordevice",
            name="lockouts",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
