"""Verified recommendation letters (A4 PDF) and the QR code that links them to the public verification page.

Two renderers share one layout:
* render_letter  — ReportLab: embedded Arabic-capable font, logos, platypus Table.
* _fallback_pdf  — dependency-free vector PDF (banner, table, stars, stamp, QR) used when ReportLab is
  missing or fails, so the letter is always a styled certificate, never plain text.
"""
import base64
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


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


PRIMARY, INK, SOFT = _rgb("#2563eb"), _rgb("#0f172a"), _rgb("#f8fafc")
MUTED, LINE, GOLD, GREEN = _rgb("#64748b"), _rgb("#e2e8f0"), _rgb("#f59e0b"), _rgb("#16a34a")
W, H, M = 595.28, 841.89, 50  # A4 points, page margin
TITLE = "INTERNEST — OFFICIAL RECOMMENDATION LETTER"
LETTER_VERSION = "v3"  # bump when the design changes: stored letters from older versions are rebuilt
STATEMENT = (
    "Internest certifies that the student named above completed the engagement described above through the "
    "Internest platform. The deliverables were reviewed by the issuing company, which confirmed the completion "
    "and rated the student's performance as shown. This document is a verified record of real, delivered work."
)


# ---------------------------------------------------------------- shared helpers
def qr_matrix(url):
    """QR modules as rows of booleans (no quiet zone), via `qrcode`, else ReportLab's encoder; None if neither."""
    try:
        import qrcode

        qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        return qr.get_matrix()
    except ImportError:
        pass
    try:
        from reportlab.graphics.barcode.qr import QrCodeWidget

        widget = QrCodeWidget(url)
        widget.draw()
        n = widget.qr.getModuleCount()
        return [[widget.qr.isDark(r, c) for c in range(n)] for r in range(n)]
    except Exception:
        logger.exception("No QR encoder available")
        return None


def qr_png_base64(url, scale=8, quiet=4) -> str:
    """Base64 PNG of the QR (for <img src="data:image/png;base64,...">); "" if unavailable."""
    matrix = qr_matrix(url)
    if not matrix:
        return ""
    from PIL import Image

    n = len(matrix)
    img = Image.new("1", (n + 2 * quiet, n + 2 * quiet), 1)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                img.putpixel((c + quiet, r + quiet), 0)
    img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


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


def _verified_skills(exp, limit=4):
    """Top verified skills with scores, e.g. "Python 95%" (empty if unavailable)."""
    try:
        records = exp.student.skills.filter(status="verified").select_related("skill").order_by("-score")[:limit]
        return [f"{r.skill.name} {r.score}%" if r.score is not None else r.skill.name for r in records]
    except Exception:
        return []


def _facts(exp):
    student = exp.student.user.get_full_name() or exp.student.user.username
    return [
        ("STUDENT NAME", student), ("COMPANY", exp.company.company_name), ("ROLE / POSITION", exp.gig_title),
        ("COMPLETION DATE", exp.completion_date.strftime("%d %B %Y")),
        ("PERFORMANCE RATING", f"{exp.star_rating} / 5 Stars"), ("VERIFICATION CODE", exp.verification_code),
    ]


def _statement(exp):
    skills = _verified_skills(exp)
    if not skills:
        return STATEMENT
    return STATEMENT + " The student's skills were independently verified through Internest's proctored assessments: " + ", ".join(skills) + "."


def _star_points(cx, cy, r):
    for i in range(10):
        radius = r if i % 2 == 0 else r * 0.45
        angle = math.pi / 2 + i * math.pi / 5
        yield cx + radius * math.cos(angle), cy + radius * math.sin(angle)


# ---------------------------------------------------------------- ReportLab renderer
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


def _lines(text, font, size, width):
    from reportlab.lib.utils import simpleSplit

    out = []
    for para in (text or "").splitlines() or [""]:
        out += simpleSplit(para, font, size, width) or [""]
    return out


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


def _logo_tile(c, x, y, size, image, letters, bold):
    """White rounded tile with the logo, or a vector monogram badge when the image is missing."""
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(x, y, size, size, 10, stroke=0, fill=1)
    if image:
        c.drawImage(image, x + 5, y + 5, size - 10, size - 10, preserveAspectRatio=True, mask="auto")
        return
    c.setFillColorRGB(*PRIMARY)
    c.roundRect(x + 6, y + 6, size - 12, size - 12, 8, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, size * 0.34)
    c.drawCentredString(x + size / 2, y + size / 2 - size * 0.12, _visual(letters))


def _star(c, cx, cy, r, filled):
    p = c.beginPath()
    for i, (x, y) in enumerate(_star_points(cx, cy, r)):
        p.moveTo(x, y) if i == 0 else p.lineTo(x, y)
    p.close()
    c.setFillColorRGB(*(GOLD if filled else LINE))
    c.drawPath(p, stroke=0, fill=1)


def _stamp(c, cx, cy, r, regular, bold):
    """Round 'Internest Verified Document' seal."""
    c.saveState()
    c.setStrokeColorRGB(*PRIMARY)
    c.setLineWidth(2.2)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.setLineWidth(0.8)
    c.circle(cx, cy, r - 5, stroke=1, fill=0)
    c.setFillColorRGB(*PRIMARY)
    c.setFont(bold, 8.5)
    c.drawCentredString(cx, cy + r * 0.42, "INTERNEST")
    c.setFont(bold, 12.5)
    c.drawCentredString(cx, cy + 1, "VERIFIED")
    for dx in (-9, 0, 9):
        _star(c, cx + dx, cy - r * 0.22, 3, True)
    c.setFillColorRGB(*PRIMARY)
    c.setFont(regular, 7)
    c.drawCentredString(cx, cy - r * 0.55, "DOCUMENT")
    c.restoreState()


def _draw_qr(c, matrix, x, y, size):
    if not matrix:
        return
    n, quiet = len(matrix), 2
    cell = size / (n + 2 * quiet)
    c.setFillColorRGB(1, 1, 1)
    c.rect(x, y, size, size, stroke=0, fill=1)
    c.setFillColorRGB(*INK)
    for r, row in enumerate(matrix):
        for col, dark in enumerate(row):
            if dark:
                c.rect(x + (col + quiet) * cell, y + size - (r + quiet + 1) * cell, cell + 0.05, cell + 0.05, stroke=0, fill=1)


def render_letter(exp, verify_url) -> bytes:
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table, TableStyle

    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    student = exp.student.user.get_full_name() or exp.student.user.username
    company = exp.company.company_name
    issued = exp.created_at.strftime("%d %B %Y") if getattr(exp, "created_at", None) else exp.completion_date.strftime("%d %B %Y")
    c.setTitle(f"Internest official recommendation — {student}")
    c.setAuthor("Internest")

    # Watermark
    c.saveState()
    c.setFillColorRGB(*PRIMARY)
    c.setFillAlpha(0.04)
    c.setFont(bold, 64)
    c.translate(W / 2, H / 2 - 60)
    c.rotate(32)
    c.drawCentredString(0, 0, "INTERNEST VERIFIED")
    c.restoreState()

    # Header banner with both logos
    band = 104
    c.setFillColorRGB(*PRIMARY)
    c.rect(0, H - band, W, band, stroke=0, fill=1)
    c.setFillColorRGB(*GOLD)
    c.rect(0, H - band - 4, W * 0.32, 4, stroke=0, fill=1)
    tile = 58
    _logo_tile(c, M, H - band / 2 - tile / 2, tile, _image(_brand_logo_path()), "IN", bold)
    partner_logo = _image(exp.company.logo.path) if getattr(exp.company, "logo", None) else None
    _logo_tile(c, W - M - tile, H - band / 2 - tile / 2, tile, partner_logo, company[:1].upper(), bold)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 13.5)
    c.drawCentredString(W / 2, H - band / 2 + 4, TITLE)
    c.setFont(regular, 9)
    c.drawCentredString(W / 2, H - band / 2 - 13, "Proof of Work · Verified by Internest")

    # Heading
    y = H - band - 42
    c.setFillColorRGB(*INK)
    c.setFont(bold, 20)
    c.drawString(M, y, "Certificate of Verified Work")
    c.setFont(bold, 11)
    c.drawRightString(W - M, y + 2, _visual("خطاب توصية رسمي وإثبات عمل"))
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 9.5)
    c.drawString(M, y - 16, f"Issued {issued}  ·  Reference {exp.verification_code}")

    # Details table
    rows = [[label, _visual(str(value))] for label, value in _facts(exp)]
    table = Table(rows, colWidths=[150, W - 2 * M - 150])
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), regular, 8.5), ("FONT", (1, 0), (1, -1), bold, 11),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.Color(*MUTED)), ("TEXTCOLOR", (1, 0), (1, -1), colors.Color(*INK)),
        ("BACKGROUND", (0, 0), (0, -1), colors.Color(*SOFT)),
        ("LINEBELOW", (0, 0), (-1, -2), 0.6, colors.Color(*LINE)),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.Color(*LINE)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
    ]))
    _w, t_h = table.wrapOn(c, W - 2 * M, H)
    t_top = y - 34
    table.drawOn(c, M, t_top - t_h)
    # Stars next to the rating text (row 5)
    row_h = t_h / len(rows)
    star_y = t_top - row_h * 4.5 - 1
    for s in range(5):
        _star(c, W - M - 92 + s * 16, star_y, 5.5, s < exp.star_rating)

    # Recommendation
    y = t_top - t_h - 32
    c.setFillColorRGB(*INK)
    c.setFont(bold, 12)
    c.drawString(M, y, "Recommendation")
    y -= 18
    c.setFont(regular, 10.3)
    for line in _lines(_statement(exp), regular, 10.3, W - 2 * M):
        c.drawString(M, y, line)
        y -= 14.5

    # Company feedback
    y -= 14
    c.setFont(bold, 12)
    c.drawString(M, y, "Feedback from the company")
    y -= 10
    rtl = bool(ARABIC.search(exp.review_text))
    quote = _lines(exp.review_text, regular, 10.3, W - 2 * M - 36)[:8]
    q_h = 18 + 14.5 * len(quote)
    c.setFillColorRGB(*SOFT)
    c.setStrokeColorRGB(*LINE)
    c.roundRect(M, y - q_h, W - 2 * M, q_h, 10, stroke=1, fill=1)
    c.setFillColorRGB(*PRIMARY)
    c.rect(W - M - 4 if rtl else M, y - q_h, 4, q_h, stroke=0, fill=1)
    c.setFillColorRGB(*INK)
    c.setFont(regular, 10.3)
    ty = y - 19
    for line in quote:
        if rtl:
            c.drawRightString(W - M - 16, ty, _visual(line))
        else:
            c.drawString(M + 16, ty, line)
        ty -= 14.5

    # Verification footer: QR (bottom left) · code + link · stamp
    c.setStrokeColorRGB(*LINE)
    c.line(M, 150, W - M, 150)
    _draw_qr(c, qr_matrix(verify_url), M - 4, 32, 106)
    tx = M + 114
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8)
    c.drawString(tx, 120, "VERIFICATION CODE")
    c.setFillColorRGB(*INK)
    c.setFont(bold, 18)
    c.drawString(tx, 100, exp.verification_code)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawString(tx, 82, "Scan the QR code or open the link to confirm authenticity:")
    c.setFillColorRGB(*PRIMARY)
    c.setFont(regular, 8)
    c.drawString(tx, 68, verify_url[:90])
    c.linkURL(verify_url, (tx, 64, tx + 300, 76), relative=0)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.5)
    c.drawString(tx, 50, _visual("وثيقة معتمدة وموثّقة رسميًا من منصة Internest"))
    _stamp(c, W - M - 46, 86, 44, regular, bold)

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- dependency-free renderer
class _RawPDF:
    """Minimal vector PDF canvas (Helvetica, fills, strokes, text) for when ReportLab is unavailable."""

    WIDTH = {"regular": 0.5, "bold": 0.56}  # average glyph width / font size, for centring and wrapping

    def __init__(self):
        self.ops = []

    @staticmethod
    def _plain(text):
        text = str(text)
        if ARABIC.search(text):  # core fonts have no Arabic glyphs
            text = "(Arabic text: see the verification page)"
        return text.replace("—", "-").replace("·", "-").encode("latin-1", "replace").decode("latin-1")

    @staticmethod
    def _escape(text):
        return text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")

    def fill(self, rgb):
        self.ops.append("%.3f %.3f %.3f rg" % rgb)

    def stroke(self, rgb, width=1):
        self.ops.append("%.3f %.3f %.3f RG %.2f w" % (*rgb, width))

    def rect(self, x, y, w, h, mode="f"):
        self.ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {mode}")

    def poly(self, points):
        pts = list(points)
        self.ops.append(" ".join([f"{pts[0][0]:.2f} {pts[0][1]:.2f} m"] + [f"{x:.2f} {y:.2f} l" for x, y in pts[1:]] + ["h f"]))

    def circle(self, cx, cy, r):
        k = 0.5523 * r
        self.ops.append(
            f"{cx + r:.2f} {cy:.2f} m {cx + r:.2f} {cy + k:.2f} {cx + k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c "
            f"{cx - k:.2f} {cy + r:.2f} {cx - r:.2f} {cy + k:.2f} {cx - r:.2f} {cy:.2f} c "
            f"{cx - r:.2f} {cy - k:.2f} {cx - k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c "
            f"{cx + k:.2f} {cy - r:.2f} {cx + r:.2f} {cy - k:.2f} {cx + r:.2f} {cy:.2f} c S")

    def text(self, x, y, size, value, bold=False, align="left"):
        value = self._plain(value)
        width = len(value) * size * self.WIDTH["bold" if bold else "regular"]
        x = x - width / 2 if align == "center" else x - width if align == "right" else x
        self.ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {x:.2f} {y:.2f} Td ({self._escape(value)}) Tj ET")

    def wrap(self, value, size, width):
        words, lines, line = self._plain(value).split(), [], ""
        for word in words:
            if line and (len(line) + 1 + len(word)) * size * self.WIDTH["regular"] > width:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        return lines + ([line] if line else [])

    def bytes(self):
        stream = "\n".join(self.ops)
        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] "
            "/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
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


def _fallback_pdf(exp, verify_url) -> bytes:
    """Styled vector certificate without ReportLab: same banner, table, stars, QR and stamp."""
    p = _RawPDF()
    company = exp.company.company_name
    # Banner with monogram badges
    band = 104
    p.fill(PRIMARY)
    p.rect(0, H - band, W, band)
    p.fill(GOLD)
    p.rect(0, H - band - 4, W * 0.32, 4)
    for x, letters in ((M, "IN"), (W - M - 58, (company[:1] if not ARABIC.search(company[:1]) else "C").upper())):
        p.fill((1, 1, 1))
        p.rect(x, H - band / 2 - 29, 58, 58)
        p.fill(PRIMARY)
        p.rect(x + 6, H - band / 2 - 23, 46, 46)
        p.fill((1, 1, 1))
        p.text(x + 29, H - band / 2 - 7, 20, letters, bold=True, align="center")
    p.fill((1, 1, 1))
    p.text(W / 2, H - band / 2 + 4, 13.5, TITLE, bold=True, align="center")
    p.text(W / 2, H - band / 2 - 13, 9, "Proof of Work - Verified by Internest", align="center")

    y = H - band - 42
    p.fill(INK)
    p.text(M, y, 20, "Certificate of Verified Work", bold=True)
    p.fill(MUTED)
    p.text(M, y - 16, 9.5, f"Reference {exp.verification_code}")

    # Details table
    facts = _facts(exp)
    row_h, label_w, t_top = 30, 150, y - 34
    p.fill(SOFT)
    p.rect(M, t_top - row_h * len(facts), label_w, row_h * len(facts))
    p.stroke(LINE, 0.8)
    p.rect(M, t_top - row_h * len(facts), W - 2 * M, row_h * len(facts), "S")
    for i, (label, value) in enumerate(facts):
        ry = t_top - (i + 1) * row_h
        if i:
            p.rect(M, ry + row_h, W - 2 * M, 0.01, "S")
        p.fill(MUTED)
        p.text(M + 12, ry + 11, 8.5, label)
        p.fill(INK)
        p.text(M + label_w + 12, ry + 10, 11, value, bold=True)
    for s in range(5):  # rating stars
        p.fill(GOLD if s < exp.star_rating else LINE)
        p.poly(_star_points(W - M - 92 + s * 16, t_top - row_h * 4.5 - 1, 5.5))

    # Recommendation + feedback
    y = t_top - row_h * len(facts) - 32
    p.fill(INK)
    p.text(M, y, 12, "Recommendation", bold=True)
    y -= 18
    for line in p.wrap(_statement(exp), 10.3, W - 2 * M):
        p.text(M, y, 10.3, line)
        y -= 14.5
    y -= 14
    p.text(M, y, 12, "Feedback from the company", bold=True)
    quote = p.wrap(exp.review_text, 10.3, W - 2 * M - 36)[:8]
    q_h = 18 + 14.5 * len(quote)
    p.fill(SOFT)
    p.rect(M, y - 10 - q_h, W - 2 * M, q_h)
    p.fill(PRIMARY)
    p.rect(M, y - 10 - q_h, 4, q_h)
    p.fill(INK)
    ty = y - 29
    for line in quote:
        p.text(M + 16, ty, 10.3, line)
        ty -= 14.5

    # Footer: QR · code · link · stamp
    p.stroke(LINE, 1)
    p.rect(M, 150, W - 2 * M, 0.01, "S")
    matrix = qr_matrix(verify_url)
    if matrix:
        n, size, x0, y0 = len(matrix), 106, M - 4, 32
        cell = size / (n + 4)
        p.fill((1, 1, 1))
        p.rect(x0, y0, size, size)
        p.fill(INK)
        for r, row in enumerate(matrix):
            for col, dark in enumerate(row):
                if dark:
                    p.rect(x0 + (col + 2) * cell, y0 + size - (r + 3) * cell, cell + 0.05, cell + 0.05)
    tx = M + 114
    p.fill(MUTED)
    p.text(tx, 120, 8, "VERIFICATION CODE")
    p.fill(INK)
    p.text(tx, 100, 18, exp.verification_code, bold=True)
    p.fill(MUTED)
    p.text(tx, 82, 8.5, "Scan the QR code or open the link to confirm authenticity:")
    p.fill(PRIMARY)
    p.text(tx, 68, 8, verify_url[:90])
    cx, cy, r = W - M - 46, 86, 44
    p.stroke(PRIMARY, 2.2)
    p.circle(cx, cy, r)
    p.stroke(PRIMARY, 0.8)
    p.circle(cx, cy, r - 5)
    p.fill(PRIMARY)
    p.text(cx, cy + r * 0.42, 8.5, "INTERNEST", bold=True, align="center")
    p.text(cx, cy + 1, 12.5, "VERIFIED", bold=True, align="center")
    p.text(cx, cy - r * 0.55, 7, "DOCUMENT", align="center")
    p.fill(GOLD)
    for dx in (-9, 0, 9):
        p.poly(_star_points(cx + dx, cy - r * 0.22, 3))
    return p.bytes()


# ---------------------------------------------------------------- storage + delivery
def generate_letter(exp, verify_url) -> bool:
    """Render and store the PDF. Never raises: the letter can always be rebuilt on download."""
    try:
        pdf = render_letter(exp, verify_url)
    except Exception:
        logger.exception("Could not render recommendation letter %s", exp.pk)
        return False
    if exp.pdf_file:
        exp.pdf_file.delete(save=False)
    exp.pdf_file.save(_letter_name(exp), ContentFile(pdf), save=True)
    return True


def _letter_name(exp):
    return f"letter-{LETTER_VERSION}-{exp.verification_uuid}.pdf"


def letter_bytes(exp, verify_url) -> bytes:
    """The letter as PDF bytes, always: stored file → fresh ReportLab render (stored) → styled fallback."""
    if exp.pdf_file and f"-{LETTER_VERSION}-" in exp.pdf_file.name:  # older designs are rebuilt
        try:
            with exp.pdf_file.open("rb") as f:
                return f.read()
        except Exception:
            logger.info("Stored letter %s missing, rebuilding", exp.pk)
    try:
        pdf = render_letter(exp, verify_url)
    except Exception:
        logger.exception("ReportLab letter failed for %s; serving the built-in certificate", exp.pk)
        return _fallback_pdf(exp, verify_url)
    try:
        if exp.pdf_file:
            exp.pdf_file.delete(save=False)
        exp.pdf_file.save(_letter_name(exp), ContentFile(pdf), save=True)
    except Exception:
        logger.exception("Could not store letter %s", exp.pk)
    return pdf
