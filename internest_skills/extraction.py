"""Document text and the local skill matcher: upload → clean text → explicit (declared) + implicit (evidenced) skills.

Also the grounding helpers every AI skill reader uses: `relevant_known` (top-N taxonomy names for a prompt),
`locate_evidence` (an AI quote must be found in the text) and `record_suggestion` (unknown AI names become
SkillSuggestion rows for staff, never Skill rows: audit M1). The AI reading itself lives in cv_analysis (internest_ai).
"""
import io
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from .models import Discipline, Skill, StudentSkill

MAX_TEXT_CHARS = 200_000

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


_CID = re.compile(r"\(cid:\d+\)")  # unmapped glyphs from PDF fonts
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff\ufffd]")  # escapes, not raw bidi characters (Trojan Source)
_BULLETS = re.compile(r"^[\s•·▪●○■□◆◇►▶✓✔➢➤*-]+")


def sanitize_text(text: str) -> str:
    """Clean raw PDF/DOCX text: Unicode forms, broken glyphs, hyphenated line breaks, bullets, junk lines."""
    text = unicodedata.normalize("NFKC", text or "")  # ligatures (ﬁ) and Arabic presentation forms
    text = _CONTROL.sub("", _CID.sub(" ", text)).replace("\r", "\n").replace("\t", " ")
    text = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)  # "manage-\nment" → "management"
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r" {2,}", " ", _BULLETS.sub("", raw)).strip()
        if line and sum(ch.isalnum() for ch in line) >= len(line) * 0.2:  # drop lines of mostly symbols
            lines.append(line)
    return "\n".join(lines)


# DOCX files are zip archives: python-docx inflates the parts it opens, so a tiny "zip bomb" could expand to gigabytes
# in memory. The central directory (read without inflating anything) must look like a real Word file first.
DOCX_MAX_UNCOMPRESSED = 20 * 1024 * 1024
DOCX_MAX_RATIO = 100         # whole archive (Word files are typically 3-10x)
DOCX_MAX_ENTRY_RATIO = 1000  # one part
DOCX_MAX_ENTRIES = 2000
_UNREADABLE = _lazy("Could not read this file. Make sure it is a valid, non-encrypted PDF or DOCX.")
NO_TEXT_PDF = _lazy("This PDF has no selectable text: it looks like a scanned image. We don't read images (no OCR): "
                    "export your CV from Word or Google Docs as PDF or DOCX, or paste its text instead.")
NO_TEXT = _lazy("No readable text found in this file. Upload a PDF or DOCX made from a text document, or paste the text.")


def check_docx_archive(data: bytes) -> None:
    """Raise ExtractionError for an archive that would inflate past 20 MB or with an absurd compression ratio.
    zipfile never inflates an entry past its declared size, so checking the declared sizes bounds the real work."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ExtractionError(str(_UNREADABLE)) from exc
    total = sum(e.file_size for e in entries)
    packed = sum(e.compress_size for e in entries) or 1
    bomb = (len(entries) > DOCX_MAX_ENTRIES or total > DOCX_MAX_UNCOMPRESSED
            or (total > 1024 * 1024 and total / packed > DOCX_MAX_RATIO)
            or any(e.file_size > 1024 * 1024 and e.file_size / max(e.compress_size, 1) > DOCX_MAX_ENTRY_RATIO for e in entries))
    if bomb:
        logger.warning("Refused a DOCX upload: %s entries, %s bytes unpacked from %s", len(entries), total, packed)
        raise ExtractionError(_("This DOCX file is unusually large once unpacked. Save it again from Word or upload a PDF."))


def read_document_text(uploaded_file) -> str:
    name = (uploaded_file.name or "").lower()
    data = uploaded_file.read()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
        elif name.endswith(".docx"):
            check_docx_archive(data)
            import docx
            document = docx.Document(io.BytesIO(data))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            text = "\n".join(parts)
        else:
            raise ExtractionError(_("Unsupported file type. Upload a PDF or DOCX."))
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(str(_UNREADABLE)) from exc
    text = sanitize_text(text)[:MAX_TEXT_CHARS]
    if len(text.strip()) < 40:
        raise ExtractionError(str(NO_TEXT_PDF if name.endswith(".pdf") else NO_TEXT))
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
    provenance: str = ""          # StudentSkill.PROVENANCE_*; derived from `source` when left empty
    confidence: int | None = None  # 0-100

    def __post_init__(self):
        if not self.provenance:
            self.provenance = (StudentSkill.PROVENANCE_EVIDENCED if self.source == StudentSkill.SOURCE_IMPLICIT
                               else StudentSkill.PROVENANCE_DECLARED)


def extract_skills(text: str, skills=None) -> list[ExtractedSkill]:
    """Skills a student can save straight away (declared / evidenced; AI-inferred ones need a confirmation)."""
    from .cv_analysis import analyze_cv

    return analyze_cv(text, skills).skills


def _clean_str(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _years(value):
    try:
        years = Decimal(str(value)).quantize(Decimal("0.1"))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return years if years.is_finite() and Decimal("0") < years <= Decimal("60") else None


_NOISE_LINE = re.compile(r"^(?:[\w.+-]+@[\w-]+\.[\w.]+|\+?[\d\s()./-]{7,}|(?:https?://|www\.)\S+|page \d+(?: of \d+)?)$", re.I)


def compact_cv_text(text: str) -> str:
    """Keep every section but drop tokens that carry no skill signal (contacts, URLs, page footers, repeats)."""
    seen, lines = set(), []
    for raw in sanitize_text(text).splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t•·-–—|")
        key = line.lower()
        if len(line) < 2 or key in seen or _NOISE_LINE.match(line):
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


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
                found = ExtractedSkill(skill, StudentSkill.SOURCE_EXPLICIT, _evidence(source_text, m.start(), m.end()),
                                       StudentSkill.PROVENANCE_DECLARED, 95)
                break
        if found is None:
            for phrase in skill.implicit_signal_list:
                m = _term_pattern(phrase).search(norm)
                if m:
                    found = ExtractedSkill(skill, StudentSkill.SOURCE_IMPLICIT, _evidence(source_text, m.start(), m.end()),
                                           StudentSkill.PROVENANCE_EVIDENCED, 70)
                    break
        if found:
            results.append(found)
    return results


# ---------------------------------------------------------------- grounding of AI output (every AI skill reader)
KNOWN_LIMIT = 60  # taxonomy names sent to a model: the most relevant ones for this text, never the whole table


def relevant_known(text: str, skills, found_ids=(), limit: int = KNOWN_LIMIT) -> list:
    """Top-N taxonomy skills for this text: local matches first, then skills whose name words appear in it, then
    skills of the same fields as the matches; ties by name. Keeps prompts small whatever the taxonomy size."""
    words = set(re.findall(r"[\w+#.]{2,}", _normalize(text or "")))
    found_ids = set(found_ids)
    fields = {s.discipline for s in skills if s.pk in found_ids}

    def rank(skill):
        if skill.pk in found_ids:
            return 0
        if any(w in words for w in _normalize(skill.name).split() if len(w) >= 3):
            return 1
        return 2 if skill.discipline in fields else 3

    return sorted(skills, key=lambda s: (rank(s), s.name.lower()))[:limit]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", _normalize(text or "")).strip(" .,;:\"'“”«»()")


def locate_evidence(quote: str, text: str) -> str:
    """The CV line(s) holding `quote` (an AI's verbatim evidence), or "" when the quote is not in the text.
    "…" / "..." splits a quote into parts that must all be found."""
    parts = [p for p in (_flat(x) for x in re.split(r"\.\.\.|…", quote or "")) if len(p) >= 4]
    if not parts:
        return ""
    lines = (text or "").splitlines()
    flat_lines = [_flat(line) for line in lines]
    whole = _flat(text)
    found = []
    for part in parts:
        index = next((i for i, line in enumerate(flat_lines) if part in line), None)
        if index is None and part not in whole:
            return ""
        if index is not None:
            found.append(lines[index].strip())
    snippet = " … ".join(dict.fromkeys(found)) if found else quote
    return _clean_str(snippet, 280)


def record_suggestion(name: str, discipline: str = "", example: str = ""):
    """An unknown skill name from an AI reply becomes a SkillSuggestion for staff (never an active Skill)."""
    from django.db.models import F

    from .models import SkillSuggestion
    from .taxonomy import TaxonomyIndex, normalize_key

    name = TaxonomyIndex(()).canonical_name(_clean_str(name, 100))  # "Rust developer skills" → "Rust"
    key = normalize_key(name)[:100]
    if not (2 <= len(name) <= 60) or len(name.split()) > 5 or not key:
        return None
    defaults = {"name": name, "discipline": discipline if discipline in Discipline.values else Discipline.GENERAL,
                "example": (example or "")[:300]}
    try:
        with transaction.atomic():
            suggestion, created = SkillSuggestion.objects.get_or_create(key=key, defaults=defaults)
    except IntegrityError:  # created by a concurrent request a moment ago
        suggestion, created = SkillSuggestion.objects.get(key=key), False
    if not created:
        SkillSuggestion.objects.filter(pk=suggestion.pk).update(seen_count=F("seen_count") + 1)
    return suggestion


# ---------------------------------------------------------------- saving
_PROVENANCE_RANK = {StudentSkill.PROVENANCE_INFERRED: 0, StudentSkill.PROVENANCE_EVIDENCED: 1, StudentSkill.PROVENANCE_DECLARED: 2}


@transaction.atomic
def save_claimed_skills(student_profile, extracted: list[ExtractedSkill], years_by_skill=None) -> tuple[int, int]:
    """Upsert as Claimed. Never touches the status of an existing row (a Verified/Lag skill is never downgraded); a
    stronger provenance (inferred < evidenced < declared) replaces a weaker one with its evidence. Returns (created, updated)."""
    created = updated = 0
    years_by_skill = years_by_skill or {}
    for item in extracted:
        years = years_by_skill.get(item.skill.pk)
        obj, was_created = StudentSkill.objects.get_or_create(
            student=student_profile, skill=item.skill,
            defaults={"source": item.source, "evidence": item.evidence[:300], "years_experience": years,
                      "provenance": item.provenance, "confidence": item.confidence},
        )
        if was_created:
            created += 1
            continue
        fields = []
        if years is not None and obj.years_experience != years:
            obj.years_experience = years
            fields.append("years_experience")
        if _PROVENANCE_RANK.get(item.provenance, 0) > _PROVENANCE_RANK.get(obj.provenance, 0):
            obj.provenance, obj.confidence, obj.source = item.provenance, item.confidence, item.source
            obj.evidence = item.evidence[:300] or obj.evidence
            fields += ["provenance", "confidence", "source", "evidence"]
            updated += 1
        elif obj.source == StudentSkill.SOURCE_IMPLICIT and item.source == StudentSkill.SOURCE_EXPLICIT:
            obj.source, obj.evidence = item.source, item.evidence[:300]
            fields += ["source", "evidence"]
            updated += 1
        if fields:
            obj.save(update_fields=fields + ["updated_at"])
    return created, updated
