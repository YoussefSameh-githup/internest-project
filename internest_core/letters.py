"""Verified recommendation letters (A4 PDF) and the QR code that links them to the public verification page.

The letter's title depends on the rating (see models.LETTER_TIERS); gigs rated below 3.5 get no letter.
Two renderers share one layout, built from letter_content():
* render_letter  — ReportLab: embedded Arabic-capable font, logos, platypus Table, gradient banner.
* _fallback_pdf  — dependency-free vector PDF used when ReportLab is missing or fails.
"""
import base64
import io
import logging
import math
import os
import re
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile

from .models import LETTER_TIERS

logger = logging.getLogger(__name__)

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Fonts with Arabic glyphs, first match wins (PDF_FONT_PATH in settings overrides).
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
]
LETTER_VERSION = "v4"  # bump when the design changes: stored letters from older versions are rebuilt


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


DEEP, PRIMARY, INK, SOFT = _rgb("#1e3a8a"), _rgb("#2563eb"), _rgb("#0f172a"), _rgb("#f8fafc")
MUTED, LINE, GOLD = _rgb("#64748b"), _rgb("#e2e8f0"), _rgb("#f59e0b")
W, H, M = 595.28, 841.89, 48  # A4 points, page margin
STATEMENTS = {
    "excellence": "delivered outstanding work and earned the issuing company's highest rating. Internest and the company "
                  "recommend the student without reservation.",
    "recommendation": "completed the engagement to a high professional standard. The issuing company confirmed the "
                      "deliverables and recommends the student.",
    "completion": "successfully completed the engagement described above. The issuing company confirmed the completion "
                  "of the assigned tasks.",
}


# ---------------------------------------------------------------- content (shared by both renderers)
def tier_for(rating):
    rating = Decimal(str(rating))
    return next(((key, en, ar) for minimum, key, en, ar in LETTER_TIERS if rating >= minimum), None)


def star_icons(rating):
    rating = Decimal(str(rating))
    return ["full" if n <= rating else "half" if n - Decimal("0.5") <= rating else "empty" for n in range(1, 6)]


def _rating_label(rating):
    return f"{Decimal(str(rating)):.1f}".removesuffix(".0")


def _verified_skills(exp, limit=4):
    """Top verified skills with scores, e.g. "Python 95%" (empty if unavailable)."""
    try:
        records = exp.student.skills.filter(status="verified").select_related("skill").order_by("-score")[:limit]
        return [f"{r.skill.name} {r.score}%" if r.score is not None else r.skill.name for r in records]
    except Exception:
        return []


def letter_content(exp):
    key, title_en, title_ar = tier_for(exp.star_rating)
    student = exp.student.user.get_full_name() or exp.student.user.username
    statement = f"Internest certifies that {student} " + STATEMENTS[key]
    skills = _verified_skills(exp)
    if skills:
        statement += " Skills independently verified by Internest: " + ", ".join(skills) + "."
    issued = getattr(exp, "created_at", None) or exp.completion_date
    return {
        "key": key, "title_en": title_en, "title_ar": title_ar, "statement": statement,
        "issued": issued.strftime("%d %B %Y"),
        "facts": [
            ("STUDENT NAME", student), ("COMPANY NAME", exp.company.company_name), ("ROLE TITLE", exp.gig_title),
            ("COMPLETION DATE", exp.completion_date.strftime("%d %B %Y")),
            ("RATING SCORE", f"{_rating_label(exp.star_rating)} / 5"),
        ],
        "scope": (getattr(exp, "gig_description", "") or "").strip(),
        "review": exp.review_text,
        "stars": star_icons(exp.star_rating),
    }


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


def _lines(text, font, size, width, limit=None):
    """Wrapped lines; the last kept line gets an ellipsis when text is cut."""
    from reportlab.lib.utils import simpleSplit

    out = []
    for para in (text or "").splitlines() or [""]:
        out += simpleSplit(para, font, size, width) or [""]
    if limit and len(out) > limit:
        out = out[:limit]
        out[-1] = out[-1].rstrip(" .,") + "…"
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
    c.roundRect(x, y, size, size, 12, stroke=0, fill=1)
    if image:
        c.drawImage(image, x + 5, y + 5, size - 10, size - 10, preserveAspectRatio=True, mask="auto")
        return
    c.setFillColorRGB(*PRIMARY)
    c.roundRect(x + 6, y + 6, size - 12, size - 12, 9, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, size * 0.32)
    c.drawCentredString(x + size / 2, y + size / 2 - size * 0.11, _visual(letters))


def _star(c, cx, cy, r, icon):
    """Star with 'full' | 'half' | 'empty' fill (half = gold left half)."""
    def path():
        p = c.beginPath()
        for i, (x, y) in enumerate(_star_points(cx, cy, r)):
            p.moveTo(x, y) if i == 0 else p.lineTo(x, y)
        p.close()
        return p

    c.setFillColorRGB(*(GOLD if icon == "full" else LINE))
    c.drawPath(path(), stroke=0, fill=1)
    if icon == "half":
        c.saveState()
        clip = c.beginPath()
        clip.rect(cx - r, cy - r, r, 2 * r)
        c.clipPath(clip, stroke=0, fill=0)
        c.setFillColorRGB(*GOLD)
        c.drawPath(path(), stroke=0, fill=1)
        c.restoreState()


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
        _star(c, cx + dx, cy - r * 0.22, 3, "full")
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


def _section_title(c, y, text, bold):
    c.setFillColorRGB(*PRIMARY)
    c.rect(M, y - 1, 3, 13, stroke=0, fill=1)
    c.setFillColorRGB(*INK)
    c.setFont(bold, 11.5)
    c.drawString(M + 10, y, text)


def _paragraph(c, y, text, font, size, width, limit, x=M, leading=14):
    """Draw wrapped text (Arabic right-aligned) and return the new y."""
    rtl = bool(ARABIC.search(text or ""))
    for line in _lines(text, font, size, width, limit):
        if rtl:
            c.drawRightString(x + width, y, _visual(line))
        else:
            c.drawString(x, y, line)
        y -= leading
    return y


def render_letter(exp, verify_url) -> bytes:
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table, TableStyle

    content = letter_content(exp)
    regular, bold = _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Internest — {content['title_en']} — {content['facts'][0][1]}")
    c.setAuthor("Internest")

    # Watermark
    c.saveState()
    c.setFillColorRGB(*PRIMARY)
    c.setFillAlpha(0.04)
    c.setFont(bold, 62)
    c.translate(W / 2, H / 2 - 70)
    c.rotate(32)
    c.drawCentredString(0, 0, "INTERNEST VERIFIED")
    c.restoreState()

    # Banner: deep → primary gradient, logos, tier titles
    band = 118
    c.saveState()
    clip = c.beginPath()
    clip.rect(0, H - band, W, band)
    c.clipPath(clip, stroke=0, fill=0)
    c.linearGradient(0, H, W, H - band, (colors.Color(*DEEP), colors.Color(*PRIMARY)), extend=False)
    c.restoreState()
    c.setFillColorRGB(*GOLD)
    c.rect(0, H - band - 4, W * 0.3, 4, stroke=0, fill=1)
    tile = 60
    _logo_tile(c, M, H - band / 2 - tile / 2, tile, _image(_brand_logo_path()), "IN", bold)
    partner_logo = _image(exp.company.logo.path) if getattr(exp.company, "logo", None) else None
    _logo_tile(c, W - M - tile, H - band / 2 - tile / 2, tile, partner_logo, exp.company.company_name[:1].upper(), bold)
    c.setFillColorRGB(0.75, 0.83, 1)
    c.setFont(bold, 8)
    c.drawCentredString(W / 2, H - 34, "INTERNEST  ·  OFFICIAL VERIFIED DOCUMENT")
    c.setFillColorRGB(1, 1, 1)
    c.setFont(bold, 15)
    c.drawCentredString(W / 2, H - 58, content["title_en"])
    c.setFont(bold, 12.5)
    c.drawCentredString(W / 2, H - 80, _visual(content["title_ar"]))
    c.setFillColorRGB(0.85, 0.9, 1)
    c.setFont(regular, 8.5)
    c.drawCentredString(W / 2, H - 98, f"Issued {content['issued']}  ·  Ref. {exp.verification_code}")

    # Summary table
    rows = [[label, _visual(str(value))] for label, value in content["facts"]]
    table = Table(rows, colWidths=[140, W - 2 * M - 140])
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), regular, 8), ("FONT", (1, 0), (1, -1), bold, 10.5),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.Color(*MUTED)), ("TEXTCOLOR", (1, 0), (1, -1), colors.Color(*INK)),
        ("BACKGROUND", (0, 0), (0, -1), colors.Color(*SOFT)),
        ("LINEBELOW", (0, 0), (-1, -2), 0.6, colors.Color(*LINE)),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.Color(*LINE)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
    ]))
    _w, t_h = table.wrapOn(c, W - 2 * M, H)
    t_top = H - band - 26
    table.drawOn(c, M, t_top - t_h)
    row_h = t_h / len(rows)
    for i, icon in enumerate(content["stars"]):  # stars on the rating row (last)
        _star(c, W - M - 96 + i * 17, t_top - row_h * 4.5 - 1, 6, icon)

    # Recommendation
    y = t_top - t_h - 26
    _section_title(c, y, "Recommendation", bold)
    c.setFillColorRGB(*INK)
    c.setFont(regular, 10)
    y = _paragraph(c, y - 18, content["statement"], regular, 10, W - 2 * M, limit=4)

    # Gig scope
    if content["scope"]:
        y -= 12
        _section_title(c, y, "Gig Scope & Tasks Performed", bold)
        c.setFillColorRGB(*INK)
        c.setFont(regular, 9.6)
        y = _paragraph(c, y - 18, content["scope"], regular, 9.6, W - 2 * M, limit=7, leading=13.2)

    # Manager's assessment (executive blockquote)
    y -= 14
    _section_title(c, y, "Manager's Assessment & Feedback", bold)
    y -= 10
    review_lines = _lines(content["review"], regular, 10, W - 2 * M - 52, limit=6)
    box_h = 34 + 14 * len(review_lines)
    c.setFillColorRGB(0.94, 0.96, 1)
    c.setStrokeColorRGB(0.8, 0.86, 0.99)
    c.roundRect(M, y - box_h, W - 2 * M, box_h, 12, stroke=1, fill=1)
    c.setFillColorRGB(*PRIMARY)
    c.setFont(bold, 34)
    c.drawString(M + 12, y - 34, "“")
    c.setFillColorRGB(*INK)
    c.setFont(regular, 10)
    ty = _paragraph(c, y - 22, content["review"], regular, 10, W - 2 * M - 52, limit=6, x=M + 36)
    c.setFillColorRGB(*MUTED)
    c.setFont(bold, 8.5)
    c.drawRightString(W - M - 14, ty - 2, "— " + _visual(exp.company.company_name) + ", hiring manager")

    # Verification footer: QR (bottom left) · verification ID · stamp
    c.setStrokeColorRGB(*LINE)
    c.line(M, 146, W - M, 146)
    _draw_qr(c, qr_matrix(verify_url), M - 4, 30, 104)
    tx = M + 112
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 7.5)
    c.drawString(tx, 124, "VERIFICATION CODE")
    c.setFillColorRGB(*INK)
    c.setFont(bold, 17)
    c.drawString(tx, 105, exp.verification_code)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 7.5)
    c.drawString(tx, 90, "VERIFICATION ID")
    c.setFillColorRGB(*INK)
    c.setFont(regular, 8.2)
    c.drawString(tx, 78, str(exp.verification_uuid))
    c.setFillColorRGB(*PRIMARY)
    c.setFont(regular, 8)
    c.drawString(tx, 62, verify_url[:88])
    c.linkURL(verify_url, (tx, 58, tx + 300, 70), relative=0)
    c.setFillColorRGB(*MUTED)
    c.setFont(regular, 8.2)
    c.drawString(tx, 44, _visual("وثيقة معتمدة وموثّقة رسميًا من منصة Internest"))
    _stamp(c, W - M - 46, 84, 44, regular, bold)

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- dependency-free renderer
class _RawPDF:
    """Minimal vector PDF canvas (Helvetica, fills, strokes, clipping, text) for when ReportLab is unavailable."""

    WIDTH = {"regular": 0.5, "bold": 0.56}  # average glyph width / font size, for centring and wrapping

    def __init__(self):
        self.ops = []

    @staticmethod
    def _plain(text):
        text = str(text)
        if ARABIC.search(text):  # core fonts have no Arabic glyphs
            text = "(Arabic text: see the verification page)"
        return text.replace("—", "-").replace("·", "-").replace("’", "'").encode("latin-1", "replace").decode("latin-1")

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

    def star(self, cx, cy, r, icon):
        self.fill(GOLD if icon == "full" else LINE)
        self.poly(_star_points(cx, cy, r))
        if icon == "half":
            self.ops.append(f"q {cx - r:.2f} {cy - r:.2f} {r:.2f} {2 * r:.2f} re W n")
            self.fill(GOLD)
            self.poly(_star_points(cx, cy, r))
            self.ops.append("Q")

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

    def wrap(self, value, size, width, limit=None):
        words, lines, line = self._plain(value).split(), [], ""
        for word in words:
            if line and (len(line) + 1 + len(word)) * size * self.WIDTH["regular"] > width:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines += [line] if line else []
        if limit and len(lines) > limit:
            lines = lines[:limit]
            lines[-1] = lines[-1].rstrip(" .,") + "..."
        return lines

    def section(self, y, title):
        self.fill(PRIMARY)
        self.rect(M, y - 1, 3, 13)
        self.fill(INK)
        self.text(M + 10, y, 11.5, title, bold=True)

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
    """Styled vector certificate without ReportLab: same banner, table, sections, QR and stamp."""
    content = letter_content(exp)
    p = _RawPDF()
    company = exp.company.company_name
    band = 118
    p.fill(DEEP)
    p.rect(0, H - band, W, band)
    p.fill(PRIMARY)
    p.rect(W / 2, H - band, W / 2, band)  # two-tone banner instead of a gradient
    p.fill(GOLD)
    p.rect(0, H - band - 4, W * 0.3, 4)
    for x, letters in ((M, "IN"), (W - M - 60, (company[:1] if not ARABIC.search(company[:1]) else "C").upper())):
        p.fill((1, 1, 1))
        p.rect(x, H - band / 2 - 30, 60, 60)
        p.fill(PRIMARY)
        p.rect(x + 6, H - band / 2 - 24, 48, 48)
        p.fill((1, 1, 1))
        p.text(x + 30, H - band / 2 - 7, 19, letters, bold=True, align="center")
    p.fill((0.75, 0.83, 1))
    p.text(W / 2, H - 34, 8, "INTERNEST - OFFICIAL VERIFIED DOCUMENT", bold=True, align="center")
    p.fill((1, 1, 1))
    p.text(W / 2, H - 60, 15, content["title_en"], bold=True, align="center")
    p.fill((0.85, 0.9, 1))
    p.text(W / 2, H - 82, 8.5, f"Issued {content['issued']} - Ref. {exp.verification_code}", align="center")

    facts, row_h, label_w, t_top = content["facts"], 26, 140, H - band - 26
    p.fill(SOFT)
    p.rect(M, t_top - row_h * len(facts), label_w, row_h * len(facts))
    p.stroke(LINE, 0.8)
    p.rect(M, t_top - row_h * len(facts), W - 2 * M, row_h * len(facts), "S")
    for i, (label, value) in enumerate(facts):
        ry = t_top - (i + 1) * row_h
        if i:
            p.rect(M, ry + row_h, W - 2 * M, 0.01, "S")
        p.fill(MUTED)
        p.text(M + 12, ry + 9.5, 8, label)
        p.fill(INK)
        p.text(M + label_w + 12, ry + 9, 10.5, value, bold=True)
    for i, icon in enumerate(content["stars"]):
        p.star(W - M - 96 + i * 17, t_top - row_h * 4.5 - 1, 6, icon)

    y = t_top - row_h * len(facts) - 26
    p.section(y, "Recommendation")
    y -= 18
    for line in p.wrap(content["statement"], 10, W - 2 * M, limit=4):
        p.text(M, y, 10, line)
        y -= 14
    if content["scope"]:
        y -= 12
        p.section(y, "Gig Scope & Tasks Performed")
        y -= 18
        for line in p.wrap(content["scope"], 9.6, W - 2 * M, limit=7):
            p.text(M, y, 9.6, line)
            y -= 13.2
    y -= 14
    p.section(y, "Manager's Assessment & Feedback")
    y -= 10
    review = p.wrap(content["review"], 10, W - 2 * M - 52, limit=6)
    box_h = 34 + 14 * len(review)
    p.fill((0.94, 0.96, 1))
    p.rect(M, y - box_h, W - 2 * M, box_h)
    p.fill(PRIMARY)
    p.rect(M, y - box_h, 4, box_h)
    p.fill(INK)
    ty = y - 22
    for line in review:
        p.text(M + 36, ty, 10, line)
        ty -= 14
    p.fill(MUTED)
    signature = "- Hiring manager, issuing company" if ARABIC.search(company) else f"- {company}, hiring manager"
    p.text(W - M - 14, ty - 2, 8.5, signature, bold=True, align="right")

    p.stroke(LINE, 1)
    p.rect(M, 146, W - 2 * M, 0.01, "S")
    matrix = qr_matrix(verify_url)
    if matrix:
        n, size, x0, y0 = len(matrix), 104, M - 4, 30
        cell = size / (n + 4)
        p.fill((1, 1, 1))
        p.rect(x0, y0, size, size)
        p.fill(INK)
        for r, row in enumerate(matrix):
            for col, dark in enumerate(row):
                if dark:
                    p.rect(x0 + (col + 2) * cell, y0 + size - (r + 3) * cell, cell + 0.05, cell + 0.05)
    tx = M + 112
    p.fill(MUTED)
    p.text(tx, 124, 7.5, "VERIFICATION CODE")
    p.fill(INK)
    p.text(tx, 105, 17, exp.verification_code, bold=True)
    p.fill(MUTED)
    p.text(tx, 90, 7.5, "VERIFICATION ID")
    p.fill(INK)
    p.text(tx, 78, 8.2, str(exp.verification_uuid))
    p.fill(PRIMARY)
    p.text(tx, 62, 8, verify_url[:88])
    cx, cy, r = W - M - 46, 84, 44
    p.stroke(PRIMARY, 2.2)
    p.circle(cx, cy, r)
    p.stroke(PRIMARY, 0.8)
    p.circle(cx, cy, r - 5)
    p.fill(PRIMARY)
    p.text(cx, cy + r * 0.42, 8.5, "INTERNEST", bold=True, align="center")
    p.text(cx, cy + 1, 12.5, "VERIFIED", bold=True, align="center")
    p.text(cx, cy - r * 0.55, 7, "DOCUMENT", align="center")
    for dx in (-9, 0, 9):
        p.star(cx + dx, cy - r * 0.22, 3, "full")
    return p.bytes()


# ---------------------------------------------------------------- storage + delivery
def _letter_name(exp):
    return f"letter-{LETTER_VERSION}-{exp.verification_uuid}.pdf"


def generate_letter(exp, verify_url) -> bool:
    """Render and store the PDF for a verified gig. Never raises: the letter can always be rebuilt on download."""
    if tier_for(exp.star_rating) is None:
        return False
    try:
        pdf = render_letter(exp, verify_url)
    except Exception:
        logger.exception("Could not render recommendation letter %s", exp.pk)
        return False
    if exp.pdf_file:
        exp.pdf_file.delete(save=False)
    exp.pdf_file.save(_letter_name(exp), ContentFile(pdf), save=True)
    return True


def letter_bytes(exp, verify_url) -> bytes:
    """The letter as PDF bytes: stored file → fresh ReportLab render (stored) → styled fallback.

    Callers must only pass verified gigs (rating ≥ 3.5); see GigExperience.objects.verified().
    """
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
