"""Freelance work importer: AI-parse a gig (Mostaql, Khamsat, Upwork, Fiverr, portfolio) and verify its skills.

The link is passed to the AI as context only — we never scrape the platforms. Skills are normalized onto
the Skill taxonomy; unknown names are dropped because only taxonomy skills can carry a verified badge.
"""
import logging
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .extraction import _clean_str, compact_cv_text, extract_skills_local
from .llm import LLMUnavailable, chat_json, llm_enabled
from .models import Skill, StudentFreelanceGig, StudentSkill
from .taxonomy import TaxonomyIndex
from .utils import OWNER_MATCH_THRESHOLD, best_owner_match, extract_platform_username

logger = logging.getLogger(__name__)

MAX_GIG_SKILLS = 8
_PROMPT = (
    "You read a freelance gig a student completed (job post, delivery description and/or client review; Arabic or "
    "English; from Mostaql, Khamsat, Upwork, Fiverr or a portfolio). Use only what the text says; never invent. "
    "project_title: short English title. client_feedback: the client's review quoted or summarized in its original "
    "language, empty if none. rating: the client's star rating 1-5 if stated, else null. deliverables: up to 5 short "
    "English phrases. extracted_skills: up to 8 professional skills the work clearly demonstrates; if a skill is the "
    "same as a Known name, use that Known name exactly. JSON only: "
    '{"project_title":"","client_feedback":"","rating":null,"deliverables":[""],"extracted_skills":[""]}'
)


def _rating(value):
    try:
        rating = Decimal(str(value)).quantize(Decimal("0.1"))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return rating if rating.is_finite() and Decimal("1") <= rating <= Decimal("5") else None


def parse_gig(platform: str, link: str, text: str) -> dict:
    """Structured gig data. AI when available; otherwise the local skill matcher (ai_parsed=False)."""
    skills = list(Skill.objects.filter(is_active=True))
    index = TaxonomyIndex(skills)
    if llm_enabled():
        try:
            data = chat_json(_PROMPT, f"Known: {', '.join(s.name for s in skills)}\nPlatform: {platform}\nLink: {link}\n"
                                      f"Text:\n{compact_cv_text(text)[:8000]}", max_tokens=700, timeout=45)
            names = data.get("extracted_skills") if isinstance(data.get("extracted_skills"), list) else []
            matched = []
            for name in names:
                skill = index.match(_clean_str(name, 100)) if isinstance(name, str) else None
                if skill and skill not in matched:
                    matched.append(skill)
            deliverables = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
            return {
                "project_title": _clean_str(data.get("project_title"), 160),
                "client_feedback": _clean_str(data.get("client_feedback"), 1000),
                "rating": _rating(data.get("rating")),
                "deliverables": [d for d in (_clean_str(x, 120) for x in deliverables) if d][:5],
                "skills": matched[:MAX_GIG_SKILLS], "ai_parsed": True,
            }
        except LLMUnavailable:
            logger.warning("Freelance gig parsing: AI unavailable, using the local matcher")
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    rating = re.search(r"\b([1-5](?:\.\d)?)\s*/\s*5\b", text)
    return {"project_title": _clean_str(first_line, 160), "client_feedback": "", "rating": _rating(rating.group(1)) if rating else None,
            "deliverables": [], "skills": [e.skill for e in extract_skills_local(text, skills)][:MAX_GIG_SKILLS], "ai_parsed": False}


def student_identifiers(student) -> list[str]:
    """Who the student is: full name, username, email prefixes, and LinkedIn / GitHub / portfolio handles."""
    user = student.user
    ids = [user.get_full_name(), user.username]
    ids += [e.split("@")[0] for e in (user.email, student.personal_email, student.university_email) if e]
    links = [student.linkedin_url, student.portfolio_url]
    cv = getattr(student, "cv", None)  # CV builder record, if any
    if cv and isinstance(cv.data, dict):
        links += [cv.data.get("linkedin"), cv.data.get("github")]
    ids += [h for h in (extract_platform_username(u) for u in links) if h]
    return [i for i in dict.fromkeys(i.strip() for i in ids if i and i.strip())]


def ownership_check(student, link: str) -> tuple[str, bool | None]:
    """(handle in the link, matched?). matched is None when the link has no readable username (e.g. a gig id)."""
    handle = extract_platform_username(link) or ""
    if not handle:
        return "", None
    return handle, best_owner_match(handle, student_identifiers(student)) >= OWNER_MATCH_THRESHOLD


@transaction.atomic
def import_gig(student, platform: str, link: str, text: str, proof=None) -> StudentFreelanceGig:
    parsed = parse_gig(platform, link, text)
    handle, matched = ownership_check(student, link)
    gig = StudentFreelanceGig.objects.create(
        student=student, platform=platform, link=link, raw_text=text, proof=proof,
        platform_username=handle[:100], owner_matched=bool(matched),
        # A handle that doesn't look like the student: the reviewer must see proof of account ownership.
        status=StudentFreelanceGig.STATUS_NEEDS_PROOF if matched is False else StudentFreelanceGig.STATUS_PENDING,
        **{k: parsed[k] for k in ("project_title", "client_feedback", "rating", "deliverables", "ai_parsed")})
    gig.skills.set(parsed["skills"])
    if settings.FREELANCE_AUTO_VERIFY and gig.owner_matched and gig.skills.exists():
        verify_gig(gig)
    return gig


@transaction.atomic
def verify_gig(gig, note: str = "") -> int:
    """Approve a gig: its skills become Verified (badge "Verified skill ✅"). Returns how many skills were upgraded."""
    now, upgraded = timezone.now(), 0
    for skill in gig.skills.all():
        ss, _created = StudentSkill.objects.get_or_create(
            student=gig.student, skill=skill,
            defaults={"source": StudentSkill.SOURCE_EXPLICIT, "evidence": f"Freelance: {gig.project_title or gig.link}"[:300]})
        if ss.status != StudentSkill.STATUS_VERIFIED:
            ss.status, ss.verified_via, ss.verified_at = StudentSkill.STATUS_VERIFIED, StudentSkill.VIA_FREELANCE, now
            ss.save(update_fields=["status", "verified_via", "verified_at", "updated_at"])
            upgraded += 1
    gig.status, gig.reviewed_at, gig.review_note = StudentFreelanceGig.STATUS_VERIFIED, now, note[:300]
    gig.save(update_fields=["status", "reviewed_at", "review_note"])
    return upgraded


def reject_gig(gig, note: str = ""):
    gig.status, gig.reviewed_at, gig.review_note = StudentFreelanceGig.STATUS_REJECTED, timezone.now(), note[:300]
    gig.save(update_fields=["status", "reviewed_at", "review_note"])
