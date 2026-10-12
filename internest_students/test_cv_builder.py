"""CV builder links: tracking parameters stripped, clean display text, clickable in the PDF and the preview."""
import io

from django.test import SimpleTestCase
from django.urls import reverse
from pypdf import PdfReader

from internest_skills.tests import Base

from .cv_builder_service import CleanURL, clean_cv, clean_url, contact_items
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
