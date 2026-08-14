"""The sidebar, as arranged from the settings screen.

One row, one JSON column, and ``NULL`` meaning "the code decides" — the same bargain
every column on ``SiteConfiguration`` makes, one level up: an absent row is not an empty
sidebar, it is no opinion at all, and the panel falls back to ``VANTAGE["NAV"]`` and
then to per-app inference.

No ``contenttypes`` dependency and no foreign key to anything the arrangement names.
Items point at models, permissions and groups by *text*, so unregistering a model or
renaming a group leaves an inert entry the editor can show and remove rather than a
cascade that deletes somebody's sidebar. See :mod:`vantage.nav`.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vantage', '0005_alter_vantagelogentry_action'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='NavConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('groups', models.JSONField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'sidebar',
                'verbose_name_plural': 'sidebar',
            },
        ),
    ]
