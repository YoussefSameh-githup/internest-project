from django.core.management.base import BaseCommand

from internest_skills.models import Skill
from internest_skills.semantic import relate_skill_ai


class Command(BaseCommand):
    help = "Generate AI semantic relations for skills used as required skills (or all with --all). Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Relate every active skill, not only required ones.")
        parser.add_argument("--stale-only", action="store_true", help="Skip skills that were already related.")

    def handle(self, *args, **opts):
        qs = Skill.objects.filter(is_active=True)
        if not opts["all"]:
            qs = qs.filter(opportunities__isnull=False).distinct()
        if opts["stale_only"]:
            qs = qs.filter(relations_refreshed_at__isnull=True)
        total = 0
        for skill in qs:
            n = relate_skill_ai(skill)
            total += n
            self.stdout.write(f"{skill.name}: {n} relations")
        self.stdout.write(self.style.SUCCESS(f"Done: {total} relations written."))
