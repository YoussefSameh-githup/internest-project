from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from internest_skills.models import ChallengeItem, Skill, SubSkill
from internest_skills.seed_data import CASE, SKILLS


class Command(BaseCommand):
    help = "Create/update the starter skill taxonomy and challenge bank (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        items_created = 0
        for spec in SKILLS:
            skill, _ = Skill.objects.update_or_create(
                slug=slugify(spec["name"]),
                defaults={
                    "name": spec["name"],
                    "discipline": spec["discipline"],
                    "aliases": spec["aliases"],
                    "implicit_signals": spec["implicit"],
                },
            )
            subs = {}
            for name, keywords in spec["sub_skills"].items():
                subs[name], _ = SubSkill.objects.update_or_create(skill=skill, name=name, defaults={"keywords": keywords})

            for row in spec["items"]:
                kind, sub_name, prompt = row[0], row[1], row[2]
                if kind == CASE:
                    defaults = {"kind": kind, "rubric": row[3], "time_limit_seconds": row[4], "sub_skill": subs[sub_name]}
                else:
                    defaults = {"kind": kind, "choices": row[3], "correct_index": row[4], "time_limit_seconds": row[5], "sub_skill": subs[sub_name]}
                _, created = ChallengeItem.objects.update_or_create(skill=skill, prompt=prompt, defaults=defaults)
                items_created += created

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(SKILLS)} skills, {items_created} new challenge items."))
