"""Async jobs of the skills app (registered at startup by internest_core.async_tasks.autodiscover).
Every job returns (http_status, json_body), the shape the status endpoint hands back to the browser."""
import logging

from django.urls import reverse

from internest_core.async_tasks import RetryTask, TaskThrottled, enqueue, register

logger = logging.getLogger(__name__)


@register("skill_challenge_start")
def skill_challenge_start_job(user_id, student_skill_id):
    from internest_core.models import StudentProfile

    from .views import challenge_outcome

    return challenge_outcome(StudentProfile.objects.get(user_id=user_id), student_skill_id)


@register("cv_analysis")
def cv_analysis_job(user_id, analysis_id, text):
    from . import cv_analysis

    return cv_analysis.run(analysis_id, text)


@register("skill_extract")
def skill_extract_job(user_id, text):
    """Skills in a pasted text (/api/ai/skills/extract/): nothing is saved, the student decides what to keep."""
    from .cv_analysis import analyze_cv

    found = analyze_cv(text, user=user_id)
    return 200, {
        "skills": [{"skill_id": e.skill.pk, "name": e.skill.name, "provenance": e.provenance, "confidence": e.confidence,
                    "evidence": e.evidence} for e in found.skills],
        "skills_to_confirm": found.to_confirm,
        "ai_status": found.ai_status,
        "message": found.ai_message,
    }


@register("quiz_verify")
def quiz_verify_job(user_id, attempt_id, item_ids, student_user_id=None):
    from internest_ai.errors import AIUnavailable, QuotaExceeded

    from .quizgen import verify_items

    try:
        result = verify_items(item_ids, attempt_id=attempt_id, user_id=student_user_id)
    except QuotaExceeded:  # the questions stay unchecked (and unreviewed); staff can still review them
        return 200, {"checked": 0, "skipped": "quota"}
    except AIUnavailable as exc:
        raise RetryTask(str(exc), delay=60) from exc
    return 200, result


@register("answer_evaluation")
def answer_evaluation_job(user_id, response_id):
    """Advisory AI grading of one written answer, for the reviewer who asked (staff only, checked by the view)."""
    from internest_ai.errors import AIUnavailable

    from . import review
    from .models import ChallengeResponse

    response = ChallengeResponse.objects.select_related("item", "attempt").get(pk=response_id)
    try:
        response.ai_suggestion = review.ai_suggestion(response, user=user_id)
    except AIUnavailable as exc:
        return 503, {"code": "ai_unavailable", "error": getattr(exc, "user_message", "") or "AI unavailable",
                     "redirect": reverse("skills_review_detail", args=[response.attempt.token])}
    response.save(update_fields=["ai_suggestion"])
    return 200, {"ai_suggestion": response.ai_suggestion,
                 "redirect": reverse("skills_review_detail", args=[response.attempt.token])}


@register("skills_relate")
def skills_relate_job(user_id, skill_ids):
    """Semantic relations for skills a partner just defined (was run inside the partner's submit request)."""
    from .models import Skill
    from .semantic import relate_skill_ai

    written = {skill.pk: relate_skill_ai(skill) for skill in Skill.objects.filter(pk__in=skill_ids)}
    return 200, {"written": written}


def relate_new_skills(skill_ids) -> None:
    """Queue the AI relation of new skills (system job). A full queue only delays the links: log and move on."""
    if not skill_ids:
        return
    try:
        enqueue("skills_relate", None, idempotency_key=f"relate:{','.join(map(str, sorted(skill_ids)))}", priority=-1,
                skill_ids=list(skill_ids))
    except TaskThrottled as exc:
        logger.warning("Skill relations for %s not queued: %s", skill_ids, exc.code)
