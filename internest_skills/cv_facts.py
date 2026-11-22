"""Deterministic CV parser (no AI, no network), Arabic + English:

    facts = parse(text, skills)      # JSON-ready dict stored in CVAnalysis.facts
    rows = describe(facts)           # the missing-information list + improvement tips in the current language
    safe = ai_safe_text(text)        # the CV without contact details: the only text cv_analysis sends to a model

`facts` keys: sections (names found), contacts, education, experience, projects, certifications, languages, skills
(taxonomy matches with provenance / confidence / evidence), unlisted_skills (skills-section items not in the taxonomy),
experience_months, word_count, quality_score (0-100) and missing (codes, see MISSING).

Contacts stay on our side: they are detected so the score and the student can see them, and `ai_safe_text` removes
them (plus every URL) before any AI request; the engine also redacts private tasks for external providers.
Scanned PDFs: there is deliberately no OCR dependency. A PDF without a text layer is refused by
extraction.read_document_text with a clear "no text found" message (export the CV as a text PDF / DOCX or paste it).
"""
import re
from datetime import date

from django.utils.translation import gettext_lazy as _

from .extraction import _normalize, extract_skills_local
from .models import StudentSkill
from .taxonomy import TaxonomyIndex

MAX_ITEMS = 12  # entries kept per list
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

SECTION_HEADINGS = {
    "summary": ("summary", "professional summary", "profile", "professional profile", "about", "about me", "objective",
                "career objective", "personal statement", "نبذه", "نبذه عني", "ملخص", "الملخص", "الملف الشخصي",
                "الهدف الوظيفي", "الهدف المهني", "عني"),
    "education": ("education", "academic background", "qualifications", "academic qualifications", "education and training",
                  "التعليم", "المؤهلات", "المؤهلات العلميه", "المؤهل الدراسي", "المؤهل العلمي", "التعليم الاكاديمي", "الدراسه"),
    "experience": ("experience", "work experience", "professional experience", "employment", "employment history",
                   "work history", "internships", "internship", "career history", "الخبرات", "الخبره", "الخبره العمليه",
                   "الخبرات العمليه", "الخبرات المهنيه", "الخبره المهنيه", "التدريب", "التدريبات", "الخبرات السابقه"),
    "projects": ("projects", "academic projects", "personal projects", "key projects", "portfolio", "المشاريع",
                 "المشروعات", "مشاريع"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "core competencies", "competencies", "tools",
               "technologies", "skills and tools", "المهارات", "المهارات التقنيه", "المهارات الفنيه", "مهارات", "الكفاءات"),
    "certifications": ("certifications", "certificates", "courses", "licenses", "licenses and certifications",
                       "licenses & certifications", "training courses", "awards", "الشهادات", "الدورات",
                       "الدورات التدريبيه", "الشهادات والدورات", "الجوائز"),
    "languages": ("languages", "language skills", "اللغات", "اللغه"),
    "activities": ("volunteering", "volunteer experience", "activities", "extracurricular activities", "leadership",
                   "التطوع", "الانشطه", "الانشطه الطلابيه", "العمل التطوعي"),
}
_HEADING_OF = {term: name for name, terms in SECTION_HEADINGS.items() for term in terms}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL = re.compile(r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)?(?:linkedin|github|behance|gitlab)\.com/\S*", re.I)
PHONE = re.compile(r"(?<![\w/])\+?\d[\d\s().-]{7,}\d(?![\w/])")

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6,
    "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6, "يونيه": 6, "يوليو": 7, "يوليه": 7,
    "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
_MONTH = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_PRESENT = r"present|current|now|today|ongoing|to date|till now|الان|حتي الان|حاليا|حتي تاريخه|مستمر"
_POINT = rf"(?:(?<!\w)(?:{_MONTH})\.?,?\s+(?:19|20)\d\d|(?:0?[1-9]|1[0-2])\s*[/.-]\s*(?:19|20)\d\d|(?:19|20)\d\d)"
DATE_RANGE = re.compile(rf"({_POINT})\s*(?:-|–|—|to|until|till|الي|حتي)\s*({_POINT}|{_PRESENT})", re.I)
YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")

_DEGREE = re.compile(
    r"\b(?:bachelor'?s?|b\.?\s?sc|bsc|b\.?\s?eng|b\.?a\b|master'?s?|m\.?\s?sc|msc|mba|ph\.?\s?d|doctorate|diploma|"
    r"associate degree|high school|secondary school|thanaweya)\b|بكالوريوس|ليسانس|ماجستير|دكتوراه|دبلوم|الثانويه|ثانويه عامه",
    re.I)
_INSTITUTION = re.compile(r"\b(?:university|college|institute|academy|school|faculty)\b|جامعه|كليه|معهد|اكاديميه|مدرسه", re.I)
_GPA = re.compile(r"(?:gpa|cgpa|grade|التقدير|المعدل)\s*[:：]?\s*(\d(?:\.\d+)?\s*/\s*\d(?:\.\d+)?|\d(?:\.\d+)?|excellent|"
                  r"very good|good|امتياز|جيد جدا|جيد)", re.I)
_QUANTIFIED = re.compile(r"\d+(?:\.\d+)?\s*%|\b\d+\+|(?<![\d/])(?!(?:19|20)\d\d\b)\d{2,}(?![\d/])")
_CERT_WORD = re.compile(r"\b(?:certified|certificate|certification)\b|شهاده|معتمد", re.I)
_SEPARATORS = re.compile(r"\s+(?:at|@|-|–|—|\||لدى|لدي|في)\s+|\s*[|،,]\s*")

LANGUAGES = {
    "english": "English", "انجليزي": "English", "الانجليزيه": "English", "الانجليزي": "English", "انجليزيه": "English",
    "arabic": "Arabic", "عربي": "Arabic", "العربيه": "Arabic", "عربيه": "Arabic",
    "french": "French", "فرنسي": "French", "الفرنسيه": "French", "german": "German", "الالمانيه": "German", "الماني": "German",
    "spanish": "Spanish", "الاسبانيه": "Spanish", "italian": "Italian", "الايطاليه": "Italian", "chinese": "Chinese",
    "الصينيه": "Chinese", "japanese": "Japanese", "اليابانيه": "Japanese", "turkish": "Turkish", "التركيه": "Turkish",
    "russian": "Russian", "الروسيه": "Russian", "korean": "Korean", "الكوريه": "Korean",
}
_LANGUAGE = re.compile(r"(?<!\w)(?:" + "|".join(sorted(map(re.escape, LANGUAGES), key=len, reverse=True)) + r")(?!\w)")
_LEVEL = re.compile(r"native|mother tongue|fluent|proficient|advanced|upper[- ]intermediate|intermediate|basic|beginner|"
                    r"elementary|\b[abc][12]\b|ielts\s*\d(?:\.\d)?|toefl\s*\d+|اللغه الام|لغه ام|بطلاقه|طلاقه|ممتاز|"
                    r"جيد جدا|جيد|متوسط|مبتدئ", re.I)

# Score: points per signal (sums to 100) and the code reported when it is missing.
MISSING = {
    "email": _("An email address"),
    "phone": _("A phone number"),
    "links": _("A LinkedIn, GitHub or portfolio link"),
    "summary": _("A short profile summary"),
    "education": _("Your education (degree and university)"),
    "education_dates": _("Dates for your education"),
    "experience": _("Experience, internships or volunteering"),
    "experience_dates": _("Start and end dates for each experience"),
    "projects": _("Projects"),
    "skills_section": _("A skills section"),
    "few_skills": _("At least 3 skills we can recognise"),
    "certifications": _("Courses or certificates"),
    "languages": _("Languages and your level"),
    "quantified": _("Numbers that show results"),
    "too_short": _("More detail (the CV is very short)"),
    "too_long": _("A tighter CV (it is very long)"),
}
SUGGESTIONS = {
    "email": _("Add a professional email address at the top."),
    "phone": _("Add a phone number employers can call."),
    "links": _("Add your LinkedIn profile, and GitHub or a portfolio if you have one."),
    "summary": _("Open with 2-3 lines on who you are, your field and the role you want."),
    "education": _("Add your degree, university and expected graduation year."),
    "education_dates": _("Add the start and graduation (or expected) year of your studies."),
    "experience": _("List internships, part-time work, student activities or volunteering with what you did."),
    "experience_dates": _("Give every experience a start and end month, e.g. Jun 2024 – Aug 2024."),
    "projects": _("Add 1-3 projects: the goal, the tools you used and the result."),
    "skills_section": _("Add a Skills section listing your tools and methods."),
    "few_skills": _("Name your skills with their standard names (e.g. Excel, SQL, Financial Analysis)."),
    "certifications": _("Add relevant courses or certificates with the issuer and year."),
    "languages": _("Add your languages with a level (e.g. English – fluent, IELTS 7)."),
    "quantified": _("Show results with numbers: %, time saved, users, budget, grades."),
    "too_short": _("Describe each experience and project in 2-4 bullet points."),
    "too_long": _("Keep it to 1-2 pages: cut old or unrelated details."),
}


def _key(text: str) -> str:
    key = _normalize(text).translate(_DIGITS)
    return re.sub(r"\s+", " ", key).strip(" .:：-–—|•*#")


def _heading(line: str):
    """(section name, rest of the line) when the line is a section heading, else None."""
    key = _key(line)
    if len(key.split()) <= 5 and key in _HEADING_OF:
        return _HEADING_OF[key], ""
    head, sep, rest = line.partition(":")
    if not sep:
        head, sep, rest = line.partition("：")
    if sep and _key(head) in _HEADING_OF and len(_key(head).split()) <= 4:
        return _HEADING_OF[_key(head)], rest.strip()
    first = re.split(r"\s*(?:&|/|\band\b|\s و)\s*", key)[0]
    if len(key.split()) <= 5 and first in _HEADING_OF:
        return _HEADING_OF[first], ""
    return None


def split_sections(lines: list[str]) -> dict:
    """{section: [lines]}; lines before the first heading are under "header"."""
    sections, current = {"header": []}, "header"
    for line in lines:
        found = _heading(line)
        if found:
            current, rest = found
            sections.setdefault(current, [])
            if rest:
                sections[current].append(rest)
            continue
        sections.setdefault(current, []).append(line)
    return sections


def contacts(text: str) -> dict:
    flat = text.translate(_DIGITS)
    phones = []
    for match in PHONE.finditer(flat):
        digits = re.sub(r"\D", "", match.group())
        if 9 <= len(digits) <= 15 and not DATE_RANGE.fullmatch(match.group().strip()):
            phones.append(match.group().strip())
    urls = [u.rstrip(".,;)") for u in URL.findall(flat)]
    linkedin = next((u for u in urls if "linkedin.com" in u.lower()), "")
    github = next((u for u in urls if "github.com" in u.lower()), "")
    others = [u for u in urls if u not in (linkedin, github)]
    return {"email": list(dict.fromkeys(EMAIL.findall(flat)))[:2], "phone": list(dict.fromkeys(phones))[:2],
            "linkedin": linkedin[:200], "github": github[:200], "websites": others[:3]}


def ai_safe_text(text: str) -> str:
    """The CV without emails, phone numbers and URLs (contacts are kept local)."""
    flat = text.translate(_DIGITS)
    flat = EMAIL.sub(" ", flat)
    flat = URL.sub(" ", flat)
    flat = PHONE.sub(lambda m: m.group() if len(re.sub(r"\D", "", m.group())) < 9 else " ", flat)
    return "\n".join(line for line in (re.sub(r"[ \t]{2,}", " ", raw).strip(" |·•-") for raw in flat.splitlines()) if line)


def _point(token: str):
    """(year, month or None) of one date token."""
    token = _key(token)
    year = int(YEAR.search(token).group())
    month = None
    for word, number in _MONTHS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", token):
            month = number
            break
    numeric = re.match(r"(\d{1,2})\s*[/.-]\s*(?:19|20)\d\d", token)
    if numeric:
        month = int(numeric.group(1))
    return year, month


def date_range(line: str, today=None):
    """{"start", "end", "months"} for the first date range in the line, else None."""
    match = DATE_RANGE.search(_key(line))
    if not match:
        return None
    today = today or date.today()
    start = _point(match.group(1))
    present = not YEAR.search(match.group(2))
    end = (today.year, today.month) if present else _point(match.group(2))
    months = (end[0] - start[0]) * 12 + ((end[1] or 12 if not present else end[1]) - (start[1] or 1)) + 1
    fmt = lambda p: f"{p[0]}-{p[1]:02d}" if p[1] else str(p[0])  # noqa: E731
    return {"start": fmt(start), "end": "present" if present else fmt(end), "months": max(0, min(months, 600))}


def _strip_range(line: str) -> str:
    key_line = line.translate(_DIGITS)
    cut = DATE_RANGE.sub("", _normalize(key_line))
    if cut == _normalize(key_line):
        return line.strip()
    # Map back to the original characters: _normalize keeps the length for the characters CVs use.
    match = DATE_RANGE.search(_normalize(key_line))
    text = (key_line[:match.start()] + key_line[match.end():]) if match and len(key_line) == len(_normalize(key_line)) else cut
    return re.sub(r"\s{2,}", " ", text).strip(" |,،-–—()")


def _title_org(text: str) -> dict:
    parts = [p.strip() for p in _SEPARATORS.split(text, maxsplit=1) if p and p.strip()]
    return {"title": parts[0][:120] if parts else "", "organization": parts[1][:120] if len(parts) > 1 else ""}


def experience_entries(lines: list[str], today=None) -> list[dict]:
    """Entries start at a line with a date range; the title is on that line or on the 1-2 lines just before it."""
    entries, buffer = [], []

    def close(bullets):
        if entries:
            entries[-1]["bullets"] += len(bullets)
            entries[-1]["quantified"] = entries[-1]["quantified"] or any(_QUANTIFIED.search(b.translate(_DIGITS)) for b in bullets)

    for line in lines:
        found = date_range(line, today)
        if not found:
            buffer.append(line)
            continue
        rest = _strip_range(line)
        if rest:
            title_lines, bullets = [rest], buffer
        else:
            take = [b for b in buffer[-2:] if len(b) <= 90]
            title_lines, bullets = take, buffer[:len(buffer) - len(take)]
        close(bullets)
        entries.append({**_title_org(" | ".join(title_lines)), **found, "bullets": 0, "quantified": False})
        buffer = []
    close(buffer)
    return entries[:MAX_ITEMS]


def education_entries(lines: list[str], today=None) -> list[dict]:
    entries = []
    for line in lines:
        key = _key(line)
        has_degree, has_school = bool(_DEGREE.search(key)), bool(_INSTITUTION.search(key))
        if not (has_degree or has_school or entries):
            continue
        if has_degree and (not entries or entries[-1]["degree"]) or (has_school and (not entries or entries[-1]["institution"])):
            entries.append({"degree": "", "institution": "", "start": "", "end": "", "gpa": ""})
        entry = entries[-1]
        if has_degree and not entry["degree"]:
            entry["degree"] = line[:160]
        if has_school and not entry["institution"]:
            entry["institution"] = line[:160]
        found = date_range(line, today)
        years = YEAR.findall(key)
        if found and not entry["start"]:
            entry["start"], entry["end"] = found["start"], found["end"]
        elif years and not entry["end"]:
            entry["end"] = years[-1]
        gpa = _GPA.search(key)
        if gpa and not entry["gpa"]:
            entry["gpa"] = gpa.group(1)[:20]
    return [e for e in entries if e["degree"] or e["institution"]][:MAX_ITEMS]


def languages(lines: list[str], in_section: bool) -> list[dict]:
    found = {}
    for line in lines:
        key = _key(line)
        level_match = _LEVEL.search(key)
        if not in_section and not (level_match or re.search(r"language|لغه|اللغات", key)):
            continue
        for match in _LANGUAGE.finditer(key):
            name = LANGUAGES[match.group()]
            level = _LEVEL.search(key[match.end():match.end() + 40]) or level_match
            found.setdefault(name, {"language": name, "level": level.group()[:30] if level else ""})
    return list(found.values())[:MAX_ITEMS]


def _skill_items(lines: list[str]) -> list[str]:
    items = []
    for line in lines:
        items += [p.strip(" .") for p in re.split(r"[,،;|/•·]|\s-\s", line) if p.strip(" .")]
    return [i for i in items if 2 <= len(i) <= 40]


def skills(text: str, skill_list, skills_section: list[str], local=None) -> tuple[list[dict], list[str]]:
    """Taxonomy matches (declared / evidenced) and the skills-section items the taxonomy does not know."""
    local = local if local is not None else extract_skills_local(text, skill_list)
    rows = {e.skill.pk: {"skill_id": e.skill.pk, "name": e.skill.name, "provenance": e.provenance,
                         "confidence": e.confidence, "evidence": e.evidence} for e in local}
    index, unlisted = TaxonomyIndex(skill_list), []
    for item in _skill_items(skills_section):
        skill = index.match(item)
        if skill is None:
            unlisted.append(item[:40])
        elif skill.pk not in rows or rows[skill.pk]["provenance"] != StudentSkill.PROVENANCE_DECLARED:
            rows[skill.pk] = {"skill_id": skill.pk, "name": skill.name, "provenance": StudentSkill.PROVENANCE_DECLARED,
                              "confidence": 90, "evidence": item[:280]}
    return list(rows.values()), list(dict.fromkeys(unlisted))[:15]


def quality(facts: dict) -> tuple[int, list[str]]:
    """Deterministic CV quality score (0-100) and the missing-information codes, most important first."""
    sections, c = set(facts["sections"]), facts["contacts"]
    score, missing = 0, []

    def award(points, ok, code):
        nonlocal score
        if ok:
            score += points
        else:
            missing.append(code)

    experience, education = facts["experience"], facts["education"]
    if experience:
        score += 20
    elif sections & {"experience", "activities"}:
        score += 10
        missing.append("experience_dates")
    else:
        missing.append("experience")
    if education:
        score += 15
        if not any(e["end"] for e in education):
            missing.append("education_dates")
    elif "education" in sections:
        score += 8
        missing.append("education_dates")
    else:
        missing.append("education")
    found = len(facts["skills"])
    score += 10 if found >= 3 else 5 if found else 0
    if found < 3:
        missing.append("few_skills")
    award(5, "skills" in sections, "skills_section")
    award(8, bool(c["email"]), "email")
    award(4, bool(c["phone"]), "phone")
    award(3, bool(c["linkedin"] or c["github"] or c["websites"]), "links")
    award(10, "summary" in sections, "summary")
    award(10, bool(facts["projects"]), "projects")
    award(5, any(e["quantified"] for e in experience), "quantified")
    award(5, bool(facts["certifications"]), "certifications")
    award(5, bool(facts["languages"]), "languages")
    words = facts["word_count"]
    if words < 120:
        missing.append("too_short")
        score -= 10
    elif words > 1500:
        missing.append("too_long")
        score -= 5
    return max(0, min(score, 100)), missing


def parse(text: str, skill_list, today=None, local=None) -> dict:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    sections = split_sections(lines)
    in_lang_section = "languages" in sections
    experience = experience_entries(sections.get("experience", []) + sections.get("activities", []), today)
    edu_lines = sections.get("education") or [line for line in lines if _DEGREE.search(_key(line))]
    certs = list(dict.fromkeys(sections.get("certifications", []) + [
        line for name, rows in sections.items() if name != "certifications" for line in rows if _CERT_WORD.search(_key(line))]))
    found_skills, unlisted = skills(text, skill_list, sections.get("skills", []), local)
    facts = {
        "sections": sorted(name for name, rows in sections.items() if name != "header"),
        "contacts": contacts(text),
        "education": education_entries(edu_lines, today),
        "experience": experience,
        "experience_months": sum(e["months"] for e in experience),
        "projects": [line[:160] for line in sections.get("projects", [])][:MAX_ITEMS],
        "certifications": [line[:160] for line in certs][:MAX_ITEMS],
        "languages": languages(sections.get("languages") if in_lang_section else lines, in_lang_section),
        "skills": found_skills,
        "unlisted_skills": unlisted,
        "word_count": len(re.findall(r"\w+", text or "")),
    }
    facts["quality_score"], facts["missing"] = quality(facts)
    return facts


def describe(facts: dict) -> list[dict]:
    """[{code, label, suggestion}] for the missing information, in the active language."""
    return [{"code": code, "label": str(MISSING[code]), "suggestion": str(SUGGESTIONS[code])}
            for code in facts.get("missing", []) if code in MISSING]
