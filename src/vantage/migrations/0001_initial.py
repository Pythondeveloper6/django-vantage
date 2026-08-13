"""The package's first migration, and — if the design holds — close to its last.

Vantage configures itself from ``settings.VANTAGE``; the only reason it owns a table at
all is that the person who runs the panel is usually not the person who can edit that
file. Every column is nullable because ``NULL`` is how this schema spells "not
overridden".
"""

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import vantage.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteConfiguration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("site_title", models.CharField(blank=True, max_length=120, null=True)),
                ("site_acronym", models.CharField(blank=True, max_length=8, null=True)),
                (
                    "logo",
                    models.FileField(
                        blank=True,
                        upload_to="vantage/branding/",
                        validators=[vantage.models.validate_branding_upload],
                    ),
                ),
                (
                    "logo_dark",
                    models.FileField(
                        blank=True,
                        upload_to="vantage/branding/",
                        validators=[vantage.models.validate_branding_upload],
                    ),
                ),
                (
                    "favicon",
                    models.FileField(
                        blank=True,
                        upload_to="vantage/branding/",
                        validators=[vantage.models.validate_branding_upload],
                    ),
                ),
                (
                    "login_image",
                    models.FileField(
                        blank=True,
                        upload_to="vantage/branding/",
                        validators=[vantage.models.validate_branding_upload],
                    ),
                ),
                (
                    "brand_color",
                    models.CharField(
                        blank=True,
                        max_length=7,
                        null=True,
                        validators=[vantage.models.validate_brand_color],
                    ),
                ),
                (
                    "brand_radius",
                    models.CharField(
                        blank=True,
                        max_length=16,
                        null=True,
                        validators=[vantage.models.validate_css_length],
                    ),
                ),
                (
                    "theme",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("light", "Light"),
                            ("dark", "Dark"),
                            ("system", "Follow the device"),
                        ],
                        max_length=8,
                        null=True,
                    ),
                ),
                ("theme_toggle", models.BooleanField(blank=True, null=True)),
                ("environment", models.CharField(blank=True, max_length=120, null=True)),
                (
                    "default_per_page",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(500),
                        ],
                    ),
                ),
                ("command_palette", models.BooleanField(blank=True, null=True)),
                ("read_only", models.BooleanField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
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
                "verbose_name": "site configuration",
                "verbose_name_plural": "site configuration",
            },
        ),
    ]
