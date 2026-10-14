"""Security audit regressions: private verification evidence, rate limits, and ownership (IDOR) checks."""
import shutil
import tempfile

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from internest_skills.certificates import submit_certificate
from internest_skills.models import SkillCertificate, StudentFreelanceGig, StudentSkill
from internest_skills.tests import Base
from internest_startups.models import ProUpgradeRequest

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class PrivateEvidenceTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.cert = submit_certificate(self.student, self.skill, "Cert", file=SimpleUploadedFile("c.pdf", b"%PDF-1.4 x", "application/pdf"))
        self.gig = StudentFreelanceGig.objects.create(student=self.student, platform="khamsat", link="https://khamsat.com/x/1",
                                                      raw_text="x", project_title="P",
                                                      proof=SimpleUploadedFile("p.png", b"\x89PNG\r\n\x1a\n0000", "image/png"))
        self.urls = [reverse("serve_media", args=[self.cert.file.name]), reverse("serve_media", args=[self.gig.proof.name])]

    def _status(self, user=None):
        self.client.logout()
        if user:
            self.client.force_login(user)
        return [self.client.get(u).status_code for u in self.urls]

    def test_only_owner_and_staff_can_open_certificates_and_proofs(self):
        self.assertEqual(self._status(), [302, 302])                   # anonymous → login
        self.assertEqual(self._status(self.other_user), [404, 404])    # another student
        self.assertEqual(self._status(self.pro_user), [404, 404])      # a company
        self.assertEqual(self._status(self.student_user), [200, 200])  # owner
        staff = User.objects.create_user("staffer", password="pw", is_staff=True)
        self.assertEqual(self._status(staff), [200, 200])

    def test_path_traversal_is_blocked(self):
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get("/media/../manage.py").status_code, 404)
        self.assertEqual(self.client.get("/media/skill_certificates/..%2F..%2Fmanage.py").status_code, 404)


@override_settings(RATELIMIT_ENABLED=True)
class RateLimitTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_login_is_limited_per_ip_with_a_clean_429(self):
        for _ in range(10):
            self.assertEqual(self.client.post(reverse("login"), {"username": "stud", "password": "wrong"}).status_code, 200)
        blocked = self.client.post(reverse("login"), {"username": "stud", "password": "wrong"})
        self.assertEqual(blocked.status_code, 429)
        self.assertContains(blocked, "Too many attempts", status_code=429)
        self.assertNotContains(blocked, "Traceback", status_code=429)
        other_ip = self.client.post(reverse("login"), {"username": "stud", "password": "wrong"}, REMOTE_ADDR="10.0.0.9")
        self.assertEqual(other_ip.status_code, 200)

    def test_spoofed_forwarded_for_does_not_reset_the_limit(self):
        for i in range(10):
            self.client.post(reverse("login"), {"username": "x", "password": "y"}, HTTP_X_FORWARDED_FOR=f"1.2.3.{i}")
        self.assertEqual(self.client.post(reverse("login"), {}, HTTP_X_FORWARDED_FOR="9.9.9.9").status_code, 429)

    def test_password_reset_and_signup_limits(self):
        for _ in range(5):
            self.client.post(reverse("password_reset"), {"email": "a@example.com"})
        self.assertEqual(self.client.post(reverse("password_reset"), {"email": "a@example.com"}).status_code, 429)
        for _ in range(5):
            self.client.post(reverse("signup"), {})
        self.assertEqual(self.client.post(reverse("signup"), {}).status_code, 429)

    def test_certificate_submissions_are_limited_per_student(self):
        self.client.force_login(self.student_user)
        data = {"skill": self.skill.pk, "title": "C", "url": "https://example.com/c"}
        for _ in range(10):
            self.client.post(reverse("certificate_submit"), data)
        self.assertEqual(self.client.post(reverse("certificate_submit"), data).status_code, 429)
        self.assertEqual(SkillCertificate.objects.filter(student=self.student).count(), 10)

    def test_get_requests_are_never_limited(self):
        for _ in range(15):
            self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_disabled_in_tests_by_default(self):
        with override_settings(RATELIMIT_ENABLED=False):
            for _ in range(12):
                self.assertNotEqual(self.client.post(reverse("login"), {}).status_code, 429)


class OwnershipTests(Base):
    """Students and companies can only reach their own objects (404, never someone else's data)."""

    def test_student_cannot_touch_another_students_skill_or_gig(self):
        theirs = StudentSkill.objects.create(student=self.other_student, skill=self.skill, source="explicit")
        gig = StudentFreelanceGig.objects.create(student=self.other_student, platform="khamsat", link="https://khamsat.com/x/2",
                                                 raw_text="x", project_title="P", status=StudentFreelanceGig.STATUS_NEEDS_PROOF)
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.post(reverse("skills_challenge_start", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("skills_api_recommendations", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("freelance_gig_proof", args=[gig.pk])).status_code, 404)

    def test_certificate_is_always_filed_under_the_signed_in_student(self):
        self.client.force_login(self.student_user)
        self.client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "C", "url": "https://example.com/c",
                                                         "student": self.other_student.pk})
        self.assertEqual(list(SkillCertificate.objects.values_list("student", flat=True)), [self.student.pk])

    def test_company_cannot_open_another_companys_order(self):
        order = ProUpgradeRequest.objects.create(partner=self.free, list_price=100, final_price=100)
        self.client.force_login(self.pro_user)
        resp = self.client.get(reverse("checkout_return", args=[order.pk, "success"]))
        self.assertNotEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_PENDING)
