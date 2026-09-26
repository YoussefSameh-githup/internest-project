import sys
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

sys.path.insert(0, str(Path(settings.BASE_DIR) / "scripts"))
import i18n_catalog  # noqa: E402


class LanguageAndDirectionTests(TestCase):
    def _get(self, lang, url_name="landing"):
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = lang
        return self.client.get(reverse(url_name))

    def test_arabic_is_default_and_rtl(self):
        resp = self.client.get(reverse("landing"))
        self.assertContains(resp, '<html lang="ar" dir="rtl">')
        self.assertContains(resp, "عمل حقيقي مع شركات ناشئة حقيقية")
        self.assertContains(resp, "bootstrap.rtl.min.css")

    def test_english_is_ltr_with_english_copy(self):
        resp = self._get("en")
        self.assertContains(resp, '<html lang="en" dir="ltr">')
        self.assertContains(resp, "Real work with real startups,")
        self.assertNotContains(resp, "bootstrap.rtl.min.css")

    def test_toggle_offers_the_other_language(self):
        self.assertContains(self._get("ar"), 'name="language" type="hidden" value="en"')
        self.assertContains(self._get("en"), 'name="language" type="hidden" value="ar"')

    def test_toggle_switches_language_and_returns_to_page(self):
        resp = self.client.post(reverse("set_language"), {"language": "en", "next": reverse("list")})
        self.assertRedirects(resp, reverse("list"), fetch_redirect_response=False)
        self.assertEqual(resp.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")

    def test_only_arabic_and_english_are_offered(self):
        self.assertEqual([code for code, _name in settings.LANGUAGES], ["ar", "en"])

    def test_copy_reflects_marketplace_not_summer_internships(self):
        for lang in ("ar", "en"):
            body = self._get(lang).content.decode()
            self.assertNotIn("summer", body.lower())
            self.assertNotIn("صيف", body)
        english = self._get("en").content.decode()
        for phrase in ("Gigs", "Freelance projects", "Part-time roles", "For startups", "For universities", "NAQAAE"):
            self.assertIn(phrase, english)


class TranslationCoverageTests(TestCase):
    def test_every_ui_string_has_an_arabic_translation(self):
        msgids = i18n_catalog.collect_msgids()
        self.assertGreater(len(msgids), 300)
        with translation.override("ar"):
            untranslated = sorted(m for m in msgids if translation.gettext(m) == m and any(c.isalpha() for c in m))
        # Brand/product names are intentionally identical in both languages.
        allowed = {"Internest Pro", "LinkedIn", "Facebook", "Twitter", "Instagram"}
        self.assertEqual([m for m in untranslated if m not in allowed], [])

    def test_no_hardcoded_arabic_in_python_ui_code(self):
        import re
        arabic = re.compile("[؀-ۿ]")
        for path in ("internest_core/views.py", "internest_core/forms.py", "internest_skills/views.py"):
            code = (Path(settings.BASE_DIR) / path).read_text(encoding="utf-8")
            strings = re.findall(r"""["']([^"'\n]*)["']""", "\n".join(
                line for line in code.splitlines() if not line.lstrip().startswith("#")))
            self.assertEqual([s for s in strings if arabic.search(s)], [], path)
