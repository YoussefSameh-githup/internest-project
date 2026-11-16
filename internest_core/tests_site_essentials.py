"""Production web essentials: universal logout, branded error pages, SEO meta, robots/sitemap/healthz, contact page, submit guard."""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import constants as levels
from django.contrib.messages.storage.base import Message
from django.db import DatabaseError
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation
from django.views import defaults

from internest_core import site_views
from internest_core.models import Internship, PartnerProfile
from internest_skills.tests import Base


class LogoutTests(Base):
    """Logout is one POST form in the shared navbar, for every signed-in role."""

    def _check(self, user):
        self.client.force_login(user)
        html = self.client.get(reverse("terms"), follow=True).content.decode()  # incomplete companies land on their profile form
        self.assertEqual(html.count('id="logout-form"'), 1)
        form = html[html.index('id="logout-form"'):]
        form = form[:form.index("</form>")]
        self.assertIn(f'action="{reverse("logout")}"', form)
        self.assertIn('method="post"', form)
        self.assertIn("csrfmiddlewaretoken", form)
        self.assertIn("nav__cta-label", form)  # desktop bar button
        self.assertEqual(html.count('form="logout-form"'), 1)  # phone drawer button submits the same single form
        out = self.client.post(reverse("logout"), follow=True)
        self.assertRedirects(out, reverse("landing"), status_code=302, target_status_code=200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_student(self):
        self._check(self.student_user)

    def test_company(self):
        company = PartnerProfile.objects.create(user=User.objects.create_user("acme", password="x"), company_name="Acme Co", is_academic=False)
        self._check(company.user)

    def test_staff(self):
        self._check(User.objects.create_user("boss", password="x", is_staff=True))

    def test_anonymous_has_no_logout(self):
        self.assertNotContains(self.client.get(reverse("landing")), 'id="logout-form"')

    def test_logout_needs_post(self):
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)


@override_settings(DEBUG=False)
class ErrorPageTests(TestCase):
    def setUp(self):
        self.client.cookies["internest_lang"] = "en"
        override = translation.override("en")
        override.__enter__()
        self.addCleanup(override.__exit__, None, None, None)

    def test_404_is_branded_and_localised(self):
        page = self.client.get("/definitely/not/here/")
        self.assertContains(page, "ep__card", status_code=404)
        self.assertContains(page, "404", status_code=404)
        self.assertContains(page, f'href="{reverse("landing")}" class="ep__home"', status_code=404)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get("/definitely/not/here/"), "الصفحة غير موجودة", status_code=404)

    def test_403_renders_the_branded_page(self):
        request = RequestFactory().get("/x/")
        request.user = mock.Mock(is_authenticated=False)
        response = defaults.permission_denied(request, PermissionError("no"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("Access denied", response.content.decode())
        self.assertIn("ep__card", response.content.decode())

    def test_csrf_failure_page(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.cookies["internest_lang"] = "en"
        page = client.post(reverse("login_or_signup"), {})
        self.assertEqual(page.status_code, 403)
        self.assertIn("Session expired", page.content.decode())

    def test_500_is_standalone(self):
        response = defaults.server_error(RequestFactory().get("/x/"))
        html = response.content.decode()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something went wrong", html)
        self.assertIn('href="/"', html)
        self.assertNotIn("siteNav", html)  # never extends the base layout


class SeoMetaTests(TestCase):
    def test_public_page_has_social_and_canonical_meta(self):
        html = self.client.get(reverse("terms")).content.decode()
        for expected in ('name="description"', 'property="og:title"', 'property="og:description"', 'property="og:image"',
                         'property="og:url"', 'name="twitter:card"', 'name="twitter:image"', 'rel="canonical"',
                         'rel="apple-touch-icon"', 'rel="icon"'):
            self.assertIn(expected, html)
        self.assertIn(f'rel="canonical" href="http://testserver{reverse("terms")}"', html)
        self.assertIn('property="og:image" content="http://testserver/static/', html)
        self.assertNotIn('name="robots"', html)

    def test_signed_in_pages_are_noindex(self):
        self.client.force_login(User.objects.create_user("signed", password="x"))
        self.assertContains(self.client.get(reverse("terms")), '<meta name="robots" content="noindex, nofollow">')


class CrawlerEndpointTests(TestCase):
    def test_robots_txt(self):
        page = self.client.get("/robots.txt")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "text/plain; charset=utf-8")
        body = page.content.decode()
        self.assertIn("User-agent: *", body)
        self.assertIn("Allow: /", body)
        for private in ("/admin/", "/accounts/", "/app/", "/partner/", "/profile/", "/startups/", "/media/"):
            self.assertIn(f"Disallow: {private}", body)
        self.assertIn("Sitemap: http://testserver/sitemap.xml", body)
        self.assertNotIn("Disallow: /list/", body)

    def test_sitemap_lists_public_pages_and_only_open_opportunities(self):
        partner = PartnerProfile.objects.create(user=User.objects.create_user("co", password="x"), company_name="Acme", is_academic=False)
        today = timezone.localdate()
        open_one = Internship.objects.create(partner=partner, title="Open", description="d", location="Cairo", required_majors="any",
                                             deadline=today + timezone.timedelta(days=5))
        old = Internship.objects.create(partner=partner, title="Old", description="d", location="Cairo", required_majors="any",
                                        deadline=today - timezone.timedelta(days=5))
        page = self.client.get("/sitemap.xml")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "application/xml; charset=utf-8")
        body = page.content.decode()
        for name in site_views.PUBLIC_PAGES:
            self.assertIn(f"<loc>http://testserver{reverse(name)}</loc>", body)
        self.assertIn(reverse("internship_detail", args=[open_one.pk]), body)
        self.assertNotIn(reverse("internship_detail", args=[old.pk]), body)
        self.assertNotIn("/admin/", body)

    def test_healthz(self):
        page = self.client.get("/healthz/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(json.loads(page.content), {"status": "ok", "database": "ok"})
        self.assertIn("no-cache", page["Cache-Control"])
        self.assertEqual(self.client.post("/healthz/").status_code, 405)

    def test_healthz_reports_a_down_database(self):
        with mock.patch.object(site_views.connection, "cursor", side_effect=DatabaseError("down")):
            page = self.client.get("/healthz/")
        self.assertEqual(page.status_code, 503)
        self.assertEqual(json.loads(page.content)["status"], "error")


class TrustPagesTests(TestCase):
    def test_policies_and_contact_are_public_and_linked_in_the_footer(self):
        for name in ("privacy", "terms", "refund_policy", "contact"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        html = self.client.get(reverse("landing")).content.decode()
        footer = html[html.index('<footer class="footer">'):]
        for name in ("privacy", "terms", "refund_policy", "contact"):
            self.assertIn(f'href="{reverse(name)}"', footer)

    def test_contact_page_content_in_both_languages(self):
        self.client.cookies["internest_lang"] = "en"
        page = self.client.get(reverse("contact"))
        self.assertContains(page, "Contact &amp; support")
        self.assertContains(page, "mailto:")
        self.assertContains(page, reverse("contact_partner"))
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("contact")), "تواصل معنا والدعم")


class GlobalUxTests(Base):
    def test_flash_component_maps_every_level(self):
        for level, css, icon in ((levels.SUCCESS, "success", "bi-check-circle-fill"), (levels.ERROR, "error", "bi-x-circle-fill"),
                                 (levels.WARNING, "warning", "bi-exclamation-triangle-fill"), (levels.INFO, "info", "bi-info-circle-fill")):
            html = render_to_string("components/flash.html", {"message": Message(level, "Saved!")})
            self.assertIn(f"flash flash--{css}", html)
            self.assertIn(icon, html)
            self.assertIn("Saved!", html)

    def test_saving_the_profile_shows_a_toast(self):
        self.client.force_login(self.student_user)
        type(self.student).objects.filter(pk=self.student.pk).update(personal_email="y@x.com", personal_email_verified_at=timezone.now())
        data = {"personal_email": "y@x.com", "university": "Cairo", "major": "CS", "study_level": "3", "phone_number": "",
                "linkedin_url": "", "bio": "", "portfolio_url": ""}
        page = self.client.post(reverse("profile"), data, follow=True)
        self.assertContains(page, 'id="flashStack"')
        self.assertContains(page, "flash flash--success")
        self.assertContains(page, "Your profile was updated.")

    def test_submit_guard_ships_once_on_every_page(self):
        html = self.client.get(reverse("login_or_signup")).content.decode()
        for expected in ("Double-submit guard", "data-no-lock", "form.dataset.submitting", ".is-loading"):
            self.assertIn(expected, html)
        self.assertEqual(html.count("Double-submit guard"), 1)
