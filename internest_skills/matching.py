"""Market Readiness Gate: a student may apply only with ≥80% of an opportunity's required skills verified."""
from dataclasses import dataclass, field

from .models import StudentSkill

MATCH_THRESHOLD = 80


@dataclass
class SkillMatch:
    score: int                                   # 0–100
    required: list = field(default_factory=list)  # Skill objects
    matched: list = field(default_factory=list)
    missing: list = field(default_factory=list)  # [(Skill, StudentSkill | None)]

    @property
    def unlocked(self) -> bool:
        return self.score >= MATCH_THRESHOLD

    @property
    def threshold(self) -> int:
        return MATCH_THRESHOLD


def verified_skill_ids(student) -> set:
    if student is None:
        return set()
    return set(
        StudentSkill.objects.filter(student=student, status=StudentSkill.STATUS_VERIFIED).values_list("skill_id", flat=True)
    )


def skill_match(student, internship, verified_ids=None) -> SkillMatch:
    """|verified ∩ required| / |required|. No required skills listed → nothing to gate (100%)."""
    required = list(internship.required_skills.all())
    if not required:
        return SkillMatch(score=100)
    if verified_ids is None:
        verified_ids = verified_skill_ids(student)
    matched = [s for s in required if s.id in verified_ids]
    missing_skills = [s for s in required if s.id not in verified_ids]
    records = {}
    if student is not None and missing_skills:
        records = {
            ss.skill_id: ss
            for ss in StudentSkill.objects.filter(student=student, skill__in=missing_skills)
        }
    # Floor, so 79.9% never rounds up into an unlock.
    score = (len(matched) * 100) // len(required)
    return SkillMatch(
        score=score,
        required=required,
        matched=matched,
        missing=[(s, records.get(s.id)) for s in missing_skills],
    )
