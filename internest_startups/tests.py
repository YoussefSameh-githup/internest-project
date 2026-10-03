import io
import shutil
import tempfile
from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
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
        return self.client.post(reverse("startup_company_profile_edit"), {**PROFILE, **overrides})

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
        self.assertRedirects(resp, reverse("startup_company_profile_edit"), fetch_redirect_response=False)
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
            self.assertRedirects(self.client.get(reverse(name)), reverse("startup_company_profile_edit"), fetch_redirect_response=False)

    def test_profile_validation(self):
        self.register()
        resp = self.submit_profile(linkedin_url="", company_name="", founded_year=str(timezone.now().year + 1))
        self.assertContains(resp, "Add at least one social media link")
        self.assertContains(resp, "The founded year cannot be in the future.")
        self.assertFalse(CompanyProfile.objects.exists())

    def test_profile_submission_unlocks_dashboard_with_pending_banner(self):
        self.register()
        resp = self.submit_profile()
        self.assertRedirects(resp, reverse("startup_company_profile"), fetch_redirect_response=False)
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
        page = self.client.get(reverse("startup_company_profile_edit"))
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
        page = self.client.get(reverse("startup_company_profile_edit"))
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


class ProfileShowcaseAndDashboardTests(OnboardingBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.register()

    def _png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "#1d3a8a").save(buf, "PNG")
        return SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")

    def test_new_startup_view_page_redirects_to_edit_form(self):
        self.assertRedirects(self.client.get(reverse("startup_company_profile")),
                             reverse("startup_company_profile_edit"), fetch_redirect_response=False)
        page = self.client.get(reverse("startup_company_profile_edit"))
        self.assertContains(page, '<form method="post" enctype="multipart/form-data"')
        self.assertContains(page, 'name="logo"')

    def test_saved_profile_renders_read_only_showcase(self):
        self.submit_profile()
        page = self.client.get(reverse("startup_company_profile"))
        self.assertTemplateUsed(page, "startups/company_showcase.html")
        for field in ("company_name", "official_email", "founder_email"):
            self.assertNotContains(page, f'name="{field}"')  # no profile form in view mode (only the instant logo upload)
        self.assertNotContains(page, "<textarea")
        for text in ("StartupX", "contact@startupx.io", "mona@startupx.io", "startupx.io", "LinkedIn", "2023", "Pending verification"):
            self.assertContains(page, text)
        self.assertContains(page, reverse("startup_company_profile_edit"))
        self.approve()
        self.assertContains(self.client.get(reverse("startup_company_profile")), "Verified startup")

    def test_logo_upload_saves(self):
        with override_settings(MEDIA_ROOT=self.media):
            resp = self.client.post(reverse("startup_company_profile_edit"), {**PROFILE, "logo": self._png()})
            self.assertRedirects(resp, reverse("startup_company_profile"), fetch_redirect_response=False)
            partner = self.partner()
            self.assertTrue(partner.logo.name.startswith("partner_logos/"))
            self.assertTrue(partner.logo.storage.exists(partner.logo.name))
            self.assertContains(self.client.get(reverse("startup_company_profile")), partner.logo.url)

    def test_dashboard_action_tiles_route_to_sections(self):
        self.submit_profile()
        page = self.client.get(reverse("partner_dashboard"))
        for url in (reverse("partner_submit_choose"), reverse("partner_dashboard_section", args=["applicants"]),
                    reverse("partner_dashboard_section", args=["pending"]), reverse("startup_company_profile")):
            self.assertContains(page, f'href="{url}"')
        self.assertContains(page, '<span class="dash-card__count">0</span>', count=2)  # applicants + pending
        self.assertNotContains(page, "Applicant inbox")  # sections are not dumped on the overview
        self.assertNotContains(page, reverse("lounge_feed"))  # unverified: no lounge tile

        applicants = self.client.get(reverse("partner_dashboard_section", args=["applicants"]))
        self.assertContains(applicants, "Applicant inbox")
        self.assertNotContains(applicants, "Internship submissions under review")
        pending = self.client.get(reverse("partner_dashboard_section", args=["pending"]))
        self.assertContains(pending, "Internship submissions under review")
        self.assertNotContains(pending, "Applicant inbox")
        self.assertEqual(self.client.get("/partner/dashboard/nope/").status_code, 404)

        self.approve()
        self.assertContains(self.client.get(reverse("partner_dashboard")), f'href="{reverse("lounge_feed")}"')

    def test_pending_counter_counts_pending_requests(self):
        self.submit_profile()
        self.approve()
        self.client.post(reverse("partner_submit_internship"), OPPORTUNITY)
        page = self.client.get(reverse("partner_dashboard"))
        self.assertEqual(page.context["pending_count"], 1)


class ProTierTests(OnboardingBase):
    """BMC tier model: Free = 1 opportunity/month, Pro = unlimited; promo codes; gold badge only for Pro."""

    def setUp(self):
        super().setUp()
        self.register()
        self.submit_profile()
        self.approve()

    def _post(self, title="Frontend gig"):
        return self.client.post(reverse("partner_submit_internship"), {**OPPORTUNITY, "title": title})

    def _count(self):
        return PartnerInternshipSubmission.objects.filter(partner=self.partner()).count()

    def _make_pro(self):
        from .tiers import activate_pro
        activate_pro(self.partner())

    # --- posting limits ---------------------------------------------------------
    def test_free_tier_blocked_at_second_post_with_upsell(self):
        self._post("First")
        self.assertEqual(self._count(), 1)
        resp = self._post("Second")
        self.assertEqual(self._count(), 1)
        self.assertEqual(resp.url, reverse("partner_dashboard") + "?upsell=limit")
        page = self.client.get(resp.url)
        self.assertContains(page, 'id="upsell-dialog"')
        self.assertContains(page, "data-autoopen")
        self.assertContains(page, "You've reached this month's limit")
        self.assertContains(page, "Subscribe now and unlock every feature")
        self.assertContains(page, "1/1 used this month")

    def test_free_quota_resets_monthly_and_ignores_rejected(self):
        from datetime import timedelta as td
        self._post("Old")
        PartnerInternshipSubmission.objects.update(submission_date=timezone.now() - td(days=40))
        self._post("This month")
        self.assertEqual(self._count(), 2)
        PartnerInternshipSubmission.objects.filter(title="This month").update(status="Rejected")
        self._post("Retry")
        self.assertEqual(self._count(), 3)

    def test_pro_tier_unlimited_posts(self):
        self._make_pro()
        for i in range(4):
            self._post(f"Role {i}")
        self.assertEqual(self._count(), 4)
        page = self.client.get(reverse("partner_dashboard"))
        self.assertNotContains(page, 'id="upsell-dialog"')
        self.assertContains(page, "Unlimited with Pro")

    # --- promo codes & checkout -------------------------------------------------
    def test_promo_code_application_and_validation(self):
        from .models import PromoCode
        page = self.client.get(reverse("startup_upgrade"))
        self.assertContains(page, "$100.00")
        self.client.post(reverse("startup_upgrade"), {"action": "apply", "promo_code": "startup50"})
        page = self.client.get(reverse("startup_upgrade"))
        self.assertEqual(page.context["quote"]["final_price"], 50)
        self.assertContains(page, "$50.00")
        self.assertContains(page, "STARTUP50")

        for code, setup in (("NOPE", None),
                            ("OLD10", dict(discount_percent=10, valid_until=timezone.now().date() - timedelta(days=1))),
                            ("USEDUP", dict(discount_percent=10, max_uses=1, times_used=1)),
                            ("OFF", dict(discount_percent=10, is_active=False))):
            if setup:
                PromoCode.objects.create(code=code, **setup)
            self.client.post(reverse("startup_upgrade"), {"action": "remove"})
            resp = self.client.post(reverse("startup_upgrade"), {"action": "apply", "promo_code": code}, follow=True)
            self.assertContains(resp, "This promo code is invalid or has expired.")
            self.assertEqual(resp.context["quote"]["final_price"], 100)

    def test_subscribe_creates_request_and_admin_activation_grants_pro(self):
        from django.contrib.admin.sites import AdminSite
        from .admin import ProUpgradeRequestAdmin, activate_requests
        from .models import PromoCode, ProUpgradeRequest
        self.client.post(reverse("startup_upgrade"), {"action": "apply", "promo_code": "EGYPT2026"})
        self.client.post(reverse("startup_upgrade"), {"action": "subscribe"})
        self.client.post(reverse("startup_upgrade"), {"action": "subscribe"})  # duplicate ignored
        req = ProUpgradeRequest.objects.get()
        self.assertEqual((req.final_price, req.discount_percent, req.promo_code.code, req.status), (80, 20, "EGYPT2026", "pending"))
        from .tiers import is_pro
        self.assertFalse(is_pro(self.partner()))  # no Pro before payment

        request = RequestFactory().post("/admin/")
        request.user = User.objects.get(username="root")
        request.session = {}
        request._messages = FallbackStorage(request)
        activate_requests(ProUpgradeRequestAdmin(ProUpgradeRequest, AdminSite()), request, ProUpgradeRequest.objects.all())
        partner = self.partner()
        self.assertTrue(is_pro(partner))
        self.assertEqual(partner.subscription.valid_until, timezone.now().date() + timedelta(days=30))
        self.assertEqual(PromoCode.objects.get(code="EGYPT2026").times_used, 1)

    # --- badges & Founders Network UI ------------------------------------------
    def test_gold_badge_only_for_pro(self):
        self.client.post(reverse("lounge_feed"), {"title": "Hello founders", "body": "First post here", "category": "advice"})
        for url in (reverse("lounge_feed"), reverse("partner_dashboard"), reverse("startup_company_profile")):
            self.assertNotContains(self.client.get(url), "Pro Verified", msg_prefix=url)
        self._make_pro()
        for url in (reverse("lounge_feed"), reverse("partner_dashboard"), reverse("startup_company_profile")):
            self.assertContains(self.client.get(url), "👑 Pro Verified", msg_prefix=url)

    def test_founders_network_feed_ui(self):
        self.client.post(reverse("lounge_feed"), {"title": "Pricing advice", "body": "How do you price B2B SaaS?", "category": "advice"})
        page = self.client.get(reverse("lounge_feed"))
        self.assertContains(page, "Founders Network")
        self.assertContains(page, "Share an idea or ask fellow founders for advice...")
        self.assertContains(page, 'id="compose-dialog"')
        self.assertNotContains(page, 'id="compose-dialog" class="compose-dialog" data-autoopen')
        self.assertContains(page, 'class="comment-drawer"')
        self.assertContains(page, 'class="tag-pill"')
        resp = self.client.post(reverse("lounge_feed"), {"title": "", "body": ""})  # errors reopen the modal
        self.assertContains(resp, "data-autoopen")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("lounge_feed")), "شبكة رواد الأعمال")

    def test_pro_posts_are_pinned_to_top(self):
        from internest_lounge.models import LoungePost
        other = User.objects.create_user("free2", password="pw")
        free = PartnerProfile.objects.create(user=other, company_name="FreeCo2", partner_code="F2", is_fully_verified=True)
        CompanyProfile.objects.create(partner=free, industry="software", founded_year=2022, description="x")
        self._make_pro()
        LoungePost.objects.create(author=self.partner(), title="Pro post", body="a", body_fingerprint="1")
        LoungePost.objects.create(author=free, title="Newer free post", body="b", body_fingerprint="2")
        titles = [p.title for p in self.client.get(reverse("lounge_feed")).context["page"]]
        self.assertEqual(titles[0], "Pro post")


class DashboardGridAndShowcaseDesignTests(OnboardingBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.submit_profile()

    def test_dashboard_renders_card_grid_with_icon_badges(self):
        import re
        page = self.client.get(reverse("partner_dashboard"))
        html = page.content.decode()
        self.assertContains(page, 'class="dash-grid')
        cards = re.findall(r'class="dash-card[ "]', html)
        self.assertGreaterEqual(len(cards), 5)
        self.assertEqual(html.count('class="dash-card__icon '), len(cards))  # every card has a styled icon badge
        for emoji in ("➕", "📥", "⏳", "💬", "✏️"):
            self.assertNotIn(emoji, html.split('class="dash-grid')[1].split("</nav>")[0])
        for title in ("Post Opportunity", "Applicants Inbox", "Pending Requests", "Company Profile", "Upgrade to Internest Pro"):
            self.assertContains(page, title)
        self.approve()
        page = self.client.get(reverse("partner_dashboard"))
        self.assertContains(page, f'href="{reverse("lounge_feed")}" class="dash-card"')
        self.assertContains(page, "Founders Network")

    def test_card_links_resolve(self):
        from django.urls import resolve
        self.approve()
        html = self.client.get(reverse("partner_dashboard")).content.decode()
        import re
        nav = html.split('class="dash-grid')[1].split("</nav>")[0]
        hrefs = re.findall(r'href="([^"]+)"', nav)
        self.assertGreaterEqual(len(hrefs), 5)
        for href in hrefs:
            resolve(href)  # raises Resolver404 if a card points nowhere
            self.assertIn(self.client.get(href).status_code, (200, 302), href)

    def test_stylesheets_are_cache_busted(self):
        import re
        html = self.client.get(reverse("partner_dashboard")).content.decode()
        self.assertRegex(html, r'/static/css/styles\.css\?v=[0-9a-f]{10}"')
        self.assertRegex(html, r'/static/startups/startups\.css\?v=[0-9a-f]{10}"')

    def test_showcase_has_cover_avatar_and_two_column_cards_with_svg_icons(self):
        page = self.client.get(reverse("startup_company_profile"))
        for cls in ("co-hero__cover", "co-hero__avatar", "co-hero__name", "co-hero__edit", "co-grid"):
            self.assertContains(page, cls)
        self.assertContains(page, 'class="co-card"', count=2)
        self.assertContains(page, 'class="co-social co-social--linkedin"')
        self.assertContains(page, '<svg class="svg-icon"', count=4)  # website, company email, founder email, LinkedIn
        self.assertContains(page, "Contact & social")
        self.assertContains(page, "About")


class LogoUploadTests(OnboardingBase):
    """Logo changes are instant and self-service: they never reset admin verification."""

    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.register()
        self.submit_profile()
        self.approve()

    def _image(self, name="logo.png", fmt="PNG", size=(16, 16)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, "#2746a8").save(buf, fmt)
        return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")

    def test_logo_only_upload_saves_and_keeps_verification(self):
        before = self.partner()
        self.assertTrue(before.is_fully_verified)
        resp = self.client.post(reverse("startup_logo_update"), {"logo": self._image()})
        self.assertRedirects(resp, reverse("startup_company_profile"), fetch_redirect_response=False)
        partner = self.partner()
        self.assertTrue(partner.logo.name.startswith("partner_logos/"))
        self.assertTrue(partner.logo.storage.exists(partner.logo.name))
        self.assertTrue(partner.is_fully_verified)
        self.assertEqual(partner.profile_completion_score, before.profile_completion_score)
        self.assertContains(self.client.get(reverse("startup_company_profile")), partner.logo.url)

    def test_full_profile_edit_with_logo_keeps_verification(self):
        resp = self.client.post(reverse("startup_company_profile_edit"), {**PROFILE, "logo": self._image("x.jpg", "JPEG")})
        self.assertEqual(resp.status_code, 302)
        partner = self.partner()
        self.assertTrue(partner.logo)
        self.assertTrue(partner.is_fully_verified)

    def test_long_or_arabic_filenames_get_safe_names(self):
        for name in ("شعار الشركة النهائي.png", ("x" * 180) + ".png", "WhatsApp Image 2026-10-04 at 12.34.56 PM.png"):
            resp = self.client.post(reverse("startup_logo_update"), {"logo": self._image(name)})
            self.assertEqual(resp.status_code, 302, name)
            logo = self.partner().logo.name
            self.assertRegex(logo, r"^partner_logos/\d+-[0-9a-f]{8}\.png$")
            self.assertLessEqual(len(logo), 100)

    def test_replacing_logo_deletes_old_file(self):
        self.client.post(reverse("startup_logo_update"), {"logo": self._image()})
        first = self.partner().logo
        storage, old = first.storage, first.name
        self.client.post(reverse("startup_logo_update"), {"logo": self._image("new.png")})
        self.assertFalse(storage.exists(old))
        self.assertTrue(storage.exists(self.partner().logo.name))

    def test_invalid_files_show_friendly_error_not_400(self):
        too_big = SimpleUploadedFile("big.png", b"0" * (5 * 1024 * 1024 + 1), content_type="image/png")
        for upload, message in ((SimpleUploadedFile("logo.svg", b"<svg/>", content_type="image/svg+xml"), "Upload a valid image"),
                                (SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"), "Upload a valid image"),
                                (self._image("anim.gif", "GIF"), "Upload a PNG, JPG or WEBP image."),
                                (too_big, "The logo must be 5 MB or smaller."),
                                (SimpleUploadedFile("fake.png", b"not an image", content_type="image/png"), "Upload a valid image")):
            resp = self.client.post(reverse("startup_logo_update"), {"logo": upload}, follow=True)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, message)
            self.assertFalse(self.partner().logo)
            self.assertTrue(self.partner().is_fully_verified)

    def test_logo_shows_in_founders_network(self):
        self.client.post(reverse("startup_logo_update"), {"logo": self._image()})
        self.client.post(reverse("lounge_feed"), {"title": "Hello", "body": "Logo test post", "category": "advice"})
        self.assertContains(self.client.get(reverse("lounge_feed")), self.partner().logo.url)

    def test_logo_endpoint_requires_post_and_csrf(self):
        self.assertEqual(self.client.get(reverse("startup_logo_update")).status_code, 405)
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.partner().user)
        self.assertEqual(strict.post(reverse("startup_logo_update"), {"logo": self._image()}).status_code, 403)

    def test_showcase_has_instant_logo_form(self):
        page = self.client.get(reverse("startup_company_profile"))
        self.assertContains(page, f'action="{reverse("startup_logo_update")}" enctype="multipart/form-data"')
        self.assertContains(page, "data-logo-input")
