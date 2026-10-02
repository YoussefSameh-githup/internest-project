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
    "company_name": "StartupX", "official_email": "contact@startupx.io", "founder_email": "mona@startupx.io",
    "official_website": "https://startupx.io", "linkedin_url": "https://linkedin.com/company/startupx",
    "facebook_url": "", "twitter_url": "", "instagram_url": "",
    "industry": "software", "founded_year": "2023", "description": "We build hiring tools for Egyptian SMEs.",
}
OPPORTUNITY = {"title": "Frontend gig", "description": "React work", "location": "Remote", "required_majors": "CS",
               "deadline": (timezone.now().date() + timedelta(days=20)).isoformat()}


class OnboardingBase(TestCase):
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


class StartupOnboardingTests(OnboardingBase):
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
        self.assertContains(self.client.get(reverse("partner_dashboard")), "جاري المراجعة وسيتم الرد خلال 24 ساعة")

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
        self.assertEqual(partner.profile_completion_score, 100)  # from the fields, not from verification
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


class PublishGateAndFieldsTests(OnboardingBase):
    """Publish needs (A) admin verification AND (B) 100% profile; redundant partner fields are gone."""

    def setUp(self):
        super().setUp()
        self.register()

    def _can_post(self):
        before = PartnerInternshipSubmission.objects.count()
        resp = self.client.post(reverse("partner_submit_internship"), OPPORTUNITY)
        return PartnerInternshipSubmission.objects.count() > before, resp

    def test_redundant_fields_removed(self):
        from internest_core.forms import PartnerProfileEditForm
        from .forms import CompanyIdentityForm, CompanyProfileForm
        for form in (PartnerProfileEditForm(), CompanyIdentityForm(instance=self.partner()), CompanyProfileForm()):
            for name in ("partner_code", "is_academic", "official_phone"):
                self.assertNotIn(name, form.fields)
        page = self.client.get(reverse("startup_company_profile"))
        for name in ("partner_code", "is_academic", "official_phone"):
            self.assertNotContains(page, f'name="{name}"')
        uni = User.objects.create_user("uni2", password="pw")
        PartnerProfile.objects.create(user=uni, company_name="Uni2", partner_code="U2", is_academic=True)
        self.client.force_login(uni)
        page = self.client.get(reverse("partner_profile"))
        for name in ("partner_code", "is_academic", "official_phone"):
            self.assertNotContains(page, f'name="{name}"')
        self.assertRedirects(self.client.get(reverse("partner_login")), reverse("login"), fetch_redirect_response=False)

    def test_company_and_founder_emails_present_and_validated(self):
        page = self.client.get(reverse("startup_company_profile"))
        self.assertContains(page, 'name="official_email"')
        self.assertContains(page, 'name="founder_email"')
        for bad, msg in (({"founder_email": ""}, "This field is required."),
                         ({"official_email": "not-an-email"}, "Enter a valid email address."),
                         ({"founder_email": "CONTACT@startupx.io"}, "The founder email must be different from the company email.")):
            self.assertContains(self.submit_profile(**bad), msg)
        self.assertFalse(CompanyProfile.objects.exists())
        self.submit_profile()
        self.assertEqual(self.partner().company_profile.founder_email, "mona@startupx.io")
        self.assertEqual(self.partner().official_email, "contact@startupx.io")

    def test_verified_but_incomplete_cannot_post(self):
        self.submit_profile(official_website="")  # 7 of 8 items → 87%
        self.approve()
        self.assertEqual(self.partner().profile_completion_score, 87)
        posted, resp = self._can_post()
        self.assertFalse(posted)
        page = self.client.get(resp.url)
        self.assertContains(page, "Your profile is 87% complete")
        self.assertContains(page, "Profile is incomplete (87%).")

    def test_complete_but_unverified_cannot_post(self):
        self.submit_profile()
        self.assertEqual(self.partner().profile_completion_score, 100)
        posted, resp = self._can_post()
        self.assertFalse(posted)
        self.assertContains(self.client.get(resp.url), "Your startup is under review")

    def test_verified_and_complete_can_post_and_completion_needs_no_reapproval(self):
        self.submit_profile(official_website="")
        self.approve()
        self.assertFalse(self._can_post()[0])
        self.submit_profile()  # self-service completion, no new admin action
        self.assertEqual(self.partner().profile_completion_score, 100)
        self.assertTrue(self.partner().is_fully_verified)
        self.assertTrue(self._can_post()[0])
