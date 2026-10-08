from django.db import migrations

QUIZ_LENGTH = 10


def raise_length(apps, schema_editor):
    apps.get_model("internest_skills", "Skill").objects.filter(challenge_length__lt=QUIZ_LENGTH).update(challenge_length=QUIZ_LENGTH)


class Migration(migrations.Migration):
    dependencies = [("internest_skills", "0004_semantic_skill_relations")]

    operations = [migrations.RunPython(raise_length, migrations.RunPython.noop)]
