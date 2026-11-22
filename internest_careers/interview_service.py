"""Interview kits: who may build one for what, the deterministic question set, and the optional AI enrichment.

    target = resolve_target(user, {"application": 7})        # raises KitDenied (-> 404) when not allowed
    kit = create_kit(user, {"application": 7}, seniority="intern", use_ai=True)
    enrich(kit)                                              # extra AI questions; the kit is kept on any AI failure

Access:
  * partners: their own applicants (`application`) or their own opportunities (`internship`). Assessment details
    (scores, flagged areas, unverified claims) are used only when their paid access covers the opportunity
    (internest_startups.entitlements.covers), exactly like the applicant page; otherwise only verified skill names.
  * students: practice for themselves, for an open opportunity or one they applied to (`internship`), or for a
    career role path (`role`, see career_service.role_profiles). Their own assessment data is always used.
The AI sees only skill names, levels / status, the role title and seniority (no names, contacts or CV text).
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import get_language, gettext as _

from internest_ai import engine
from internest_ai.errors import AIUnavailable, QuotaExceeded
from internest_core import identity
from internest_core.models import Application, Internship, opportunity_today
from internest_skills.matching import SKILL_COVERED
from internest_skills.models import StudentSkill
from internest_skills.semantic import aggregate_score

from . import question_bank as bank
from .ai_tasks import INTERVIEW_TASK
from .models import InterviewKit

logger = logging.getLogger(__name__)

MAX_SKILL_QUESTIONS = 6
MAX_AI_QUESTIONS = 4
MAX_QUESTIONS = 12
REPEAT_WINDOW = timedelta(minutes=5)
SENIORITIES = {value for value, _label in InterviewKit.SENIORITY_CHOICES}
KIND_PRIORITY = {"subskill": 0, "evidence": 1, "gap": 2, "depth": 3}  # probe the weak spots first
DIFFICULTY_ORDER = {d: i for i, d in enumerate(bank.DIFFICULTIES)}


class KitDenied(Exception):
    """The user may not build or read this kit (answered as 404, so nothing leaks)."""


@dataclass
class KitTarget:
    role: str
    required: list                    # Skill objects
    partner: object = None
    internship: object = None
    application: object = None
    student: object = None            # whose skills shape the questions (None: a generic kit for the opportunity)
    include_assessments: bool = False
    candidate: dict = field(default_factory=dict)   # skill_id -> {"skill", "status", "score", "areas"}
    coverage: dict = field(default_factory=dict)    # skill_id -> 0..1 (semantic, from verified skills)


# ---------------------------------------------------------------- access
def _required(internship) -> list:
    return sorted(internship.required_skills.all(), key=lambda s: s.name)


def candidate_skills(student, include_assessments: bool) -> dict:
    """The candidate's skills as the kit may use them: verified only unless assessments are allowed."""
    rows = StudentSkill.objects.filter(student=student).select_related("skill")
    if not include_assessments:
        rows = rows.filter(status=StudentSkill.STATUS_VERIFIED)
    out = {}
    for ss in rows:
        out[ss.skill_id] = {
            "skill": ss.skill, "status": ss.status,
            "score": ss.score if include_assessments else None,
            "areas": [a.get("name", "") for a in (ss.lag_sub_skills or []) if isinstance(a, dict) and a.get("name")]
            if include_assessments else [],
        }
    return out


def _with_candidate(target: KitTarget) -> KitTarget:
    if target.student is None:
        return target
    target.candidate = candidate_skills(target.student, target.include_assessments)
    verified = [c["skill"] for c in target.candidate.values() if c["status"] == StudentSkill.STATUS_VERIFIED]
    if target.required:
        _score, target.coverage = aggregate_score(target.required, verified)
    return target


def resolve_target(user, spec: dict) -> KitTarget:
    """Check access and gather what the questions are built from. Raises KitDenied."""
    from internest_startups.entitlements import covers

    spec = spec or {}
    partner = identity.partner_profile(user)
    if partner is not None:
        if spec.get("application"):
            app = (Application.objects.select_related("internship", "applicant__studentprofile")
                   .filter(pk=spec["application"], internship__partner=partner).first())
            if app is None:
                raise KitDenied
            student = getattr(app.applicant, "studentprofile", None)
            return _with_candidate(KitTarget(role=app.internship.title, required=_required(app.internship), partner=partner,
                                             internship=app.internship, application=app, student=student,
                                             include_assessments=covers(partner, app.internship)))
        if spec.get("internship"):
            internship = Internship.objects.filter(pk=spec["internship"], partner=partner).first()
            if internship is None:
                raise KitDenied
            return KitTarget(role=internship.title, required=_required(internship), partner=partner, internship=internship)
        raise KitDenied
    student = identity.student_profile(user)
    if student is None:
        raise KitDenied
    if spec.get("internship"):
        visible = Q(is_active=True, deadline__gte=opportunity_today()) | Q(application__applicant=user)
        internship = Internship.objects.filter(visible, pk=spec["internship"]).distinct().first()
        if internship is None:
            raise KitDenied
        return _with_candidate(KitTarget(role=internship.title, required=_required(internship), internship=internship,
                                         student=student, include_assessments=True))
    if spec.get("role"):
        from .career_service import role_by_key

        profile = role_by_key(spec["role"])
        if profile is None:
            raise KitDenied
        return _with_candidate(KitTarget(role=profile["title"], required=profile["skills"], student=student,
                                         include_assessments=True))
    raise KitDenied


# ---------------------------------------------------------------- deterministic questions
def _kind(skill, target: KitTarget) -> tuple[str, str, object]:
    """(kind, flagged area, score) for one required skill."""
    if target.student is None:
        return "evidence", "", None
    info = target.candidate.get(skill.id)
    if info and info["status"] == StudentSkill.STATUS_LAG and info["areas"]:
        return "subskill", info["areas"][0], info["score"]
    if info and info["status"] == StudentSkill.STATUS_VERIFIED:
        return "depth", "", info["score"]
    if info or target.coverage.get(skill.id, 0) >= SKILL_COVERED:
        return "evidence", "", None  # listed / under review, or covered by a related verified skill
    return "gap", "", None


def build_questions(target: KitTarget, seniority: str) -> list[dict]:
    """The question bank applied to the target: one question per required skill (weak spots first), framed by
    two behavioural questions. Same input -> same output."""
    skills = list(target.required)
    if not skills and target.candidate:  # no required skills: interview on what the candidate has verified
        skills = sorted((c["skill"] for c in target.candidate.values() if c["status"] == StudentSkill.STATUS_VERIFIED),
                        key=lambda s: s.name)
    planned = [(skill, *_kind(skill, target)) for skill in skills]
    planned.sort(key=lambda p: (KIND_PRIORITY[p[1]], p[0].name.casefold()))
    items = [bank.skill_question(kind, skill=skill.name, discipline=skill.discipline, area=area, seniority=seniority,
                                 score=score) for skill, kind, area, score in planned[:MAX_SKILL_QUESTIONS]]
    items.sort(key=lambda q: (DIFFICULTY_ORDER[q["difficulty"]], q["skill"].casefold()))
    return [bank.behavioral_question(0, role=target.role, seniority=seniority), *items,
            bank.behavioral_question(2 if seniority == "mid" else 1, role=target.role, seniority=seniority)]


# ---------------------------------------------------------------- AI enrichment
def ai_payload(target: KitTarget, seniority: str, questions: list[dict]) -> dict:
    """What the AI may see: role, seniority, skills with level / status. No person, no contacts, no free text."""
    skills = []
    for skill in target.required:
        info = target.candidate.get(skill.id) if target.student is not None else None
        skills.append({"skill": skill.name, "status": (info or {}).get("status") or ("unknown" if target.student is None else "missing"),
                       "level": (info or {}).get("score"), "weak_areas": (info or {}).get("areas", [])})
    return {"role": target.role[:120], "seniority": seniority, "skills": skills,
            "existing_questions": [q["question"] for q in questions]}


SYSTEM = ("You design structured interviews for internships and junior roles. You receive a role title, the seniority "
          "and the required skills with the candidate's status and level (no personal data). Write additional questions "
          "that are fair, job-related and answerable by a student. Never ask about age, gender, religion, marital "
          "status, health, nationality or any other protected trait. Do not repeat the existing questions. Each "
          "question has: purpose, expected_evidence, 3 criteria, 2 follow_ups, difficulty (easy, medium or hard) and "
          "skill (exactly one of the given skill names, or an empty string for a general question).")


def _language_name() -> str:
    return "Arabic (Modern Standard, simple)" if (get_language() or "").startswith("ar") else "English"


def _clean_ai_questions(reply: dict, allowed: set[str], existing: list[dict]) -> list[dict]:
    seen = {re.sub(r"\W+", " ", q["question"]).strip().casefold() for q in existing}
    by_name = {name.casefold(): name for name in allowed}
    out = []
    for raw in reply.get("questions") or []:
        text = " ".join(str(raw.get("question") or "").split())[:400]
        key = re.sub(r"\W+", " ", text).strip().casefold()
        skill = by_name.get(str(raw.get("skill") or "").strip().casefold(), "")
        if not text or key in seen or (raw.get("skill") and not skill):
            continue  # empty, duplicate, or about a skill the role does not need
        seen.add(key)
        out.append({
            "question": text, "purpose": " ".join(str(raw.get("purpose") or "").split())[:300],
            "expected_evidence": " ".join(str(raw.get("expected_evidence") or "").split())[:300],
            "criteria": [" ".join(str(c).split())[:200] for c in raw.get("criteria") or [] if str(c).strip()][:4],
            "follow_ups": [" ".join(str(f).split())[:200] for f in raw.get("follow_ups") or [] if str(f).strip()][:3],
            "difficulty": raw.get("difficulty") if raw.get("difficulty") in bank.DIFFICULTIES else "medium",
            "skill": skill, "kind": "ai", "source": "ai",
        })
        if len(out) == MAX_AI_QUESTIONS:
            break
    return out


def enrich(kit: InterviewKit, target: KitTarget | None = None) -> str:
    """Add AI questions to the kit. Returns the new ai_status; the deterministic questions always stay."""
    if target is None:
        target = target_for_kit(kit)
    payload = ai_payload(target, kit.seniority, kit.questions)
    allowed = {s.name for s in target.required}
    try:
        reply = engine.run(INTERVIEW_TASK, system=SYSTEM, data=payload, user=kit.owner_id, partner=kit.partner_id,
                           prompt=f"Write up to {MAX_AI_QUESTIONS} additional questions in {_language_name()} as "
                                  '{"questions": [...]}.', max_tokens=1800, timeout=40)
        added = _clean_ai_questions(reply, allowed, kit.questions)
        status = InterviewKit.AI_DONE
    except QuotaExceeded:
        added, status = [], InterviewKit.AI_QUOTA
    except AIUnavailable as exc:
        logger.info("Interview kit %s: AI enrichment unavailable (%s)", kit.pk, exc)
        added, status = [], InterviewKit.AI_UNAVAILABLE
    if added:
        kit.questions = (kit.questions + added)[:MAX_QUESTIONS]
        kit.source = InterviewKit.SOURCE_AI
    kit.ai_status = status
    kit.save(update_fields=["questions", "source", "ai_status", "updated_at"])
    return status


def target_for_kit(kit: InterviewKit) -> KitTarget:
    """Rebuild the target of an existing kit for its owner (access is checked again)."""
    owner = kit.owner
    if kit.application_id:
        return resolve_target(owner, {"application": kit.application_id})
    if kit.internship_id:
        return resolve_target(owner, {"internship": kit.internship_id})
    from internest_skills.models import Skill

    names = {q["skill"] for q in kit.questions if q.get("skill")}
    student = identity.student_profile(owner) if kit.is_practice else None
    return _with_candidate(KitTarget(role=kit.role, required=list(Skill.objects.filter(name__in=names).order_by("name")),
                                     student=student, include_assessments=student is not None))


# ---------------------------------------------------------------- create / read
def clean_seniority(value) -> str:
    return value if value in SENIORITIES else InterviewKit.SENIORITY_INTERN


def clean_role(value, default: str) -> str:
    role = " ".join(str(value or "").split())[:120]
    return role or default[:120]


def create_kit(user, spec: dict, *, seniority: str = "intern", role: str = "", use_ai: bool = False) -> InterviewKit:
    target = resolve_target(user, spec)
    seniority = clean_seniority(seniority)
    target.role = clean_role(role, target.role)
    # Idempotent: the same request again within a few minutes (double click, retried job) returns the same kit.
    recent = InterviewKit.objects.filter(
        owner=user, application=target.application, internship=target.internship, role=target.role, seniority=seniority,
        created_at__gte=timezone.now() - REPEAT_WINDOW).order_by("-created_at").first()
    if recent is not None:
        if use_ai and recent.ai_status == InterviewKit.AI_NONE:
            enrich(recent, target)
        return recent
    kit = InterviewKit.objects.create(
        owner=user, partner=target.partner, internship=target.internship, application=target.application,
        role=target.role, seniority=seniority, questions=build_questions(target, seniority),
        includes_assessments=bool(target.student is not None and target.include_assessments))
    if use_ai:
        enrich(kit, target)
    return kit


def own_kit(user, kit_id) -> InterviewKit:
    """The kit if `user` owns it (and, for a company kit, still belongs to that company); else KitDenied."""
    kit = (InterviewKit.objects.select_related("internship", "application__applicant").filter(pk=kit_id, owner=user).first()
           if user.is_authenticated else None)
    if kit is None:
        raise KitDenied
    if kit.partner_id:
        partner = identity.partner_profile(user)
        if partner is None or partner.pk != kit.partner_id:
            raise KitDenied
    return kit


def kits_for(user, limit: int = 30):
    return InterviewKit.objects.filter(owner=user).select_related("internship").only(
        "id", "role", "seniority", "source", "created_at", "partner_id", "internship__title", "questions")[:limit]


def kit_json(kit: InterviewKit) -> dict:
    from django.urls import reverse

    return {"id": kit.pk, "role": kit.role, "seniority": kit.seniority, "source": kit.source, "ai_status": kit.ai_status,
            "includes_assessments": kit.includes_assessments, "internship_id": kit.internship_id,
            "application_id": kit.application_id, "questions": kit.questions, "created_at": kit.created_at.isoformat(),
            "url": reverse("interview_kit", args=[kit.pk]), "print_url": reverse("interview_kit_print", args=[kit.pk])}


def describe_ai_status(status: str) -> str:
    return {
        InterviewKit.AI_DONE: _("AI-tailored questions were added."),
        InterviewKit.AI_QUOTA: _("You have reached today's AI limit, so this kit uses the question bank only. Try adding AI questions tomorrow."),
        InterviewKit.AI_UNAVAILABLE: _("The AI is busy right now, so this kit uses the question bank only. You can add AI questions later."),
    }.get(status, "")
