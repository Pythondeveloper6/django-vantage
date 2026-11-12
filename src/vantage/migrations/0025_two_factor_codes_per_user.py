import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Step three: recovery codes belong to the person, not to one device."""

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("vantage", "0024_two_factor_codes_to_user"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="twofactorrecoverycode",
            name="vantage_2fa_code_unique",
        ),
        migrations.RemoveField(model_name="twofactorrecoverycode", name="device"),
        migrations.AlterField(
            model_name="twofactorrecoverycode",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="twofactorrecoverycode",
            constraint=models.UniqueConstraint(
                fields=("user", "code_hash"), name="vantage_2fa_code_unique"
            ),
        ),
    ]
