"""Semantic skill relevance for the Market Readiness Gate.

A required skill counts as covered when the student verified it exactly, or verified *related* skills.
Relatedness comes from stored SkillRelation rows (seeded, AI-generated or admin-curated) plus a cheap
name heuristic ("Graphic Designer" ≈ "Graphic Design"). Several related skills combine with a noisy-OR,
so Photoshop + Illustrator + UI Design together can cover "Graphic Designer".
No LLM call happens while computing a match — AI relations are generated once, when a skill is created.
"""
import logging
import re

from django.db.models import Q
from django.utils import timezone

from .models import Skill, SkillRelation

logger = logging.getLogger(__name__)

STEM_MATCH_WEIGHT = 0.95
AI_MIN_WEIGHT = 0.3
AI_CANDIDATES = 300

# Curated pairs for the seeded taxonomy (name, name, weight).
SEED_RELATIONS = [
    ("Graphic Design", "UI/UX Design", 0.6),
    ("Graphic Design", "Video Editing", 0.4),
    ("UI/UX Design", "Web Development", 0.4),
    ("Web Development", "Python", 0.35),
    ("SQL", "Python", 0.4),
    ("SQL", "Excel", 0.4),
    ("Financial Analysis", "Excel", 0.6),
    ("Digital Marketing", "Business Communication", 0.35),
    ("Legal Research", "Contract Drafting", 0.6),
    ("Project Management", "Business Communication", 0.4),
]

_SUFFIXES = ("ers", "er", "ing", "ists", "ist", "s")


def _stems(name: str) -> frozenset:
    words = re.findall(r"[a-z0-9؀-ۿ+#.]+", (name or "").lower())
    out = set()
    for w in words:
        for suffix in _SUFFIXES:
            if len(w) > len(suffix) + 3 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w)
    return frozenset(out)


def name_relevance(a: Skill, b: Skill) -> float:
    """Same stems ("Graphic Designer" vs "Graphic Design") → near-equivalent."""
    sa, sb = _stems(a.name), _stems(b.name)
    return STEM_MATCH_WEIGHT if sa and sa == sb else 0.0


def relation_map(skill_ids) -> dict:
    """{(a_id, b_id): weight} for every stored relation touching these skills, both directions."""
    ids = set(skill_ids)
    if not ids:
        return {}
    weights = {}
    for a, b, w in SkillRelation.objects.filter(Q(skill_id__in=ids) | Q(related_id__in=ids)).values_list(
        "skill_id", "related_id", "weight"
    ):
        weights[(a, b)] = max(w, weights.get((a, b), 0))
        weights[(b, a)] = max(w, weights.get((b, a), 0))
    return weights


def coverage(required: Skill, verified: list, weights: dict) -> float:
    """0–1: how well the student's verified skills cover one required skill (noisy-OR of related evidence)."""
    if any(v.id == required.id for v in verified):
        return 1.0
    miss = 1.0
    for v in verified:
        w = max(weights.get((required.id, v.id), 0.0), name_relevance(required, v))
        if w:
            miss *= 1.0 - min(w, 0.99)
    return 1.0 - miss


def aggregate_score(required: list, verified: list, weights=None) -> tuple[int, dict]:
    """(score 0–100 floored, {required_id: coverage}). No required skills → 100."""
    if not required:
        return 100, {}
    if weights is None:
        weights = relation_map([s.id for s in required] + [v.id for v in verified])
    per_skill = {r.id: coverage(r, verified, weights) for r in required}
    return int(sum(per_skill.values()) * 100 // len(required)), per_skill


# ---------------------------------------------------------------- relation generation
def seed_relations():
    by_name = {s.name.lower(): s for s in Skill.objects.all()}
    created = 0
    for a, b, w in SEED_RELATIONS:
        sa, sb = by_name.get(a.lower()), by_name.get(b.lower())
        if sa and sb:
            _, made = SkillRelation.objects.update_or_create(skill=sa, related=sb, defaults={"weight": w, "source": "seed"})
            created += made
    return created


_AI_PROMPT = (
    "You map professional skills. Given a TARGET skill and a list of KNOWN skills, return the known skills that "
    "are semantically related to the target (synonyms, sub-skills, tools of the trade, adjacent skills), with a "
    "weight 0-1 = how strongly evidence of that known skill demonstrates the target (1 = equivalent, 0.8 = core tool, "
    "0.5 = adjacent). Only names from KNOWN, exactly as written; skip weights below 0.3; max 15. "
    'JSON only: {"r":[{"n":"<known name>","w":0.8}]}'
)


def relate_skill_ai(skill: Skill) -> int:
    """Ask the LLM which existing skills relate to `skill`; store them. Returns rows written (0 on failure)."""
    from django.db import transaction

    from .llm import LLM_ERRORS

    try:
        with transaction.atomic():
            return _relate_skill_ai(skill)
    except LLM_ERRORS as exc:
        logger.warning("AI skill relation failed for %s (%s)", skill.pk, exc)
        return 0


def _relate_skill_ai(skill: Skill) -> int:
    from .llm import chat_json, llm_enabled

    if not llm_enabled():
        return 0
    candidates = list(Skill.objects.filter(is_active=True).exclude(pk=skill.pk).order_by("-id")[:AI_CANDIDATES])
    if not candidates:
        return 0
    by_name = {c.name.lower(): c for c in candidates}
    data = chat_json(_AI_PROMPT, f"TARGET: {skill.name}\nKNOWN: {', '.join(c.name for c in candidates)}", max_tokens=400)
    written = 0
    for item in (data.get("r") or [])[:15]:
        if not isinstance(item, dict):
            continue
        other = by_name.get(str(item.get("n", "")).strip().lower())
        try:
            weight = float(item.get("w", 0))
        except (TypeError, ValueError):
            continue
        if other is None or not AI_MIN_WEIGHT <= weight <= 1:
            continue
        SkillRelation.objects.update_or_create(skill=skill, related=other, defaults={"weight": round(weight, 2), "source": "ai"})
        written += 1
    Skill.objects.filter(pk=skill.pk).update(relations_refreshed_at=timezone.now())
    return written
