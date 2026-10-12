"""ATS resume builder: clean CV JSON, voice transcription (Whisper), AI polishing and profile sync.

Every AI call goes through the llm.py key-failover chain; when AI is down the student keeps the data
they typed and gets a clear message, never a 500.
"""
import json
import logging
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction

from internest_skills import llm
from internest_skills.extraction import ExtractedSkill, save_claimed_skills
from internest_skills.models import Skill, StudentSkill
from internest_skills.taxonomy import TaxonomyIndex

from .models import StudentProject

logger = logging.getLogger(__name__)

PERSONAL_FIELDS = ("name", "email", "phone", "linkedin", "github")
SECTIONS = {  # section → (fields, max items)
    "education": (("university", "major", "graduation_year"), 4),
    "experiences": (("title", "organization", "achievements"), 8),
    "projects": (("title", "description", "tech_stack", "link"), 8),
}
EMPTY_CV = {**{f: "" for f in PERSONAL_FIELDS}, "summary": "", "education": [], "experiences": [], "projects": [], "skills": []}
TEXT_LIMITS = {"name": 80, "phone": 30, "summary": 600, "university": 120, "major": 120, "graduation_year": 20,
               "title": 120, "organization": 120, "description": 600, "tech_stack": 200}
MAX_ACHIEVEMENTS, MAX_SKILLS = 6, 30
PLACEHOLDER = re.compile(r"\[(?:X|N|\d*X)[^\]]{0,12}\]")  # "[X%]", "[N] users" — AI asks the student for a real number
WHISPER_PROMPT = "مزيج من العربية المصرية والإنجليزية. Internship, university, project, Python, Excel, marketing."
AUDIO_MAX_BYTES = 15 * 1024 * 1024


class VoiceUnavailable(Exception):
    """No transcription key configured, or every key failed."""


# ---------------------------------------------------------------- cleaning
def _text(value, limit):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


def _url(value):
    url = _text(value, 200)
    if url and not re.match(r"https?://", url, re.I):
        url = "https://" + url
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        return ""
    return url


def _email(value):
    email = _text(value, 120)
    try:
        validate_email(email)
    except ValidationError:
        return ""
    return email


def _lines(value, limit, per_line=220):
    items = value.splitlines() if isinstance(value, str) else value if isinstance(value, list) else []
    lines = [_text(re.sub(r"^\s*[-•*▪]\s*", "", item) if isinstance(item, str) else item, per_line) for item in items]
    return [line for line in lines if line][:limit]


def clean_cv(data) -> dict:
    """Coerce any input (form, LLM reply) into the CV schema with safe types and lengths."""
    data = data if isinstance(data, dict) else {}
    cv = {"name": _text(data.get("name"), 80), "email": _email(data.get("email")), "phone": _text(data.get("phone"), 30),
          "linkedin": _url(data.get("linkedin")), "github": _url(data.get("github")),
          "summary": _text(data.get("summary"), TEXT_LIMITS["summary"])}
    for section, (fields, max_items) in SECTIONS.items():
        rows = []
        for raw in data.get(section) or [] if isinstance(data.get(section), list) else []:
            if not isinstance(raw, dict):
                continue
            row = {}
            for f in fields:
                if f == "achievements":
                    row[f] = _lines(raw.get(f), MAX_ACHIEVEMENTS)
                elif f == "link":
                    row[f] = _url(raw.get(f))
                else:
                    row[f] = _text(raw.get(f), TEXT_LIMITS[f])
            if any(row.values()):
                rows.append(row)
        cv[section] = rows[:max_items]
    seen, skills = set(), []
    for name in _lines(data.get("skills"), MAX_SKILLS * 2, per_line=60):
        if name.casefold() not in seen:
            seen.add(name.casefold())
            skills.append(name)
    cv["skills"] = skills[:MAX_SKILLS]
    return cv


def placeholders(cv) -> list[str]:
    """AI placeholders ("[X%]") the student still has to replace with real figures."""
    return sorted(set(PLACEHOLDER.findall(json.dumps(cv, ensure_ascii=False))))


def initial_cv(student) -> dict:
    """Starting point for a student who has not saved a CV yet: whatever the profile already knows."""
    user = student.user
    skills = list(student.skills.select_related("skill").order_by("-status", "skill__name").values_list("skill__name", flat=True))
    return clean_cv({
        "name": user.get_full_name() or user.username, "email": student.personal_email or user.email,
        "phone": student.phone_number, "linkedin": student.linkedin_url, "summary": student.bio,
        "education": [{"university": student.university, "major": student.major}] if student.university else [],
        "projects": list(student.projects.values("title", "description", "tech_stack", "link")),
        "skills": skills,
    })


# ---------------------------------------------------------------- taxonomy
def standardize_skills(names, skills=None):
    """(CV skill names, matched Skill objects): taxonomy names where they exist, the student's text otherwise."""
    index = TaxonomyIndex(list(skills if skills is not None else Skill.objects.filter(is_active=True)))
    out, matched, seen = [], [], set()
    for name in names:
        skill = index.match(name)
        label = skill.name if skill else name
        if label.casefold() in seen:
            continue
        seen.add(label.casefold())
        out.append(label)
        if skill:
            matched.append(skill)
    return out, matched


# ---------------------------------------------------------------- AI
_POLISH_PROMPT = (
    "You are an expert ATS resume writer for students and fresh graduates. You get CURRENT resume JSON (may be "
    "partly empty) and optionally a VOICE transcript (Egyptian Arabic / English code-switching). Merge them: the "
    "transcript adds or corrects details; keep CURRENT data unless the transcript corrects it. "
    "Write everything in clear professional English (translate Arabic; transliterate names of people, universities "
    "and companies). Never invent employers, degrees, dates, contacts, links or numbers. "
    "achievements: 2-4 bullets per experience, each starting with a strong past-tense action verb (Led, Developed, "
    "Optimized, Designed, Analyzed, Organized), max 25 words, quantified when the input gives figures; where a "
    "number would help but none was given, insert a placeholder like [X%] or [N] for the student to fill. "
    "summary: 2-3 sentences, max 60 words. skills: max 20; if a skill is the same as a Known name, use that "
    "Known name exactly. JSON only, this schema: "
    '{"name":"","email":"","phone":"","linkedin":"","github":"","summary":"",'
    '"education":[{"university":"","major":"","graduation_year":""}],'
    '"experiences":[{"title":"","organization":"","achievements":[""]}],'
    '"projects":[{"title":"","description":"","tech_stack":"","link":""}],"skills":[""]}'
)


def polish_cv(current: dict, transcript: str = "") -> dict:
    """AI-structured and polished CV. Raises llm.LLMUnavailable when every key fails."""
    current = clean_cv(current)
    known = ", ".join(Skill.objects.filter(is_active=True).values_list("name", flat=True)[:400])
    payload = f"Known: {known}\nCURRENT: {json.dumps(current, ensure_ascii=False)}\nVOICE: {transcript or '(none)'}"
    polished = clean_cv(llm.chat_json(_POLISH_PROMPT, payload, max_tokens=2500, timeout=45))
    for field in PERSONAL_FIELDS:  # contacts typed by the student always win
        polished[field] = current[field] or polished[field]
    polished["skills"], _matched = standardize_skills(polished["skills"])
    return polished


def transcription_providers() -> list[llm.Provider]:
    """Whisper endpoints in failover order: WHISPER_API_KEY, then any configured Groq / OpenAI chat key."""
    chain = []
    if settings.WHISPER_API_KEY:
        chain.append(llm.Provider("WHISPER_API_KEY", settings.WHISPER_API_KEY, settings.WHISPER_BASE_URL, settings.WHISPER_MODEL))
    for p in llm.providers():
        if "groq.com" in p.base_url:
            chain.append(llm.Provider(p.label, p.api_key, p.base_url, "whisper-large-v3"))
        elif "api.openai.com" in p.base_url:
            chain.append(llm.Provider(p.label, p.api_key, p.base_url, "whisper-1"))
    seen, unique = set(), []
    for p in chain:
        if (p.api_key, p.base_url) not in seen:
            seen.add((p.api_key, p.base_url))
            unique.append(p)
    return unique


def voice_enabled() -> bool:
    return bool(transcription_providers())


def transcribe(audio: bytes, filename: str, content_type: str) -> str:
    """Speech → text (Arabic/English mixed). Raises VoiceUnavailable when no key works."""
    chain = transcription_providers()
    if not chain:
        raise VoiceUnavailable("No transcription key configured")
    for i, provider in enumerate(chain):
        try:
            resp = llm._client(60, provider, 0 if i + 1 < len(chain) else 1).audio.transcriptions.create(
                model=provider.model, file=(filename, audio, content_type), prompt=WHISPER_PROMPT, temperature=0,
            )
            return (getattr(resp, "text", "") or "").strip()
        except llm.LLM_ERRORS as exc:
            logger.warning("Transcription via %s failed (%s: %s)%s", provider.label, type(exc).__name__, exc,
                           "; failing over" if i + 1 < len(chain) else "")
    raise VoiceUnavailable("All transcription keys failed")


# ---------------------------------------------------------------- profile sync
@transaction.atomic
def sync_profile(student, cv) -> dict:
    """Taxonomy skills → Claimed StudentSkills; projects → the profile's portfolio (builder rows replaced)."""
    cv["skills"], matched = standardize_skills(cv["skills"])
    created, _updated = save_claimed_skills(
        student, [ExtractedSkill(skill, StudentSkill.SOURCE_EXPLICIT, "Listed in the Internest CV builder") for skill in matched])
    StudentProject.objects.filter(student=student, source=StudentProject.SOURCE_CV_BUILDER).delete()
    StudentProject.objects.bulk_create([
        StudentProject(student=student, position=i, title=p["title"][:120] or "Project", description=p["description"],
                       tech_stack=p["tech_stack"], link=p["link"])
        for i, p in enumerate(cv["projects"])
    ])
    return {"skills": len(matched), "new_skills": created, "projects": len(cv["projects"])}
