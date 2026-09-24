"""LLM quiz generation: tops up a skill's question bank so every extracted skill can be verified."""
import logging
import random

from .llm import chat_json, llm_enabled
from .models import ChallengeItem, Discipline, SubSkill

logger = logging.getLogger(__name__)

MIN_ITEMS_TO_START = 3
GENERATE_BATCH = 10
MAX_TOKENS = 2800

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


def generate_items(skill, count=GENERATE_BATCH) -> list[ChallengeItem]:
    """Ask the LLM for `count` fresh questions and save them (they also become the offline fallback bank)."""
    existing_subs = list(skill.sub_skills.values_list("name", flat=True))
    user = (
        f"Skill: {skill.name} ({skill.get_discipline_display()}). Questions: {count}. Write NEW questions."
        + (f" Prefer these sub-skills: {', '.join(existing_subs)}." if existing_subs else "")
    )
    data = chat_json(build_system_prompt(skill), user, max_tokens=MAX_TOKENS, timeout=60)
    raw_items = data.get("q")
    if not isinstance(raw_items, list):
        raise ValueError("LLM quiz response missing 'q' list")

    existing = {i.prompt: i for i in skill.items.filter(is_active=True)}
    items = []
    for raw in raw_items[:count]:
        cleaned = _clean_item(raw)
        if cleaned is None:
            continue
        if cleaned["prompt"] in existing:
            items.append(existing[cleaned["prompt"]])
            continue
        sub, _ = SubSkill.objects.get_or_create(
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


def live_quiz_item_ids(skill) -> list[int]:
    """Primary path: a fresh LLM-generated quiz for this attempt, easy → hard. Raises QuizGenerationError on failure."""
    if not llm_enabled():
        raise QuizGenerationError("LLM not configured")
    try:
        items = generate_items(skill, count=skill.challenge_length)
    except Exception as exc:
        raise QuizGenerationError(str(exc)) from exc
    if len(items) < MIN_ITEMS_TO_START:
        raise QuizGenerationError(f"only {len(items)} usable questions generated")
    items.sort(key=lambda i: ChallengeItem.DIFFICULTY_ORDER.get(i.difficulty, 1))
    return [i.id for i in items]


def quiz_item_ids_for(skill):
    """Live quiz first; ONLY on failure return None so the engine falls back to the saved question bank."""
    try:
        return live_quiz_item_ids(skill)
    except QuizGenerationError:
        logger.warning("Live quiz generation failed for skill %s; using saved questions", skill.pk, exc_info=True)
        return None
