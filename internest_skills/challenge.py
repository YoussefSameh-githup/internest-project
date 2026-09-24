"""Proctored challenge engine. All timing and scoring decisions are server-side."""
import math
import random
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .extraction import _normalize, _term_pattern
from .integrity import analyze_typing
from .models import ChallengeAttempt, ChallengeItem, ChallengeResponse, StudentSkill

NETWORK_GRACE_SECONDS = 3
SUB_SKILL_LAG_THRESHOLD = 60
CASE_PASS_CREDIT = 0.6


class ChallengeError(Exception):
    pass


def _pick_items(skill):
    by_sub = defaultdict(list)
    for item in ChallengeItem.objects.filter(skill=skill, is_active=True).only("id", "sub_skill_id"):
        by_sub[item.sub_skill_id].append(item.id)
    if sum(len(v) for v in by_sub.values()) < 3:
        raise ChallengeError("This skill does not have a challenge yet.")
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
    return picked


def _max_duration(attempt):
    limits = ChallengeItem.objects.filter(id__in=attempt.item_ids).values_list("time_limit_seconds", flat=True)
    return timedelta(seconds=sum(limits) + 60 * 5)


def start_attempt(student_skill: StudentSkill) -> ChallengeAttempt:
    if student_skill.status == StudentSkill.STATUS_VERIFIED:
        raise ChallengeError("This skill is already verified.")
    if student_skill.in_cooldown:
        raise ChallengeError("Retest is available after the cooldown ends.")
    active = student_skill.attempts.filter(state=ChallengeAttempt.STATE_ACTIVE).first()
    if active:
        if timezone.now() - active.started_at <= _max_duration(active):
            return active
        terminate(active, "abandoned")
        raise ChallengeError("Your previous session was abandoned. Retest is available after the cooldown ends.")
    return ChallengeAttempt.objects.create(student_skill=student_skill, item_ids=_pick_items(student_skill.skill))


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
    norm = _normalize(answer_text or "")
    groups = [g for g in (item.rubric or []) if g]
    if not groups:
        return 0.0
    hits = sum(1 for group in groups if any(_term_pattern(term).search(norm) for term in group))
    return hits / len(groups)


def submit_answer(attempt, item_id, choice_index=None, answer_text="", typing_stats=None):
    if not attempt.is_active:
        raise ChallengeError("This session has ended.")
    item = _current_item(attempt)
    if item is None or item.id != item_id or attempt.current_served_at is None:
        raise ChallengeError("Out-of-order answer.")

    now = timezone.now()
    elapsed = now - attempt.current_served_at
    timed_out = now > _deadline(attempt, item) + timedelta(seconds=NETWORK_GRACE_SECONDS)
    answer_text = (answer_text or "")[:4000]

    flags = []
    if item.kind == ChallengeItem.KIND_CASE and not timed_out:
        flags = analyze_typing(answer_text, typing_stats or {})

    credit = 0.0 if (timed_out or flags) else _grade(item, choice_index, answer_text)
    ChallengeResponse.objects.create(
        attempt=attempt, item=item,
        choice_index=choice_index if item.kind == ChallengeItem.KIND_MCQ else None,
        answer_text=answer_text if item.kind == ChallengeItem.KIND_CASE else "",
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
    others = list(
        StudentSkill.objects.filter(skill=skill, score__isnull=False).exclude(pk=exclude_pk).values_list("score", flat=True)
    )
    n = len(others) + 1
    below = sum(1 for s in others if s < score)
    equal = sum(1 for s in others if s == score) + 1
    return round((below + 0.5 * equal) / n * 100)


def finalize(attempt):
    responses = list(attempt.responses.select_related("item__sub_skill"))
    earned = total = 0.0
    per_sub = defaultdict(lambda: {"earned": 0.0, "total": 0.0, "name": ""})
    for r in responses:
        w = r.item.weight
        earned += r.credit * w
        total += w
        bucket = per_sub[r.item.sub_skill_id]
        bucket["name"] = r.item.sub_skill.name
        bucket["earned"] += r.credit * w
        bucket["total"] += w

    score = round(earned / total * 100) if total else 0
    breakdown = {
        str(sid): {"name": b["name"], "accuracy": round(b["earned"] / b["total"] * 100) if b["total"] else 0}
        for sid, b in per_sub.items()
    }
    attempt.state = ChallengeAttempt.STATE_COMPLETED
    attempt.finished_at = timezone.now()
    attempt.score_pct = score
    attempt.sub_skill_breakdown = breakdown
    attempt.save()

    ss = attempt.student_skill
    lag = [
        {"id": int(sid), "name": b["name"], "accuracy": b["accuracy"]}
        for sid, b in breakdown.items() if b["accuracy"] < SUB_SKILL_LAG_THRESHOLD
    ]
    passed = score >= ss.skill.pass_threshold and not lag
    if not passed and not lag and breakdown:
        weakest = min(breakdown.items(), key=lambda kv: kv[1]["accuracy"])
        lag = [{"id": int(weakest[0]), "name": weakest[1]["name"], "accuracy": weakest[1]["accuracy"]}]

    ss.score = score
    ss.percentile = _percentile(ss.skill, score, ss.pk)
    if passed:
        ss.status = StudentSkill.STATUS_VERIFIED
        ss.verified_at = timezone.now()
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
