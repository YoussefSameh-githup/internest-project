"""Diagnose recommendation-letter rendering on this server: python manage.py letters_doctor"""
import datetime
import importlib
import os
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check the libraries, font and media folder used by verified recommendation letters."

    def handle(self, *args, **options):
        from internest_core import letters

        ok = True
        for module, why in (("reportlab", "full letter design"), ("qrcode", "QR codes"),
                            ("arabic_reshaper", "Arabic text in letters"), ("bidi", "Arabic text in letters"), ("PIL", "QR image on the page")):
            try:
                mod = importlib.import_module(module)
                self.stdout.write(self.style.SUCCESS(f"✓ {module} {getattr(mod, '__version__', getattr(mod, 'Version', ''))}"))
            except ImportError:
                ok = False
                self.stdout.write(self.style.ERROR(f"✗ {module} missing ({why}) → pip install -r requirements.txt"))
        fonts = [p for pair in letters.FONT_CANDIDATES for p in pair[:1]] + [getattr(settings, "PDF_FONT_PATH", "")]
        found = next((p for p in fonts if p and os.path.exists(p)), None)
        if found:
            self.stdout.write(self.style.SUCCESS(f"✓ Arabic-capable font: {found}"))
        else:
            self.stdout.write(self.style.WARNING("! No Arabic-capable font found: set PDF_FONT_PATH to a .ttf with Arabic glyphs"))
        letters_dir = os.path.join(settings.MEDIA_ROOT, "letters")
        writable = os.access(letters_dir if os.path.isdir(letters_dir) else settings.MEDIA_ROOT, os.W_OK)
        self.stdout.write((self.style.SUCCESS if writable else self.style.WARNING)(
            f"{'✓' if writable else '!'} media folder {'writable' if writable else 'not writable (letters are still served, just not cached)'}"))

        sample = SimpleNamespace(
            student=SimpleNamespace(user=SimpleNamespace(get_full_name=lambda: "Sample Student", username="sample")),
            company=SimpleNamespace(company_name="Sample Co", logo=None, pk=0), gig_title="Sample gig", star_rating=5,
            completion_date=datetime.date.today(), created_at=None, review_text="Great work.", verification_code="TEST1234",
        )
        try:
            letters.render_letter(sample, "https://example.com/verify/test/")
            self.stdout.write(self.style.SUCCESS("✓ Full ReportLab letter renders"))
        except Exception as exc:  # noqa: BLE001 - reporting the reason is the point
            ok = False
            self.stdout.write(self.style.ERROR(f"✗ Full letter failed ({type(exc).__name__}: {exc}); the built-in styled certificate is served instead"))
        self.stdout.write(self.style.SUCCESS("All good.") if ok else self.style.WARNING("Fix the items above, then Reload the web app."))
