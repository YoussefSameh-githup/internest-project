from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone, translation

from internest_core.models import PartnerInternshipSubmission, PartnerProfile

from .admin import PendingCompanyProfileAdmin, approve_startups
from .models import CompanyProfile, PendingCompanyProfile

SIGNUP = {"full_name": "Mona Adel", "email": "Mona@StartupX.io", "password1": "Str0ng-pass-2026!", "password2": "Str0ng-pass-2026!"}
PROFILE = {
    "company_name": "StartupX", "official_website": "", "linkedin_url": "https://linkedin.com/company/startupx",
    "facebook_url": "", "twitter_url": "", "instagram_url": "",
    "industry": "software", "founded_year": "2023", "description": "We build hiring tools for Egyptian SMEs.",
}
OPPORTUNITY = {"title": "Frontend gig", "description": "React work", "location": "Remote", "required_majors": "CS",
               "deadline": (timezone.now().date() + timedelta(days=20)).isoformat()}


class StartupOnboardingTests(TestCase):
    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"

    def register(self):
        return self.client.post(reverse("startup_register"), SIGNUP)

    def submit_profile(self, **overrides):
        return self.client.post(reverse("startup_company_profile"), {**PROFILE, **overrides})

    def partner(self):
        return PartnerProfile.objects.get(user__username="mona@startupx.io")

    def approve(self):
        request = RequestFactory().post("/admin/")
        request.user = User.objects.create_superuser("root", "r@x.com", "pw")
        request.session = {}
        request._messages = FallbackStorage(request)
        admin = PendingCompanyProfileAdmin(PendingCompanyProfile, AdminSite())
        approve_startups(admin, request, CompanyProfile.objects.all())

    # --- registration ---------------------------------------------------------
    def test_landing_startup_cta_points_to_registration(self):
        self.assertContains(self.client.get(reverse("landing")), 'href="/startups/register/"')

    def test_self_registration_creates_logged_in_founder(self):
        resp = self.register()
        self.assertRedirects(resp, reverse("startup_company_profile"), fetch_redirect_response=False)
        partner = self.partner()
        self.assertEqual((partner.user.email, partner.user.first_name, partner.user.last_name), ("mona@startupx.io", "Mona", "Adel"))
        self.assertFalse(partner.is_fully_verified)
        self.assertFalse(partner.is_academic)
        self.assertEqual(int(self.client.session["_auth_user_id"]), partner.user.pk)

    def test_registration_validation(self):
        self.register()
        self.client.logout()
        self.client.cookies["internest_lang"] = "en"
        resp = self.client.post(reverse("startup_register"), {**SIGNUP, "email": "MONA@startupx.io"})
        self.assertContains(resp, "An account with this email already exists.")
        resp = self.client.post(reverse("startup_register"), {**SIGNUP, "email": "b@b.io", "password2": "different"})
        self.assertContains(resp, "The two passwords do not match.")
        self.assertEqual(User.objects.count(), 1)

    # --- mandatory company profile ---------------------------------------------
    def test_dashboard_and_posting_redirect_until_profile_submitted(self):
        self.register()
        for name in ("partner_dashboard", "partner_submit_internship", "partner_submit_choose", "lounge_feed", "list"):
            self.assertRedirects(self.client.get(reverse(name)), reverse("startup_company_profile"), fetch_redirect_response=False)

    def test_profile_validation(self):
        self.register()
        resp = self.submit_profile(linkedin_url="", company_name="", founded_year=str(timezone.now().year + 1))
        self.assertContains(resp, "Add at least one social media link")
        self.assertContains(resp, "The founded year cannot be in the future.")
        self.assertFalse(CompanyProfile.objects.exists())

    def test_profile_submission_unlocks_dashboard_with_pending_banner(self):
        self.register()
        resp = self.submit_profile()
        self.assertRedirects(resp, reverse("partner_dashboard"), fetch_redirect_response=False)
        partner = self.partner()
        self.assertEqual(partner.company_name, "StartupX")
        self.assertEqual(partner.company_profile.founded_year, 2023)
        page = self.client.get(reverse("partner_dashboard"))
        self.assertContains(page, "Your verification request is under review. You will be notified within 24 hours.")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("partner_dashboard")), "طلبك قيد المراجعة، وسيتم الرد عليك وتوثيق حسابك خلال 24 ساعة.")

    # --- verification gate -------------------------------------------------------
    def test_unverified_startup_cannot_post_opportunities(self):
        self.register()
        self.submit_profile()
        for name in ("partner_submit_choose", "partner_submit_internship", "partner_submit_course"):
            self.assertRedirects(self.client.get(reverse(name)), reverse("partner_dashboard"), fetch_redirect_response=False)
        resp = self.client.post(reverse("partner_submit_internship"), OPPORTUNITY)
        self.assertRedirects(resp, reverse("partner_dashboard"), fetch_redirect_response=False)
        self.assertFalse(PartnerInternshipSubmission.objects.exists())
        self.assertEqual(self.client.get(reverse("lounge_feed")).status_code, 302)

    def test_admin_approval_grants_posting_and_lounge(self):
        self.register()
        self.submit_profile()
        self.assertEqual(list(PendingCompanyProfile.objects.filter(partner__is_fully_verified=False)), [self.partner().company_profile])
        self.approve()
        partner = self.partner()
        self.assertTrue(partner.is_fully_verified)
        self.assertEqual(partner.profile_completion_score, 100)
        self.assertIsNotNone(partner.company_profile.verified_at)

        self.assertNotContains(self.client.get(reverse("partner_dashboard")), "Your verification request is under review")
        self.assertEqual(self.client.get(reverse("lounge_feed")).status_code, 200)
        self.assertEqual(self.client.get(reverse("partner_submit_internship")).status_code, 200)
        self.client.post(reverse("partner_submit_internship"), OPPORTUNITY)
        self.assertTrue(PartnerInternshipSubmission.objects.filter(partner=partner).exists())

    def test_universities_are_not_forced_through_startup_flow(self):
        user = User.objects.create_user("uni", password="pw")
        PartnerProfile.objects.create(user=user, company_name="Uni", partner_code="U1", is_academic=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("partner_dashboard")).status_code, 200)
