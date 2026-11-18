"""Freemium pricing: regional currency, billing periods, comparison matrix and paywall gates."""
from datetime import timedelta

from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, Internship, PartnerProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import EmployerSubscription, Skill, StudentSkill

from .models import ProUpgradeRequest
from .pricing import currency_for, format_price, pricing_country
from .tiers import activate_pro

UPGRADE = reverse("startup_upgrade")


class RegionDetectionTests(ApplicationsBase):
    def test_currency_by_country(self):
        self.assertEqual([currency_for(c) for c in ("EG", "eg", "DE", "FR", "SA", "US", "GB", "")],
                         ["EGP", "EGP", "EUR", "EUR", "USD", "USD", "EUR", "USD"])  # GB: European → EUR
        self.assertEqual((format_price(1500, "EGP"), format_price(12, "USD"), format_price(9.5, "EUR")), ("1,500 EGP", "$12", "€9.50"))

    def test_registration_country_beats_visitor_country(self):
        request = RequestFactory().get("/", HTTP_CF_IPCOUNTRY="AE")
        self.assertEqual(pricing_country(request, self.partner), "AE")  # no registration country yet → visitor
        self.partner.country_of_registration = "EG"
        self.assertEqual(pricing_country(request, self.partner), "EG")
        self.assertEqual(pricing_country(RequestFactory().get("/", HTTP_CF_IPCOUNTRY="XX")), "EG")  # unknown → default

    @override_settings(GEOIP_PATH="/nonexistent/geoip")
    def test_broken_geoip_falls_back_to_default(self):
        self.assertEqual(pricing_country(RequestFactory().get("/", REMOTE_ADDR="8.8.8.8")), "EG")


class UpgradePageTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.founder)

    def _country(self, code):
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration=code)

    def test_egypt_plans_page(self):
        self._country("EG")
        page = self.client.get(UPGRADE)
        for text in ("Free", "0 EGP", "Hiring Pass", "500 EGP", "One opportunity · 30 days · one-time payment · no automatic renewal",
                     "1 opportunity post per month", "Invite up to 3 candidates to your opportunity", "We do not guarantee a hire.",
                     "Founding 20", "20 of 20 seats left", "Founders Network: 3 posts per hour"):
            self.assertContains(page, text)
        self.assertNotRegex(page.content.decode(), r"(700|800|8,000|8000)\s*EGP|EGP\s*(700|800|8,000)|/ month|/ year|Subscribe now")
        self.assertNotContains(page, "$")

    def test_outside_egypt_prices(self):
        for country, label in (("SA", "$29"), ("US", "$29"), ("DE", "€27"), ("GB", "€27")):
            self._country(country)
            page = self.client.get(UPGRADE)
            self.assertEqual(page.context["quote"]["final_label"], label)
            self.assertContains(page, label)
            self.assertNotContains(page, "Contact sales")
            self.assertContains(page, "0 EGP")  # Free is free for everyone

    def test_yearly_activation_lasts_a_year(self):
        sub = activate_pro(self.partner, months=12)
        self.assertEqual(sub.valid_until, timezone.now().date() + timedelta(days=365))

    def test_registration_country_required_and_locked_after_verification(self):
        from .tests import PROFILE

        resp = self.client.post(reverse("startup_company_profile_edit"), {**PROFILE, "company_name": "NileCode",
                                                                           "country_of_registration": ""})
        self.assertContains(resp, "Country of commercial registration")
        self.assertEqual(resp.status_code, 200)  # form error, not saved
        self._country("SA")
        self.client.post(reverse("startup_company_profile_edit"), {**PROFILE, "company_name": "NileCode", "country_of_registration": "EG"})
        self.partner.refresh_from_db()
        self.assertEqual(self.partner.country_of_registration, "SA")  # verified: can't switch to the cheaper region


class PaywallGateTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit",
                                    status=StudentSkill.STATUS_VERIFIED, score=93, percentile=81)
        self.app = Application.objects.create(internship=self.gig, applicant=self.user)
        self.client.force_login(self.founder)

    def test_free_sees_locked_score_report(self):
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertContains(page, "Python")
        self.assertContains(page, "Upgrade to see the detailed score report")
        self.assertNotContains(page, "93%")
        self.assertNotContains(page, "P81")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("partner_applicant", args=[self.app.pk])), "ترقية لرؤية تقرير الدرجات التفصيلي")

    def test_pro_sees_scores(self):
        activate_pro(self.partner)
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertContains(page, "93% · P81")
        self.assertNotContains(page, "Upgrade to see the detailed score report")

    def test_pro_opportunities_listed_first(self):
        other_user = type(self.founder).objects.create_user("pro_founder")
        pro_partner = PartnerProfile.objects.create(user=other_user, company_name="ProCo", partner_code="PRO1", is_fully_verified=True)
        late = Internship.objects.create(partner=pro_partner, title="Pro gig", description="d", location="Online",
                                         required_majors="any", deadline=timezone.now().date() + timedelta(days=2))
        activate_pro(pro_partner)
        EmployerSubscription.objects.filter(partner=self.partner).delete()
        self.login_student(self.user)
        listing = list(self.client.get(reverse("list")).context["internships"])
        self.assertEqual(listing[0], late)  # earliest deadline, but Pro → first
