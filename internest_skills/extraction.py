"""Universal (all-faculty) skill extractor: document text → explicit + implicit skills."""
import io
import re
from dataclasses import dataclass

from django.db import transaction

from .models import Skill, StudentSkill

MAX_TEXT_CHARS = 200_000


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


def _term_pattern(term: str) -> re.Pattern:
    words = _normalize(term).split()
    body = r"\s+".join(re.escape(w) for w in words)
    # Arabic attaches conjunctions/prepositions to the next word (e.g. "وصياغة").
    prefix = _ARABIC_PROCLITICS if words and "؀" <= words[0][0] <= "ۿ" else ""
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
