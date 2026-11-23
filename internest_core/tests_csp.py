"""Nonce-based Content-Security-Policy: no 'unsafe-inline' scripts, a fresh nonce per response, and templates that
carry it on every inline <script> and use no inline event handlers."""
import pathlib
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from internest_skills.models import StudentSkill
from internest_skills.tests import Base

NONCE = re.compile(r"'nonce-([A-Za-z0-9_-]{22})'")
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.I)
DATA_TYPES = ("application/json", "application/ld+json")
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.I)
JS_URL = re.compile(r"""(?:href|src|action|formaction)\s*=\s*["']\s*javascript:""", re.I)


def header_nonce(response):
    csp = response["Content-Security-Policy"]
    script_src = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
    match = NONCE.search(script_src)
    return match.group(1) if match else None, script_src


def inline_script_attrs(html):
    """The attribute strings of every executable inline <script> (no src=, not a JSON data block)."""
    found = []
    for attrs in SCRIPT_TAG.findall(html):
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        kind = re.search(r"""\btype\s*=\s*["']([^"']+)""", attrs, re.I)
        if kind and kind.group(1).lower() in DATA_TYPES:
            continue
        found.append(attrs)
    return found


class NonceAssertions:
    def assert_nonced(self, response):
        self.assertEqual(response.status_code, 200)
        nonce, script_src = header_nonce(response)
        self.assertIsNotNone(nonce, script_src)
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertNotIn("'unsafe-eval'", script_src)
        html = response.content.decode()
        scripts = inline_script_attrs(html)
        for attrs in scripts:
            self.assertIn(f'nonce="{nonce}"', attrs, f"inline <script{attrs}> lacks this response's nonce")
        self.assertIsNone(INLINE_HANDLER.search(html), "inline event handler in the rendered page")
        if re.search(r"\sdata-(confirm|print|autosubmit)\b", html):
            self.assertIn("js/ui_behaviors", html)  # the replacement for the inline handlers is loaded
        return nonce, len(scripts)


class PublicPagesNonceTests(NonceAssertions, Base):
    def test_landing_login_and_signup(self):
        for name in ("landing", "login", "signup", "university_landing"):
            with self.subTest(page=name):
                self.assert_nonced(self.client.get(reverse(name)))

    def test_every_response_gets_a_unique_nonce(self):
        nonces = {header_nonce(self.client.get(reverse("login")))[0] for _ in range(5)}
        self.assertEqual(len(nonces), 5)

    def test_a_cached_public_page_gets_a_fresh_nonce_that_matches_its_header(self):
        first = self.client.get(reverse("landing"))
        second = self.client.get(reverse("landing"))
        self.assertEqual(second.get("X-Page-Cache"), "HIT")
        n1, _ = self.assert_nonced(first)
        n2, count = self.assert_nonced(second)
        self.assertNotEqual(n1, n2)
        self.assertNotIn(n1, second.content.decode())  # the first visitor's nonce never reaches the second one
        self.assertNotIn("__INTERNEST_CSP_NONCE__", second.content.decode())
        self.assertGreater(count, 0)


class SignedInPagesNonceTests(NonceAssertions, Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_dashboard_cv_analysis_and_resume_builder(self):
        for name in ("student_dashboard", "skills_hub", "cv_builder"):  # the dashboard includes market fit
            with self.subTest(page=name):
                self.assert_nonced(self.client.get(reverse(name)))

    def test_assessment_page(self):
        ss = StudentSkill.objects.create(student=self.student, skill=self.skill, source=StudentSkill.SOURCE_EXPLICIT)
        start = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assert_nonced(self.client.get(start.url))

    def test_company_pages(self):
        self.client.force_login(self.pro_user)
        for name in ("partner_dashboard", "talent_pool"):
            with self.subTest(page=name):
                self.assert_nonced(self.client.get(reverse(name)))


class PolicyHeaderTests(Base):
    def test_api_and_error_responses_carry_a_nonced_policy_without_unsafe_inline(self):
        for response in (self.client.get("/definitely-not-a-page/"), self.client.get(reverse("healthz"))):
            nonce, script_src = header_nonce(response)
            self.assertIsNotNone(nonce)
            self.assertNotIn("'unsafe-inline'", script_src)

    def test_admin_keeps_its_own_policy(self):
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        csp = self.client.get(reverse("admin:index"))["Content-Security-Policy"]
        self.assertIn("'unsafe-eval'", csp)
        self.assertNotIn("'nonce-", csp)  # a nonce would make browsers ignore the admin's 'unsafe-inline'

    @override_settings(CSP_ENABLED=False)
    def test_the_switch_still_turns_the_header_off(self):
        self.assertNotIn("Content-Security-Policy", self.client.get(reverse("login")))


class TemplateSourceTests(SimpleTestCase):
    """Static scan of every app template: what the browser would refuse under this policy must not be there."""

    def templates(self):
        root = pathlib.Path(settings.BASE_DIR)
        paths = sorted(root.glob("internest_*/templates/**/*.html"))
        self.assertGreater(len(paths), 100)
        return [(p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")) for p in paths]

    def test_no_inline_event_handlers_or_javascript_urls(self):
        for rel, text in self.templates():
            with self.subTest(template=rel):
                self.assertIsNone(INLINE_HANDLER.search(text), rel)
                self.assertIsNone(JS_URL.search(text), rel)

    def test_every_inline_script_carries_the_nonce(self):
        for rel, text in self.templates():
            for attrs in inline_script_attrs(text):
                with self.subTest(template=rel):
                    self.assertIn('nonce="{{ csp_nonce }}"', attrs, rel)
