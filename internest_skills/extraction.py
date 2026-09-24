"""Universal (all-faculty) skill extractor: document text → explicit + implicit skills.

Primary: LLM (OpenAI-compatible, AgentRouter). Fallback: local regex matcher.
"""
import io
import logging
import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from .llm import chat_json, llm_enabled
from .models import Skill, StudentSkill

MAX_TEXT_CHARS = 200_000
LLM_INPUT_CHARS = 14_000
LLM_MAX_SKILLS = 20

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


def read_document_text(uploaded_file) -> str:
    name = (uploaded_file.name or "").lower()
    data = uploaded_file.read()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
        elif name.endswith(".docx"):
            import docx
            document = docx.Document(io.BytesIO(data))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            text = "\n".join(parts)
        else:
            raise ExtractionError("Unsupported file type. Upload a PDF or DOCX.")
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("Could not read this file. Make sure it is a valid, non-encrypted PDF or DOCX.") from exc
    text = text[:MAX_TEXT_CHARS]
    if len(text.strip()) < 40:
        raise ExtractionError("No readable text found (scanned image PDFs are not supported).")
    return text


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[إأآ]", "ا", text)
    return text.replace("ى", "ي").replace("ة", "ه")


_ARABIC_PROCLITICS = r"(?:وال|بال|فال|كال|لل|ال|و|ف|ب|ل|ك)?"
_ARABIC_BLOCK = re.compile("[\\u0600-\\u06FF]")


def _term_pattern(term: str) -> re.Pattern:
    words = _normalize(term).split()
    body = r"\s+".join(re.escape(w) for w in words)
    # Arabic attaches conjunctions/prepositions to the next word (e.g. "وصياغة").
    prefix = _ARABIC_PROCLITICS if words and _ARABIC_BLOCK.match(words[0][0]) else ""
    # Lookarounds instead of \b so terms like "c++", "c#", ".net" still match.
    return re.compile(rf"(?<!\w){prefix}{body}(?!\w)")


def _evidence(text: str, start: int, end: int) -> str:
    left = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start)) + 1
    right_candidates = [i for i in (text.find("\n", end), text.find(". ", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    snippet = text[left:right].strip()
    return snippet[:280]


@dataclass
class ExtractedSkill:
    skill: Skill
    source: str
    evidence: str


def extract_skills(text: str, skills=None) -> list[ExtractedSkill]:
    """LLM extraction when configured; any failure falls back to the local matcher."""
    skills = list(skills if skills is not None else Skill.objects.filter(is_active=True))
    if llm_enabled():
        try:
            return extract_skills_llm(text, skills)
        except Exception:
            logger.warning("LLM skill extraction failed; using local matcher", exc_info=True)
    return extract_skills_local(text, skills)


_SYSTEM_PROMPT = (
    "Read the WHOLE CV (summary, experience, projects, education, courses, activities; English/Arabic, any discipline) "
    "and list ALL professional skills, max 20, most relevant first; include implicit ones shown by projects/work. "
    "Standardize every name to the global ESCO / O*NET skill taxonomy (preferred label, 1-4 words, English); "
    "if a Known name is the same skill, return that Known name exactly. "
    "t: e=explicitly stated, i=implied by projects/work. "
    "d: c=computing b=business m=media l=law e=engineering h=health g=general. "
    'JSON only: {"skills":[{"n":"","t":"e","d":"c"}]}'
)
_DISCIPLINE_CODES = {"c": "computing", "b": "business", "m": "media", "l": "law", "e": "engineering", "h": "health", "g": "general"}


def _get_or_create_skill(name: str, discipline_code: str, by_name: dict):
    key = name.lower()
    if key in by_name:
        return by_name[key]
    if not (2 <= len(name) <= 60) or len(name.split()) > 5:
        return None
    skill = Skill.objects.filter(name__iexact=name).first()
    if skill is None:
        base = slugify(name, allow_unicode=True)[:90] or "skill"
        slug, n = base, 2
        while Skill.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        try:
            skill = Skill.objects.create(
                name=name, slug=slug,
                discipline=_DISCIPLINE_CODES.get(discipline_code, "general"),
                aliases=name,
            )
        except IntegrityError:
            skill = Skill.objects.filter(name__iexact=name).first()
    if skill is not None:
        by_name[key] = skill
    return skill if skill is not None and skill.is_active else None


_NOISE_LINE = re.compile(r"^(?:[\w.+-]+@[\w-]+\.[\w.]+|\+?[\d\s()./-]{7,}|(?:https?://|www\.)\S+|page \d+(?: of \d+)?)$", re.I)


def compact_cv_text(text: str) -> str:
    """Keep every section but drop tokens that carry no skill signal (contacts, URLs, page footers, repeats)."""
    seen, lines = set(), []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t•·-–—|")
        key = line.lower()
        if len(line) < 2 or key in seen or _NOISE_LINE.match(line):
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


def extract_skills_llm(text: str, skills) -> list[ExtractedSkill]:
    cv = compact_cv_text(text)[:LLM_INPUT_CHARS]
    known = ", ".join(s.name for s in skills)
    data = chat_json(_SYSTEM_PROMPT, f"Known: {known}\nCV:\n{cv}", max_tokens=300)
    items = data.get("skills")
    if not isinstance(items, list):
        raise ValueError("LLM response missing 'skills' list")

    by_name = {s.name.lower(): s for s in skills}
    local = {e.skill.pk: e for e in extract_skills_local(text, skills)}
    results, seen = [], set()
    for item in items[:LLM_MAX_SKILLS]:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("n", ""))).strip()
        skill = _get_or_create_skill(name, str(item.get("d", "g")), by_name) if name else None
        if skill is None or skill.pk in seen:
            continue
        seen.add(skill.pk)
        source = StudentSkill.SOURCE_EXPLICIT if item.get("t") == "e" else StudentSkill.SOURCE_IMPLICIT
        evidence = local[skill.pk].evidence if skill.pk in local else ""
        results.append(ExtractedSkill(skill, source, evidence))
    return results


def extract_skills_local(text: str, skills=None) -> list[ExtractedSkill]:
    skills = list(skills if skills is not None else Skill.objects.filter(is_active=True))
    norm = _normalize(text)
    source_text = text if len(text) == len(norm) else norm
    results = []
    for skill in skills:
        found = None
        for term in skill.alias_list:
            m = _term_pattern(term).search(norm)
            if m:
                found = ExtractedSkill(skill, StudentSkill.SOURCE_EXPLICIT, _evidence(source_text, m.start(), m.end()))
                break
        if found is None:
            for phrase in skill.implicit_signal_list:
                m = _term_pattern(phrase).search(norm)
                if m:
                    found = ExtractedSkill(skill, StudentSkill.SOURCE_IMPLICIT, _evidence(source_text, m.start(), m.end()))
                    break
        if found:
            results.append(found)
    return results


@transaction.atomic
def save_claimed_skills(student_profile, extracted: list[ExtractedSkill]) -> tuple[int, int]:
    """Upsert as Claimed. Never downgrades a Verified/Lag skill. Returns (created, updated)."""
    created = updated = 0
    for item in extracted:
        obj, was_created = StudentSkill.objects.get_or_create(
            student=student_profile, skill=item.skill,
            defaults={"source": item.source, "evidence": item.evidence},
        )
        if was_created:
            created += 1
            continue
        if obj.source == StudentSkill.SOURCE_IMPLICIT and item.source == StudentSkill.SOURCE_EXPLICIT:
            obj.source = item.source
            obj.evidence = item.evidence
            obj.save(update_fields=["source", "evidence", "updated_at"])
            updated += 1
    return created, updated
