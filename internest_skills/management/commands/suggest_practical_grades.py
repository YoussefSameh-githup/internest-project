"""Fill advisory AI suggestions for practical answers waiting for human review.

Runs outside web requests (schedule it on PythonAnywhere, e.g. hourly), so students never wait for the AI provider.
Every call goes through internest_skills.llm.chat_json, which is monitored by internest_core.api_monitor.
"""
from django.core.management.base import BaseCommand

from internest_skills import llm, review
from internest_skills.models import ChallengeAttempt, ChallengeItem, ChallengeResponse


class Command(BaseCommand):
    help = "Ask the configured AI provider for advisory rubric checks on practical answers that are waiting for review."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if not llm.llm_enabled():
            self.stdout.write("AI is not configured; reviewers grade practical answers without a suggestion.")
            return
        todo = (ChallengeResponse.objects.filter(item__kind=ChallengeItem.KIND_PRACTICAL, ai_suggestion={},
                                                 attempt__review_status=ChallengeAttempt.REVIEW_PENDING)
                .exclude(answer_text="").select_related("item", "attempt__student_skill")[: options["limit"]])
        done = failed = 0
        for response in todo:
            try:
                response.ai_suggestion = review.ai_suggestion(response)
            except Exception as exc:  # provider down or a bad reply: the reviewer grades without it
                failed += 1
                self.stderr.write(f"response {response.pk}: {exc}")
                continue
            response.save(update_fields=["ai_suggestion"])
            done += 1
        self.stdout.write(self.style.SUCCESS(f"{done} suggestion(s) saved, {failed} failed."))
