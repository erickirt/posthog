# Generated by Django 4.2.18 on 2025-06-04 12:58

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0753_remove_posthog_person_email_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="is_platform",
            field=models.BooleanField(blank=True, default=False, null=True),
        ),
    ]
