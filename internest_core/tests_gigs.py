"""Verified work experience: completion + rating, PDF letter with QR, public verification, proof of work."""
import shutil
import tempfile

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, GigExperience
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill
from internest_startups.tiers import activate_pro

TEMP_MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class GigExperienceTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        self.app = Application.objects.create(internship=self.gig, applicant=self.user, status=Application.STATUS_ACCEPTED)
        self.client.force_login(self.founder)

    def _complete(self, rating=4, review="Delivered a clean landing page two days early. Great communication."):
        return self.client.post(reverse("partner_applicant_complete", args=[self.app.pk]),
                                {"star_rating": rating, "review_text": review}, follow=True)

    def test_completion_creates_experience_and_pdf(self):
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertContains(page, "Mark gig as completed and rate")
        resp = self._complete()
        self.assertContains(resp, "verified recommendation letter is ready")
        exp = GigExperience.objects.get()
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, exp.star_rating, exp.student, exp.company, exp.gig_title),
                         (Application.STATUS_COMPLETED, 4, self.student, self.partner, "Design gig"))
        with exp.pdf_file.open("rb") as f:
            self.assertEqual(f.read(4), b"%PDF")
        self.assertNotContains(self.client.get(reverse("partner_applicant", args=[self.app.pk])), "Mark gig as completed and rate")
        self._complete(rating=1)  # second submission is ignored
        self.assertEqual(GigExperience.objects.count(), 1)

    def test_arabic_feedback_renders(self):
        self._complete(review="طالب ممتاز، سلّم التصميم قبل الموعد وكان التواصل معه سهلاً جداً.")
        with GigExperience.objects.get().pdf_file.open("rb") as f:
            self.assertEqual(f.read(4), b"%PDF")

    def test_validation_and_status_rules(self):
        self._complete(rating=7)
        self._complete(review="short")
        self.assertFalse(GigExperience.objects.exists())
        Application.objects.filter(pk=self.app.pk).update(status=Application.STATUS_SHORTLISTED)
        self._complete()
        self.assertFalse(GigExperience.objects.exists())  # only accepted gigs can be completed

    def test_other_startup_cannot_complete(self):
        other = type(self.founder).objects.create_user("other_founder")
        partner = type(self.partner).objects.create(user=other, company_name="Other", partner_code="OT1", is_fully_verified=True)
        from internest_startups.models import CompanyProfile
        CompanyProfile.objects.create(partner=partner, industry="software", founded_year=2022, description="d")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("partner_applicant_complete", args=[self.app.pk]),
                                          {"star_rating": 5, "review_text": "x" * 20}).status_code, 404)

    def test_public_verification_and_letter(self):
        self._complete()
        exp = GigExperience.objects.get()
        self.client.logout()  # also clears cookies: back to English
        self.client.cookies["internest_lang"] = "en"
        page = self.client.get(reverse("verify_experience", args=[exp.verification_uuid]))
        self.assertEqual(page.status_code, 200)
        for text in ("Officially certified and verified document from Internest", "Amr", "NileCode", "Design gig", "4/5", exp.verification_code):
            self.assertContains(page, text)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("verify_experience", args=[exp.verification_uuid])),
                            "وثيقة معتمدة وموثّقة رسميًا من منصة Internest")
        pdf = self.client.get(reverse("experience_letter", args=[exp.verification_uuid]))
        self.assertEqual((pdf.status_code, pdf["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(pdf.content[:4], b"%PDF")
        self.assertIn(self.client.get(f"/media/{exp.pdf_file.name}").status_code, (302, 404))  # no direct media access
        exp.pdf_file.storage.delete(exp.pdf_file.name)
        self.assertEqual(self.client.get(reverse("experience_letter", args=[exp.verification_uuid])).status_code, 200)  # rebuilt
        self.assertEqual(self.client.get("/verify/00000000-0000-0000-0000-000000000000/").status_code, 404)

    def test_student_profile_and_applications(self):
        self._complete(rating=5)
        self.login_student(self.user)
        profile = self.client.get(reverse("profile"))
        self.assertContains(profile, "Completed 1 verified gigs | Average rating ⭐️ 5.0/5")
        self.assertContains(profile, "Download the verified recommendation letter")
        self.assertNotContains(profile, "🏅 Top Verified Candidate</span>")
        self.assertContains(self.client.get(reverse("my_applications")), "Recommendation letter")
        for i in range(2):
            GigExperience.objects.create(student=self.student, company=self.partner, gig_title=f"Gig {i}",
                                         completion_date=timezone.now().date(), star_rating=4, review_text="Solid work, on time.")
        self.assertContains(self.client.get(reverse("profile")), "🏅 Top Verified Candidate</span>")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("profile")), "أنجز 3 جيجات موثّقة | متوسط التقييم ⭐️ 4.3/5")

    def test_top_verified_ranked_first_in_talent_pool(self):
        activate_pro(self.partner)
        python = Skill.objects.get(slug="python")
        star_user, star = _student("star")
        type(star).objects.filter(pk__in=[star.pk, self.student.pk]).update(talent_pool_visible=True)
        StudentSkill.objects.create(student=star, skill=python, source="explicit", status="verified", score=99)
        StudentSkill.objects.create(student=self.student, skill=python, source="explicit", status="verified", score=75)
        for i in range(3):
            GigExperience.objects.create(student=self.student, company=self.partner, gig_title=f"Gig {i}",
                                         completion_date=timezone.now().date(), star_rating=5, review_text="Excellent.")
        page = self.client.get(reverse("talent_pool"))
        self.assertEqual([c.user.username for c in page.context["page"]], ["amr", "star"])  # 3 gigs beat a higher score
        self.assertContains(page, "🏅 Top Verified Candidate")
        self.assertContains(page, "✅ 3 verified gigs · ⭐ 5.0/5")
        preview = self.client.get(reverse("talent_pool_candidate", args=[self.student.pk]))
        self.assertContains(preview, "Verified work experience")
        self.assertContains(preview, "Excellent.")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class LetterDeliveryTests(ApplicationsBase):
    """The letter link must never 404: stored file → on-the-fly render → plain fallback PDF."""

    def setUp(self):
        super().setUp()
        _user, student = _student("lina")
        self.exp = GigExperience.objects.create(student=student, company=self.partner, gig_title="Design gig",
                                                completion_date=timezone.now().date(), star_rating=5, review_text="Great work.")
        self.url = reverse("experience_letter", args=[self.exp.verification_uuid])

    def _pdf(self):
        resp = self.client.get(self.url)
        self.assertEqual((resp.status_code, resp["Content-Type"]), (200, "application/pdf"))
        self.assertIn("attachment;", resp["Content-Disposition"])
        self.assertEqual(resp.content[:4], b"%PDF")
        return resp.content

    def test_generated_on_the_fly_without_stored_file(self):
        self.assertFalse(self.exp.pdf_file)
        self._pdf()
        self.exp.refresh_from_db()
        self.assertTrue(self.exp.pdf_file)  # stored for next time

    def test_fallback_pdf_when_rendering_fails(self):
        from unittest import mock

        with mock.patch("internest_core.letters.render_letter", side_effect=ImportError("No module named 'reportlab'")):
            content = self._pdf()
        self.assertIn(self.exp.verification_code.encode(), content)
        self.assertIn(b"/verify/", content)

    def test_served_even_if_storage_fails(self):
        from unittest import mock

        with mock.patch("django.db.models.fields.files.FieldFile.save", side_effect=OSError("read-only")):
            self._pdf()

    def test_verification_page_shows_qr(self):
        page = self.client.get(reverse("verify_experience", args=[self.exp.verification_uuid]))
        self.assertContains(page, 'src="data:image/svg+xml;base64,')
        self.assertContains(page, f"http://testserver/verify/{self.exp.verification_uuid}/")
        self.assertContains(page, self.exp.verification_code)
