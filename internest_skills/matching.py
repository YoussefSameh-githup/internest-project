"""Market Readiness Gate: a student may apply only with ≥MATCH_THRESHOLD% semantic coverage of an opportunity's required skills.

Each required skill scores 1.0 when verified exactly, otherwise the combined relevance of the student's
*related* verified skills (see semantic.py). The opportunity score is the average across required skills.
"""
from dataclasses import dataclass, field

from .models import Skill, StudentSkill
from .semantic import aggregate_score, relation_map

MATCH_THRESHOLD = 70  # single source for the apply gate, Talent Pool eligibility and every UI text that mentions it
SKILL_COVERED = MATCH_THRESHOLD / 100  # a single required skill counts as covered at this relevance


@dataclass
class SkillMatch:
    score: int                                    # 0–100
    required: list = field(default_factory=list)  # Skill objects
    matched: list = field(default_factory=list)   # covered (exactly or semantically)
    missing: list = field(default_factory=list)   # [(Skill, StudentSkill | None)]
    coverage: dict = field(default_factory=dict)  # {skill_id: 0–1}

    @property
    def unlocked(self) -> bool:
        return self.score >= MATCH_THRESHOLD

    @property
    def threshold(self) -> int:
        return MATCH_THRESHOLD


def verified_skills(student) -> list:
    if student is None:
        return []
    return list(Skill.objects.filter(student_skills__student=student, student_skills__status=StudentSkill.STATUS_VERIFIED))


def verified_skill_ids(student) -> set:
    return {s.id for s in verified_skills(student)}


def skill_match(student, internship, verified=None) -> SkillMatch:
    required = list(internship.required_skills.all())
    if not required:
        return SkillMatch(score=100)
    if verified is None:
        verified = verified_skills(student)
    score, coverage = aggregate_score(required, verified)
    matched = [s for s in required if coverage[s.id] >= SKILL_COVERED]
    missing_skills = [s for s in required if coverage[s.id] < SKILL_COVERED]
    records = {}
    if student is not None and missing_skills:
        records = {ss.skill_id: ss for ss in StudentSkill.objects.filter(student=student, skill__in=missing_skills)}
    return SkillMatch(
        score=score, required=required, matched=matched,
        missing=[(s, records.get(s.id)) for s in missing_skills], coverage=coverage,
    )


def bulk_match_scores(internships, student) -> dict:
    """{internship_id: score} for a page of opportunities with two queries total (list views)."""
    verified = verified_skills(student)
    required_by_opp = {i.id: list(i.required_skills.all()) for i in internships}  # use prefetch_related
    all_ids = {s.id for skills in required_by_opp.values() for s in skills} | {v.id for v in verified}
    weights = relation_map(all_ids)
    return {opp_id: aggregate_score(req, verified, weights)[0] for opp_id, req in required_by_opp.items()}
