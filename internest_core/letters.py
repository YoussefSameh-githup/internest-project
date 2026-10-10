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


def render_letter(exp, verify_url) -> bytes:
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 56
    student = exp.student.user.get_full_name() or exp.student.user.username
    company = exp.company.company_name
    date = exp.completion_date.strftime("%d %B %Y")
    c.setTitle(f"Internest recommendation — {student}")

    # Header band
    c.setFillColorRGB(*BRAND)
    c.rect(0, h - 110, w, 110, stroke=0, fill=1)
    x_text = margin
    if exp.company.logo:
        try:
            c.drawImage(ImageReader(exp.company.logo.path), margin, h - 95, 64, 64, preserveAspectRatio=True, mask="auto")
            x_text = margin + 80
        except Exception:  # SVG or missing file: the name alone is fine
            logger.info("Logo not usable in letter for partner %s", exp.company.pk)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 20)
    c.drawString(x_text, h - 58, _visual(company))
    c.setFont(regular, 10)
    c.drawString(x_text, h - 76, "Verified via Internest")
    c.setFont(bold, 13)
    c.drawRightString(w - margin, h - 58, _visual("خطاب توصية موثّق"))

    # Title
    y = h - 160
    c.setFillColorRGB(*INK)
    c.setFont(bold, 22)
    c.drawString(margin, y, "Verified Recommendation Letter")
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 10)
    c.drawString(margin, y - 18, f"Issued {exp.created_at.strftime('%d %B %Y') if exp.created_at else date}")

    # Body: a fixed English statement, then each value on its own line (no mixed-script lines to reorder)
    y -= 52
    c.setFillColorRGB(*INK)
    c.setFont(regular, 12)
    statement = ("This letter confirms that the student below successfully completed the engagement below. "
                 "The work was reviewed and rated by the company on the Internest platform.")
    for line in _lines(statement, regular, 12, w - 2 * margin):
        c.drawString(margin, y, line)
        y -= 18
    y -= 10
    for label, value in (("Student", student), ("Company", company), ("Role", exp.gig_title), ("Completed on", date)):
        c.setFillColorRGB(*MUTED)
        c.setFont(regular, 11)
        c.drawString(margin, y, label)
        c.setFillColorRGB(*INK)
        c.setFont(bold, 12)
        c.drawString(margin + 120, y, _visual(value)[:90])
        y -= 22

    # Rating
    y -= 8
    c.setFont(bold, 12)
    c.drawString(margin, y, "Rating")
    for i in range(5):
        _star(c, margin + 70 + i * 22, y + 4, 8, i < exp.star_rating)
    c.setFillColorRGB(*INK)
    c.setFont(regular, 12)
    c.drawString(margin + 70 + 5 * 22 + 6, y, f"{exp.star_rating} / 5")

    # Feedback (Arabic lines right-aligned)
    y -= 36
    c.setFont(bold, 12)
    c.drawString(margin, y, "Feedback")
    y -= 22
    c.setFont(regular, 11.5)
    rtl = bool(ARABIC.search(exp.review_text))
    for line in _lines(exp.review_text, regular, 11.5, w - 2 * margin)[:18]:
        if rtl:
            c.drawRightString(w - margin, y, _visual(line))
        else:
            c.drawString(margin, y, line)
        y -= 17

    # Verification footer with QR
    c.setStrokeColorRGB(0.89, 0.91, 0.94)
    c.line(margin, 170, w - margin, 170)
    qr = QrCodeWidget(verify_url)
    x0, y0, x1, y1 = qr.getBounds()
    size = 110
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, c, w - margin - size, 40)
    c.setFillColorRGB(*INK)
    c.setFont(bold, 12)
    c.drawString(margin, 140, f"Verification code: {exp.verification_code}")
    c.setFont(regular, 10)
    c.setFillColorRGB(*MUTED)
    c.drawString(margin, 122, "Scan the QR code or open the link below to confirm this letter is authentic:")
    c.setFillColorRGB(*BRAND)
    c.drawString(margin, 106, verify_url)
    c.setFillColorRGB(*MUTED)
    c.drawRightString(w - margin - 130, 64, _visual("وثيقة معتمدة وموثّقة رسميًا من منصة Internest"))
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
