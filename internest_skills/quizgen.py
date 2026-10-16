"""LLM quiz generation: tops up a skill's question bank so every extracted skill can be verified."""
import hashlib
import logging
import random
import re

from django.db import transaction

from .llm import LLM_ERRORS, chat_json, llm_enabled
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
    parts = [f"Skill: {skill.name} ({skill.get_discipline_display()})."]
    aliases = [a.strip() for a in (skill.aliases or "").split(",") if a.strip()]
    if aliases:
        parts.append(f"Also known as: {', '.join(aliases[:8])}.")
    signals = [s.strip() for s in (skill.implicit_signals or "").split(",") if s.strip()]
    if signals:
        parts.append(f"Used in work such as: {', '.join(signals[:8])}.")
    subs = list(skill.sub_skills.values_list("name", flat=True)[:12])
    if subs:
        parts.append(f"Prefer these sub-skills: {', '.join(subs)}.")
    return " ".join(parts)


def generate_items(skill, count=GENERATE_BATCH, avoid=()) -> list[ChallengeItem]:
    """Ask the LLM for `count` fresh questions and save them (they also become the offline fallback bank).

    `avoid` = ChallengeItems the student already saw: their topics and openings go into the prompt, and any
    returned question matching one of them (by fingerprint) is dropped, so a retake never repeats a question.
    """
    avoid = list(avoid)
    avoid_prints = {fingerprint(i.prompt, i.code_snippet) for i in avoid}
    user = (
        skill_context(skill)
        + f" Write exactly {count} NEW, distinct real-world questions; each tests a different point (no repeats or rewordings)."
    )
    if avoid:
        seen = "; ".join(f"[{i.topic or i.sub_skill.name}] {i.prompt[:90]}" for i in avoid[-AVOID_LIMIT:])
        user += f" The student already answered these, so do NOT reuse or reword them; pick other angles: {seen}"
    data = chat_json(build_system_prompt(skill), user, max_tokens=MAX_TOKENS, timeout=60)
    raw_items = data.get("q")
    if not isinstance(raw_items, list):
        raise ValueError("LLM quiz response missing 'q' list")

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
        )
        existing[item.prompt] = item
        items.append(item)
    return items


def live_quiz_item_ids(skill, exclude_ids=()) -> list[int]:
    """Primary path: a fresh LLM-generated quiz for this attempt, easy → hard. Raises QuizGenerationError on failure."""
    if not llm_enabled():
        raise QuizGenerationError("LLM not configured")
    exclude_ids = set(exclude_ids)
    avoid = list(ChallengeItem.objects.filter(id__in=exclude_ids).select_related("sub_skill").order_by("id"))
    try:
        with transaction.atomic():  # savepoint: half-saved questions are rolled back on failure
            generated = generate_items(skill, count=skill.challenge_length, avoid=avoid)
            items = [i for i in generated if i.id not in exclude_ids]
    except LLM_ERRORS as exc:
        raise QuizGenerationError(str(exc)) from exc
    if len(items) < MIN_ITEMS_TO_START:
        raise QuizGenerationError(f"only {len(items)} usable questions generated")
    missing = skill.challenge_length - len(items)
    if missing > 0:  # the model returned fewer usable items: top up from the saved bank
        items += list(skill.items.filter(is_active=True).exclude(id__in=[i.id for i in items] + list(exclude_ids)).order_by("?")[:missing])
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


def quiz_item_ids_for(skill, student_skill=None) -> list[int]:
    """AI quiz FIRST (Gemini / AgentRouter, with key failover); on ANY failure (rate limit, timeout, bad output)
    the saved bank. On a retake, questions the student already saw are excluded. [] = no quiz possible."""
    from .challenge import seen_item_ids

    seen = seen_item_ids(student_skill) if student_skill is not None else set()
    try:
        return live_quiz_item_ids(skill, exclude_ids=seen)
    except (QuizGenerationError, Exception) as exc:
        logger.warning("AI quiz generation failed for skill %s: %s. Falling back to saved items.", skill.pk, exc)
        return saved_quiz_item_ids(skill, exclude_ids=seen)
