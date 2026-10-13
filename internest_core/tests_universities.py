"""/universities/ landing page: rendering, entry points, demo/pilot booking log and no online checkout for universities."""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from .models import PartnerProfile, UniversityInquiry

URL = reverse("university_landing")
INQUIRY = reverse("university_inquiry")
FORM = {"name": "Dr. Mona Adel", "role": "Vice Dean", "uni": "Cairo University", "fac": "Commerce",
        "email": "mona@cu.edu.eg", "phone": "", "msg": "Pilot for two faculties", "channel": "mail"}


class UniversityLandingTests(TestCase):
    def setUp(self):
        cache.clear()
        translation.activate("en")

    def test_page_renders_with_site_config(self):
        self.assertEqual(URL, "/universities/")
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "landing/universities.html")
        for marker in ('lang="ar" dir="rtl"', "احجز عرض تعريفي", "تيرم Pilot مجاني", 'email: "engineerthe973@gmail.com"',
                       'whatsapp: "201113999387"', 'phoneDisplay: "0111 399 9387"', 'phoneTel: "+201113999387"',
                       f'inquiryUrl: "{INQUIRY}"', "/static/img/internest_logo.jpg", 'href="/privacy/"', 'name="website"'):
            self.assertContains(page, marker)
        self.assertNotContains(page, "data:image/webp;base64")  # the 52 KB inline logo is replaced by the static file
        self.assertNotContains(page, "/checkout/")

    def test_english_follows_the_site_language(self):
        self.client.cookies["internest_lang"] = "en"
        page = self.client.get(URL)
        self.assertContains(page, 'lang="en" dir="ltr"')
        self.assertContains(page, 'let lang = "en";')

    def test_home_card_nav_and_footer_lead_to_the_page(self):
        self.client.cookies["internest_lang"] = "en"
        page = self.client.get(reverse("landing"))
        card = page.content.decode()
        card = card[card.index('id="universities"'):]
        self.assertIn(f'href="{URL}" class="btn btn--outline btn--lg" data-university-cta', card[:3000])
        self.assertContains(page, f'<a href="{URL}" class="nav__link">')
        self.assertContains(page, f'<li><a href="{URL}">Internest for universities</a></li>')

    def test_booking_requests_are_logged(self):
        resp = self.client.post(INQUIRY, FORM)
        self.assertEqual(resp.json(), {"ok": True})
        inquiry = UniversityInquiry.objects.get()
        self.assertEqual((inquiry.university, inquiry.position, inquiry.channel, inquiry.status),
                         ("Cairo University", "Vice Dean", "mail", "new"))

    def test_booking_validation_honeypot_and_rate_limit(self):
        self.assertEqual(self.client.post(INQUIRY, {**FORM, "email": "", "phone": ""}).status_code, 400)
        self.assertEqual(self.client.post(INQUIRY, {**FORM, "uni": ""}).status_code, 400)
        self.assertEqual(self.client.post(INQUIRY, {**FORM, "website": "http://spam"}).json(), {"ok": True})
        self.assertFalse(UniversityInquiry.objects.exists())
        self.assertEqual(self.client.get(INQUIRY).status_code, 405)
        for _ in range(10):
            self.client.post(INQUIRY, FORM)
        self.assertEqual(self.client.post(INQUIRY, FORM).status_code, 429)
        self.assertEqual(UniversityInquiry.objects.count(), 10)

    def test_universities_cannot_reach_paid_checkout(self):
        user = User.objects.create_user("cairo-uni", password="pw")
        PartnerProfile.objects.create(user=user, company_name="Cairo University", partner_code="U9", is_academic=True,
                                      is_fully_verified=True)
        self.client.force_login(user)
        for name in ("checkout", "startup_upgrade"):
            with self.subTest(page=name):
                resp = self.client.get(reverse(name))
                self.assertEqual(resp.status_code, 302)
                self.assertNotIn("fawaterk", resp.get("Location", ""))
