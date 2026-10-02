from django.db import migrations, models


def keep_first_vote_per_student(apps, schema_editor):
    """Old rule allowed one vote per university; keep each student's earliest vote so the new constraint applies."""
    Vote = apps.get_model("internest_campus", "CampusDemandVote")
    seen = set()
    for vote in Vote.objects.order_by("student_id", "created_at", "id").only("id", "student_id"):
        if vote.student_id in seen:
            vote.delete()
        else:
            seen.add(vote.student_id)


class Migration(migrations.Migration):

    dependencies = [
        ('internest_campus', '0001_initial'),
        ('internest_core', '0005_email_verification'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='campusdemandvote',
            name='one_vote_per_user_per_university',
        ),
        migrations.RunPython(keep_first_vote_per_student, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='campusdemandvote',
            constraint=models.UniqueConstraint(fields=('student',), name='one_vote_per_student_account'),
        ),
    ]
