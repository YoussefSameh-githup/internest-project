"""Single-column, ATS-friendly PDF (real selectable text, standard fonts, plain headings, no tables or images).

Mirrors templates/students/cv_ats.html so the preview and the download read the same.
"""
import io
from xml.sax.saxutils import escape

from internest_core.letters import ARABIC, _fonts, _visual

from .cv_builder_service import clean_url, contact_items, format_period

INK = "#1e293b"  # dark slate
MUTED = "#475569"


def _p(text):
    """Escaped Paragraph markup; Arabic is shaped right-to-left."""
    return escape(_visual(text) if ARABIC.search(text or "") else text or "")


def render_cv_pdf(cv: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer

    has_arabic = bool(ARABIC.search(str(cv)))
    regular, bold = _fonts() if has_arabic else ("Helvetica", "Helvetica-Bold")
    ink, muted = colors.HexColor(INK), colors.HexColor(MUTED)

    def style(name, size, font=regular, color=ink, leading=None, before=0, after=0, indent=0):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading or size * 1.35, textColor=color,
                              spaceBefore=before, spaceAfter=after, leftIndent=indent, alignment=TA_LEFT)

    name_s = style("name", 20, bold, after=2)
    contact_s = style("contact", 9.5, color=muted, after=6)
    heading_s = style("heading", 11.5, bold, before=10, after=2)
    item_s = style("item", 10.5, bold, after=1)
    meta_s = style("meta", 9.5, color=muted, after=2)
    body_s = style("body", 10, after=2)
    bullet_s = style("bullet", 10, indent=12, after=1)

    story = [Paragraph(_p(cv["name"] or "Curriculum Vitae"), name_s)]
    def link(href, text):
        return f'<a href="{escape(href, {chr(34): "&quot;"})}" color="{MUTED}">{_p(text)}</a>'

    contacts = [link(c["href"], c["text"]) if c["href"] else _p(c["text"]) for c in contact_items(cv)]
    if contacts:  # clickable links that show only domain + path
        story.append(Paragraph(" | ".join(contacts), contact_s))

    def section(title, blocks):
        if not blocks:
            return
        story.append(Paragraph(title.upper(), heading_s))
        story.append(HRFlowable(width="100%", thickness=0.8, color=ink, spaceBefore=1, spaceAfter=4))
        story.extend(blocks)

    if cv.get("summary"):
        section("Summary", [Paragraph(_p(cv["summary"]), body_s)])
    section("Education", [KeepTogether([
        Paragraph(_p(e["university"] or e["major"]), item_s),
        Paragraph(_p(" | ".join(v for v in (e["major"] if e["university"] else "",
                                            format_period(e["start_date"], e["end_date"], e["is_current"])) if v)), meta_s),
    ]) for e in cv.get("education", [])])
    section("Experience", [KeepTogether(
        [Paragraph(_p(" | ".join(v for v in (x["title"], x["organization"]) if v)), item_s)]
        + [Paragraph("• " + _p(a), bullet_s) for a in x["achievements"]]
        + [Spacer(1, 3)]
    ) for x in cv.get("experiences", [])])
    section("Projects", [KeepTogether(
        [Paragraph(_p(p["title"]), item_s)]
        + ([Paragraph(_p(p["description"]), body_s)] if p["description"] else [])
        + ([Paragraph(_p("Tech: " + p["tech_stack"]), meta_s)] if p["tech_stack"] else [])
        + ([Paragraph(link(*clean_url(p["link"])), meta_s)] if clean_url(p["link"]).href_url else [])
    ) for p in cv.get("projects", [])])
    if cv.get("skills"):
        section("Skills", [Paragraph(_p(", ".join(cv["skills"])), body_s)])

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{cv.get('name') or 'CV'} - Resume", author=cv.get("name") or "", subject="Resume")
    doc.build(story)
    return buf.getvalue()
