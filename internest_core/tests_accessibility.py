"""Phase 6: structural accessibility checks on rendered pages (WCAG 2.2 AA basics), in Arabic and English.

Colour contrast and layout overflow need a browser and were measured in the Phase 6 sweep (see reports/); these tests
pin what the HTML itself must always carry: language/direction, a skip link to <main>, one <h1>, alt text, labelled
form fields, named buttons and links, and an accessible menu button.
"""
import re
from html.parser import HTMLParser

from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, StudentProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.h1, self.imgs_no_alt, self.labels_for, self.fields = [], 0, [], set(), []
        self.controls, self.html_attrs, self.main_id, self.skip_href, self.mains = [], {}, None, None, 0
        self._text_for = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.html_attrs = a
        if tag == "h1":
            self.h1 += 1
        if tag == "img" and "alt" not in a:
            self.imgs_no_alt.append(a.get("src", "")[-40:])
        if tag == "label":
            if a.get("for"):
                self.labels_for.add(a["for"])
            self.stack.append("label")
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button") and a.get("aria-hidden") != "true":
            self.fields.append({"id": a.get("id"), "name": a.get("name"), "aria": a.get("aria-label") or a.get("aria-labelledby") or a.get("title"),
                                "wrapped": "label" in self.stack})
        if tag in ("button", "a"):
            self.controls.append({"tag": tag, "aria": a.get("aria-label") or a.get("title"), "text": "", "img_alt": False,
                                  "hidden": a.get("aria-hidden") == "true" or "hidden" in a, "href": a.get("href")})
            self._text_for.append(self.controls[-1])
        if tag == "img" and self._text_for and a.get("alt"):
            self._text_for[-1]["img_alt"] = True
        if tag == "main":
            self.main_id = a.get("id")
            self.mains += 1
        if tag == "a" and "skip-link" in (a.get("class") or ""):
            self.skip_href = a.get("href")

    def handle_endtag(self, tag):
        if tag == "label" and "label" in self.stack:
            self.stack.remove("label")
        if tag in ("button", "a") and self._text_for:
            self._text_for.pop()

    def handle_data(self, data):
        for c in self._text_for:
            c["text"] += data.strip()


def audit(html, lang=None):
    p = _Page()
    p.feed(html)
    problems = []
    if not p.html_attrs.get("lang") or p.html_attrs.get("dir") not in ("rtl", "ltr"):
        problems.append("html lang/dir")
    if lang and (p.html_attrs.get("lang", "")[:2] != lang or p.html_attrs.get("dir") != ("rtl" if lang == "ar" else "ltr")):
        problems.append(f"lang/dir {p.html_attrs} for {lang}")
    if p.mains != 1:
        problems.append(f"{p.mains} <main> elements")
    if p.h1 != 1:
        problems.append(f"{p.h1} h1")
    if p.imgs_no_alt:
        problems.append(f"img without alt: {p.imgs_no_alt}")
    if p.main_id != "main" or p.skip_href != "#main":
        problems.append("skip link / main#main")
    for f in p.fields:
        if not (f["aria"] or f["wrapped"] or (f["id"] and f["id"] in p.labels_for)):
            problems.append(f"unlabelled field {f['name']}")
    for c in p.controls:
        if not c["hidden"] and c["href"] != "#main" and not (c["text"] or c["aria"] or c["img_alt"]):
            problems.append(f"unnamed {c['tag']}")
    return problems


class AccessibilityTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        StudentProfile.objects.filter(pk=self.student.pk).update(talent_pool_visible=True)
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit",
                                    status="verified", score=90, verified_via="challenge", verified_at=timezone.now())
        self.app = Application.objects.create(internship=self.gig, applicant=self.user)

    def _check(self, urls):
        for lang in ("ar", "en"):
            self.client.cookies["internest_lang"] = lang
            for url in urls:
                with self.subTest(url=url, lang=lang):
                    resp = self.client.get(url)
                    self.assertEqual(resp.status_code, 200)
                    self.assertEqual(audit(resp.content.decode(), lang), [])

    def test_public_pages(self):
        self._check([reverse(n) for n in ("landing", "trust", "privacy", "terms", "refund_policy", "contact", "login",
                                          "login_or_signup", "startup_register")]
                    + [reverse("internship_detail", args=[self.gig.pk])])

    def test_student_pages(self):
        self.login_student(self.user)
        self._check([reverse(n) for n in ("list", "profile", "my_applications", "skills_hub", "student_dashboard")])

    def test_company_pages(self):
        self.client.force_login(self.founder)
        self._check([reverse("partner_dashboard"), reverse("partner_dashboard_section", args=["applicants"]),
                     reverse("partner_applicant", args=[self.app.pk]), reverse("talent_pool_preview"), reverse("startup_upgrade"),
                     reverse("checkout"), reverse("startup_company_profile"), reverse("lounge_feed")])

    def test_menu_button_is_accessible(self):
        html = self.client.get(reverse("landing")).content.decode()
        self.assertRegex(html, r'id="navToggle"[^>]*aria-label="[^"]+"[^>]*aria-expanded="false"[^>]*aria-controls="siteNavLinks"')
        self.assertIn('id="siteNavLinks"', html)
        for js in ("setAttribute('aria-expanded'", "e.key === 'Escape'", "toggle.focus()"):
            self.assertIn(js, html)  # the menu state is announced and Escape closes it
        self.assertRegex(html, r'class="nav__brand" aria-label="Internest"')  # named even when the brand text is hidden

    def test_reduced_motion_and_focus_rules_ship(self):
        from django.contrib.staticfiles import finders

        css = open(finders.find("css/styles.css"), encoding="utf-8").read()
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(".ia-spinner, .sx-spin, .cvb-spin, .fl-spin", css)  # progress stays visible under reduced motion
        self.assertRegex(css, r":focus-visible\s*\{\s*outline: 3px solid")
        self.assertIn("--color-success:   #15803d", css)  # AA as text and behind white text
