"""Live check of the AI question generator: `python manage.py test_ai_connection --skill Cybersecurity`.

1. Calls every configured key separately (no failover) with a tiny JSON prompt and prints HTTP status,
   model, latency and, on failure, the provider's exact error (status code + response body).
2. Generates a real 10-question quiz for the skill through the normal failover chain and prints it.
Nothing is saved to the database. Exit code 1 when no key works.
"""
import json
import time

from django.core.management.base import BaseCommand, CommandError

from internest_skills.llm import _ask, describe_error, providers
from internest_skills.models import Discipline, Skill
from internest_skills.quizgen import QUIZ_LENGTH, _clean_item, build_system_prompt, request_raw_items


class Command(BaseCommand):
    help = "Make a live call to Gemini / AgentRouter and print 10 generated questions for a skill."

    def add_arguments(self, parser):
        parser.add_argument("--skill", default="Python", help="Skill name (an existing one, or any free text).")
        parser.add_argument("--count", type=int, default=QUIZ_LENGTH)
        parser.add_argument("--timeout", type=float, default=60)

    def handle(self, *args, skill, count, timeout, **opts):
        chain = providers()
        if not chain:
            raise CommandError("No AI key configured: set GEMINI_API_KEY (and/or API_KEY_1, API_KEY_2, AZURE_OPENAI_API_KEY) in .env.")

        self.stdout.write(self.style.MIGRATE_HEADING("1) Connection check, in failover order (one call per key)"))
        working = []
        for n, p in enumerate(chain, 1):
            started = time.monotonic()
            try:
                reply = _ask(p, "You are a health check.", 'Reply exactly {"ok": true}', max_tokens=20, timeout=timeout, max_retries=0)
            except Exception as exc:  # report every failure mode, never crash the command
                ms = int((time.monotonic() - started) * 1000)
                self.stdout.write(self.style.ERROR(f"  {n}. ✗ {p.label}  {p.base_url}  model={p.model}  {ms} ms\n       {describe_error(exc)}"))
                continue
            ms = int((time.monotonic() - started) * 1000)
            self.stdout.write(self.style.SUCCESS(f"  {n}. ✓ {p.label}  {p.base_url}  model={p.model}  200 OK  {ms} ms  reply={reply}"))
            working.append(p)

        target = Skill.objects.filter(name__iexact=skill).first() or Skill(name=skill, discipline=_guess_discipline(skill))
        bank = target.items.filter(is_active=True).count() if target.pk else 0
        self.stdout.write(f"  {len(chain) + 1}. ⛁ saved question bank (last resort): {bank} questions for “{target.name}”")
        if not working:
            raise CommandError("Every AI key failed; see the errors above (also in logs/ai.log). Quizzes use the saved bank.")
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n2) Generating {count} questions for “{target.name}” (not saved)"))
        self.stdout.write(f"  style: {'technical (code)' if 'TECHNICAL skill' in build_system_prompt(target) else 'scenario'}")
        started = time.monotonic()
        try:
            raw = request_raw_items(target, count=count)
        except Exception as exc:
            raise CommandError(f"Generation failed: {describe_error(exc)}") from exc
        items = [i for i in (_clean_item(r) for r in raw) if i]
        took = time.monotonic() - started
        for n, item in enumerate(items, 1):
            self.stdout.write(f"\n  {n}. [{item['difficulty']} · {item['sub_skill']}] {item['prompt']}")
            if item["code"]:
                self.stdout.write("     " + item["code"].replace("\n", "\n     "))
            for i, choice in enumerate(item["choices"]):
                mark = "✓" if i == item["correct_index"] else " "
                self.stdout.write(f"     {mark} {'ABCDEF'[i]}) {choice}")
        summary = f"\n{len(items)}/{len(raw)} valid questions in {took:.1f}s"
        if len(items) >= min(count, 3):
            self.stdout.write(self.style.SUCCESS(summary + " ✓ AI generation works."))
        else:
            self.stdout.write(self.style.WARNING(summary + " — too few valid questions; raw reply:\n" + json.dumps(raw, ensure_ascii=False)[:1500]))


def _guess_discipline(name: str) -> str:
    from internest_skills.quizgen import TECHNICAL_HINTS
    return Discipline.COMPUTING if any(h in name.lower() for h in TECHNICAL_HINTS) else Discipline.BUSINESS
