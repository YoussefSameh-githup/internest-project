import uuid

from django.db import migrations, models


def fill_tokens(apps, schema_editor):
    TalentInvitation = apps.get_model("internest_startups", "TalentInvitation")
    for invitation in TalentInvitation.objects.filter(token__isnull=True):
        invitation.token = uuid.uuid4()
        invitation.save(update_fields=["token"])


class Migration(migrations.Migration):
    dependencies = [("internest_startups", "0005_talent_invitation")]

    operations = [
        migrations.AddField(
            model_name="talentinvitation",
            name="status",
            field=models.CharField(choices=[("pending", "Pending"), ("accepted", "Applied")], default="pending", max_length=10),
        ),
        migrations.AddField(model_name="talentinvitation", name="accepted_at", field=models.DateTimeField(blank=True, null=True)),
        # Unique token in three steps so existing invitations each get their own value.
        migrations.AddField(model_name="talentinvitation", name="token", field=models.UUIDField(editable=False, null=True)),
        migrations.RunPython(fill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="talentinvitation",
            name="token",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
