"""Search signals (internest_core/seo.py): canonical origin, unique descriptions, index rules, JSON-LD."""
import json
import re

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core import seo
from internest_core.models import Internship, PartnerProfile

LD = re.compile(r'<script type="application/ld\+json" nonce="[^"]*">(.*?)</script>', re.S)


def ld_blocks(response):
    return [json.loads(raw) for raw in LD.findall(response.content.decode())]


@override_settings(SITE_URL="https://internest.app", ALLOWED_HOSTS=["testserver", "www.internest.app", "internest.app"])
class CanonicalOriginTests(TestCase):
    def test_canonical_and_og_use_site_url_not_the_host_header(self):
        html = self.client.get(reverse("terms"), HTTP_HOST="www.internest.app").content.decode()
        self.assertIn(f'<link rel="canonical" href="https://internest.app{reverse("terms")}">', html)
        self.assertIn(f'<meta property="og:url" content="https://internest.app{reverse("terms")}">', html)
        self.assertIn('content="https://internest.app/static/', html)  # og:image
        self.assertNotIn("www.internest.app", html)

    def test_robots_and_sitemap_point_at_the_canonical_origin(self):
        self.assertIn("Sitemap: https://internest.app/sitemap.xml", self.client.get("/robots.txt", HTTP_HOST="www.internest.app").content.decode())
        self.assertIn("<loc>https://internest.app/</loc>", self.client.get("/sitemap.xml", HTTP_HOST="www.internest.app").content.decode())


class IndexRulesTests(TestCase):
    def test_robots_txt_keeps_crawlers_out_of_private_areas_only(self):
        body = self.client.get("/robots.txt").content.decode()
        for prefix in ("/oauth/", "/accounts/", "/app/", "/partner/", "/api/", "/media/", "/checkout/", "/staff/"):
            self.assertIn(f"Disallow: {prefix}\n", body)
        for public in ("/internship/", "/universities/", "/trust/", "/verify/"):
            self.assertNotIn(f"Disallow: {public}", body)  # /verify/ must be crawlable so its noindex is seen

    def test_sitemap_has_only_indexable_pages(self):
        body = self.client.get("/sitemap.xml").content.decode()
        self.assertIn(f"<loc>http://testserver{reverse('trust')}</loc>", body)
        self.assertNotIn(f"<loc>http://testserver{reverse('list')}</loc>", body)  # login-only: a redirect is not indexable

    def test_personal_and_private_responses_carry_x_robots_tag(self):
        self.assertEqual(self.client.get("/verify/00000000-0000-0000-0000-000000000000/")["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get("/no-such-page/")["X-Robots-Tag"], "noindex, nofollow")
        self.assertNotIn("X-Robots-Tag", self.client.get(reverse("terms")))

    def test_signed_in_responses_are_noindex(self):
        self.client.force_login(User.objects.create_user("s", password="x"))
        response = self.client.get(reverse("terms"))
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertContains(response, '<meta name="robots" content="noindex, nofollow">')


class DescriptionTests(TestCase):
    def test_every_public_page_has_its_own_description(self):
        seen = {}
        for name in seo.PUBLIC_PAGES:
            html = self.client.get(reverse(name)).content.decode()
            match = re.search(r'<meta name="description" content="([^"]+)"', html)
            self.assertIsNotNone(match, name)
            self.assertNotIn(match.group(1), seen, f"{name} repeats the description of {seen.get(match.group(1))}")
            self.assertLessEqual(len(match.group(1)), 230, name)
            seen[match.group(1)] = name

    def test_homepage_title_names_the_brand_and_the_intent(self):
        html = self.client.get(reverse("landing"), HTTP_ACCEPT_LANGUAGE="en").content.decode()
        self.assertIn("<title>Internest — Internships and verified skills for students in Egypt</title>", html)


class StructuredDataTests(TestCase):
    def setUp(self):
        self.partner = PartnerProfile.objects.create(user=User.objects.create_user("co", password="x"), company_name="Acme </script>",
                                                     official_website="https://acme.example", is_academic=False)

    def job(self, **kw):
        fields = {"partner": self.partner, "title": "Data intern", "description": "Real   work\nwith a mentor. " * 30, "location": "Cairo",
                  "required_majors": "any", "deadline": timezone.localdate() + timezone.timedelta(days=9)}
        fields.update(kw)
        return Internship.objects.create(**fields)

    def test_homepage_declares_the_organization_and_website_honestly(self):
        graph = ld_blocks(self.client.get(reverse("landing")))[0]["@graph"]
        org, site = graph
        self.assertEqual((org["@type"], org["name"], site["@type"]), ("Organization", "Internest", "WebSite"))
        self.assertIn("InterNest", site["alternateName"])
        for invented in ("aggregateRating", "review", "address", "sameAs", "foundingDate"):
            self.assertNotIn(invented, org)

    def test_open_opportunity_is_a_job_posting(self):
        response = self.client.get(reverse("internship_detail", args=[self.job().pk]))
        posting = ld_blocks(response)[0]
        self.assertEqual((posting["@type"], posting["employmentType"]), ("JobPosting", "INTERN"))
        self.assertEqual(posting["hiringOrganization"]["name"], "Acme </script>")  # escaped in HTML, intact as data
        self.assertEqual(posting["jobLocation"]["address"]["addressCountry"], "EG")
        self.assertNotIn('name="robots"', response.content.decode())
        description = re.search(r'<meta name="description" content="([^"]+)"', response.content.decode()).group(1)
        self.assertLessEqual(len(description), 155)
        self.assertNotIn("  ", description)

    def test_remote_opportunity_is_telecommute(self):
        posting = ld_blocks(self.client.get(reverse("internship_detail", args=[self.job(location="Remote").pk])))[0]
        self.assertEqual(posting["jobLocationType"], "TELECOMMUTE")
        self.assertNotIn("jobLocation", posting)

    def test_expired_or_closed_opportunity_leaves_the_index(self):
        for job in (self.job(deadline=timezone.localdate() - timezone.timedelta(days=1)), self.job(is_active=False)):
            response = self.client.get(reverse("internship_detail", args=[job.pk]))
            self.assertEqual(ld_blocks(response), [])
            self.assertContains(response, '<meta name="robots" content="noindex, nofollow">')

    def test_ld_json_cannot_break_out_of_the_script_tag(self):
        self.assertNotIn("</script>", seo.ld_json({"x": "</script><script>alert(1)</script>"}))
