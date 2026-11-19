"""Staff review of assessment attempts that wait for a human decision (see review.py)."""
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from internest_core.views import get_user_context

from . import review
from .models import ChallengeAttempt, ChallengeItem

SIGNAL_LABELS = {
    "fast_correct_answers": gettext_lazy("Many correct answers in under 2 seconds"),
    "similar_answer": gettext_lazy("Written answer nearly identical to another student's"),
    "typing_flags": gettext_lazy("Paste or typing-pattern flags"),
    "focus_warnings": gettext_lazy("Left the test window (within the allowed limit)"),
    "practical_review": gettext_lazy("Practical task to grade"),
}


def _labelled(attempt):
    attempt.review_reasons = [{**s, "label": SIGNAL_LABELS.get(s.get("type"), s.get("type"))} for s in attempt.review_reasons or []]
    return attempt


@staff_member_required
def review_queue(request):
    context = get_user_context(request)
    context["attempts"] = [_labelled(a) for a in review.pending_queue()]
    return render(request, "skills/review_queue.html", context)


@staff_member_required
def review_detail(request, token):
    attempt = get_object_or_404(ChallengeAttempt.objects.select_related("student_skill__skill", "student_skill__student__user"),
                                token=token, review_status=ChallengeAttempt.REVIEW_PENDING)
    responses = list(attempt.responses.select_related("item__sub_skill").order_by("pk"))
    written = [r for r in responses if r.item.kind != ChallengeItem.KIND_MCQ]
    if request.method == "POST":
        decision = request.POST.get("decision")
        if decision in ("approve", "reject"):
            credits = {}
            for r in written:
                raw = request.POST.get(f"credit_{r.pk}", "")
                try:
                    credits[str(r.pk)] = min(max(int(raw), 0), 100) / 100
                except (TypeError, ValueError):
                    messages.error(request, _("Give every written answer a credit between 0 and 100."))
                    return redirect("skills_review_detail", token=token)
            try:
                done = review.apply_review(attempt.pk, request.user, decision == "approve", credits, request.POST.get("note", ""))
            except ChallengeAttempt.DoesNotExist:  # another reviewer decided it a moment ago
                messages.info(request, _("This attempt was already decided by another reviewer."))
                return redirect("skills_review_queue")
            ss = done.student_skill
            ss.refresh_from_db()
            messages.success(request, _("Saved. Final score %(s)s%% · skill status: %(status)s.") % {
                "s": done.score_pct, "status": ss.get_status_display()})
            return redirect("skills_review_queue")
    similar_ids = {s.get("response") for s in attempt.review_reasons or [] if s.get("type") == "similar_answer"}
    for r in written:
        r.criteria = review.criteria(r.item) if r.item.kind == ChallengeItem.KIND_PRACTICAL else []
        r.auto_pct = round(r.credit * 100)
        r.ai_pct = round((r.ai_suggestion or {}).get("credit", 0) * 100) if r.ai_suggestion else None
        r.default_pct = round((r.review_credit if r.review_credit is not None else r.credit) * 100)
    context = get_user_context(request)
    context.update({"attempt": _labelled(attempt), "written": written, "similar_ids": similar_ids})
    return render(request, "skills/review_detail.html", context)
