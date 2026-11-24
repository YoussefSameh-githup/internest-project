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
        for part in ('class="brand-lockup"', 'class="bm-ribbon"', 'class="bm-head bm-head--a"', 'class="bm-star"',
                     'class="wm-intern"', 'class="wm-intern-n"', 'class="wm-nest"', 'class="bl-spark"', 'class="lp-intro__tagline"'):
            self.assertIn(part, intro[:12000], part)
        self.assertEqual(self.html.count("<h1"), 1)

    def test_intern_and_nest_share_their_n(self):
        """The reveal's idea: "Intern" + "nest" overlap on one "n". The white n of "Intern" and the first (orange) n of
        "nest" must sit on exactly the same spot in the final layout, or the merged word would show a double n."""
        lockup = self.html[self.html.index('class="brand-lockup"'):]
        intern = lockup[lockup.index('class="wm-intern"'):lockup.index('class="wm-nest"')]
        nest = lockup[lockup.index('class="wm-nest"'):]
        shared = re.search(r'class="wm-intern-n" transform="translate\(([\d.]+) 0\)" d="([^"]+)"', intern)
        first_nest = re.search(r'<path transform="translate\(([\d.]+) 0\)" d="([^"]+)"', nest)
        self.assertEqual(shared.groups(), first_nest.groups())
        self.assertEqual(len(re.findall(r"<path ", intern)), 6)  # I n t e r n
        self.assertEqual(len(re.findall(r"<path ", nest[:nest.index("</g>")])), 4)  # n e s t
        for var in ("--intern-from-x:", "--intern-from-y:", "--nest-from-x:", "--nest-from-y:", "--sep:"):
            self.assertIn(var, lockup[:600])

    def test_page_only_extras_load_on_the_homepage(self):
        self.assertIn('<body class="lp">', self.html)
        self.assertIn("css/landing", self.html)
        self.assertNotIn("sessionStorage", self.html)  # the reveal needs no script: it plays on each homepage load
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
        reduce_uses = len(re.findall(r"\banimation:", css[css.index("@media (prefers-reduced-motion: reduce)"):]))
        self.assertEqual(len(re.findall(r"\banimation:", block)) + reduce_uses, len(uses))  # no unguarded animation
        reduce = css.index("@media (prefers-reduced-motion: reduce)")
        self.assertIn("lp-fade", css[reduce:reduce + 200])  # reduced motion: the finished logo fades in, nothing moves
        hidden = re.findall(r"([^{}]+)\{[^{}]*opacity: 0;", re.sub(r"/\*.*?\*/", "", css[:block_start], flags=re.S))
        self.assertEqual([h.strip() for h in hidden], [".lp-intro .wm-intern-n", ".lp-intro .bl-spark"])  # covered / decorative only
        for name in ("lp-draw", "lp-intern", "lp-nest", "lp-handover", "lp-spark", "lp-tag", "lp-ambient"):
            self.assertIn(f"@keyframes {name}", css)


class BrandChromeStylesTests(SimpleTestCase):
    def test_phone_drawer_is_dark_on_every_page(self):
        """The phone drawer opens white inside the 900px media query; the brand rule (same specificity, later in the
        file) must win, or the dark nav shows pale links on white (found on the live site)."""
        css = pathlib.Path(finders.find("css/styles.css")).read_text(encoding="utf-8")
        white = css.index(".nav.is-open .nav__links {")
        brand = css.index(".nav.is-open .nav__links { background: var(--ink-2)")
        self.assertGreater(brand, white)
        self.assertIn(".nav.is-open .nav__toggle", css[brand - 400:brand])

    def test_old_palette_and_logo_are_gone(self):
        root = pathlib.Path(settings.BASE_DIR)
        old = re.compile(r"#(0b1e4d|112c6b|1d3a8a|2746a8|3b6fe0|f59e0b|d97706)\b|internest_logo", re.I)
        hits = [p.relative_to(root).as_posix() for pattern in ("internest_*/templates/**/*.html", "internest_*/static/**/*.css")
                for p in root.glob(pattern) if old.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])
