from django.db import migrations


def sync_existing_profiles(apps, schema_editor):
    """One-time: copy each CV's primary education entry onto StudentProfile.university / major. Idempotent."""
    from internest_students.cv_builder_service import primary_education

    StudentCV = apps.get_model("internest_students", "StudentCV")
    StudentProfile = apps.get_model("internest_core", "StudentProfile")
    for record in StudentCV.objects.exclude(data={}).iterator():
        primary = primary_education((record.data or {}).get("education"))
        if primary:
            StudentProfile.objects.filter(pk=record.student_id).exclude(
                university=primary.get("university", ""), major=primary.get("major", ""),
            ).update(university=primary.get("university", ""), major=primary.get("major", ""))


class Migration(migrations.Migration):
    dependencies = [
        ("internest_students", "0001_initial"),
        ("internest_core", "0020_other_country_industry"),
    ]
    operations = [migrations.RunPython(sync_existing_profiles, migrations.RunPython.noop)]
