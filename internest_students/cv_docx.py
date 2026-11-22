"""ATS-friendly Word (.docx) export: one column, built-in heading / list styles, real text, no tables, text boxes,
headers / footers or images (parsers skip those). Mirrors cv_pdf.py; Arabic paragraphs are marked right-to-left."""
import io

from internest_core.letters import ARABIC

from .cv_builder_service import clean_url, contact_items, format_period

FONT = "Calibri"


def _rtl(paragraph):
    """Right-to-left paragraph and runs for Arabic text (python-docx has no API for w:bidi / w:rtl)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    p_pr.append(OxmlElement("w:bidi"))
    for run in paragraph.runs:
        r_pr = run._r.get_or_add_rPr()
        r_pr.append(OxmlElement("w:rtl"))
        fonts = r_pr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            r_pr.append(fonts)
        fonts.set(qn("w:cs"), "Arial")


def _add(doc, text, style=None, bold=False, size=None, color=None):
    from docx.shared import Pt, RGBColor

    paragraph = doc.add_paragraph(style=style)
    run = paragraph.add_run(text or "")
    run.bold = bold or None
    if size:
        run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if ARABIC.search(text or ""):
        _rtl(paragraph)
    return paragraph


def render_cv_docx(cv: dict) -> bytes:
    from docx import Document
    from docx.shared import Mm, Pt

    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    for side in ("left_margin", "right_margin"):
        setattr(section, side, Mm(18))
    section.top_margin = section.bottom_margin = Mm(16)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = FONT, Pt(10.5)
    normal.paragraph_format.space_after = Pt(2)
    for name in ("Heading 1", "Heading 2"):
        doc.styles[name].font.name = FONT
    props = doc.core_properties
    props.title, props.author, props.subject = f"{cv.get('name') or 'CV'} - Resume", cv.get("name") or "", "Resume"

    title = doc.add_heading(level=1)
    title.add_run(cv.get("name") or "Curriculum Vitae").font.size = Pt(20)
    if ARABIC.search(cv.get("name") or ""):
        _rtl(title)
    contacts = " | ".join(c["text"] for c in contact_items(cv))
    if contacts:
        _add(doc, contacts, size=9.5, color="475569")

    def heading(text):
        doc.add_heading(text.upper(), level=2)

    if cv.get("summary"):
        heading("Summary")
        _add(doc, cv["summary"])
    if cv.get("education"):
        heading("Education")
        for e in cv["education"]:
            _add(doc, e["university"] or e["major"], bold=True)
            meta = " | ".join(v for v in (e["major"] if e["university"] else "",
                                          format_period(e["start_date"], e["end_date"], e["is_current"])) if v)
            if meta:
                _add(doc, meta, size=9.5, color="475569")
    if cv.get("experiences"):
        heading("Experience")
        for x in cv["experiences"]:
            _add(doc, " | ".join(v for v in (x["title"], x["organization"]) if v), bold=True)
            for achievement in x["achievements"]:
                _add(doc, achievement, style="List Bullet")
    if cv.get("projects"):
        heading("Projects")
        for p in cv["projects"]:
            _add(doc, p["title"], bold=True)
            if p["description"]:
                _add(doc, p["description"])
            if p["tech_stack"]:
                _add(doc, "Tech: " + p["tech_stack"], size=9.5, color="475569")
            link = clean_url(p["link"])
            if link.href_url:
                _add(doc, link.display_url, size=9.5, color="475569")
    if cv.get("skills"):
        heading("Skills")
        _add(doc, ", ".join(cv["skills"]))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
