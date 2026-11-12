from django.db import migrations
from django.db.models import OuterRef, Subquery


def codes_to_their_owner(apps, schema_editor):
    """Each recovery code belongs to the person who owned the device it was issued for."""
    Code = apps.get_model("vantage", "TwoFactorRecoveryCode")
    Device = apps.get_model("vantage", "TwoFactorDevice")
    owner = Device.objects.filter(pk=OuterRef("device_id")).values("user_id")[:1]
    Code.objects.filter(user__isnull=True).update(user_id=Subquery(owner))


def codes_back_to_a_device(apps, schema_editor):
    """Reverse: each code goes back to its owner's oldest device, when they have one."""
    Code = apps.get_model("vantage", "TwoFactorRecoveryCode")
    Device = apps.get_model("vantage", "TwoFactorDevice")
    oldest = Device.objects.filter(user_id=OuterRef("user_id")).order_by("pk").values("pk")[:1]
    Code.objects.filter(device__isnull=True).update(device_id=Subquery(oldest))


class Migration(migrations.Migration):
    dependencies = [
        ("vantage", "0023_two_factor_many_devices"),
    ]

    operations = [
        migrations.RunPython(codes_to_their_owner, codes_back_to_a_device),
    ]
