"""Multi-modal skill verification (certificate review) and the Market Fit dashboard (readiness index, radar, gaps)."""
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Internship

from .certificates import is_trusted_link, reject_certificate, submit_certificate, verify_certificate
from .forms import FreelanceImportForm
from .market_fit import market_demand, market_fit
from .models import Course, Discipline, Skill, SkillCertificate, StudentSkill
from .tests import Base


class CertificateVerificationTests(Base):
    def test_submit_is_under_review_and_claims_the_skill(self):
        cert = submit_certificate(self.student, self.skill, "Financial Modelling", "Coursera", url="https://www.coursera.org/verify/ABC")
        self.assertEqual(cert.status, SkillCertificate.STATUS_UNDER_REVIEW)
        self.assertTrue(cert.trusted_issuer)
        ss = StudentSkill.objects.get(student=self.student, skill=self.skill)
        self.assertEqual(ss.status, StudentSkill.STATUS_CLAIMED)  # a link alone never verifies

    def test_verify_marks_the_skill_verified_via_certificate(self):
        cert = submit_certificate(self.student, self.skill, "CFA Level 1", url="https://example.com/cert")
        self.assertFalse(cert.trusted_issuer)
        self.assertTrue(verify_certificate(cert))
        cert.refresh_from_db()
        ss = StudentSkill.objects.get(student=self.student, skill=self.skill)
        self.assertEqual(cert.status, SkillCertificate.STATUS_VERIFIED)
        self.assertIsNotNone(cert.reviewed_at)
        self.assertEqual((ss.status, ss.verified_via), (StudentSkill.STATUS_VERIFIED, StudentSkill.VIA_CERTIFICATE))
        self.assertIsNotNone(ss.verified_at)

    def test_verify_keeps_an_earlier_challenge_verification(self):
        StudentSkill.objects.create(student=self.student, skill=self.skill, source=StudentSkill.SOURCE_EXPLICIT,
                                    status=StudentSkill.STATUS_VERIFIED, verified_via=StudentSkill.VIA_CHALLENGE, score=88)
        cert = submit_certificate(self.student, self.skill, "Extra cert", url="https://example.com/c")
        self.assertFalse(verify_certificate(cert))
        ss = StudentSkill.objects.get(student=self.student, skill=self.skill)
        self.assertEqual((ss.verified_via, ss.score), (StudentSkill.VIA_CHALLENGE, 88))

    def test_reject_leaves_the_skill_unverified(self):
        cert = submit_certificate(self.student, self.skill, "Fake", url="https://example.com/f")
        reject_certificate(cert)
        cert.refresh_from_db()
        self.assertEqual(cert.status, SkillCertificate.STATUS_REJECTED)
        self.assertEqual(StudentSkill.objects.get(student=self.student, skill=self.skill).status, StudentSkill.STATUS_CLAIMED)

    def test_trusted_hosts_match_subdomains_only(self):
        self.assertTrue(is_trusted_link("https://www.credly.com/badges/1"))
        self.assertFalse(is_trusted_link("https://credly.com.evil.example/x"))
        self.assertFalse(is_trusted_link(""))

    def test_admin_approve_action(self):
        from django.contrib.auth.models import User
        admin_user = User.objects.create_superuser("boss", "boss@example.com", "pw")
        cert = submit_certificate(self.student, self.skill, "Cert", url="https://example.com/c")
        self.client.force_login(admin_user)
        url = reverse("admin:internest_skills_skillcertificate_changelist")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"action": "approve", "_selected_action": [cert.pk]})
        self.assertEqual(StudentSkill.objects.get(student=self.student, skill=self.skill).verified_via, StudentSkill.VIA_CERTIFICATE)


class CertificateSubmitViewTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_link_submission_creates_a_certificate_under_review(self):
        response = self.client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "Valuation",
                                                                    "url": "coursera.org/verify/XYZ"}, follow=True)
        cert = SkillCertificate.objects.get(student=self.student)
        self.assertEqual((cert.status, cert.url), (SkillCertificate.STATUS_UNDER_REVIEW, "https://coursera.org/verify/XYZ"))
        self.assertContains(response, "under review")

    def test_file_submission(self):
        pdf = SimpleUploadedFile("cert.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "Valuation", "file": pdf})
        self.assertTrue(SkillCertificate.objects.get(student=self.student).file)

    def test_needs_a_link_or_a_file(self):
        self.client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "Valuation"})
        self.assertFalse(SkillCertificate.objects.exists())

    def test_rejects_external_next(self):
        response = self.client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "V", "url": "https://x.example/c",
                                                                    "next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("student_dashboard"))

    def test_github_is_a_freelance_platform(self):
        self.assertIn("github", dict(FreelanceImportForm.base_fields["platform"].choices))


class ReadinessTests(Base):
    """Readiness = demand covered by verified skills / total demand of the top skills in open opportunities."""

    def setUp(self):
        super().setUp()
        self.today = timezone.now().date()
        self.python, self.sql = Skill.objects.get(slug="python"), Skill.objects.get(name="SQL")

    def _gig(self, *skills, active=True, days=10):
        gig = Internship.objects.create(partner=self.pro, title="Gig", description="d", location="Cairo", required_majors="any",
                                        deadline=self.today + timedelta(days=days), is_active=active)
        gig.required_skills.set(skills)
        return gig

    def _verify(self, skill, score=None):
        return StudentSkill.objects.create(student=self.student, skill=skill, source=StudentSkill.SOURCE_EXPLICIT,
                                           status=StudentSkill.STATUS_VERIFIED, verified_via=StudentSkill.VIA_CHALLENGE, score=score)

    def test_only_open_opportunities_count(self):
        self._gig(self.python, self.sql)
        self._gig(self.python)
        self._gig(self.sql, active=False)       # inactive
        self._gig(self.sql, days=-1)            # expired
        self.assertEqual([(s.name, n) for s, n in market_demand()], [(self.python.name, 2), ("SQL", 1)])

    def test_readiness_is_weighted_by_demand(self):
        self._gig(self.python, self.sql)
        self._gig(self.python)
        self._gig(self.python)
        self.assertEqual(market_fit(self.student).readiness, 0)
        self._verify(self.python, score=80)
        fit = market_fit(self.student)
        self.assertEqual(fit.readiness, 75)     # python covers 3 of 4 required-skill slots
        self.assertEqual([s.name for s, _n in fit.gaps], ["SQL"])
        self.assertEqual(fit.open_opportunities, 3)
        self.assertEqual(fit.radar, {"labels": [self.python.name, "SQL"], "market": [100, 33], "student": [80, 0]})

    def test_claimed_or_lagging_skills_do_not_count(self):
        self._gig(self.sql)
        StudentSkill.objects.create(student=self.student, skill=self.sql, source=StudentSkill.SOURCE_EXPLICIT, status=StudentSkill.STATUS_LAG)
        self.assertEqual(market_fit(self.student).readiness, 0)

    def test_certificate_verification_counts_as_full_level(self):
        self._gig(self.sql)
        verify_certificate(submit_certificate(self.student, self.sql, "SQL cert", url="https://example.com/s"))
        fit = market_fit(self.student)
        self.assertEqual((fit.readiness, fit.radar["student"]), (100, [100]))

    def test_discipline_filter(self):
        self._gig(self.python, self.skill)
        fit = market_fit(self.student, self.skill.discipline)
        self.assertEqual([s.pk for s, _n in fit.demand], [self.skill.pk])
        self.assertEqual(market_fit(self.student, "not-a-field").demand, market_fit(self.student).demand)

    def test_no_open_opportunities(self):
        fit = market_fit(self.student)
        self.assertEqual((fit.readiness, fit.demand, fit.gaps), (0, [], []))


class DashboardViewTests(ReadinessTests):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_dashboard_shows_index_radar_and_gap_actions(self):
        self._gig(self.python, self.sql)
        self._verify(self.python, score=90)
        Course.objects.create(title="SQL for analysts", skill=self.sql, provider_name="Academy", url="https://learn.example/sql", is_partner=True)
        page = self.client.get(reverse("student_dashboard"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "50%")
        self.assertContains(page, 'id="radar-data"')
        self.assertEqual(page.context["radar"]["student"], [90, 0])
        self.assertContains(page, reverse("skills_challenge_start_for_skill", args=[self.sql.pk]))
        self.assertContains(page, f'data-cert-skill="{self.sql.pk}"')
        self.assertContains(page, "SQL for analysts")
        self.assertContains(page, "Certified partner")
        self.assertContains(page, "chart.js@4.4.1")

    def test_gap_without_catalogue_course_falls_back_to_search(self):
        self._gig(self.sql)
        page = self.client.get(reverse("student_dashboard"))
        self.assertContains(page, "coursera.org")

    def test_empty_state_and_certificate_list(self):
        submit_certificate(self.student, self.skill, "My cert", url="https://example.com/c")
        page = self.client.get(reverse("student_dashboard"))
        self.assertContains(page, "no open opportunities")
        self.assertContains(page, "My cert")
        self.assertContains(page, "Under review")

    def test_arabic(self):
        self.client.cookies["internest_lang"] = "ar"
        self._gig(self.sql)
        page = self.client.get(reverse("student_dashboard"))
        self.assertContains(page, "مؤشر الجاهزية")

    def test_requires_student(self):
        self.client.force_login(self.pro_user)
        self.assertNotEqual(self.client.get(reverse("student_dashboard")).status_code, 200)
