"""LLM quiz generation: tops up a skill's question bank so every extracted skill can be verified; a second model
pass (verify_items) re-solves new questions and drops the ones whose answer key it disputes."""
import hashlib
import logging
import random
import re

from django.db import transaction

from internest_core.ai_cache import no_ai_cache
from internest_core.api_monitor import api_feature

from .llm import LLM_ERRORS, llm_enabled
from .models import ChallengeItem, Discipline, SubSkill

logger = logging.getLogger(__name__)

QUIZ_LENGTH = 10  # questions per challenge (Skill.challenge_length defaults to this)
MIN_ITEMS_TO_START = 3
GENERATE_BATCH = QUIZ_LENGTH
MAX_TOKENS = 6000  # room for 10 items with code snippets; Gemini 2.5 thinking tokens also count here

TECHNICAL_DISCIPLINES = {Discipline.COMPUTING, Discipline.ENGINEERING}
TECHNICAL_HINTS = ("python", "sql", "java", "javascript", "c++", "c#", "security", "cyber", "network", "data", "cloud",
                   "linux", "devops", "excel", "matlab", "machine learning", "web", "api", "programming", "code")

_DIFFICULTY = {"e": ChallengeItem.DIFFICULTY_EASY, "m": ChallengeItem.DIFFICULTY_MEDIUM, "h": ChallengeItem.DIFFICULTY_HARD}
_TIME_LIMIT = {ChallengeItem.DIFFICULTY_EASY: 30, ChallengeItem.DIFFICULTY_MEDIUM: 45, ChallengeItem.DIFFICULTY_HARD: 60}

_BASE_PROMPT = (
    "You write proctored skill-verification MCQs for university students and fresh graduates. "
    "Exactly one correct answer, 4 short distinct options, no 'all/none of the above', question <= 300 chars. "
    "Progressive difficulty: ~30% e(easy), 40% m(medium), 30% h(hard). Spread across 2-4 sub-skills. "
    "Each item names the precise concept tested in 't' (<= 8 words, actionable study topic). "
)
_TECH_STYLE = (
    "TECHNICAL skill: most items must include a short code/config/query snippet in 'k' (<= 12 lines, real syntax, "
    "use \\n for newlines) and ask to trace output, find the bug, pick the fix, or predict behaviour. "
)
_SCENARIO_STYLE = (
    "NON-TECHNICAL skill: most items are realistic workplace scenarios or mini case studies "
    "(stakeholders, constraints, numbers) asking for the best decision or next step; 'k' stays empty. "
)
_FORMAT = 'JSON only: {"q":[{"s":"sub-skill","d":"e|m|h","t":"topic","p":"question","k":"","c":["","","",""],"a":0}]}'


def is_technical(skill) -> bool:
    if skill.discipline in TECHNICAL_DISCIPLINES:
        return True
    name = skill.name.lower()
    return any(h in name for h in TECHNICAL_HINTS)


def build_system_prompt(skill) -> str:
    return _BASE_PROMPT + (_TECH_STYLE if is_technical(skill) else _SCENARIO_STYLE) + _FORMAT


def _clean_item(raw):
    if not isinstance(raw, dict):
        return None
    prompt = str(raw.get("p", "")).strip()
    choices = raw.get("c")
    answer = raw.get("a")
    if not (10 <= len(prompt) <= 600) or not isinstance(choices, list) or not isinstance(answer, int):
        return None
    choices = [str(c).strip()[:200] for c in choices]
    if not (2 <= len(choices) <= 6) or any(not c for c in choices) or len(set(choices)) != len(choices):
        return None
    if not 0 <= answer < len(choices):
        return None
    correct = choices[answer]
    random.shuffle(choices)  # remove the model's answer-position bias
    return {
        "sub_skill": str(raw.get("s", "")).strip()[:120] or "Core concepts",
        "difficulty": _DIFFICULTY.get(str(raw.get("d", "m")).strip().lower()[:1], ChallengeItem.DIFFICULTY_MEDIUM),
        "topic": str(raw.get("t", "")).strip()[:160],
        "prompt": prompt,
        "code": str(raw.get("k", "") or "").strip("\n")[:1200],
        "choices": choices,
        "correct_index": choices.index(correct),
    }


class QuizGenerationError(Exception):
    pass


AVOID_LIMIT = 30  # previous questions described to the model on a retake


def fingerprint(prompt: str, code: str = "") -> str:
    """Stable hash of a question (case/spacing-insensitive) to spot repeats across attempts."""
    text = re.sub(r"\s+", " ", f"{prompt} {code}".lower()).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def skill_context(skill) -> str:
    """Everything the prompt knows about ANY skill: name, field, aliases, typical signals and sub-skills."""
    field = skill.get_discipline_display() if skill.discipline else "general"
    parts = [f"Skill: {skill.name} ({field})."]
    aliases = [a.strip() for a in (skill.aliases or "").split(",") if a.strip()]
    if aliases:
        parts.append(f"Also known as: {', '.join(aliases[:8])}.")
    signals = [s.strip() for s in (skill.implicit_signals or "").split(",") if s.strip()]
    if signals:
        parts.append(f"Used in work such as: {', '.join(signals[:8])}.")
    subs = list(skill.sub_skills.values_list("name", flat=True)[:12]) if skill.pk else []
    if subs:
        parts.append(f"Prefer these sub-skills: {', '.join(subs)}.")
    return " ".join(parts)


def build_user_prompt(skill, count=GENERATE_BATCH, avoid=()) -> str:
    user = (
        skill_context(skill)
        + f" Write exactly {count} NEW, distinct real-world questions; each tests a different point (no repeats or rewordings)."
        + f' Return {{"q": [...]}} with exactly {count} items.'
    )
    if avoid:
        seen = "; ".join(f"[{i.topic or i.sub_skill.name}] {i.prompt[:90]}" for i in list(avoid)[-AVOID_LIMIT:])
        user += f" The student already answered these, so do NOT reuse or reword them; pick other angles: {seen}"
    return user


def validate_quiz_reply(data) -> None:
    """A provider's 200 reply only counts if it holds enough usable questions; otherwise try the next key."""
    raw_items = data.get("q")
    if not isinstance(raw_items, list):
        raise ValueError(f"LLM quiz response missing 'q' list (keys: {sorted(data)[:5]})")
    usable = sum(1 for raw in raw_items if _clean_item(raw) is not None)
    if usable < MIN_ITEMS_TO_START:
        raise ValueError(f"only {usable} usable questions in the reply")




GENERATION_TASK = "question_generation"  # engine task (per-student daily quota: ai_tasks.DEFAULT_TASK_LIMITS)
VERIFY_TASK = "quiz_verify"
VERIFY_JOB = "quiz_verify"


def request_raw_items(skill, count=GENERATE_BATCH, avoid=(), chat=None, user=None) -> list:
    """One trip down the provider chain → the raw 'q' list of the first provider whose reply is usable.
    `user` (the student) is charged against the daily generation quota (audit M2)."""
    from internest_ai import engine

    from . import llm

    system, prompt = build_system_prompt(skill), build_user_prompt(skill, count, avoid)
    # Never cached: every attempt (and every student) must get fresh questions.
    with api_feature("Question Generation"), no_ai_cache():
        if chat is not None:
            data = chat(system, prompt, max_tokens=MAX_TOKENS, timeout=60, validate=validate_quiz_reply)
        else:
            data = engine.run(GENERATION_TASK, system=system, prompt=prompt, user=user, max_tokens=MAX_TOKENS, timeout=60,
                              validate=validate_quiz_reply, cache=False, log=llm.logger)
    return data["q"]


def generate_items(skill, count=GENERATE_BATCH, avoid=(), user=None) -> list[ChallengeItem]:
    """Ask the LLM for `count` fresh questions and save them (they also become the offline fallback bank).

    `avoid` = ChallengeItems the student already saw: their topics and openings go into the prompt, and any
    returned question matching one of them (by fingerprint) is dropped, so a retake never repeats a question.
    New questions enter the bank unreviewed; verify_items re-solves them with a second model.
    """
    avoid = list(avoid)
    avoid_prints = {fingerprint(i.prompt, i.code_snippet) for i in avoid}
    raw_items = request_raw_items(skill, count, avoid, user=user)

    existing = {i.prompt: i for i in skill.items.filter(is_active=True)}
    items = []
    for raw in raw_items:  # read past dropped repeats/invalid items until we have `count` usable ones
        if len(items) >= count:
            break
        cleaned = _clean_item(raw)
        if cleaned is None or fingerprint(cleaned["prompt"], cleaned["code"]) in avoid_prints:
            continue
        if cleaned["prompt"] in existing:
            items.append(existing[cleaned["prompt"]])
            continue
        sub, _created = SubSkill.objects.get_or_create(
            skill=skill, name=cleaned["sub_skill"], defaults={"keywords": cleaned["topic"] or cleaned["sub_skill"]},
        )
        item = ChallengeItem.objects.create(
            skill=skill, sub_skill=sub, kind=ChallengeItem.KIND_MCQ,
            prompt=cleaned["prompt"], code_snippet=cleaned["code"], topic=cleaned["topic"],
            difficulty=cleaned["difficulty"], choices=cleaned["choices"], correct_index=cleaned["correct_index"],
            time_limit_seconds=_TIME_LIMIT[cleaned["difficulty"]] + (15 if cleaned["code"] and cleaned["difficulty"] != "hard" else 0),
            reviewed=False,
        )
        existing[item.prompt] = item
        items.append(item)
    return items


def live_quiz_item_ids(skill, exclude_ids=(), user=None) -> list[int]:
    """Primary path: a fresh LLM-generated quiz for this attempt, easy → hard. Raises QuizGenerationError on failure."""
    if not llm_enabled():
        raise QuizGenerationError("LLM not configured")
    exclude_ids = set(exclude_ids)
    avoid = list(ChallengeItem.objects.filter(id__in=exclude_ids).select_related("sub_skill").order_by("id"))
    try:
        with transaction.atomic():  # savepoint: half-saved questions are rolled back on failure
            generated = generate_items(skill, count=skill.challenge_length, avoid=avoid, user=user)
            items = [i for i in generated if i.id not in exclude_ids]
    except LLM_ERRORS as exc:
        raise QuizGenerationError(str(exc)) from exc
    if len(items) < MIN_ITEMS_TO_START:
        raise QuizGenerationError(f"only {len(items)} usable questions generated")
    missing = skill.challenge_length - len(items)
    if missing > 0:  # the model returned fewer usable items: top up from the saved bank
        items += list(skill.items.filter(is_active=True).exclude(kind=ChallengeItem.KIND_PRACTICAL)
                      .exclude(id__in=[i.id for i in items] + list(exclude_ids)).order_by("?")[:missing])
    items.sort(key=lambda i: ChallengeItem.DIFFICULTY_ORDER.get(i.difficulty, 1))
    return [i.id for i in items]


def saved_quiz_item_ids(skill, exclude_ids=()) -> list[int]:
    """Questions from the saved bank (balanced across sub-skills, easy → hard); [] if there aren't enough."""
    from .challenge import _pick_items

    try:
        return _pick_items(skill, exclude=exclude_ids)
    except Exception as exc:  # ChallengeError (bank too small) or anything unexpected
        logger.warning("No saved quiz for skill %s: %s", skill.pk, exc)
        return []


def quiz_item_ids_for(skill, student_skill=None, user=None) -> list[int]:
    """AI quiz FIRST (Gemini / AgentRouter, with key failover); on ANY failure (rate limit, timeout, bad output, the
    student's daily generation quota) the saved bank. On a retake, questions the student already saw are excluded.
    [] = no quiz possible."""
    from .challenge import seen_item_ids

    seen = seen_item_ids(student_skill) if student_skill is not None else set()
    if user is None and student_skill is not None:
        user = student_skill.student.user_id
    try:
        return live_quiz_item_ids(skill, exclude_ids=seen, user=user)
    except (QuizGenerationError, Exception) as exc:
        logger.warning("AI quiz generation failed for skill %s: %s. Falling back to saved items.", skill.pk, exc)
        return saved_quiz_item_ids(skill, exclude_ids=seen)


# ---------------------------------------------------------------- answer-key verification (second model pass)
VERIFY_SCHEMA = {"answers": [{"i": int, "answer": int}]}
VERIFY_SYSTEM = (
    "You are an independent exam checker. Solve every multiple-choice question in the data block yourself and give "
    "the 0-based index of the single best choice. Do not guess a key from wording or position: reason about the "
    "content. If no choice is correct or two are equally correct, answer -1. "
    'JSON only: {"answers":[{"i":0,"answer":2}]}'
)
VERIFY_PROMPT = "Solve each question in the data block and return the JSON object."


def schedule_verification(attempt, user_id=None) -> None:
    """After the attempt is committed, re-solve its unchecked AI questions in the background (system job)."""
    ids = list(ChallengeItem.objects.filter(id__in=attempt.item_ids, kind=ChallengeItem.KIND_MCQ, reviewed=False,
                                            key_verified__isnull=True).values_list("id", flat=True))
    if not ids:
        return

    def enqueue():
        from internest_core import async_tasks

        try:
            async_tasks.enqueue(VERIFY_JOB, None, idempotency_key=f"verify:{attempt.pk}", priority=-1,
                                attempt_id=attempt.pk, item_ids=ids, student_user_id=user_id)
        except async_tasks.TaskThrottled as exc:  # the queue is full: the questions stay unchecked (and unreviewed)
            logger.warning("Quiz verification for attempt %s not queued: %s", attempt.pk, exc.code)

    transaction.on_commit(enqueue)


def verify_items(item_ids, attempt_id=None, user_id=None) -> dict:
    """Ask a second model to solve the questions; drop (deactivate) every item whose key it disputes, mark the others
    verified, and take disputed questions the student has not reached out of the attempt. Raises AIUnavailable."""
    from internest_ai import engine

    items = list(ChallengeItem.objects.filter(id__in=item_ids, kind=ChallengeItem.KIND_MCQ, key_verified__isnull=True))
    if not items:
        return {"checked": 0, "disputed": []}
    data = {"questions": [{"i": n, "question": item.prompt, "code": item.code_snippet, "choices": item.choices}
                          for n, item in enumerate(items)]}
    with api_feature("Question Verification"), no_ai_cache():
        reply = engine.run(VERIFY_TASK, system=VERIFY_SYSTEM, prompt=VERIFY_PROMPT, data=data, user=user_id,
                           max_tokens=800, timeout=60, cache=False)
    answers = {a["i"]: a["answer"] for a in reply["answers"]}
    agreed = [item.pk for n, item in enumerate(items) if answers.get(n) == item.correct_index]
    disputed = [item.pk for n, item in enumerate(items) if n in answers and answers[n] != item.correct_index]
    with transaction.atomic():
        ChallengeItem.objects.filter(pk__in=agreed).update(key_verified=True)
        ChallengeItem.objects.filter(pk__in=disputed).update(key_verified=False, is_active=False)
        if disputed and attempt_id:
            _drop_from_attempt(attempt_id, set(disputed))
    if disputed:
        logger.warning("Quiz verification disputed %s of %s AI questions (attempt %s)", len(disputed), len(items), attempt_id)
    return {"checked": len(agreed) + len(disputed), "disputed": disputed}


def _drop_from_attempt(attempt_id, disputed: set) -> None:
    """Unserved disputed questions leave the attempt; answered ones stay but no longer count (challenge.score_responses)."""
    from .challenge import finalize
    from .models import ChallengeAttempt

    attempt = ChallengeAttempt.objects.select_for_update().filter(pk=attempt_id, state=ChallengeAttempt.STATE_ACTIVE).first()
    if attempt is None:
        return
    reached = attempt.current_index + (1 if attempt.current_served_at else 0)
    kept = attempt.item_ids[:reached] + [i for i in attempt.item_ids[reached:] if i not in disputed]
    if kept == attempt.item_ids:
        return
    attempt.item_ids = kept
    attempt.save(update_fields=["item_ids"])
    if attempt.current_index >= len(kept):
        finalize(attempt)
