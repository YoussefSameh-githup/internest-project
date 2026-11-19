"""Phase 4: the free employer journey end to end, backend-enforced paid features, honest conversion copy."""
import re
import shutil
import tempfile

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.admin import PartnerInternshipSubmissionAdmin
from internest_core.models import Application, Internship, PartnerInternshipSubmission, PartnerProfile, StudentProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill

from .models import ProUpgradeRequest, ScreeningTask, TalentInvitation
from .tests import OPPORTUNITY, OnboardingBase

MEDIA = tempfile.mkdtemp()
URGENCY = re.compile(r"(?i)\b(hurry|only today|limited time|last chance|act now|expires in|ends soon|don'?t miss)\b|آخر فرصة|لفترة محدودة")


def _verified_candidates(n, skill_slug="python"):
    for i in range(n):
        _u, student = _student(f"cand{i}")
        StudentProfile.objects.filter(pk=student.pk).update(talent_pool_visible=True, major=f"Major {i}")
        StudentSkill.objects.create(student=student, skill=Skill.objects.get(slug=skill_slug), source="explicit",
                                    status="verified", score=80 + i, verified_via="challenge")


@override_settings(MEDIA_ROOT=MEDIA)
class FreeJourneyEndToEndTests(OnboardingBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.core.management import call_command
        import io

        call_command("seed_skills", stdout=io.StringIO())

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def _journey(self):
        return self.client.get(reverse("partner_dashboard")).context["journey"]

    def _current(self):
        return next(s["key"] for s in self._journey()["steps"] if s["current"])

    def _publish_submissions(self):
        request = RequestFactory().post("/admin/")
        request.user = User.objects.filter(is_superuser=True).first() or User.objects.create_superuser("root2", "r2@x.com", "pw")
        request.session, request._messages = {}, None
        request._messages = FallbackStorage(request)
        PartnerInternshipSubmissionAdmin(PartnerInternshipSubmission, AdminSite()).approve_submissions(
            request, PartnerInternshipSubmission.objects.all())

    def test_register_to_paid_access(self):
        _verified_candidates(3)
        # 1. Register: the company profile comes first (the dashboard sends there until it exists)
        self.register()
        self.assertRedirects(self.client.get(reverse("partner_dashboard")), reverse("startup_company_profile_edit"),
                             fetch_redirect_response=False)
        # 2. Company profile: numbers are visible right away, summaries wait for verification
        self.submit_profile()
        self.assertEqual(self._current(), "verified")
        preview = self.client.get(reverse("talent_pool_preview"))
        self.assertEqual(preview.context["stats"]["candidates"], 3)
        self.assertEqual(preview.context["previews"], [])
        self.approve()
        # 3. Publish an internship for free (our team reviews it first)
        self.assertEqual(self._current(), "posted")
        self.client.post(reverse("partner_submit_internship"), OPPORTUNITY)
        self.assertEqual(self._current(), "published")
        self.assertContains(self.client.get(reverse("startup_upgrade")), "Your opportunity is waiting for our review.")
        self.assertContains(self.client.get(reverse("checkout")), "Your opportunity is waiting for our review.")
        self._publish_submissions()
        self.assertEqual(self._current(), "pass")
        # 4–7. Explore numbers, filter by skill, anonymous summaries, the method
        python = Skill.objects.get(slug="python").pk
        preview = self.client.get(reverse("talent_pool_preview"), {"skill": python})
        self.assertEqual(preview.context["skill_id"], python)
        self.assertEqual(len(preview.context["previews"]), 3)
        self.assertContains(preview, "Unlock candidate details")
        self.assertContains(preview, f'href="{reverse("trust")}"')
        self.assertEqual(self.client.get(reverse("trust")).status_code, 200)
        # 8. Decide: plans → checkout → our team confirms the payment
        plans = self.client.get(reverse("startup_upgrade"))
        self.assertContains(plans, "Purchase Hiring Pass")
        job = Internship.objects.get(partner=self.partner())
        receipt = SimpleUploadedFile("r.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png")
        self.client.post(reverse("checkout"), {"customer_name": "Mona Adel", "customer_email": "mona@startupx.io",
                                               "customer_phone": "01012345678", "accept_terms": "on", "internship": job.pk,
                                               "payment_proof": receipt})
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.final_price, order.internship_id), (500, job.pk))
        self.assertRedirects(self.client.get(reverse("talent_pool")), reverse("talent_pool_preview"), fetch_redirect_response=False)
        from .entitlements import activate_order

        activate_order(order.pk)
        self.assertIsNone(self._journey())  # paying companies don't see the journey any more
        self.assertEqual(self.client.get(reverse("talent_pool")).status_code, 200)

    def test_journey_strip_is_status_only_and_bilingual(self):
        self.register()
        self.submit_profile()
        page = self.client.get(reverse("partner_dashboard"))
        strip = page.content.decode().split('class="journey', 1)[1].split("</section>", 1)[0]
        self.assertNotIn("<a ", strip)  # actions live in the dashboard cards, once
        self.assertIn('aria-current="step"', strip)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("partner_dashboard")), "طريقك للتوظيف")


class BackendRestrictionTests(ApplicationsBase):
    """A free (verified, posting) company calling paid endpoints directly gets nothing paid."""

    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        StudentProfile.objects.filter(pk=self.student.pk).update(talent_pool_visible=True)
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit",
                                    status="verified", score=93, percentile=81, verified_via="challenge")
        self.app = Application.objects.create(internship=self.gig, applicant=self.user)
        self.client.force_login(self.founder)

    def test_paid_endpoints_refuse_free_accounts(self):
        preview = reverse("talent_pool_preview")
        self.assertRedirects(self.client.get(reverse("talent_pool")), preview, fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("talent_pool_candidate", args=[self.student.pk])), preview,
                             fetch_redirect_response=False)
        self.client.post(reverse("talent_pool_invite", args=[self.student.pk]), {"internship": self.gig.pk})
        self.assertFalse(TalentInvitation.objects.exists())
        self.client.post(reverse("screening_task_edit", args=[self.gig.pk]), {"title": "T", "instructions": "I", "deliverable": "text",
                                                                              "criteria_text": "A | 1", "pass_mark": 50, "due_days": 3})
        self.assertFalse(ScreeningTask.objects.exists())
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[self.student.pk])).status_code, 403)
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertNotContains(page, "93%")
        self.assertNotContains(page, "P81")
        self.assertContains(page, "Unlock candidate details")

    def test_universities_cannot_reach_company_paid_features(self):
        uni = User.objects.create_user("uni", password="pw")
        PartnerProfile.objects.create(user=uni, company_name="Uni", partner_code="UN1", is_academic=True, is_fully_verified=True)
        self.client.force_login(uni)
        for url in (reverse("talent_pool"), reverse("talent_pool_preview")):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_conversion_pages_use_no_fake_urgency(self):
        for url in (reverse("startup_upgrade"), reverse("talent_pool_preview"), reverse("checkout"), reverse("partner_dashboard"),
                    reverse("trust")):
            html = self.client.get(url).content.decode()
            self.assertIsNone(URGENCY.search(html), url)
            self.assertNotIn("countdown", html.lower(), url)


class JourneyReviewFixTests(ApplicationsBase):
    """Findings of the independent Phase 4 review."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.founder)

    def _close_all(self):
        Internship.objects.filter(partner=self.partner).update(is_active=False)

    def test_closed_opportunity_is_not_described_as_in_review_and_no_loop(self):
        from .journey import next_free_post_date

        PartnerInternshipSubmission.objects.create(partner=self.partner, title="Used post", description="d", location="Cairo",
                                                   required_majors="any", deadline=self.gig.deadline, status="Approved")
        self._close_all()
        journey = self.client.get(reverse("partner_dashboard")).context["journey"]
        self.assertIn("Your opportunity is closed and this month's free post is used.", str(journey["hint"]))
        self.assertNotIn("reviews your opportunity", str(journey["hint"]))
        for url in (reverse("startup_upgrade"), reverse("checkout")):
            page = self.client.get(url)
            self.assertContains(page, "free post is used. You can post a new opportunity for free on")
            self.assertNotContains(page, "Post your opportunity first (free)")
        self.assertGreater(next_free_post_date(), timezone.localdate())

    def test_limit_card_modal_explains_a_pass_adds_no_posts(self):
        PartnerInternshipSubmission.objects.create(partner=self.partner, title="Used post", description="d", location="Cairo",
                                                   required_majors="any", deadline=self.gig.deadline, status="Pending")
        page = self.client.get(reverse("partner_dashboard"))
        self.assertEqual(page.context["upsell_reason"], "limit")
        self.assertContains(page, "A Hiring Pass does not add posts")

    def test_filter_rule_is_explained_and_unverified_get_no_dead_links(self):
        _verified = [_student(f"p{i}") for i in range(3)]
        for _u, st in _verified:
            StudentProfile.objects.filter(pk=st.pk).update(talent_pool_visible=True)
            StudentSkill.objects.create(student=st, skill=Skill.objects.get(slug="python"), source="explicit", status="verified", score=80)
        page = self.client.get(reverse("talent_pool_preview"))
        self.assertContains(page, "be filtered, to protect their anonymity")
        PartnerProfile.objects.filter(pk=self.partner.pk).update(is_fully_verified=False)
        page = self.client.get(reverse("talent_pool_preview"))
        self.assertNotContains(page, 'href="?skill=')

    def test_applicant_cta_targets_this_opportunity_or_explains(self):
        user, st = _student("amr")
        StudentSkill.objects.create(student=st, skill=Skill.objects.get(slug="python"), source="explicit", status="verified", score=90)
        app = Application.objects.create(internship=self.gig, applicant=user)
        page = self.client.get(reverse("partner_applicant", args=[app.pk]))
        self.assertContains(page, f'href="{reverse("checkout")}?opportunity={self.gig.pk}"')
        self._close_all()
        page = self.client.get(reverse("partner_applicant", args=[app.pk]))
        self.assertContains(page, "can be bought only for an open opportunity")
        self.assertNotContains(page, f"?opportunity={self.gig.pk}")

    def test_current_pass_holders_cannot_take_a_founding_seat(self):
        from . import entitlements

        entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertEqual(entitlements.founding_block_reason(self.partner), "Founding 20 is for companies without paid access.")

    def test_checkout_states_fees_plainly(self):
        self.assertContains(self.client.get(reverse("checkout")), "We charge exactly this total.")
