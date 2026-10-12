"""CV builder links: tracking parameters stripped, clean display text, clickable in the PDF and the preview."""
import datetime
import io

from django.test import SimpleTestCase
from django.urls import reverse
from pypdf import PdfReader

from internest_skills.tests import Base

from . import cv_builder_service as svc
from .cv_builder_service import CleanURL, clean_cv, clean_url, contact_items, format_period, month_year
from .cv_pdf import render_cv_pdf

PROFILE = "https://linkedin.com/in/youssef-sameh-448719256"
MESSY = ("https://www.linkedin.com/in/youssef-sameh-448719256/?utm_source=share&utm_campaign=share_via"
         "&utm_content=profile&utm_medium=android_app&trackingId=abc%3D%3D&lipi=urn%3Ali#experience")


class CleanUrlTests(SimpleTestCase):
    def test_strips_tracking_parameters_and_builds_display_text(self):
        self.assertEqual(clean_url(MESSY), CleanURL(PROFILE, "linkedin.com/in/youssef-sameh-448719256"))

    def test_normalizes_common_messy_inputs(self):
        cases = {
            "linkedin.com/in/youssef-sameh-448719256": PROFILE,
            "  http://eg.linkedin.com/in/youssef-sameh-448719256?trk=public_profile ": PROFILE,
            "WWW.LinkedIn.com/in/youssef-sameh-448719256//": PROFILE,
            "https://github.com/youssef?tab=repositories&utm_source=cv&fbclid=x": "https://github.com/youssef?tab=repositories",
            "https://youssef.dev/portfolio?ref=linkedin&si=abc&gclid=1": "https://youssef.dev/portfolio",
        }
        for raw, href in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_url(raw).href_url, href)
        self.assertEqual(clean_url("https://github.com/youssef?tab=repositories&utm_source=cv").display_url,
                         "github.com/youssef?tab=repositories")

    def test_invalid_input_is_empty(self):
        for raw in ("", None, "not a url", "javascript:alert(1)", "https://exa mple.com", "example.com:abc", 42):
            with self.subTest(raw=raw):
                self.assertEqual(clean_url(raw), CleanURL("", ""))

    def test_clean_cv_stores_sanitized_links(self):
        cv = clean_cv({"linkedin": MESSY, "projects": [{"title": "P", "link": "github.com/y/p?utm_medium=x"}]})
        self.assertEqual(cv["linkedin"], PROFILE)
        self.assertEqual(cv["projects"][0]["link"], "https://github.com/y/p")

    def test_contact_items_give_display_and_href(self):
        items = contact_items({"email": "y@example.com", "phone": "+20 1", "linkedin": MESSY, "github": ""})
        self.assertEqual(items, [
            {"text": "y@example.com", "href": "mailto:y@example.com"},
            {"text": "+20 1", "href": ""},
            {"text": "linkedin.com/in/youssef-sameh-448719256", "href": PROFILE},
        ])


class PdfLinkTests(Base):
    CV = {"name": "Youssef Sameh", "email": "y@example.com", "linkedin": MESSY, "github": "github.com/youssef?utm_source=x",
          "projects": [{"title": "Internest", "link": "https://internest.example/?utm_campaign=cv"}]}

    def test_pdf_header_shows_clean_text_and_links_to_sanitized_url(self):
        reader = PdfReader(io.BytesIO(render_cv_pdf(clean_cv(self.CV))))
        page = reader.pages[0]
        text = page.extract_text()
        self.assertIn("linkedin.com/in/youssef-sameh-448719256", text)
        self.assertIn("github.com/youssef", text)
        self.assertNotIn("utm_", text)
        self.assertNotIn("https://", text)  # only domain + path is printed
        uris = {a.get_object()["/A"]["/URI"] for a in page.get("/Annots", [])}
        self.assertEqual(uris, {"mailto:y@example.com", PROFILE, "https://github.com/youssef", "https://internest.example"})

    def test_preview_links(self):
        self.client.force_login(self.student_user)
        self.client.post(reverse("cv_builder_save"), {"cv": self.CV}, content_type="application/json")
        page = self.client.get(reverse("cv_builder_preview"))
        self.assertContains(page, f'<a href="{PROFILE}">linkedin.com/in/youssef-sameh-448719256</a>', html=True)
        self.assertContains(page, '<a href="https://internest.example">internest.example</a>', html=True)
        self.assertNotContains(page, "utm_")


class EducationDateTests(SimpleTestCase):
    def test_month_year_accepts_common_formats(self):
        cases = {"2023-09": "2023-09", "9/2023": "2023-09", "Sep 2023": "2023-09", "September 2023": "2023-09",
                 "سبتمبر 2023": "2023-09", "2027": "2027", " 2027 (expected) ": "2027", "13/2023": "2023",
                 "": "", "soon": "", None: "", "1850": ""}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(month_year(raw), expected)

    def test_format_period(self):
        today = datetime.date(2026, 10, 13)
        self.assertEqual(format_period("2023-09", "", True, today), "Sep 2023 – Present")
        self.assertEqual(format_period("2023", "2027", False, today), "2023 – 2027 (Expected)")
        self.assertEqual(format_period("2026-03", "2026-08", False, today), "Mar 2026 – Aug 2026")
        self.assertEqual(format_period("", "2026", False, today), "2026 (Expected)")  # year only counts as December
        self.assertEqual(format_period("", "2025", False, today), "2025")
        self.assertEqual(format_period("2022-10", "", False, today), "Oct 2022")
        self.assertEqual(format_period("", "", False, today), "")

    def test_clean_cv_education_and_backward_compatibility(self):
        cv = clean_cv({"education": [
            {"university": "Cairo University", "major": "CS", "start_date": "Sep 2023", "end_date": "June 2027"},
            {"university": "AUC", "start_date": "2024", "end_date": "2026", "is_current": True},
            {"university": "Ain Shams", "graduation_year": "2025"},  # CV saved before the date fields existed
            {"university": "Helwan", "is_current": "yes"},
        ]})
        self.assertEqual(cv["education"], [
            {"university": "Cairo University", "major": "CS", "start_date": "2023-09", "end_date": "2027-06", "is_current": False},
            {"university": "AUC", "major": "", "start_date": "2024", "end_date": "", "is_current": True},
            {"university": "Ain Shams", "major": "", "start_date": "", "end_date": "2025", "is_current": False},
            {"university": "Helwan", "major": "", "start_date": "", "end_date": "", "is_current": False},
        ])
        self.assertEqual(clean_cv({"education": [{"is_current": True}]})["education"], [])


class EducationRenderTests(Base):
    CV = {"name": "Youssef Sameh", "education": [
        {"university": "Cairo University", "major": "Computer Science", "start_date": "2023-09", "end_date": "2027-06"},
        {"university": "ITI", "major": "Web Development", "start_date": "2025-03", "is_current": True}]}

    def test_pdf_and_preview_show_periods(self):
        text = PdfReader(io.BytesIO(render_cv_pdf(clean_cv(self.CV)))).pages[0].extract_text()
        self.assertIn("Computer Science | Sep 2023 – Jun 2027 (Expected)", text)
        self.assertIn("Web Development | Mar 2025 – Present", text)
        self.client.force_login(self.student_user)
        self.client.post(reverse("cv_builder_save"), {"cv": self.CV}, content_type="application/json")
        page = self.client.get(reverse("cv_builder_preview"))
        self.assertContains(page, "Computer Science | Sep 2023 – Jun 2027 (Expected)")

    def test_builder_has_date_pickers_and_prompt_asks_for_dates(self):
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("cv_builder"))
        for marker in ('data-date="start_date"', 'data-date="end_date"', 'data-flag="is_current"', "bindDate"):
            self.assertContains(page, marker)
        self.assertNotContains(page, "graduation_year")
        self.assertIn('"start_date":"YYYY-MM or YYYY"', svc._POLISH_PROMPT)
        self.assertIn("is_current", svc._POLISH_PROMPT)

    def test_arabic_labels(self):
        self.client.force_login(self.student_user)
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(reverse("cv_builder"))
        self.assertContains(page, "تاريخ البداية")
        self.assertContains(page, "تاريخ النهاية / المتوقع")
