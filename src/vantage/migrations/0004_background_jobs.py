"""Background jobs: one table, holding a predicate rather than a selection.

The column that decides the shape of this table is ``selection``. It stores the filter
query string plus the handful of rows the user unticked, so one row describes a
40,000-row bulk action in a few hundred bytes and re-resolves against the data as it
stands when the worker gets to it. Storing the resolved primary keys instead would make
the row size proportional to the selection and freeze the scope at click time.

``processed``, ``total`` and ``failed`` are stored rather than derived because a
progress bar has to read them without recomputing anything, and because a job whose
worker was killed must still be able to say how far it got.

Depends only on the user model — no ``contenttypes``. The model is identified by its
``app_label.modelname`` label, so a job for a model that has since been unregistered
stays listable instead of cascading away. See :mod:`vantage.jobs`.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('vantage', '0003_list_preferences'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VantageJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('actor_repr', models.CharField(blank=True, max_length=150)),
                ('resource_key', models.CharField(max_length=100)),
                ('action_name', models.CharField(max_length=100)),
                ('action_label', models.CharField(blank=True, max_length=150)),
                ('selection', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('pending', 'Queued'), ('running', 'Running'), ('succeeded', 'Finished'), ('failed', 'Failed'), ('denied', 'Stopped — permission withdrawn')], default='pending', max_length=12)),
                ('total', models.PositiveIntegerField(default=0)),
                ('processed', models.PositiveIntegerField(default=0)),
                ('failed', models.PositiveIntegerField(default=0)),
                ('summary', models.CharField(blank=True, max_length=300)),
                ('errors', models.JSONField(blank=True, default=list)),
                ('error', models.TextField(blank=True)),
                ('backend', models.CharField(blank=True, max_length=20)),
                ('task_id', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='vantage_jobs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'background job',
                'verbose_name_plural': 'background jobs',
                'ordering': ['-created_at', '-pk'],
                'indexes': [models.Index(fields=['actor', '-created_at'], name='vantage_job_actor_idx'), models.Index(fields=['status', '-created_at'], name='vantage_job_status_idx'), models.Index(fields=['resource_key', '-created_at'], name='vantage_job_model_idx')],
            },
        ),
    ]
