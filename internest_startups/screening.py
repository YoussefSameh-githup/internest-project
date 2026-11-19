"""Employer screening tasks (Phase 2): a short real task the company attaches to one opportunity.

Every applicant to that opportunity submits an answer before a due date; the company scores it against its own
criteria; when the task is marked required, only applicants who passed it can be shortlisted. Creating or changing a
task is a paid feature (the opportunity must be covered by a Hiring Pass or a legacy Pro subscription). Scoring keeps
working after the pass ends, and the company can always make the task optional or extend the time, so no applicant is
stuck behind it.

Near-identical answers from two applicants are flagged for the company to look at. A flag is a reason to look closer,
never proof of copying.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from internest_skills.review import SIMILARITY_THRESHOLD, comparable, jaccard, own_shingles

MAX_CRITERIA = 6


def can_manage(partner, internship) -> bool:
    from .entitlements import covers

    return covers(partner, internship)


def task_for(internship):
    from .models import ScreeningTask

    return ScreeningTask.objects.filter(internship=internship).first()


def due_at(task, application):
    """Applicants get `due_days` from whichever is later: their application or the task's creation."""
    start = max(application.application_date, task.created_at)
    return start + timedelta(days=task.due_days)


def is_open(task, application) -> bool:
    return timezone.now() <= due_at(task, application)


def submission_for(application):
    from .models import ScreeningSubmission

    return ScreeningSubmission.objects.filter(application=application).first()


def submit(task, application, answer_text, link=""):
    """Store the applicant's one submission. Raises ValueError with a user message."""
    from django.db import IntegrityError

    from .models import ScreeningSubmission

    if not is_open(task, application):
        raise ValueError(_("The due date for this task has passed."))
    answer_text = (answer_text or "").strip()
    link = (link or "").strip()
    if link and not link.lower().startswith(("https://", "http://")):
        raise ValueError(_("Enter a valid link starting with https://"))
    if task.deliverable == task.DELIVER_TEXT and not answer_text:
        raise ValueError(_("Write your answer before submitting."))
    if task.deliverable == task.DELIVER_LINK and not link:
        raise ValueError(_("Add the link to your work before submitting."))
    if task.deliverable == task.DELIVER_BOTH and not (answer_text and link):
        raise ValueError(_("This task needs both a written answer and a link."))
    try:
        with transaction.atomic():
            submission = ScreeningSubmission.objects.create(task=task, application=application, answer_text=answer_text[:6000],
                                                            link=link[:500])
    except IntegrityError:
        raise ValueError(_("You already submitted this task."))
    _flag_similar(submission)
    return submission


def _flag_similar(submission):
    instructions = submission.task.instructions
    mine = own_shingles(submission.answer_text, instructions)  # restating the instructions is not copying
    if not comparable(submission.answer_text, mine):
        return
    for other in submission.task.submissions.exclude(pk=submission.pk).exclude(answer_text="").only("pk", "answer_text")[:300]:
        if jaccard(mine, own_shingles(other.answer_text, instructions)) >= SIMILARITY_THRESHOLD:
            type(submission).objects.filter(pk__in=[submission.pk, other.pk]).update(similarity_flag=True)
            submission.similarity_flag = True
            return


def score(submission, points: dict, note=""):
    """Company scores each criterion (0..its points). Pass/fail follows the task's pass mark."""
    rows = submission.task.criteria or []
    total = sum(int(r["points"]) for r in rows) or 1
    clean = {}
    for i, row in enumerate(rows):
        try:
            value = int(round(float(points.get(str(i), 0) or 0)))  # "6.0" is 6, not 0
        except (TypeError, ValueError):
            value = 0
        clean[str(i)] = min(max(value, 0), int(row["points"]))
    pct = round(sum(clean.values()) / total * 100)
    submission.scores, submission.total_pct = clean, pct
    submission.status = submission.STATUS_PASSED if pct >= submission.task.pass_mark else submission.STATUS_FAILED
    submission.reviewer_note, submission.reviewed_at = (note or "")[:500], timezone.now()
    submission.save(update_fields=["scores", "total_pct", "status", "reviewer_note", "reviewed_at"])
    return submission


def shortlist_block_reason(application):
    """Why this applicant cannot be shortlisted yet because of a required screening task, or None."""
    task = task_for(application.internship)
    if task is None or not task.required:
        return None
    submission = submission_for(application)
    if submission is None:
        return _("This opportunity requires its screening task. The applicant hasn't submitted it yet.")
    if submission.status == submission.STATUS_SUBMITTED:
        return _("Score the screening task first.")
    if submission.status == submission.STATUS_FAILED:
        return _("This applicant did not pass the required screening task.")
    return None


def parse_criteria(text):
    """'criterion | points' per line → [{"criterion", "points"}]. Raises ValueError with a user message."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _sep, pts = line.rpartition("|")
        name, pts = (name or line).strip(), pts.strip() if _sep else "1"
        if not name:
            raise ValueError(_("Each line needs a criterion, e.g. \"Clear structure | 2\"."))
        try:
            pts = int(pts)
        except ValueError:
            raise ValueError(_("Points must be a whole number from 1 to 10."))
        if not 1 <= pts <= 10:
            raise ValueError(_("Points must be a whole number from 1 to 10."))
        rows.append({"criterion": name[:160], "points": pts})
    if not rows:
        raise ValueError(_("Add at least one criterion."))
    if len(rows) > MAX_CRITERIA:
        raise ValueError(_("Use at most %(n)s criteria.") % {"n": MAX_CRITERIA})
    return rows


def criteria_text(rows):
    return "\n".join(f"{r['criterion']} | {r['points']}" for r in rows or [])
