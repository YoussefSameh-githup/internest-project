"""Homepage identity: the logo reveal, its fallbacks, the brand assets and the honest product claims."""
import pathlib
import re

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import translation


class LandingIdentityTests(TestCase):
    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.html = self.client.get(reverse("landing")).content.decode()

    def test_logo_reveal_is_an_accessible_image_with_its_tagline(self):
        self.assertIn('class="lp-intro" role="img" aria-label="Internest — Learn, Grow, Get Hired"', self.html)
        intro = self.html[self.html.index('class="lp-intro"'):]
        for part in ('class="bm-ribbon"', 'class="bm-head bm-head--a"', 'class="bm-star"', 'class="wm-inter"', 'class="wm-nest"',
                     'class="lp-intro__tagline"'):
            self.assertIn(part, intro[:9000], part)
        self.assertEqual(self.html.count("<h1"), 1)

    def test_page_only_extras_load_on_the_homepage(self):
        self.assertIn('<body class="lp">', self.html)
        self.assertIn("css/landing", self.html)
        self.assertRegex(self.html, r'<script nonce="[A-Za-z0-9_-]{22}">\s*try \{ if \(sessionStorage')  # replay once per session
        other = self.client.get(reverse("privacy")).content.decode()
        self.assertNotIn("css/landing", other)
        self.assertIn('<body class="">', other)

    def test_gradient_ids_are_unique_on_the_page(self):
        ids = re.findall(r'<linearGradient id="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertGreaterEqual(len(ids), 3)  # nav, hero and footer marks

    def test_social_card_and_icons(self):
        for expected in ('property="og:image" content="http://testserver/static/img/brand/og-image', 'property="og:image:width" content="1200"',
                         'name="twitter:card" content="summary_large_image"', 'rel="icon" type="image/svg+xml"',
                         'rel="apple-touch-icon" href="/static/img/brand/apple-touch-icon'):
            self.assertIn(expected, self.html)
        for name in ("og-image.png", "favicon-32.png", "apple-touch-icon.png", "icon-192.png", "internest-mark.svg"):
            self.assertTrue(finders.find(f"img/brand/{name}"), name)

    def test_every_call_to_action_resolves(self):
        for name in ("login_or_signup", "startup_register", "university_landing", "list", "trust"):
            self.assertIn(f'href="{reverse(name)}"', self.html, name)
        for anchor in ("features", "how-it-works", "startups", "universities"):
            self.assertIn(f'id="{anchor}"', self.html)

    def test_arabic_page_is_rtl_and_fully_translated(self):
        self.client.cookies["internest_lang"] = "ar"
        html = self.client.get(reverse("landing")).content.decode()
        self.assertIn('dir="rtl"', html)
        for english in ("A CV can claim anything", "Built on evidence", "Hire verified students", "Get Hired"):
            self.assertNotIn(english, html, english)


class LandingMotionStylesheetTests(SimpleTestCase):
    """The reveal may only animate inside prefers-reduced-motion: no-preference; the static logo is the default state."""

    def test_animations_are_opt_in_for_motion_and_skippable(self):
        css = pathlib.Path(finders.find("css/landing.css")).read_text(encoding="utf-8")
        block_start = css.index("@media (prefers-reduced-motion: no-preference)")
        uses = [m.start() for m in re.finditer(r"\banimation:", css)]
        self.assertTrue(uses)
        block = css[block_start:css.index("@keyframes", block_start)]
        self.assertEqual(len(re.findall(r"\banimation:", block)), len(uses))  # every animation lives in the guarded block
        self.assertIn("html:not(.lp-intro-seen)", block)
        self.assertNotIn("opacity: 0;", css[:block_start])  # nothing is hidden outside the animation itself
