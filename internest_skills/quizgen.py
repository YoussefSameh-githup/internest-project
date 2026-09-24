"""LLM quiz generation: tops up a skill's question bank so every extracted skill can be verified."""
import logging
import random

from .llm import chat_json, llm_enabled
from .models import ChallengeItem, SubSkill

logger = logging.getLogger(__name__)

MIN_ITEMS_TO_START = 3
GENERATE_BATCH = 8
GENERATED_TIME_LIMIT = 45

_SYSTEM_PROMPT = (
    "You write proctored skill-verification quizzes for university students and fresh graduates. "
    "Multiple choice, practical and scenario-based, exactly one correct answer, 4 short options, question <= 250 chars, "
    "no 'all/none of the above'. Spread questions across 2-4 sub-skills. "
    'JSON only: {"q":[{"s":"sub-skill","p":"question","c":["","","",""],"a":0}]}'
)


def _clean_item(raw):
    if not isinstance(raw, dict):
        return None
    prompt = str(raw.get("p", "")).strip()
    sub = str(raw.get("s", "")).strip()[:120] or "Core concepts"
    choices = raw.get("c")
    answer = raw.get("a")
    if not (10 <= len(prompt) <= 400) or not isinstance(choices, list) or not isinstance(answer, int):
        return None
    choices = [str(c).strip()[:200] for c in choices]
    if not (2 <= len(choices) <= 6) or any(not c for c in choices) or len(set(choices)) != len(choices):
        return None
    if not 0 <= answer < len(choices):
        return None
    correct = choices[answer]
    random.shuffle(choices)  # remove the model's answer-position bias
    return sub, prompt, choices, choices.index(correct)


def generate_items(skill, count=GENERATE_BATCH) -> int:
    existing_subs = list(skill.sub_skills.values_list("name", flat=True))
    user = (
        f"Skill: {skill.name} ({skill.get_discipline_display()}). Questions: {count}."
        + (f" Use these sub-skills where relevant: {', '.join(existing_subs)}." if existing_subs else "")
    )
    data = chat_json(_SYSTEM_PROMPT, user, max_tokens=1500, timeout=40)
    raw_items = data.get("q")
    if not isinstance(raw_items, list):
        raise ValueError("LLM quiz response missing 'q' list")

    existing_prompts = set(skill.items.values_list("prompt", flat=True))
    created = 0
    for raw in raw_items[:count]:
        cleaned = _clean_item(raw)
        if cleaned is None:
            continue
        sub_name, prompt, choices, correct_index = cleaned
        if prompt in existing_prompts:
            continue
        sub, _ = SubSkill.objects.get_or_create(skill=skill, name=sub_name, defaults={"keywords": sub_name})
        ChallengeItem.objects.create(
            skill=skill, sub_skill=sub, kind=ChallengeItem.KIND_MCQ, prompt=prompt,
            choices=choices, correct_index=correct_index, time_limit_seconds=GENERATED_TIME_LIMIT,
        )
        existing_prompts.add(prompt)
        created += 1
    return created


def ensure_challenge_items(skill) -> int:
    """Make sure `skill` has a full challenge; returns the number of active items afterwards."""
    active = skill.items.filter(is_active=True).count()
    if active >= skill.challenge_length or not llm_enabled():
        return active
    try:
        generate_items(skill, count=min(GENERATE_BATCH, max(skill.challenge_length - active, MIN_ITEMS_TO_START)))
    except Exception:
        logger.warning("Quiz generation failed for skill %s", skill.pk, exc_info=True)
    return skill.items.filter(is_active=True).count()
