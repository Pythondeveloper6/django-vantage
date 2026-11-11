import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Step one of three: let a person have several authenticators.

    Additive only. The existing device rows keep working, with the default name; the
    recovery codes gain a nullable owner that the next migration fills in.
    """

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("vantage", "0022_two_factor_lockouts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="twofactordevice",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="twofactordevice",
            name="name",
            field=models.CharField(default="Authenticator", max_length=64),
        ),
        migrations.AddField(
            model_name="twofactorrecoverycode",
            name="user",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="twofactorrecoverycode",
            name="device",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="codes",
                to="vantage.twofactordevice",
            ),
        ),
    ]
