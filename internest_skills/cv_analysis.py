"""CV analysis (feature 1): deterministic facts first, then an AI enrichment that is kept apart from them.

    sha = fingerprint(files, pasted)                           # idempotency key = hash of the uploaded bytes / text
    text, source = read_sources(cv_file, linkedin_pdf, pasted) # local text extraction (raises ExtractionError)
    started = start(user, student, text, sha=sha, source=source)       # daily cap + reuse + enqueue the "cv_analysis" job
    run(analysis_id, text)                                     # the job: facts, AI, grounding, claimed skills
    result = analyze_cv(text, skills, user=user)               # skills only (also /api/ai/skills/extract/)

Facts (cv_facts.parse) never depend on the AI. The AI request (engine task "cv_analysis", private: PII-redacted for
external providers) receives the CV without contact details in `data=`, plus the top-N relevant taxonomy names; the
instructions stay in `system` / `prompt`. Nothing the model says is trusted:
  * a skill counts only when its evidence quote is found in the CV (locate_evidence); a skill the CV also names
    literally is "declared" and saved, otherwise it is "inferred" and waits for the student's confirmation;
  * names outside the taxonomy become SkillSuggestion rows for staff (never Skill rows, audit M1);
  * roles / degrees are kept only when their quote is found and contains the title; years only when the quote states them.
Daily cap: MAX_ANALYSES_PER_DAY per student, taken atomically before queueing and given back when the request is
throttled, deduplicated, or the AI could not answer (so a failed or refused try never uses one up).
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import OperationalError, transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from internest_core.api_monitor import api_feature
from internest_core.async_tasks import RetryTask

from . import cv_facts
from .extraction import (
    ExtractedSkill, _clean_str, _flat, _normalize, _years, extract_skills_local, locate_evidence, read_document_text,
    record_suggestion, relevant_known, sanitize_text, save_claimed_skills,
)
from .models import CVAnalysis, Discipline, Skill, SkillProfile, StudentSkill
from .taxonomy import TaxonomyIndex

logger = logging.getLogger(__name__)

TASK = "cv_analysis"
JOB = "cv_analysis"
MAX_ANALYSES_PER_DAY = 5
REUSE_WINDOW = timedelta(hours=24)
AI_INPUT_CHARS = 10_000  # contacts and repeated lines are stripped first
AI_TIMEOUT = 45.0
AI_MAX_TOKENS = 1400
AI_MAX_SKILLS = 20
INFERRED_MAX_CONFIDENCE = 60
DOMAINS = [d.value for d in Discipline]

AI_OK, AI_OFF, AI_UNAVAILABLE, AI_QUOTA = "ok", "off", "unavailable", "quota"

# "skills" is optional in the task schema only so the legacy chat_json calls labelled "CV Analysis" keep working;
# every cv_analysis request below requires it through `validate=_require_skills`.
SCHEMA = {
    "skills?": [{"name": str, "evidence?": str, "implied?": bool, "years?": None, "confidence?": (int, float)}],
    "summary?": str,
    "top_domains?": [str],
    "suggestions?": [],
    "roles?": [{"title": str, "organization?": str, "evidence?": str}],
    "degrees?": [{"degree": str, "institution?": str, "evidence?": str}],
}
SYSTEM = (
    "You read one CV (Arabic or English, any field) for a student career platform. Use ONLY the CV text in the data "
    "block. Never invent a skill, job, employer, degree, date or number: every item must carry an 'evidence' quote "
    "copied verbatim from the CV (max 120 characters). "
    "skills: up to 20 professional skills, most relevant first, including ones the CV shows through work or projects "
    "without naming them (implied=true). If a skill is the same as a name in known_skills, return that name exactly; "
    "otherwise use the ESCO / O*NET preferred English label (1-4 words, no versions or job titles). "
    "years: only when the CV states years for that skill. confidence: 0-1. "
    "roles and degrees: as written in the CV. summary: one sentence, max 25 words. "
    f"top_domains: 1-3 of: {', '.join(DOMAINS)}. "
    "suggestions: up to 5 concrete improvements to the CV itself (what to add or rewrite), max 25 words each, in the "
    "CV's language. Compact JSON, skills first, exactly this schema: "
    '{"skills":[{"name":"","evidence":"","implied":false,"years":null,"confidence":0.8}],"summary":"",'
    '"top_domains":[""],"suggestions":[{"text":""}],"roles":[{"title":"","organization":"","evidence":""}],'
    '"degrees":[{"degree":"","institution":"","evidence":""}]}'
)
PROMPT = "Analyse the CV in the data block and return the JSON object."


def ai_configured() -> bool:
    from internest_ai import providers

    return bool(providers.configured())


# ---------------------------------------------------------------- skills (deterministic + grounded AI)
@dataclass
class SkillExtraction:
    skills: list                                   # ExtractedSkill to save now (declared / evidenced)
    to_confirm: list = field(default_factory=list)  # [{skill_id, name, evidence, confidence}] AI-inferred, grounded
    summary: str = ""
    top_domains: list = field(default_factory=list)
    years_by_skill: dict = field(default_factory=dict)  # Skill pk → Decimal years (stated in the CV)
    suggestions: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    degrees: list = field(default_factory=list)
    suggested_names: list = field(default_factory=list)  # unknown names sent to staff as SkillSuggestion
    ai_status: str = AI_OFF
    ai_message: str = ""

    @property
    def ai_used(self):
        return self.ai_status == AI_OK

    @property
    def ai_failed(self):
        return self.ai_status in (AI_UNAVAILABLE, AI_QUOTA)


_YEARS_WORD = r"\+?\s*(?:years?|yrs?|سنه|سنوات|سنين|عام|اعوام)"


def _stated_years(raw, snippet):
    """The AI's years for a skill, only when the evidence quote itself states that number of years."""
    years = _years(raw)
    if years is None or not snippet:
        return None
    for number in re.findall(rf"(\d+(?:\.\d+)?)\s*{_YEARS_WORD}", _normalize(snippet)):
        if _years(number) == years:
            return years
    return None


def _names_skill(snippet: str, skill, index: TaxonomyIndex) -> bool:
    """Does the snippet literally name this skill (any alias or variant such as "Python3")?"""
    words = re.findall(r"[\w+#./-]+", snippet)
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            if index.match_exact(" ".join(words[i:i + size])) == skill:
                return True
    return False


def _confidence(value) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 50
    number = number * 100 if number <= 1 else number
    return int(max(1, min(number, INFERRED_MAX_CONFIDENCE)))


def _grounded_entries(items, text, title_key, extra_key):
    kept = []
    for raw in (items or [])[:8]:
        if not isinstance(raw, dict):
            continue
        title = _clean_str(raw.get(title_key), 120)
        snippet = locate_evidence(_clean_str(raw.get("evidence"), 300), text)
        if title and snippet and _flat(title) in _flat(snippet):
            kept.append({title_key: title, extra_key: _clean_str(raw.get(extra_key), 120), "evidence": snippet})
    return kept


def _apply_ai(result: SkillExtraction, reply: dict, text: str, index: TaxonomyIndex):
    domains = [d for d in (reply.get("top_domains") or []) if d in DOMAINS][:3]
    result.top_domains, result.summary = domains, _clean_str(reply.get("summary"), 300)
    local = {e.skill.pk: e for e in result.skills}
    seen = set()
    for raw in reply.get("skills", [])[:AI_MAX_SKILLS]:
        name = _clean_str(raw.get("name"), 100)
        snippet = locate_evidence(_clean_str(raw.get("evidence"), 300), text)
        if not name or not snippet:
            continue  # not grounded in the CV: dropped
        skill = index.match(name)
        if skill is None:
            if record_suggestion(name, domains[0] if domains else "", snippet):
                result.suggested_names.append(name)
            continue
        if skill.pk in seen:
            continue
        seen.add(skill.pk)
        years = _stated_years(raw.get("years"), snippet)
        if years is not None:
            result.years_by_skill[skill.pk] = years
        if skill.pk in local:
            continue
        if _names_skill(snippet, skill, index):
            item = ExtractedSkill(skill, StudentSkill.SOURCE_EXPLICIT, snippet, StudentSkill.PROVENANCE_DECLARED, 90)
            result.skills.append(item)
            local[skill.pk] = item
        else:
            result.to_confirm.append({"skill_id": skill.pk, "name": skill.name, "evidence": snippet,
                                      "confidence": _confidence(raw.get("confidence"))})
    for raw in (reply.get("suggestions") or [])[:5]:
        tip = _clean_str(raw.get("text") if isinstance(raw, dict) else raw, 220)
        if tip:
            result.suggestions.append(tip)
    result.roles = _grounded_entries(reply.get("roles"), text, "title", "organization")
    result.degrees = _grounded_entries(reply.get("degrees"), text, "degree", "institution")


def _require_skills(reply: dict) -> None:
    if not isinstance(reply.get("skills"), list):
        raise ValueError("CV analysis reply without a 'skills' list")


def ai_enrich(text: str, known, *, user=None) -> dict:
    """The one AI call of a CV analysis. Raises AIUnavailable / QuotaExceeded."""
    from internest_ai import engine

    data = {"known_skills": ", ".join(s.name for s in known), "cv": cv_facts.ai_safe_text(text)[:AI_INPUT_CHARS]}
    with api_feature("CV Analysis"):
        return engine.run(TASK, system=SYSTEM, prompt=PROMPT, data=data, user=user, max_tokens=AI_MAX_TOKENS,
                          timeout=AI_TIMEOUT, private=True, validate=_require_skills)


def analyze_cv(text: str, skills=None, *, user=None, local=None) -> SkillExtraction:
    """Local matcher first; with an AI configured, grounded AI skills on top. AI failures keep the local result."""
    from internest_ai.errors import AIUnavailable, QuotaExceeded

    skills = list(skills if skills is not None else Skill.objects.filter(is_active=True))
    local = list(local if local is not None else extract_skills_local(text, skills))
    result = SkillExtraction(skills=local)
    if not ai_configured():
        return result
    known = relevant_known(text, skills, found_ids=[e.skill.pk for e in local])
    try:
        # The model call stays OUTSIDE any transaction: inside one, the quota rows it updates (and, on SQLite, the
        # whole database) stay write-locked for the full call; the load benchmark measured every queue worker
        # waiting in line behind it (4 workers = the throughput of 1) and "database is locked" errors on SQLite.
        reply = ai_enrich(text, known, user=user)
        with transaction.atomic():  # savepoint: a failed write never poisons the caller's transaction
            _apply_ai(result, reply, text, TaxonomyIndex(skills))
    except QuotaExceeded as exc:
        result.ai_status, result.ai_message = AI_QUOTA, exc.user_message
    except AIUnavailable as exc:
        logger.warning("AI CV analysis failed (%s); using the local matcher", type(exc).__name__)
        result.ai_status = AI_UNAVAILABLE
    except Exception:  # a reply we could not use must never break the upload
        logger.exception("AI CV analysis reply could not be applied; using the local matcher")
        result.ai_status = AI_UNAVAILABLE
    else:
        result.ai_status = AI_OK
    return result


# ---------------------------------------------------------------- request side
def fingerprint(files, pasted: str = "") -> str:
    digest = hashlib.sha256()
    for upload in files:
        upload.seek(0)
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        digest.update(b"\x00")
    digest.update(sanitize_text(pasted).encode("utf-8"))
    return digest.hexdigest()


def read_sources(cv_file=None, linkedin_pdf=None, pasted: str = "") -> tuple[str, str]:
    """(text, source) for the uploaded files and / or pasted text. Raises ExtractionError (unreadable, zip bomb, scan)."""
    files = [f for f in (cv_file, linkedin_pdf) if f]
    parts = [read_document_text(f) for f in files] + ([sanitize_text(pasted)] if pasted else [])
    kinds = [kind for kind, present in (("cv", cv_file), ("linkedin", linkedin_pdf), ("text", pasted)) if present]
    return "\n\n".join(parts), (kinds[0] if len(kinds) == 1 else "mixed")


def take_daily_use(profile_id) -> bool:
    today = timezone.localdate()
    with transaction.atomic():
        SkillProfile.objects.filter(pk=profile_id).exclude(extraction_day=today).update(extraction_day=today, extraction_count_today=0)
        return bool(SkillProfile.objects.filter(pk=profile_id, extraction_day=today, extraction_count_today__lt=MAX_ANALYSES_PER_DAY)
                    .update(extraction_count_today=F("extraction_count_today") + 1))


def give_back_use(analysis: CVAnalysis) -> None:
    """Undo the daily use this analysis took (throttled, deduplicated, failed, or no AI answer)."""
    if not analysis.counted:
        return
    SkillProfile.objects.filter(student__user_id=analysis.user_id, extraction_day=timezone.localdate(),
                                extraction_count_today__gt=0).update(extraction_count_today=F("extraction_count_today") - 1)
    CVAnalysis.objects.filter(pk=analysis.pk).update(counted=False)
    analysis.counted = False


def seconds_until_tomorrow() -> int:
    now = timezone.localtime()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


@dataclass
class Started:
    outcome: str                 # "started" | "reused" | "limit" | "throttled"
    analysis: CVAnalysis | None = None
    task: object = None
    error: object = None         # TaskThrottled when throttled


def _reusable(user, sha):
    recent = CVAnalysis.objects.filter(user=user, file_sha256=sha, created_at__gte=timezone.now() - REUSE_WINDOW).first()
    if recent is None:
        return None
    if recent.is_active or (recent.status == CVAnalysis.STATUS_DONE and (recent.ai or {}).get("status") in (AI_OK, AI_OFF)):
        return recent
    return None  # failed, or the AI was down last time: analyse again


def start(user, student, text: str, *, sha: str, source: str = "cv") -> Started:
    from internest_core import async_tasks

    profile, _created = SkillProfile.objects.get_or_create(student=student)
    existing = _reusable(user, sha)
    if existing is not None:
        SkillProfile.objects.filter(pk=profile.pk).update(last_analysis=existing)
        return Started("reused", existing)
    if not take_daily_use(profile.pk):
        return Started("limit")
    analysis = CVAnalysis.objects.create(user=user, source=source, file_sha256=sha, counted=True)
    SkillProfile.objects.filter(pk=profile.pk).update(last_analysis=analysis)
    try:
        task = async_tasks.enqueue(JOB, user, idempotency_key=sha, analysis_id=analysis.pk, text=text)
    except async_tasks.TaskThrottled as exc:
        give_back_use(analysis)
        analysis.delete()
        return Started("throttled", error=exc)
    if getattr(task, "deduplicated", False):  # a concurrent double-submit of the same file is already running
        give_back_use(analysis)
        analysis.delete()
        existing = CVAnalysis.objects.filter(user=user, file_sha256=sha).first()
        SkillProfile.objects.filter(pk=profile.pk).update(last_analysis=existing)
        return Started("reused", existing, task)
    analysis.refresh_from_db()  # the sync backend has already run it
    return Started("started", analysis, task)


# ---------------------------------------------------------------- the job
def run(analysis_id: int, text: str) -> tuple[int, dict]:
    from internest_core.models import StudentProfile

    analysis = CVAnalysis.objects.filter(pk=analysis_id).first()
    if analysis is None:
        return 404, {"error": "not_found"}
    if analysis.status == CVAnalysis.STATUS_DONE:  # a retried job: the work is already saved
        return 200, payload(analysis)
    CVAnalysis.objects.filter(pk=analysis.pk).update(status=CVAnalysis.STATUS_RUNNING)
    try:
        student = StudentProfile.objects.get(user_id=analysis.user_id)
        _analyse(analysis, student, text)
    except OperationalError as exc:  # a busy database is transient: the queue retries with backoff
        CVAnalysis.objects.filter(pk=analysis.pk).update(status=CVAnalysis.STATUS_QUEUED)
        raise RetryTask(f"database busy: {exc}") from exc
    except Exception:
        logger.exception("CV analysis %s failed", analysis_id)
        give_back_use(analysis)
        CVAnalysis.objects.filter(pk=analysis.pk).update(status=CVAnalysis.STATUS_FAILED, error="internal_error",
                                                         finished_at=timezone.now())
        analysis.refresh_from_db()
        return 500, {**payload(analysis), "error": _("The analysis failed. Please try again.")}
    return 200, payload(analysis)


def _analyse(analysis, student, text):
    skills = list(Skill.objects.filter(is_active=True))
    by_pk = {s.pk: s for s in skills}
    local = extract_skills_local(text, skills)
    facts = cv_facts.parse(text, skills, local=local)
    analysis.facts, analysis.quality_score = facts, facts["quality_score"]
    analysis.save(update_fields=["facts", "quality_score", "updated_at"])  # the facts show even if the AI is slow

    found = analyze_cv(text, skills, user=analysis.user_id, local=local)
    have = {e.skill.pk for e in found.skills}
    for row in facts["skills"]:  # skills-section items the regex matcher missed (fuzzy taxonomy match)
        if row["skill_id"] not in have and row["skill_id"] in by_pk:
            found.skills.append(ExtractedSkill(by_pk[row["skill_id"]], StudentSkill.SOURCE_EXPLICIT, row["evidence"],
                                               row["provenance"], row["confidence"]))
    with transaction.atomic():
        created, _updated = save_claimed_skills(student, found.skills, found.years_by_skill)
        owned = set(StudentSkill.objects.filter(student=student).values_list("skill_id", flat=True))
        analysis.ai = {
            "status": found.ai_status, "message": found.ai_message, "summary": found.summary,
            "top_domains": found.top_domains, "suggestions": found.suggestions,
            "skills_to_confirm": [s for s in found.to_confirm if s["skill_id"] not in owned],
            "roles": found.roles, "degrees": found.degrees, "suggested_names": found.suggested_names,
        }
        analysis.facts["saved"] = {
            "created": created,
            "declared": sum(1 for e in found.skills if e.provenance == StudentSkill.PROVENANCE_DECLARED),
            "evidenced": sum(1 for e in found.skills if e.provenance == StudentSkill.PROVENANCE_EVIDENCED),
        }
        analysis.status, analysis.finished_at = CVAnalysis.STATUS_DONE, timezone.now()
        analysis.save(update_fields=["facts", "ai", "status", "finished_at", "updated_at"])
        profile_update = {"last_extracted_at": timezone.now()}
        if found.ai_used:
            profile_update.update(cv_summary=found.summary, top_domains=found.top_domains)
        SkillProfile.objects.filter(student=student).update(**profile_update)
    if found.ai_failed:
        give_back_use(analysis)  # like before: a try without the AI does not use up a daily analysis


def confirm_skills(analysis: CVAnalysis, student, skill_ids) -> int:
    """Save the AI-inferred skills the student confirmed (only ids this analysis offered). Returns how many."""
    offered = {s["skill_id"]: s for s in (analysis.ai or {}).get("skills_to_confirm", [])}
    chosen = [offered[i] for i in dict.fromkeys(skill_ids) if i in offered]
    skills = {s.pk: s for s in Skill.objects.filter(pk__in=[c["skill_id"] for c in chosen], is_active=True)}
    items = [ExtractedSkill(skills[c["skill_id"]], StudentSkill.SOURCE_IMPLICIT, c["evidence"], StudentSkill.PROVENANCE_INFERRED,
                            c["confidence"]) for c in chosen if c["skill_id"] in skills]
    with transaction.atomic():
        created, _updated = save_claimed_skills(student, items)
        done = {i.skill.pk for i in items}
        analysis.ai["skills_to_confirm"] = [s for s in analysis.ai.get("skills_to_confirm", []) if s["skill_id"] not in done]
        analysis.save(update_fields=["ai", "updated_at"])
    return len(items)


# ---------------------------------------------------------------- presentation (hub + API share it)
def payload(analysis: CVAnalysis) -> dict:
    facts, ai = analysis.facts or {}, analysis.ai or {}
    return {
        "id": analysis.pk,
        "status": analysis.status,
        "source": analysis.source,
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        "finished_at": analysis.finished_at.isoformat() if analysis.finished_at else None,
        "quality_score": analysis.quality_score,
        "facts": {k: v for k, v in facts.items() if k not in ("missing", "quality_score")},
        "missing": cv_facts.describe(facts),
        "ai": {**ai, "advisory": True},
        "status_url": reverse("skills_api_cv_analysis_detail", args=[analysis.pk]),
    }


def view_context(analysis: CVAnalysis | None) -> dict | None:
    """What the hub's analysis card shows (no query: the analysis comes with the SkillProfile)."""
    if analysis is None:
        return None
    facts, ai = analysis.facts or {}, analysis.ai or {}
    saved = facts.get("saved", {})
    notice = None
    if ai.get("status") in (AI_UNAVAILABLE, AI_QUOTA):
        notice = "partial" if saved.get("declared", 0) + saved.get("evidenced", 0) else "empty"
    score = analysis.quality_score
    return {
        "obj": analysis,
        "active": analysis.is_active,
        "failed": analysis.status == CVAnalysis.STATUS_FAILED,
        "score": score,
        "level": None if score is None else ("good" if score >= 75 else "fair" if score >= 50 else "low"),
        "facts": facts,
        "missing": cv_facts.describe(facts)[:6],
        "ai": ai,
        "notice": notice,
        "quota_message": ai.get("message") if ai.get("status") == AI_QUOTA else "",
        "to_confirm": ai.get("skills_to_confirm", []),
        "status_url": reverse("skills_api_cv_analysis_detail", args=[analysis.pk]),
    }


def hub_message(analysis: CVAnalysis) -> tuple[str, str] | None:
    """(level, text) for the flash message after an analysis finished (None while it runs)."""
    if analysis is None or analysis.status != CVAnalysis.STATUS_DONE:
        return None
    saved = (analysis.facts or {}).get("saved", {})
    total = saved.get("declared", 0) + saved.get("evidenced", 0)
    if (analysis.ai or {}).get("status") in (AI_UNAVAILABLE, AI_QUOTA) and not total:
        return None  # the inline notice explains it
    return "success", _(
        "Analysis complete: %(explicit)s explicit and %(inferred)s inferred skills found (%(new)s new). "
        "They are marked as Claimed until you verify them.") % {
        "explicit": saved.get("declared", 0), "inferred": saved.get("evidenced", 0), "new": saved.get("created", 0)}
