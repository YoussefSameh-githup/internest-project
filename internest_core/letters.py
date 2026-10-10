"""Verified recommendation letters: A4 PDF with rating, feedback and a QR code to the public verification page."""
import io
import logging
import math
import os
import re

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Fonts with Arabic glyphs, first match wins (PDF_FONT_PATH in settings overrides).
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
]
INK, MUTED, BRAND, GOLD = (0.06, 0.09, 0.16), (0.39, 0.45, 0.55), (0.12, 0.23, 0.54), (0.96, 0.62, 0.04)


def _fonts():
    """(regular, bold) font names registered with ReportLab; Helvetica if no Arabic-capable font is found."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "LetterFont" in pdfmetrics.getRegisteredFontNames():
        return "LetterFont", "LetterFont-Bold"
    custom = getattr(settings, "PDF_FONT_PATH", "")
    for regular, bold in ([(custom, custom)] if custom else []) + FONT_CANDIDATES:
        if regular and os.path.exists(regular):
            pdfmetrics.registerFont(TTFont("LetterFont", regular))
            pdfmetrics.registerFont(TTFont("LetterFont-Bold", bold if os.path.exists(bold) else regular))
            return "LetterFont", "LetterFont-Bold"
    logger.warning("No Arabic-capable font found for letters; Arabic text may not render")
    return "Helvetica", "Helvetica-Bold"


def _visual(text):
    """Shape + reorder Arabic so it renders right-to-left; Latin text is returned unchanged."""
    if not ARABIC.search(text or ""):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except ImportError:
        return text


def _lines(text, font, size, width):
    from reportlab.lib.utils import simpleSplit

    out = []
    for para in (text or "").splitlines() or [""]:
        out += simpleSplit(para, font, size, width) or [""]
    return out


def _star(c, cx, cy, r, filled):
    p = c.beginPath()
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.45
        angle = math.pi / 2 + i * math.pi / 5
        x, y = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        p.moveTo(x, y) if i == 0 else p.lineTo(x, y)
    p.close()
    c.setFillColorRGB(*(GOLD if filled else (0.89, 0.91, 0.94)))
    c.drawPath(p, stroke=0, fill=1)


def _image(path):
    """ImageReader for a raster file, or None (missing file, SVG, unreadable image)."""
    from reportlab.lib.utils import ImageReader

    try:
        if path and os.path.exists(path):
            reader = ImageReader(path)
            reader.getSize()
            return reader
    except Exception:
        logger.info("Image not usable in letter: %s", path)
    return None


def _brand_logo_path():
    from django.contrib.staticfiles import finders

    return finders.find("img/internest_logo.jpg")


def _verified_skills(exp, limit=4):
    """Top verified skills with scores, e.g. "Python 95%" (empty if unavailable)."""
    try:
        records = exp.student.skills.filter(status="verified").select_related("skill").order_by("-score")[:limit]
        return [f"{r.skill.name} {r.score}%" if r.score is not None else r.skill.name for r in records]
    except Exception:
        return []


def _stamp(c, cx, cy, r, regular, bold):
    """Round 'Internest Verified Document' seal."""
    c.saveState()
    c.setStrokeColorRGB(*BRAND)
    c.setLineWidth(2.2)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.setLineWidth(0.8)
    c.circle(cx, cy, r - 5, stroke=1, fill=0)
    c.setFillColorRGB(*BRAND)
    c.setFont(bold, 8.5)
    c.drawCentredString(cx, cy + r * 0.42, "INTERNEST")
    c.setFont(bold, 12.5)
    c.drawCentredString(cx, cy + 1, "VERIFIED")
    for dx in (-9, 0, 9):  # small accent stars under VERIFIED
        _star(c, cx + dx, cy - r * 0.22, 3, True)
    c.setFillColorRGB(*BRAND)
    c.setFont(regular, 7)
    c.drawCentredString(cx, cy - r * 0.55, "DOCUMENT")
    c.restoreState()


def render_letter(exp, verify_url) -> bytes:
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    m = 54  # page margin
    student = exp.student.user.get_full_name() or exp.student.user.username
    company = exp.company.company_name
    date = exp.completion_date.strftime("%d %B %Y")
    issued = exp.created_at.strftime("%d %B %Y") if getattr(exp, "created_at", None) else date
    c.setTitle(f"Internest official recommendation — {student}")
    c.setAuthor("Internest")

    # Faint diagonal watermark
    c.saveState()
    c.setFillColorRGB(*BRAND)
    c.setFillAlpha(0.045)
    c.setFont(bold, 64)
    c.translate(w / 2, h / 2 - 40)
    c.rotate(32)
    c.drawCentredString(0, 0, "INTERNEST VERIFIED")
    c.restoreState()

    # Header: Internest logo (left) · company logo (right) · accent bar
    top = h - m
    logo = 46
    brand = _image(_brand_logo_path())
    if brand:
        c.drawImage(brand, m, top - logo, logo, logo, preserveAspectRatio=True, mask="auto")
    c.setFillColorRGB(*INK)
    c.setFont(bold, 15)
    c.drawString(m + (logo + 10 if brand else 0), top - 20, "INTERNEST")
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawString(m + (logo + 10 if brand else 0), top - 34, "Verified student talent marketplace")

    partner_logo = _image(exp.company.logo.path) if getattr(exp.company, "logo", None) else None
    if partner_logo:
        c.drawImage(partner_logo, w - m - logo, top - logo, logo, logo, preserveAspectRatio=True, mask="auto")
    else:  # initial badge
        c.setFillColorRGB(*BRAND)
        c.roundRect(w - m - logo, top - logo, logo, logo, 10, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont(bold, 20)
        c.drawCentredString(w - m - logo / 2, top - logo / 2 - 7, _visual(company[:1].upper()))
    c.setFillColorRGB(*INK)
    c.setFont(bold, 12)
    c.drawRightString(w - m - logo - 10, top - 20, _visual(company)[:40])
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawRightString(w - m - logo - 10, top - 34, "Issuing company")

    bar_y = top - logo - 14
    c.setFillColorRGB(*BRAND)
    c.rect(m, bar_y, w - 2 * m, 3, stroke=0, fill=1)
    c.setFillColorRGB(*GOLD)
    c.rect(m, bar_y - 3, (w - 2 * m) * 0.28, 3, stroke=0, fill=1)

    # Title
    y = bar_y - 40
    c.setFillColorRGB(*INK)
    c.setFont(bold, 17)
    c.drawString(m, y, "OFFICIAL RECOMMENDATION LETTER & PROOF OF WORK")
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 9.5)
    c.drawString(m, y - 17, f"Issued {issued}  ·  Reference {exp.verification_code}")
    c.setFont(bold, 10.5)
    c.drawRightString(w - m, y - 17, _visual("خطاب توصية رسمي وإثبات عمل"))

    # Metadata box (2 × 3 grid)
    box_top, box_h = y - 36, 132
    c.setFillColorRGB(0.97, 0.98, 1)
    c.setStrokeColorRGB(0.86, 0.89, 0.95)
    c.roundRect(m, box_top - box_h, w - 2 * m, box_h, 12, stroke=1, fill=1)
    col_w = (w - 2 * m) / 2
    cells = [("STUDENT NAME", student), ("COMPANY", company), ("ROLE / POSITION", exp.gig_title),
             ("COMPLETION DATE", date), ("PERFORMANCE RATING", None), ("VERIFICATION CODE", exp.verification_code)]
    for i, (label, value) in enumerate(cells):
        x = m + 18 + (i % 2) * col_w
        cy = box_top - 30 - (i // 2) * 40
        c.setFillColorRGB(*MUTED)
        c.setFont(regular, 7.5)
        c.drawString(x, cy + 12, label)
        c.setFillColorRGB(*INK)
        if value is None:
            for s in range(5):
                _star(c, x + 6 + s * 15, cy - 1, 5.5, s < exp.star_rating)
            c.setFillColorRGB(*INK)
            c.setFont(bold, 11)
            c.drawString(x + 82, cy - 4, f"{exp.star_rating} / 5 Stars")
        else:
            c.setFont(bold, 11.5)
            c.drawString(x, cy - 4, _visual(str(value))[:46])
    c.setStrokeColorRGB(0.86, 0.89, 0.95)
    c.line(m + col_w, box_top - 12, m + col_w, box_top - box_h + 12)

    # Formal recommendation
    y = box_top - box_h - 34
    c.setFillColorRGB(*INK)
    c.setFont(bold, 12)
    c.drawString(m, y, "Recommendation")
    y -= 20
    skills = _verified_skills(exp)
    statement = (
        "Internest certifies that the student named above completed the engagement described above through the "
        "Internest platform. The deliverables were reviewed by the issuing company, which confirmed the completion "
        "and rated the student's performance as shown. This document is a verified record of real, delivered work."
    )
    if skills:
        statement += " The student's skills were independently verified through Internest's proctored assessments: " + ", ".join(skills) + "."
    c.setFont(regular, 10.5)
    for line in _lines(statement, regular, 10.5, w - 2 * m):
        c.drawString(m, y, line)
        y -= 15

    # Company feedback (quote block; Arabic right-aligned)
    y -= 14
    c.setFont(bold, 12)
    c.drawString(m, y, "Feedback from the company")
    y -= 10
    rtl = bool(ARABIC.search(exp.review_text))
    quote = _lines(exp.review_text, regular, 10.5, w - 2 * m - 36)[:9]
    q_h = 18 + 15 * len(quote)
    c.setFillColorRGB(0.94, 0.99, 0.96)
    c.roundRect(m, y - q_h, w - 2 * m, q_h, 10, stroke=0, fill=1)
    c.setFillColorRGB(0.13, 0.77, 0.37)
    c.rect(m if not rtl else w - m - 4, y - q_h, 4, q_h, stroke=0, fill=1)
    c.setFillColorRGB(0.08, 0.33, 0.18)
    c.setFont(regular, 10.5)
    ty = y - 20
    for line in quote:
        if rtl:
            c.drawRightString(w - m - 18, ty, _visual(line))
        else:
            c.drawString(m + 18, ty, line)
        ty -= 15

    # Verification footer: QR (left) · code + link · stamp (right)
    foot = 150
    c.setStrokeColorRGB(0.89, 0.91, 0.94)
    c.line(m, foot, w - m, foot)
    qr = QrCodeWidget(verify_url)
    x0, y0, x1, y1 = qr.getBounds()
    size = 104
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, c, m - 6, 32)
    tx = m + size + 8
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8)
    c.drawString(tx, 120, "VERIFICATION CODE")
    c.setFillColorRGB(*INK)
    c.setFont(bold, 18)
    c.drawString(tx, 100, exp.verification_code)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawString(tx, 82, "Scan the QR code or open the link to confirm authenticity:")
    c.setFillColorRGB(*BRAND)
    c.setFont(regular, 8)
    c.drawString(tx, 68, verify_url[:90])
    c.linkURL(verify_url, (tx, 64, tx + 300, 76), relative=0)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawString(tx, 50, _visual("وثيقة معتمدة وموثّقة رسميًا من منصة Internest"))
    _stamp(c, w - m - 46, 86, 44, regular, bold)

    c.showPage()
    c.save()
    return buf.getvalue()


def generate_letter(exp, verify_url) -> bool:
    """Render and store the PDF. Never raises: the experience stays valid and the letter can be rebuilt on download."""
    try:
        pdf = render_letter(exp, verify_url)
    except Exception:
        logger.exception("Could not render recommendation letter %s", exp.pk)
        return False
    if exp.pdf_file:
        exp.pdf_file.delete(save=False)
    exp.pdf_file.save(f"letter-{exp.verification_uuid}.pdf", ContentFile(pdf), save=True)
    return True


def qr_svg(url, size=176) -> str:
    """Compact inline SVG QR code (one path) for `url`; "" if the QR encoder is unavailable."""
    try:
        from reportlab.graphics.barcode.qr import QrCodeWidget

        widget = QrCodeWidget(url)
        widget.draw()
        qr, quiet = widget.qr, 4
        n = qr.getModuleCount()
    except Exception:
        logger.exception("QR code generation failed")
        return ""
    cells = "".join(f"M{c + quiet},{r + quiet}h1v1h-1z" for r in range(n) for c in range(n) if qr.isDark(r, c))
    total = n + 2 * quiet
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{size}" height="{size}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{cells}" fill="#0f172a"/></svg>')


def _fallback_pdf(exp, verify_url) -> bytes:
    """Plain one-page PDF written by hand: used only if ReportLab is missing or rendering fails."""
    def esc(text):
        text = str(text).encode("latin-1", "replace").decode("latin-1")  # core fonts have no Arabic glyphs
        return text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")

    student = exp.student.user.get_full_name() or exp.student.user.username
    lines = [
        (18, "Internest - Verified Recommendation Letter"), (12, ""),
        (12, f"Student: {student}"), (12, f"Company: {exp.company.company_name}"), (12, f"Role: {exp.gig_title}"),
        (12, f"Completed on: {exp.completion_date:%d %B %Y}"), (12, f"Rating: {exp.star_rating} / 5"), (12, ""),
        (12, f"Verification code: {exp.verification_code}"), (11, f"Verify this letter at: {verify_url}"),
    ]
    stream = "BT /F1 12 Tf 56 780 Td 18 TL\n" + "".join(f"/F1 {size} Tf ({esc(text)}) Tj T*\n" for size, text in lines) + "ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def letter_bytes(exp, verify_url) -> bytes:
    """The letter as PDF bytes, always: stored file → fresh render (stored if possible) → plain fallback."""
    if exp.pdf_file:
        try:
            with exp.pdf_file.open("rb") as f:
                return f.read()
        except Exception:  # file missing on disk (e.g. new server): rebuild below
            logger.info("Stored letter %s missing, rebuilding", exp.pk)
    try:
        pdf = render_letter(exp, verify_url)
    except Exception:
        logger.exception("Letter rendering failed for %s; serving the plain fallback", exp.pk)
        return _fallback_pdf(exp, verify_url)
    try:
        exp.pdf_file.save(f"letter-{exp.verification_uuid}.pdf", ContentFile(pdf), save=True)
    except Exception:  # read-only or full media storage: still serve the letter
        logger.exception("Could not store letter %s", exp.pk)
    return pdf


def qr_data_uri(url) -> str:
    """The QR as a base64 data URI for an <img> ("" if unavailable)."""
    import base64

    svg = qr_svg(url)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode() if svg else ""
