"""Market fit: compare a student's verified skills with what open opportunities on Internest require.

Demand for a skill = number of open opportunities (active, deadline not passed) that require it.
Readiness index = demand covered by the student's verified skills / total demand, over the field's top skills.
"""
from dataclasses import dataclass, field

from django.db.models import Count, Q
from django.utils import timezone

from .models import Discipline, Skill, StudentSkill

RADAR_AXES = 8   # skills shown on the radar
TOP_GAPS = 3     # missing skills with action buttons


@dataclass
class MarketFit:
    readiness: int = 0                     # 0–100
    demand: list = field(default_factory=list)   # [(Skill, opportunities requiring it)] most demanded first
    student_levels: dict = field(default_factory=dict)  # skill pk → 0–100 (verified only)
    gaps: list = field(default_factory=list)     # [(Skill, demand)] missing, most demanded first
    open_opportunities: int = 0

    @property
    def radar(self):
        top = max((n for _s, n in self.demand), default=1)
        return {
            "labels": [s.name for s, _n in self.demand],
            "market": [round(n / top * 100) for _s, n in self.demand],
            "student": [self.student_levels.get(s.pk, 0) for s, _n in self.demand],
        }


def market_demand(discipline=None, limit=RADAR_AXES):
    """Most-required skills across open opportunities, optionally within one field (Discipline)."""
    today = timezone.now().date()
    open_q = Q(opportunities__is_active=True, opportunities__deadline__gte=today)
    skills = Skill.objects.filter(is_active=True)
    if discipline in Discipline.values:
        skills = skills.filter(discipline=discipline)
    ranked = (skills.annotate(demand=Count("opportunities", filter=open_q, distinct=True))
              .filter(demand__gt=0).order_by("-demand", "name")[:limit])
    return [(s, s.demand) for s in ranked]


def verified_level(student_skill) -> int:
    """0–100 for the radar: the challenge score, or 100 when verified by freelance work / certificate."""
    if student_skill.status != StudentSkill.STATUS_VERIFIED:
        return 0
    return student_skill.score if student_skill.score is not None else 100


def market_fit(student, discipline=None) -> MarketFit:
    from internest_core.models import Internship

    demand = market_demand(discipline)
    owned = {ss.skill_id: verified_level(ss) for ss in
             StudentSkill.objects.filter(student=student, skill__in=[s for s, _n in demand])}
    total = sum(n for _s, n in demand)
    covered = sum(n for s, n in demand if owned.get(s.pk))
    return MarketFit(
        readiness=round(covered / total * 100) if total else 0,
        demand=demand,
        student_levels={pk: level for pk, level in owned.items() if level},
        gaps=[(s, n) for s, n in demand if not owned.get(s.pk)][:TOP_GAPS],
        open_opportunities=Internship.objects.filter(is_active=True, deadline__gte=timezone.now().date()).count(),
    )
