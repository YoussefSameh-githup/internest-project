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


# ---------------------------------------------------------------- audit 2: hardening regressions
from django.core.exceptions import ValidationError  # noqa: E402
from django.test import Client, RequestFactory  # noqa: E402

from internest_core.models import Application, Internship, PartnerProfile  # noqa: E402
from internest_startups.models import PromoCode  # noqa: E402


@override_settings(MEDIA_ROOT=MEDIA)
class MediaHeaderTests(Base):
    def test_uploads_are_sandboxed_nosniff_and_private_ones_uncached(self):
        cert = submit_certificate(self.student, self.skill, "Cert", file=SimpleUploadedFile("c.pdf", b"%PDF-1.4 x", "application/pdf"))
        self.client.force_login(self.student_user)
        resp = self.client.get(reverse("serve_media", args=[cert.file.name]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sandbox", resp["Content-Security-Policy"])
        self.assertIn("default-src 'none'", resp["Content-Security-Policy"])
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp["Cache-Control"], "private, no-store")

    def test_svg_logos_are_rejected(self):
        with self.assertRaises(ValidationError):
            PartnerProfile._meta.get_field("logo").run_validators(SimpleUploadedFile("x.svg", b"<svg onload=alert(1)>", "image/svg+xml"))


class SessionAndCsrfTests(Base):
    def test_logout_is_post_only_and_the_navbar_posts_it(self):
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("course_list"))
        self.assertContains(page, f'<form id="logout-form" action="{reverse("logout")}" method="post"')
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)  # a link/image can't log people out
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("logout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_state_changes_need_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.pro_user)
        self.assertEqual(client.post(reverse("checkout"), {"action": "cancel_pending"}).status_code, 403)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        client.force_login(self.student_user)
        self.assertEqual(client.post(reverse("certificate_submit"), {"skill": self.skill.pk, "title": "x", "url": "https://e.x/c"}).status_code, 403)
        self.assertFalse(SkillCertificate.objects.exists())

    def test_session_cookie_is_http_only(self):
        from django.conf import settings
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertIn(settings.SESSION_COOKIE_SAMESITE, ("Lax", "Strict"))


class XssAndIdorTests(Base):
    def setUp(self):
        super().setUp()
        self.student.bio = '<script>alert("x")</script><img src=x onerror=alert(1)>'
        self.student.save(update_fields=["bio"])
        self.gig = Internship.objects.create(partner=self.pro, title="Gig", description="d", location="Cairo", required_majors="any",
                                             deadline=self.student.user.date_joined.date() + __import__("datetime").timedelta(days=400))
        self.app = Application.objects.create(internship=self.gig, applicant=self.student_user)

    def test_student_bio_is_escaped_for_companies(self):
        self.client.force_login(self.pro_user)
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, '<script>alert("x")</script>')
        self.assertNotContains(page, "<img src=x onerror")
        self.assertContains(page, "&lt;script&gt;")

    def test_company_cannot_open_another_companys_applicant_or_cv(self):
        self.client.force_login(self.free_user)
        for name in ("partner_applicant", "partner_applicant_cv"):
            self.assertEqual(self.client.get(reverse(name, args=[self.app.pk])).status_code, 404)
        self.client.post(reverse("partner_applicant_decide", args=[self.app.pk]), {"decision": "shortlist"})
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_SUBMITTED)

    def test_student_cannot_open_company_pages_or_skill_reports_of_others(self):
        self.client.force_login(self.other_user)
        self.assertNotEqual(self.client.get(reverse("partner_applicant", args=[self.app.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[self.student.pk])).status_code, 403)


class PaymentIntegrityTests(Base):
    def _order(self, partner, promo=None, status=ProUpgradeRequest.STATUS_PENDING):
        return ProUpgradeRequest.objects.create(partner=partner, list_price=800, final_price=400 if promo else 800, promo_code=promo, status=status)

    def test_single_use_promo_is_reserved_by_an_open_order(self):
        promo = PromoCode.objects.create(code="ONCE", discount_percent=50, max_uses=1)
        self.assertTrue(promo.is_valid())
        order = self._order(self.pro, promo, ProUpgradeRequest.STATUS_VERIFYING)
        self.assertFalse(promo.is_valid())  # nobody else can apply it while that order is open
        order.status = ProUpgradeRequest.STATUS_CANCELLED
        order.save(update_fields=["status"])
        self.assertTrue(promo.is_valid())

    def test_admin_activation_ignores_get_requests(self):
        order = self._order(self.free, status=ProUpgradeRequest.STATUS_VERIFYING)
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        url = reverse("admin:internest_startups_proupgraderequest_changelist")
        self.client.get(url, {"action": "activate_requests", "_selected_action": order.pk})
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_VERIFYING)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(User.objects.get(username="boss"))
        self.assertEqual(csrf_client.post(url, {"action": "activate_requests", "_selected_action": [order.pk]}).status_code, 403)
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_VERIFYING)

    def test_pricing_ignores_spoofed_forwarded_for(self):
        from internest_startups.pricing import _client_ip
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="41.0.0.1", REMOTE_ADDR="10.0.0.9")
        self.assertEqual(_client_ip(request), "10.0.0.9")


@override_settings(RATELIMIT_ENABLED=True)
class AdminBruteForceTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_admin_login_shares_the_login_limit(self):
        url = reverse("admin:login")
        for _ in range(10):
            self.client.post(url, {"username": "boss", "password": "wrong"})
        self.assertEqual(self.client.post(url, {"username": "boss", "password": "wrong"}).status_code, 429)


# ---------------------------------------------------------------- courses: completion & certificates
import uuid  # noqa: E402

from internest_core.models import PartnerCourseSubmission, StudentEnrollment  # noqa: E402


class CourseAccessTests(Base):
    def setUp(self):
        super().setUp()
        self.course = PartnerCourseSubmission.objects.create(partner=self.pro, title="Finance 101", description="d", price=100,
                                                             instructor_name="X", status="Approved", skill=self.skill)
        self.enrollment = StudentEnrollment.objects.create(student=self.student, course=self.course, final_price=100)
        self.url = reverse("partner_course_complete", args=[self.enrollment.pk])

    def _status(self):
        self.enrollment.refresh_from_db()
        return self.enrollment.status

    def test_students_cannot_complete_their_own_course(self):
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self._status(), StudentEnrollment.STATUS_ENROLLED)

    def test_other_companies_cannot_complete_it(self):
        self.client.force_login(self.free_user)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self._status(), StudentEnrollment.STATUS_ENROLLED)

    def test_completion_needs_post_and_csrf(self):
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.pro_user)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)
        self.assertEqual(self._status(), StudentEnrollment.STATUS_ENROLLED)
        self.client.post(self.url)
        self.assertEqual(self._status(), StudentEnrollment.STATUS_COMPLETED)

    def test_certificates_only_exist_for_completed_enrollments(self):
        self.assertEqual(self.client.get(reverse("course_certificate", args=[self.enrollment.certificate_id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("course_certificate", args=[uuid.uuid4()])).status_code, 404)

    def test_enrollment_needs_a_post(self):
        other = PartnerCourseSubmission.objects.create(partner=self.pro, title="Excel", description="d", price=50,
                                                       instructor_name="X", status="Approved")
        self.client.force_login(self.student_user)
        self.client.get(reverse("course_checkout", args=[other.pk]), {"confirm_purchase": "1"})
        self.assertFalse(StudentEnrollment.objects.filter(course=other).exists())

    def test_task_results_are_private(self):
        from internest_core.models import GamificationTask, StudentTaskRecord
        task = GamificationTask.objects.create(title="T", description="d")
        StudentTaskRecord.objects.create(student=self.student, task=task, correct=1, total=2)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("task_result", args=[task.pk])).status_code, 404)


class ContentSecurityPolicyTests(Base):
    PAGES = ("landing", "login", "privacy", "university_landing", "signup")

    def test_every_page_carries_the_policy(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                csp = self.client.get(reverse(name))["Content-Security-Policy"]
                for rule in ("default-src 'self'", "object-src 'none'", "base-uri 'self'", "frame-ancestors 'none'",
                             "script-src 'self' 'unsafe-inline' https:", "img-src 'self' data: blob: https:"):
                    self.assertIn(rule, csp)
                self.assertNotIn("unsafe-eval", csp)

    def test_signed_in_pages_redirects_and_404s_too(self):
        self.client.force_login(self.student_user)
        for resp in (self.client.get(reverse("course_list")), self.client.get(reverse("partner_dashboard")),
                     self.client.get("/definitely-not-a-page/")):
            self.assertIn("Content-Security-Policy", resp)

    def test_admin_allows_eval_for_its_ui_only(self):
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        self.assertIn("'unsafe-eval'", self.client.get(reverse("admin:index"))["Content-Security-Policy"])
