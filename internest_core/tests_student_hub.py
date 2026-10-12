"""Student Hub profile: hero, tabs, skills bank, freelance proof upload and account settings."""
import shutil
import tempfile

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Internship
from internest_skills.models import Skill, SkillProfile, StudentFreelanceGig, StudentSkill
from internest_skills.tests import Base
from internest_students.models import StudentCV, StudentProject


class StudentHubTests(Base):
    def setUp(self):
        super().setUp()
        self.student_user.first_name, self.student_user.last_name = "Youssef", "Sameh"
        self.student_user.save()
        self.client.force_login(self.student_user)  # login resets email verification, so verify afterwards
        type(self.student).objects.filter(pk=self.student.pk).update(
            linkedin_url="https://linkedin.com/in/youssef", portfolio_url="https://github.com/youssef", personal_email="y@x.com",
            personal_email_verified_at=timezone.now())
        self.student.refresh_from_db()

    def _page(self, **params):
        return self.client.get(reverse("profile"), params)

    def test_hero_identity_strength_and_primary_actions(self):
        StudentCV.objects.create(student=self.student, data={"name": "Youssef", "education": [
            {"university": "Cairo", "major": "Finance", "start_date": "2023-09", "end_date": "2027-06", "is_current": False}]})
        page = self._page()
        for marker in ("Your Student Hub", "Youssef Sameh", "Jun 2027</bdi> (expected)", "LinkedIn connected",
                       "GitHub connected", "Profile strength", "Complete your profile", "CV file",
                       reverse("cv_builder_pdf"), reverse("cv_builder"), reverse("freelance_importer"),
                       "Sep 2023 – Jun 2027 (Expected)"):
            self.assertContains(page, marker)

    def test_without_saved_cv_the_pdf_action_leads_to_the_builder(self):
        page = self._page()
        self.assertNotContains(page, reverse("cv_builder_pdf"))
        self.assertContains(page, "Build it first in 5 quick steps")

    def test_tabs(self):
        page = self._page(tab="skills")
        self.assertContains(page, 'id="tab-skills" href="?tab=skills#hub" data-tab="skills" aria-controls="panel-skills" aria-selected="true"')
        self.assertContains(page, 'data-panel="details" hidden')
        self.assertEqual(self._page(tab="nope").context["active_tab"], "details")
        resp = self.client.post(reverse("profile") + "?tab=settings", {"personal_email": "bad"})
        self.assertEqual(resp.context["active_tab"], "details")  # errors live in the details tab

    def test_skill_chips_by_kind(self):
        for slug, source, status in (("python", "explicit", "claimed"), ("sql", "implicit", "claimed"), ("excel", "explicit", "verified")):
            StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug=slug), source=source, status=status)
        page = self._page(tab="skills").content.decode()
        self.assertIn('px-chip--explicit" title="Take the challenge to verify it">Python', page)
        self.assertIn('px-chip--inferred" title="Take the challenge to verify it">SQL', page)
        self.assertIn('px-chip px-chip--verified">Excel', page)
        self.assertIn("Verified skill ✅", page)
        self.assertIn('data-open-dialog="skill-dialog"', page)

    def test_freelance_cards_and_proof_upload(self):
        gig = StudentFreelanceGig.objects.create(student=self.student, platform="mostaql", link="https://mostaql.com/u/ahmed",
                                                 raw_text="x" * 50, platform_username="ahmed", status="needs_proof")
        StudentProject.objects.create(student=self.student, title="Budget Tracker", tech_stack="Python")
        page = self._page(tab="portfolio")
        for marker in ("Upload a proof screenshot", reverse("freelance_gig_proof", args=[gig.pk]), "@ahmed", "Budget Tracker",
                       '<b title="Needs proof review">1</b>'):
            self.assertContains(page, marker)

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            bad = self.client.post(reverse("freelance_gig_proof", args=[gig.pk]),
                                   {"proof": SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")})
            self.assertRedirects(bad, reverse("profile") + "?tab=portfolio", fetch_redirect_response=False)
            gig.refresh_from_db()
            self.assertFalse(gig.proof)
            png = SimpleUploadedFile("p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png")
            self.client.post(reverse("freelance_gig_proof", args=[gig.pk]), {"proof": png})
            gig.refresh_from_db()
            self.assertTrue(gig.proof.name.startswith("freelance_proofs/"))
            self.assertContains(self._page(tab="portfolio"), "Proof uploaded — waiting for review.")

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(reverse("freelance_gig_proof", args=[gig.pk]), {}).status_code, 404)

    def test_share_with_university(self):
        self.assertContains(self._page(tab="settings"), f'<option value="{self.uni.pk}"')
        self.client.post(reverse("skills_share_university"), {"partner_university": self.uni.pk})
        self.assertEqual(SkillProfile.objects.get(student=self.student).partner_university, self.uni)
        self.client.post(reverse("skills_share_university"), {"partner_university": ""})
        self.assertIsNone(SkillProfile.objects.get(student=self.student).partner_university)

    def test_settings_checkboxes_belong_to_the_profile_form(self):
        page = self._page(tab="settings")
        self.assertContains(page, 'name="talent_pool_visible" id="id_talent_pool_visible" form="pf-form"')
        self.assertContains(page, 'name="email_new_opportunities" form="pf-form"')
        self.assertContains(page, reverse("password_change"))
        data = {"personal_email": "y@x.com", "university": "Cairo", "major": "Finance", "study_level": "3",
                "phone_number": "0100", "linkedin_url": "", "bio": "", "portfolio_url": ""}
        self.client.post(reverse("profile"), data)  # unchecked = opted out
        self.student.refresh_from_db()
        self.assertFalse(self.student.email_new_opportunities)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_new_opportunity_emails_respect_the_preference(self):
        type(self.student).objects.filter(pk=self.student.pk).update(email_new_opportunities=False)
        type(self.other_student).objects.filter(pk=self.other_student.pk).update(personal_email="o@x.com")
        Internship.objects.create(partner=self.pro, title="Analyst", description="d", location="Cairo", required_majors="any",
                                  deadline=timezone.now().date())
        self.assertEqual(mail.outbox[-1].bcc, ["o@x.com"])

    def test_arabic_labels(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self._page()
        for text in ("مساحتك الطلابية", "تحميل الـ CV الاحترافي (PDF)", "توليد CV جديد بالذكاء الاصطناعي", "استيراد عمل حر جديد",
                     "إكمال بيانات الملف", "البيانات الشخصية والتعليم", "بنك المهارات الموثقة", "سجل الأعمال الحرة والمشاريع",
                     "إعدادات الحساب والسرية", "إضافة / توثيق مهارة جديدة"):
            self.assertContains(page, text)
