"""Repairs databases where 0006 was recorded as applied (e.g. faked) but its schema changes never happened."""
from django.db import migrations

from internest_core.schema_repair import ensure_application_review_columns, ensure_email_verification_indexes


def repair(apps, schema_editor):
    ensure_application_review_columns(schema_editor, apps.get_model("internest_core", "Application"))
    ensure_email_verification_indexes(schema_editor)


class Migration(migrations.Migration):
    dependencies = [("internest_core", "0006_application_lifecycle")]

    operations = [migrations.RunPython(repair, migrations.RunPython.noop)]
