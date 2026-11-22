"""Proctored challenge engine. All timing and scoring decisions are server-side.

A test is: timed questions from an unseen, balanced bank, then (when the skill has one) one practical scenario. Passing
attempts with a practical task or a blocking integrity signal wait for a human reviewer before the skill is verified
(see review.py). No layer makes the test cheat-proof.
"""
import math
import random
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .extraction import _normalize, _term_pattern
from .integrity import analyze_typing
from .models import ChallengeAttempt, ChallengeItem, ChallengeResponse, StudentSkill

NETWORK_GRACE_SECONDS = 3
SUB_SKILL_LAG_THRESHOLD = 60
CASE_PASS_CREDIT = 0.6


class ChallengeError(Exception):
    pass


def seen_item_ids(student_skill) -> set[int]:
    """Every question this student was served in earlier attempts at this skill."""
    seen = set()
    for ids in student_skill.attempts.exclude(state=ChallengeAttempt.STATE_ACTIVE).values_list("item_ids", flat=True):
        seen.update(ids or [])
    return seen


def _pick_items(skill, exclude=()):
    """Saved-bank quiz, balanced across sub-skills. Unseen questions only when the bank has enough of them;
    a bank too small for a fresh set falls back to reusing questions rather than blocking the student."""
    exclude = set(exclude)
    bank = list(ChallengeItem.objects.filter(skill=skill, is_active=True).exclude(kind=ChallengeItem.KIND_PRACTICAL)
                .only("id", "sub_skill_id", "difficulty"))
    fresh = [i for i in bank if i.id not in exclude]
    items = fresh if len(fresh) >= min(skill.challenge_length, 3) else bank
    by_sub = defaultdict(list)
    difficulty_of = {}
    for item in items:
        by_sub[item.sub_skill_id].append(item.id)
        difficulty_of[item.id] = ChallengeItem.DIFFICULTY_ORDER.get(item.difficulty, 1)
    if sum(len(v) for v in by_sub.values()) < 3:
        raise ChallengeError(_("This skill does not have a challenge yet."))
    pools = list(by_sub.values())
    for pool in pools:
        random.shuffle(pool)
    random.shuffle(pools)
    picked = []
    while len(picked) < skill.challenge_length and any(pools):
        for pool in pools:
            if pool and len(picked) < skill.challenge_length:
                picked.append(pool.pop())
    random.shuffle(picked)
    picked.sort(key=difficulty_of.__getitem__)  # progressive: easy → medium → hard (stable, random within a level)
    return picked


def _pick_practical(skill, exclude=()):
    """One practical scenario, unseen if possible, always last (after the timed questions)."""
    ids = list(ChallengeItem.objects.filter(skill=skill, is_active=True, kind=ChallengeItem.KIND_PRACTICAL).values_list("id", flat=True))
    fresh = [i for i in ids if i not in set(exclude)]
    pool = fresh or ids
    return random.choice(pool) if pool else None


def _max_duration(attempt):
    limits = ChallengeItem.objects.filter(id__in=attempt.item_ids).values_list("time_limit_seconds", flat=True)
    return timedelta(seconds=sum(limits) + 60 * 5)


def start_attempt(student_skill: StudentSkill, item_ids=None) -> ChallengeAttempt:
    """`item_ids` is a freshly generated live quiz; without it, questions come from the saved bank."""
    if student_skill.status == StudentSkill.STATUS_VERIFIED:
        raise ChallengeError(_("This skill is already verified."))
    if student_skill.status == StudentSkill.STATUS_REVIEW:
        raise ChallengeError(_("Your last attempt is being reviewed. You'll see the result here within a few days."))
    if student_skill.in_cooldown:
        raise ChallengeError(_("You can retake this skill test in %(h)s hours.") % {"h": student_skill.cooldown_hours_remaining})
    active = student_skill.attempts.filter(state=ChallengeAttempt.STATE_ACTIVE).first()
    if active:
        if timezone.now() - active.started_at <= _max_duration(active):
            return active
        terminate(active, "abandoned")
        raise ChallengeError(_("Your previous session was abandoned. Retest is available after the cooldown ends."))
    seen = seen_item_ids(student_skill)
    item_ids = item_ids or _pick_items(student_skill.skill, exclude=seen)
    return ChallengeAttempt.objects.create(student_skill=student_skill, item_ids=with_practical(student_skill.skill, item_ids, seen))


def with_practical(skill, item_ids, seen=()):
    """Every quiz (AI-generated or from the saved bank) ends with one practical task when the skill has one."""
    practical_ids = set(ChallengeItem.objects.filter(id__in=item_ids, kind=ChallengeItem.KIND_PRACTICAL).values_list("id", flat=True))
    timed = [i for i in item_ids if i not in practical_ids]
    practical = _pick_practical(skill, seen)
    return timed + ([practical] if practical else [])


def _current_item(attempt):
    if attempt.current_index >= attempt.total_items:
        return None
    return ChallengeItem.objects.select_related("sub_skill").get(id=attempt.item_ids[attempt.current_index])


def _deadline(attempt, item):
    return attempt.current_served_at + timedelta(seconds=item.time_limit_seconds)


def _advance(attempt):
    attempt.current_index += 1
    attempt.current_served_at = None
    if attempt.current_index >= attempt.total_items:
        finalize(attempt)
    else:
        attempt.save(update_fields=["current_index", "current_served_at"])


def _expire_if_overdue(attempt, item):
    if attempt.current_served_at and timezone.now() > _deadline(attempt, item) + timedelta(seconds=NETWORK_GRACE_SECONDS):
        ChallengeResponse.objects.get_or_create(
            attempt=attempt, item=item,
            defaults={"timed_out": True, "elapsed_ms": item.time_limit_seconds * 1000},
        )
        _advance(attempt)
        return True
    return False


def serve_next(attempt):
    """Return the public payload for the current item (answers never included), or None when finished."""
    if not attempt.is_active:
        return None
    item = _current_item(attempt)
    while item is not None and _expire_if_overdue(attempt, item):
        if not attempt.is_active:
            return None
        item = _current_item(attempt)
    if item is None:
        return None
    if attempt.current_served_at is None:
        attempt.current_served_at = timezone.now()
        attempt.save(update_fields=["current_served_at"])
    remaining = max(0, math.ceil((_deadline(attempt, item) - timezone.now()).total_seconds()))
    return {
        "item_id": item.id,
        "index": attempt.current_index + 1,
        "total": attempt.total_items,
        "kind": item.kind,
        "prompt": item.prompt,
        "code": item.code_snippet,
        "difficulty": item.difficulty,
        "choices": item.choices if item.kind == ChallengeItem.KIND_MCQ else [],
        "sub_skill": item.sub_skill.name,
        "time_limit": item.time_limit_seconds,
        "remaining_seconds": remaining,
        "focus_warnings": attempt.focus_warnings,
        "max_focus_warnings": ChallengeAttempt.MAX_FOCUS_WARNINGS,
    }


def _grade(item, choice_index, answer_text):
    if item.kind == ChallengeItem.KIND_MCQ:
        return 1.0 if choice_index is not None and choice_index == item.correct_index else 0.0
    if item.kind == ChallengeItem.KIND_PRACTICAL:
        from .review import provisional_credit

        return provisional_credit(item, answer_text)
    norm = _normalize(answer_text or "")
    groups = [g for g in (item.rubric or []) if g]
    if not groups:
        return 0.0
    hits = sum(1 for group in groups if any(_term_pattern(term).search(norm) for term in group))
    return hits / len(groups)


def submit_answer(attempt, item_id, choice_index=None, answer_text="", typing_stats=None):
    if not attempt.is_active:
        raise ChallengeError(_("This session has ended."))
    item = _current_item(attempt)
    if item is None or item.id != item_id or attempt.current_served_at is None:
        raise ChallengeError(_("Out-of-order answer."))

    now = timezone.now()
    elapsed = now - attempt.current_served_at
    timed_out = now > _deadline(attempt, item) + timedelta(seconds=NETWORK_GRACE_SECONDS)
    written = item.kind != ChallengeItem.KIND_MCQ
    answer_text = (answer_text or "")[:6000 if item.kind == ChallengeItem.KIND_PRACTICAL else 4000]

    flags = []
    if written and not timed_out:
        flags = analyze_typing(answer_text, typing_stats or {})

    credit = 0.0 if (timed_out or flags) else _grade(item, choice_index, answer_text)
    ChallengeResponse.objects.create(
        attempt=attempt, item=item,
        choice_index=choice_index if item.kind == ChallengeItem.KIND_MCQ else None,
        answer_text=answer_text if written else "",
        credit=credit, timed_out=timed_out,
        elapsed_ms=int(elapsed.total_seconds() * 1000),
        typing_stats=typing_stats or {}, integrity_flags=flags,
    )

    if flags:
        attempt.log_event("typing_integrity", item_id=item.id, flags=flags)
        attempt.save(update_fields=["integrity_events"])
        flagged = sum(1 for f in attempt.responses.values_list("integrity_flags", flat=True) if f)
        if flagged >= ChallengeAttempt.MAX_FLAGGED_RESPONSES:
            terminate(attempt, "ai_or_pasted_content")
            return
    _advance(attempt)


def record_focus_loss(attempt, kind="tab_hidden"):
    if not attempt.is_active:
        return
    attempt.focus_warnings += 1
    attempt.log_event(kind, warning=attempt.focus_warnings)
    if attempt.focus_warnings > ChallengeAttempt.MAX_FOCUS_WARNINGS:
        terminate(attempt, "focus_lost")
    else:
        attempt.save(update_fields=["focus_warnings", "integrity_events"])


def record_blocked_action(attempt, kind):
    if attempt.is_active:
        attempt.log_event(kind)
        attempt.save(update_fields=["integrity_events"])


def terminate(attempt, reason):
    attempt.state = ChallengeAttempt.STATE_TERMINATED
    attempt.termination_reason = reason
    attempt.finished_at = timezone.now()
    attempt.save()
    ss = attempt.student_skill
    ss.start_cooldown()
    ss.save(update_fields=["cooldown_until", "updated_at"])


def _percentile(skill, score, exclude_pk):
    """One COUNT query on the (skill, score) index instead of loading every peer's score."""
    counts = (StudentSkill.objects.filter(skill=skill, score__isnull=False).exclude(pk=exclude_pk)
              .aggregate(n=Count("id"), below=Count("id", filter=Q(score__lt=score)), equal=Count("id", filter=Q(score=score))))
    n = counts["n"] + 1
    return round((counts["below"] + 0.5 * (counts["equal"] + 1)) / n * 100)


def counts_in_score(response) -> bool:
    """An MCQ whose answer key the verifier disputed (quizgen.verify_items) counts neither for nor against the student."""
    return not (response.item.kind == ChallengeItem.KIND_MCQ and response.item.key_verified is False)


def improvement_topics(responses) -> list[dict]:
    """Actionable study topics from missed answers, fundamentals (easy misses) first."""
    topics = {}
    for r in responses:
        if not counts_in_score(r):
            continue
        item = r.item
        missed = r.final_credit < (1.0 if item.kind == ChallengeItem.KIND_MCQ else CASE_PASS_CREDIT)
        if not missed:
            continue
        topic = item.topic or item.sub_skill.name
        reason = "timed_out" if r.timed_out else ("integrity" if r.integrity_flags else "incorrect")
        entry = topics.setdefault(topic.lower(), {
            "topic": topic,
            "sub_skill": item.sub_skill.name,
            "difficulty": item.difficulty,
            "reason": reason,
            "action": f"Review {topic} ({item.sub_skill.name}) and practise {item.get_difficulty_display().lower()}-level problems on it.",
            "missed": 0,
        })
        entry["missed"] += 1
    return sorted(topics.values(), key=lambda t: (ChallengeItem.DIFFICULTY_ORDER.get(t["difficulty"], 1), -t["missed"]))


def leaderboard_position(student_skill) -> dict | None:
    """Rank among peers who completed the same skill's challenge (by best current score)."""
    if student_skill.score is None:
        return None
    counts = (StudentSkill.objects.filter(skill_id=student_skill.skill_id, score__isnull=False)
              .aggregate(peers=Count("id"), higher=Count("id", filter=Q(score__gt=student_skill.score))))
    return {"rank": 1 + counts["higher"], "peers": counts["peers"], "percentile": student_skill.percentile,
            "top_percent": max(1, 100 - (student_skill.percentile or 0))}


def score_responses(responses, credit_of=None):
    """(score %, per-sub-skill breakdown). A reviewer's credit replaces the automatic one when set."""
    credit_of = credit_of or (lambda r: r.final_credit)
    earned = total = 0.0
    per_sub = defaultdict(lambda: {"earned": 0.0, "total": 0.0, "name": ""})
    for r in responses:
        if not counts_in_score(r):
            continue
        w = r.item.weight
        earned += credit_of(r) * w
        total += w
        bucket = per_sub[r.item.sub_skill_id]
        bucket["name"] = r.item.sub_skill.name
        bucket["earned"] += credit_of(r) * w
        bucket["total"] += w
    score = round(earned / total * 100) if total else 0
    breakdown = {
        str(sid): {"name": b["name"], "accuracy": round(b["earned"] / b["total"] * 100) if b["total"] else 0}
        for sid, b in per_sub.items()
    }
    return score, breakdown


def _passes(skill, score, breakdown):
    lag = [{"id": int(sid), "name": b["name"], "accuracy": b["accuracy"]}
           for sid, b in breakdown.items() if b["accuracy"] < SUB_SKILL_LAG_THRESHOLD]
    return score >= skill.pass_threshold and not lag, lag


def finalize(attempt):
    from . import review

    responses = list(attempt.responses.select_related("item__sub_skill"))
    score, breakdown = score_responses(responses)
    attempt.state = ChallengeAttempt.STATE_COMPLETED
    attempt.finished_at = timezone.now()
    attempt.score_pct = score
    attempt.sub_skill_breakdown = breakdown
    attempt.improvement_topics = improvement_topics(responses)
    signals = review.integrity_signals(attempt, responses)
    attempt.review_reasons = signals

    skill = attempt.student_skill.skill
    passed, _lag = _passes(skill, score, breakdown)
    # Could a reviewer's credit on the practical task make this pass? Then a human looks at it before anything is final.
    best_case = score_responses(responses, lambda r: 1.0 if r.item.kind == ChallengeItem.KIND_PRACTICAL else r.credit)
    could_pass = _passes(skill, *best_case)[0]
    has_practical = any(r.item.kind == ChallengeItem.KIND_PRACTICAL for r in responses)
    if (passed and review.needs_review(signals)) or (has_practical and could_pass):
        attempt.review_status = ChallengeAttempt.REVIEW_PENDING
        attempt.save()
        ss = attempt.student_skill
        ss.status = StudentSkill.STATUS_REVIEW  # the provisional score stays on the attempt; it ranks nobody until decided
        ss.save(update_fields=["status", "updated_at"])
        return
    attempt.save()
    apply_outcome(attempt, score, breakdown)


def apply_outcome(attempt, score, breakdown, force_fail=False):
    """Set the skill's verified / lag status from a final score (after review, or straight away)."""
    ss = attempt.student_skill
    ss.refresh_from_db()
    if ss.status == StudentSkill.STATUS_VERIFIED:
        return  # verified meanwhile by a reviewed course, certificate or freelance work: this attempt never downgrades it
    passed, lag = _passes(ss.skill, score, breakdown)
    passed = passed and not force_fail
    if not passed and not lag and breakdown:
        weakest = min(breakdown.items(), key=lambda kv: kv[1]["accuracy"])
        lag = [{"id": int(weakest[0]), "name": weakest[1]["name"], "accuracy": weakest[1]["accuracy"]}]

    ss.score = None if force_fail else score  # a rejected attempt does not count in leaderboards or percentiles
    ss.percentile = None if force_fail else _percentile(ss.skill, score, ss.pk)
    if passed:
        ss.status = StudentSkill.STATUS_VERIFIED
        ss.verified_at = timezone.now()
        ss.verified_via = StudentSkill.VIA_CHALLENGE
        ss.lag_sub_skills = []
        ss.cooldown_until = None
    else:
        ss.status = StudentSkill.STATUS_LAG
        ss.lag_sub_skills = sorted(lag, key=lambda x: x["accuracy"])
        ss.start_cooldown()
    ss.save()


def active_attempt_for(student_profile, token):
    return (
        ChallengeAttempt.objects.select_for_update()
        .select_related("student_skill__skill")
        .filter(Q(token=token) & Q(student_skill__student=student_profile))
        .first()
    )
