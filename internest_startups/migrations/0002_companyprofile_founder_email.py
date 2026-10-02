from django.db import migrations, models

SOCIAL = ("linkedin_url", "facebook_url", "twitter_url", "instagram_url")


def recompute_startup_completion(apps, schema_editor):
    """Startup completion no longer jumps to 100% on verification; recompute from fields
    (mirrors internest_startups.completion.startup_completion)."""
    Partner = apps.get_model("internest_core", "PartnerProfile")
    Company = apps.get_model("internest_startups", "CompanyProfile")
    profiles = {c.partner_id: c for c in Company.objects.all()}
    for p in Partner.objects.filter(is_academic=False):
        c = profiles.get(p.pk)
        checks = [
            bool(p.company_name) and not p.company_name.startswith("__pending__"),
            bool(p.official_email),
            bool(c and c.founder_email),
            bool(c and c.industry),
            bool(c and c.founded_year),
            bool(c and (c.description or "").strip()),
            bool(p.official_website),
            any(getattr(p, f) for f in SOCIAL),
        ]
        score = sum(checks) * 100 // len(checks)
        if score != p.profile_completion_score:
            Partner.objects.filter(pk=p.pk).update(profile_completion_score=score)


class Migration(migrations.Migration):

    dependencies = [
        ('internest_startups', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='companyprofile',
            name='founder_email',
            field=models.EmailField(blank=True, max_length=254, verbose_name='Founder official email'),
        ),
        migrations.RunPython(recompute_startup_completion, migrations.RunPython.noop),
    ]
