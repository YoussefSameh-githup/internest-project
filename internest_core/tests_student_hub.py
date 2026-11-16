"""Student Hub profile: sticky identity card, four tabs, skill cards, freelance proof upload and account settings."""
import shutil
import tempfile

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, Internship
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

    def test_identity_card_readiness_and_primary_actions(self):
        StudentCV.objects.create(student=self.student, data={"name": "Youssef", "education": [
            {"university": "Cairo", "major": "Finance", "start_date": "2023-09", "end_date": "2027-06", "is_current": False}]})
        page = self._page()
        for marker in ("Your Student Hub", "Youssef Sameh", "Jun 2027</bdi> (expected)", "LinkedIn connected",
                       "GitHub connected", "Profile readiness", "Complete your profile", "CV file", "Take AI Skill Assessment",
                       reverse("cv_builder_pdf"), reverse("cv_builder"), reverse("freelance_importer"),
                       "Sep 2023 – Jun 2027 (Expected)"):
            self.assertContains(page, marker)

        self.assertContains(page, "Edit / regenerate")
        self.assertNotContains(page, "Create your CV with AI")

    def test_without_saved_cv_one_create_action(self):
        page = self._page()
        self.assertNotContains(page, reverse("cv_builder_pdf"))
        self.assertNotContains(page, "Edit / regenerate")
        self.assertContains(page, "Create your CV with AI", count=1)

    def test_single_entry_points(self):
        page = self._page().content.decode()
        self.assertEqual(page.count(f'href="{reverse("freelance_importer")}"'), 1)  # only the identity card button
        dialog = page[page.index('id="skill-dialog"'):]
        self.assertNotIn(reverse("freelance_importer"), dialog)
        for marker in ('name="pasted_text"', 'name="cv_file"', 'data-mode="text"', f'value="{reverse("profile")}?tab=skills"'):
            self.assertIn(marker, dialog)
        self.assertIn("Take a skill challenge", dialog)

    def test_market_fit_lives_in_the_hub_tabs_not_the_profile_card(self):
        card = self._page().content.decode().split('id="tab-overview"')[0]
        self.assertNotIn(reverse("student_dashboard"), card)
        self.assertContains(self.client.get(reverse("course_list")), f'href="{reverse("student_dashboard")}" class="sh-tab"')

    def test_tabs(self):
        page = self._page(tab="skills")
        self.assertContains(page, 'id="tab-skills" href="?tab=skills#hub" data-tab="skills" aria-controls="panel-skills" aria-selected="true"')
        self.assertContains(page, 'data-panel="overview" hidden')
        for name in ("Overview & badges", "Skills & AI assessments", "Internship activity", "Edit profile & settings"):
            self.assertContains(page, name)
        self.assertEqual(self._page().context["active_tab"], "overview")
        self.assertEqual(self._page(tab="nope").context["active_tab"], "overview")
        resp = self.client.post(reverse("profile") + "?tab=skills", {"personal_email": "bad"})
        self.assertEqual(resp.context["active_tab"], "edit")  # errors live in the edit tab

    def test_old_tab_names_still_open_the_right_tab(self):
        for old, new in (("details", "edit"), ("settings", "edit"), ("portfolio", "activity")):
            self.assertEqual(self._page(tab=old).context["active_tab"], new)

    def test_edit_sections_open_on_errors(self):
        resp = self.client.post(reverse("profile"), {"personal_email": "bad", "linkedin_url": "nope"})
        self.assertEqual(resp.context["open_sections"], {"contact", "about"})
        page = resp.content.decode()
        self.assertEqual(page.count('<details class="px-acc" open>'), 2)  # contact + about open, files closed
        self.assertEqual(self._page().context["open_sections"], {"contact"})

    def test_overview_shows_verified_badges_first(self):
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="excel"), source="explicit",
                                    status="verified", score=88, percentile=91, verified_via="challenge")
        page = self._page().content.decode()
        panel = page[page.index('id="panel-overview"'):page.index('id="panel-skills"')]
        self.assertIn("AI-verified skill badges", panel)
        self.assertIn('class="px-sbadge"', panel)
        self.assertIn("88%", panel)
        self.assertIn("P91", panel)
        self.assertIn("Verified skill ✅", panel)

    def test_overview_empty_state_points_to_the_assessment(self):
        page = self._page().content.decode()
        panel = page[page.index('id="panel-overview"'):page.index('id="panel-skills"')]
        self.assertIn("No verified skills yet", panel)
        self.assertIn(f'href="{reverse("skills_hub")}"', panel)

    def test_skill_cards_verify_with_one_click(self):
        skills = {}
        for slug, source, status in (("python", "explicit", "claimed"), ("sql", "implicit", "claimed"), ("excel", "explicit", "verified")):
            skills[slug] = StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug=slug), source=source, status=status)
        page = self._page(tab="skills").content.decode()
        panel = page[page.index('id="panel-skills"'):page.index('id="panel-activity"')]
        self.assertIn("Verified by AI (1)", panel)
        self.assertIn("Ready to verify (2)", panel)
        self.assertEqual(panel.count('class="px-vcard"'), 2)
        self.assertEqual(panel.count('class="px-sbadge"'), 1)
        for slug in ("python", "sql"):
            self.assertIn(f'action="{reverse("skills_challenge_start", args=[skills[slug].pk])}"', panel)
        self.assertNotIn(reverse("skills_challenge_start", args=[skills["excel"].pk]), panel)  # verified: nothing to start
        self.assertIn("From your CV", panel)
        self.assertIn("AI-inferred", panel)
        self.assertIn('data-open-dialog="skill-dialog"', panel)
        self.assertIn("data-skill-filter", panel)

    def test_skill_in_cooldown_has_no_start_button(self):
        ss = StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit", status="lag",
                                         cooldown_until=timezone.now() + timezone.timedelta(hours=5))
        panel = self._page(tab="skills").content.decode()
        self.assertNotIn(reverse("skills_challenge_start", args=[ss.pk]), panel)
        self.assertIn("You can retake this skill test in", panel)
        self.assertIn("Almost there", panel)

    def test_activity_tab_lists_applications_and_invitations(self):
        internship = Internship.objects.create(partner=self.pro, title="Finance Analyst", description="d", location="Cairo",
                                               required_majors="any", deadline=timezone.now().date() + timezone.timedelta(days=5))
        Application.objects.create(internship=internship, applicant=self.student_user, status="shortlisted")
        panel = self._page(tab="activity").content.decode()
        self.assertIn("Finance Analyst", panel)
        self.assertIn("You&#x27;re shortlisted for an interview", panel.replace("&#39;", "&#x27;"))
        self.assertIn(reverse("my_applications"), panel)
        self.assertNotIn("You haven't applied yet", panel)

    def test_activity_tab_empty_state(self):
        panel = self._page(tab="activity").content.decode()
        self.assertIn("applied yet", panel)
        self.assertIn(f'href="{reverse("list")}"', panel)

    def test_share_verified_skills_needs_a_verified_skill(self):
        self.assertNotContains(self._page(), "data-share-url=")
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="excel"), source="explicit", status="verified")
        page = self._page()
        self.assertContains(page, "data-share-url=")
        self.assertContains(page, "AI-verified in Excel on Internest")
        self.assertContains(page, "linkedin.com/sharing/share-offsite")
        self.assertNotContains(page, self.student.phone_number or "no-phone-in-share")

    def test_freelance_cards_and_proof_upload(self):
        gig = StudentFreelanceGig.objects.create(student=self.student, platform="mostaql", link="https://mostaql.com/u/ahmed",
                                                 raw_text="x" * 50, platform_username="ahmed", status="needs_proof")
        StudentProject.objects.create(student=self.student, title="Budget Tracker", tech_stack="Python")
        page = self._page(tab="activity")
        for marker in ("Upload a proof screenshot", reverse("freelance_gig_proof", args=[gig.pk]), "@ahmed", "Budget Tracker",
                       '<b title="Needs proof review">1</b>'):
            self.assertContains(page, marker)

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            bad = self.client.post(reverse("freelance_gig_proof", args=[gig.pk]),
                                   {"proof": SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")})
            self.assertRedirects(bad, reverse("profile") + "?tab=activity", fetch_redirect_response=False)
            gig.refresh_from_db()
            self.assertFalse(gig.proof)
            png = SimpleUploadedFile("p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png")
            self.client.post(reverse("freelance_gig_proof", args=[gig.pk]), {"proof": png})
            gig.refresh_from_db()
            self.assertTrue(gig.proof.name.startswith("freelance_proofs/"))
            self.assertContains(self._page(tab="activity"), "Proof uploaded — waiting for review.")

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(reverse("freelance_gig_proof", args=[gig.pk]), {}).status_code, 404)

    def test_share_with_university(self):
        self.assertContains(self._page(tab="edit"), f'<option value="{self.uni.pk}"')
        self.client.post(reverse("skills_share_university"), {"partner_university": self.uni.pk})
        self.assertEqual(SkillProfile.objects.get(student=self.student).partner_university, self.uni)
        self.client.post(reverse("skills_share_university"), {"partner_university": ""})
        self.assertIsNone(SkillProfile.objects.get(student=self.student).partner_university)

    def test_settings_checkboxes_belong_to_the_profile_form(self):
        page = self._page(tab="edit")
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
        for text in ("مساحتك الطلابية", "أنشئ سيرتك الذاتية بالذكاء الاصطناعي", "استيراد عمل حر", "لصق نص", "حلّل مهاراتي", "خُض اختبار مهارة",
                     "إكمال بيانات الملف", "نظرة عامة والشارات", "المهارات وتقييمات الذكاء الاصطناعي", "نشاط التدريب",
                     "تعديل الملف والإعدادات", "جاهزية الملف", "ابدأ تقييم المهارات بالذكاء الاصطناعي", "بيانات التواصل والدراسة",
                     "الحساب والأمان", "إضافة / توثيق مهارة جديدة"):
            self.assertContains(page, text)
