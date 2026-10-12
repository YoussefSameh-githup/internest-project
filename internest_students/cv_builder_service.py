"""ATS resume builder: clean CV JSON, voice transcription (Whisper), AI polishing and profile sync.

Every AI call goes through the llm.py key-failover chain; when AI is down the student keeps the data
they typed and gets a clear message, never a 500.
"""
import datetime
import json
import logging
import re
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
    "education": (("university", "major", "start_date", "end_date", "is_current"), 4),
    "experiences": (("title", "organization", "achievements"), 8),
    "projects": (("title", "description", "tech_stack", "link"), 8),
}
EMPTY_CV = {**{f: "" for f in PERSONAL_FIELDS}, "summary": "", "education": [], "experiences": [], "projects": [], "skills": []}
TEXT_LIMITS = {"name": 80, "phone": 30, "summary": 600, "university": 120, "major": 120,
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


# Query parameters that only track clicks/shares (LinkedIn, Google, Meta, mail tools); never part of the address.
TRACKING_PARAMS = {"trackingid", "lipi", "trk", "trkinfo", "originalsubdomain", "midtoken", "midsig", "miniprofileurn",
                   "fbclid", "gclid", "dclid", "msclkid", "igshid", "si", "ref", "ref_src", "mc_cid", "mc_eid", "_hsenc", "_hsmi"}


class CleanURL(NamedTuple):
    href_url: str     # sanitized link for <a href>: https, no tracking parameters or fragment
    display_url: str  # what the CV shows: domain + path, e.g. linkedin.com/in/youssef-sameh


def clean_url(url) -> CleanURL:
    """Sanitize a pasted profile/project URL; ("", "") when it isn't a valid web address."""
    raw = _text(url, 300)
    if not raw:
        return CleanURL("", "")
    if not re.match(r"https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    parts = urlparse(raw)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if re.fullmatch(r"[a-z]{2}\.linkedin\.com", host):  # eg.linkedin.com → linkedin.com
        host = "linkedin.com"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # "example.com:abc"
        return CleanURL("", "")
    href = urlunparse(("https", host + port, path, "", urlencode(query), ""))
    try:
        URLValidator(schemes=["https"])(href)
    except ValidationError:
        return CleanURL("", "")
    display = host + port + path + (f"?{urlencode(query)}" if query else "")
    return CleanURL(href[:200], display[:120])


def _url(value):
    return clean_url(value).href_url


def contact_items(cv) -> list[dict]:
    """CV header items: {"text", "href"}; links show the clean display_url and point to the sanitized href_url."""
    items = [{"text": cv[k], "href": ""} for k in ("email", "phone") if cv.get(k)]
    if items and cv.get("email"):
        items[0]["href"] = f"mailto:{cv['email']}"
    for key in ("linkedin", "github"):
        link = clean_url(cv.get(key))
        if link.href_url:
            items.append({"text": link.display_url, "href": link.href_url})
    return items


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NAMES = {m.lower(): i for i, m in enumerate(MONTHS, 1)} | {
    "sept": 9, "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "أبريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
    "اغسطس": 8, "أغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}


def month_year(value) -> str:
    """"YYYY-MM" or "YYYY" from "2023-09", "09/2023", "Sep 2023", "سبتمبر 2023" or "2023"; "" if unreadable."""
    text = _text(value, 30).lower()
    year = re.search(r"\b(19[7-9]\d|20[0-4]\d)\b", text)
    if not year:
        return ""
    rest = (text[:year.start()] + " " + text[year.end():]).strip(" -/.,")
    month = None
    if re.fullmatch(r"0?[1-9]|1[0-2]", rest):
        month = int(rest)
    else:
        month = next((n for name, n in _MONTH_NAMES.items() if re.search(rf"(?<!\w){name}(?!\w)", rest)), None)
    return f"{year.group(1)}-{month:02d}" if month else year.group(1)


def _label(value) -> str:
    year, _sep, month = value.partition("-")
    return f"{MONTHS[int(month) - 1]} {year}" if month else year


def format_period(start: str, end: str, is_current: bool = False, today=None) -> str:
    """"Sep 2023 – Present", "2023 – 2027 (Expected)", "Mar 2026 – Aug 2026" or one side alone."""
    today = today or datetime.date.today()
    if is_current:
        end_text = "Present"
    elif end:
        year, _sep, month = end.partition("-")
        future = (int(year), int(month or 12)) > (today.year, today.month)
        end_text = _label(end) + (" (Expected)" if future else "")
    else:
        end_text = ""
    return " – ".join(v for v in (_label(start) if start else "", end_text) if v)


def _education_row(raw) -> dict:
    row = {"university": _text(raw.get("university"), 120), "major": _text(raw.get("major"), 120),
           "start_date": month_year(raw.get("start_date")),
           # Older saved CVs only had "graduation_year".
           "end_date": month_year(raw.get("end_date") or raw.get("graduation_year")),
           "is_current": raw.get("is_current") is True}
    if row["is_current"]:
        row["end_date"] = ""
    return row


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
            row = _education_row(raw) if section == "education" else {}
            for f in fields if section != "education" else ():
                if f == "achievements":
                    row[f] = _lines(raw.get(f), MAX_ACHIEVEMENTS)
                elif f == "link":
                    row[f] = _url(raw.get(f))
                else:
                    row[f] = _text(raw.get(f), TEXT_LIMITS[f])
            if any(v for k, v in row.items() if k != "is_current"):
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
    "education dates: start_date and end_date as YYYY-MM (or YYYY if no month was given), e.g. 'from September "
    "2023 until 2027' -> 2023-09 / 2027; is_current true only if they are still studying and give no end date. "
    "summary: 2-3 sentences, max 60 words. skills: max 20; if a skill is the same as a Known name, use that "
    "Known name exactly. JSON only, this schema: "
    '{"name":"","email":"","phone":"","linkedin":"","github":"","summary":"",'
    '"education":[{"university":"","major":"","start_date":"YYYY-MM or YYYY","end_date":"YYYY-MM or YYYY, expected graduation allowed","is_current":false}],'
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
