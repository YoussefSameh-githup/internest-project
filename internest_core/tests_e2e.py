"""Phase 9: end-to-end QA across every role, through real HTTP requests (forms, admin actions, gates).

    StudentJourney        signup → email code → gate → complete profile → browse → apply with consent → track → export
    FreeCompanyJourney    register → company profile → admin approval → post → admin publish → applicant → shortlist
                          → CV unlocks → plans page → close with 0 applicants restores the free post
    PaidCompanyJourney    checkout with a receipt → admin activates → Talent Pool → invite → accept → complete
                          → public experience-letter check
    UniversityJourney     partner dashboard + skills dashboard
    AdminJourney          every registered admin changelist and add form renders; review queue; security log
    SmokeCrawl            every named GET page, for every role, in Arabic and English: never a server error

Where a step needs something only a human or a provider can do (a passed timed assessment, a real payment), the
test sets that one fact in the database and says so; every other step goes through the same URLs a browser uses.
"""
import io
import os
import re
import shutil
import tempfile
from datetime import timedelta

from PIL import Image

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone, translation

from internest_skills.models import Skill, StudentSkill
from internest_startups.models import CompanyProfile, ProUpgradeRequest

from .models import (
    Application, EmailVerification, GigExperience, Internship, PartnerInternshipSubmission, PartnerProfile,
    SecurityEvent, StudentProfile,
)

MEDIA = tempfile.mkdtemp()
PDF = b"%PDF-1.4\n% e2e\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PASSWORD = "Str0ng-pass-2026!"
STARTUP_SIGNUP = {"full_name": "Mona Adel", "email": "mona@startupx.io", "password1": PASSWORD, "password2": PASSWORD}
COMPANY_PROFILE = {
    "company_name": "StartupX", "official_email": "contact@startupx.io", "founder_email": "mona@startupx.io",
    "official_website": "https://startupx.io", "linkedin_url": "https://linkedin.com/company/startupx",
    "facebook_url": "", "twitter_url": "", "instagram_url": "",
    "industry": "software", "founded_year": "2023", "description": "We build hiring tools for Egyptian SMEs.",
    "country_of_registration": "EG",
}


def opportunity_form(title="Frontend gig"):
    return {"title": title, "description": "React work on our dashboard", "required_majors": "CS",
            "location_mode": "online", "deadline": (timezone.localdate() + timedelta(days=20)).isoformat()}


def admin_action(client, model_label, action, ids):
    app_label, model = model_label.split(".")
    return client.post(reverse(f"admin:{app_label}_{model}_changelist"),
                       {"action": action, "_selected_action": [str(i) for i in ids], "index": 0}, follow=True)


@override_settings(MEDIA_ROOT=MEDIA)
class E2EBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.root = User.objects.create_superuser("root", "root@example.com", PASSWORD)

    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.admin = self.client_class()
        self.admin.cookies["internest_lang"] = "en"
        self.admin.force_login(self.root)

    def ready_student(self, username="sara", visible=True):
        """A student who signed up, verified their email and completed their profile (the profile form itself is covered
        by tests_profile*); one skill verified as if they passed its timed assessment (covered by tests_assessment)."""
        user = User.objects.create_user(username, email=f"{username}@uni.edu", password=PASSWORD,
                                        first_name=username.title(), last_name="Test")
        profile = StudentProfile.objects.create(
            user=user, personal_email=f"{username}@uni.edu", personal_email_verified_at=timezone.now(),
            university="Cairo University", major="CS", study_level="3", linkedin_url="https://linkedin.com/in/x",
            cv_file=SimpleUploadedFile("cv.pdf", PDF), profile_picture=SimpleUploadedFile("p.png", PNG),
            talent_pool_visible=visible)
        profile.calculate_completion()
        skill, _ = Skill.objects.get_or_create(slug="e2e-react", defaults={"name": "React", "is_active": True})
        StudentSkill.objects.create(student=profile, skill=skill, source=StudentSkill.SOURCE_EXPLICIT,
                                    status=StudentSkill.STATUS_VERIFIED, score=82, percentile=80)
        return user, profile

    def sign_in_student(self, client, user):
        """Every student login issues a fresh email code (signals.re_verify_emails_on_login); enter it like a person."""
        client.force_login(user)
        code = EmailVerification.objects.filter(user=user, verified_at__isnull=True).latest("created_at").code
        client.post(reverse("verify_email"), {"code": code, "email_type": "personal"})
        self.assertEqual(client.get(reverse("my_applications")).status_code, 200)  # past the gate

    def company(self, client):
        """Register a startup through the public form and complete its profile (no admin step yet)."""
        self.assertRedirects(client.post(reverse("startup_register"), STARTUP_SIGNUP), reverse("startup_company_profile_edit"),
                             fetch_redirect_response=False)
        client.post(reverse("startup_company_profile_edit"), COMPANY_PROFILE)
        return PartnerProfile.objects.get(user__username="mona@startupx.io")

    def approve_company(self, partner):
        admin_action(self.admin, "internest_startups.pendingcompanyprofile", "approve_startups",
                     CompanyProfile.objects.filter(partner=partner).values_list("pk", flat=True))
        partner.refresh_from_db()

    def publish(self, client, partner, title="Frontend gig"):
        client.post(reverse("partner_submit_internship"), opportunity_form(title))
        submission = PartnerInternshipSubmission.objects.get(partner=partner, title=title)
        admin_action(self.admin, "internest_core.partnerinternshipsubmission", "approve_submissions", [submission.pk])
        return Internship.objects.get(partner=partner, title=title)

    def apply(self, client, internship):
        return client.post(reverse("apply", args=[internship.pk]), {"contact_consent": "on"})


class StudentJourney(E2EBase):
    def test_signup_to_application_to_export(self):
        c = self.client
        self.assertEqual(c.get(reverse("landing")).status_code, 200)
        r = c.post(reverse("signup"), {"username": "nour", "personal_email": "nour@uni.edu",
                                       "password1": PASSWORD, "password2": PASSWORD})
        self.assertRedirects(r, reverse("verify_email"), fetch_redirect_response=False)
        code = EmailVerification.objects.get(user__username="nour").code
        self.assertTrue(any(code in m.body for m in mail.outbox))
        self.assertRedirects(c.get(reverse("list")), reverse("verify_email"), fetch_redirect_response=False)  # gate
        # The gate page has no site menu: it must offer its own sign-out (wrong e-mail, shared phone).
        self.assertContains(c.get(reverse("verify_email")), f'<form method="post" action="{reverse("logout")}"')
        c.post(reverse("verify_email"), {"code": "000000" if code != "000000" else "111111", "email_type": "personal"})
        self.assertRedirects(c.get(reverse("list")), reverse("verify_email"), fetch_redirect_response=False)  # wrong code
        c.post(reverse("verify_email"), {"code": code, "email_type": "personal"})
        self.assertEqual(c.get(reverse("list")).status_code, 200)

        # an incomplete profile cannot apply yet
        partner_user = User.objects.create_user("acme", password=PASSWORD)
        acme = PartnerProfile.objects.create(user=partner_user, company_name="Acme", partner_code="AC1", is_fully_verified=True)
        CompanyProfile.objects.create(partner=acme, industry="software", founded_year=2020, description="Acme")
        job = Internship.objects.create(partner=acme, title="Data intern", description="SQL", location="Cairo",
                                        required_majors="any", deadline=timezone.localdate() + timedelta(days=9))
        self.assertRedirects(self.apply(c, job), reverse("apply_error", args=[job.pk]), fetch_redirect_response=False)

        # complete the profile through the real form (CV + photo uploads, content-checked)
        photo = io.BytesIO()
        Image.new("RGB", (64, 64), (30, 90, 200)).save(photo, "PNG")
        r = c.post(reverse("profile"), {
            "personal_email": "nour@uni.edu", "university": "Cairo University", "major": "CS", "study_level": "3",
            "linkedin_url": "https://linkedin.com/in/nour", "email_new_opportunities": "on",
            "cv_file": SimpleUploadedFile("Nour CV.pdf", PDF, "application/pdf"),
            "profile_picture": SimpleUploadedFile("me.png", photo.getvalue(), "image/png")})
        self.assertEqual(r.status_code, 302, r.content.decode()[:300] if r.status_code == 200 else "")
        profile = StudentProfile.objects.get(user__username="nour")
        self.assertEqual(profile.profile_completion_score, 100)
        self.assertRegex(profile.cv_file.name, r"^cvs/[0-9a-f]{32}\.pdf$")  # stored without the student's name

        self.assertContains(c.get(reverse("internship_detail", args=[job.pk])), "Data intern")
        self.assertContains(c.post(reverse("apply", args=[job.pk]), {}), "Please agree to sharing")  # consent required
        self.assertFalse(Application.objects.exists())
        self.apply(c, job)
        app = Application.objects.get(internship=job, applicant__username="nour")
        self.assertIsNotNone(app.contact_consent_at)
        self.assertContains(c.get(reverse("my_applications")), "Data intern")
        for page in ("profile", "skills_hub", "student_dashboard", "cv_builder"):
            self.assertEqual(c.get(reverse(page)).status_code, 200, page)

        data = c.post(reverse("account_export")).json()
        self.assertEqual(data["records"]["internest_core.application"][0]["id"], app.pk)


class FreeCompanyJourney(E2EBase):
    def test_register_post_shortlist_and_limits(self):
        c = self.client
        partner = self.company(c)
        self.assertRedirects(c.get(reverse("partner_submit_internship")), reverse("partner_dashboard"),
                             fetch_redirect_response=False)  # waits for admin approval of the company
        self.approve_company(partner)
        job = self.publish(c, partner)
        self.assertTrue(Internship.objects.open().filter(pk=job.pk).exists())
        self.assertContains(c.get(reverse("partner_dashboard")), "Frontend gig")

        # a second post in the same month goes to the plans page
        r = c.get(reverse("partner_submit_internship"))
        self.assertRedirects(r, reverse("partner_dashboard") + "?upsell=limit", fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("startup_upgrade")).status_code, 200)

        student = self.client_class()
        user, profile = self.ready_student()
        self.sign_in_student(student, user)
        self.apply(student, job)
        app = Application.objects.get(internship=job)

        self.assertEqual(c.get(reverse("partner_applicant", args=[app.pk])).status_code, 200)
        self.assertEqual(c.get(reverse("partner_applicant_cv", args=[app.pk])).status_code, 404)  # locked before shortlist
        sent_before = len(mail.outbox)
        c.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "shortlist"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_SHORTLISTED)
        self.assertIsNotNone(app.shortlisted_at)
        self.assertEqual(mail.outbox[sent_before].to, [profile.personal_email])  # the student is told
        cv = c.get(reverse("partner_applicant_cv", args=[app.pk]))
        self.assertEqual(cv.status_code, 200)
        self.assertEqual(b"".join(cv.streaming_content), PDF)
        self.assertEqual(student.get(reverse("my_applications")).status_code, 200)

    def test_closing_an_empty_opportunity_gives_the_free_post_back(self):
        c = self.client
        partner = self.company(c)
        self.approve_company(partner)
        job = self.publish(c, partner, "Empty post")
        c.post(reverse("partner_close_opportunity", args=[job.pk]))
        job.refresh_from_db()
        self.assertFalse(job.is_active)
        self.assertEqual(c.get(reverse("partner_submit_internship")).status_code, 200)  # the free post is back
        second = self.publish(c, partner, "Second empty post")
        c.post(reverse("partner_close_opportunity", args=[second.pk]))
        self.assertRedirects(c.get(reverse("partner_submit_internship")), reverse("partner_dashboard") + "?upsell=limit",
                             fetch_redirect_response=False)  # restored once a month, not every time


class PaidCompanyJourney(E2EBase):
    def test_pay_activate_talent_pool_invite_hire_and_letter(self):
        c = self.client
        partner = self.company(c)
        self.approve_company(partner)
        job = self.publish(c, partner)
        pool = c.get(reverse("talent_pool"))  # no pass yet: the anonymised preview, not the pool
        self.assertEqual(pool.status_code, 302)
        self.assertIn(reverse("talent_pool_preview"), pool.url)

        order_form = {"customer_name": "Mona Adel", "customer_email": "mona@startupx.io", "customer_phone": "01000000000",
                      "accept_terms": "on", "internship": job.pk, "payment_reference": "IP-123"}
        fake = c.post(reverse("checkout"), {**order_form, "payment_proof": SimpleUploadedFile("r.png", b"MZ exe", "image/png")})
        self.assertEqual(fake.status_code, 200)  # the form comes back with the error
        self.assertContains(fake, "doesn&#x27;t match its type")
        self.assertFalse(ProUpgradeRequest.objects.filter(partner=partner).exists())  # no order from a fake receipt

        r = c.post(reverse("checkout"), {**order_form, "payment_proof": SimpleUploadedFile("receipt.png", PNG, "image/png")})
        self.assertEqual(r.status_code, 302)
        order = ProUpgradeRequest.objects.get(partner=partner)
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_VERIFYING)  # never active before a human checks it
        self.assertRegex(order.payment_proof.name, r"^payment_proofs/[0-9a-f]{32}\.png$")

        admin_action(self.admin, "internest_startups.proupgraderequest", "activate_requests", [order.pk])
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_ACTIVE)
        self.assertTrue(SecurityEvent.objects.filter(action="order_activated", target=f"order:{order.pk}").exists())

        user, profile = self.ready_student()
        rust, _ = Skill.objects.get_or_create(slug="e2e-rust", defaults={"name": "Rust", "is_active": True})
        job.required_skills.add(rust)  # the student has React only: the skill gate is closed without an invitation
        pool = c.get(reverse("talent_pool"))
        self.assertContains(pool, "React")
        r = c.post(reverse("talent_pool_invite", args=[profile.pk]), {"internship": job.pk})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(profile.talent_invitations.filter(internship=job).exists())

        student = self.client_class()
        self.sign_in_student(student, user)
        invite = profile.talent_invitations.get(internship=job)
        student.post(reverse("apply", args=[job.pk]), {"contact_consent": "on"})
        self.assertFalse(Application.objects.filter(internship=job).exists())  # skill gate holds without the link
        student.post(reverse("apply", args=[job.pk]), {"contact_consent": "on", "invite_token": invite.token})
        app = Application.objects.get(internship=job, applicant=user)
        invite.refresh_from_db()
        self.assertEqual((app.source, invite.status), (Application.SOURCE_INVITED, invite.STATUS_ACCEPTED))
        c.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "shortlist"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_SHORTLISTED)
        c.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "accept"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_ACCEPTED)
        c.post(reverse("partner_applicant_complete", args=[app.pk]),
               {"star_rating": 5, "review_text": "Shipped the dashboard early, clear communication throughout."})
        exp = GigExperience.objects.get(application=app)
        anonymous = self.client_class()
        self.assertEqual(anonymous.get(reverse("verify_experience", args=[exp.verification_uuid])).status_code, 200)


class UniversityJourney(E2EBase):
    def test_students_share_and_universities_see_only_their_own(self):
        uni_user = User.objects.create_user("uni", password=PASSWORD)
        uni = PartnerProfile.objects.create(user=uni_user, company_name="Cairo University", partner_code="UNI1",
                                            is_academic=True, is_fully_verified=True)
        other_user = User.objects.create_user("uni2", password=PASSWORD)
        PartnerProfile.objects.create(user=other_user, company_name="Ain Shams University", partner_code="UNI2",
                                      is_academic=True, is_fully_verified=True)
        sharer, sharer_profile = self.ready_student("sharer")
        _quiet, quiet_profile = self.ready_student("quiet")
        student = self.client_class()
        self.sign_in_student(student, sharer)
        student.post(reverse("skills_share_university"), {"partner_university": uni.pk})

        self.client.force_login(uni_user)
        for name in ("partner_dashboard", "skills_university_dashboard"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[sharer_profile.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[quiet_profile.pk])).status_code, 403)
        other = self.client_class()
        other.force_login(other_user)
        self.assertEqual(other.get(reverse("skills_student_report", args=[sharer_profile.pk])).status_code, 403)


class DecisionStateTests(E2EBase):
    """Phase 9 QA bugs: decisions are final, closing never rewrites finished applications."""

    def setUp(self):
        super().setUp()
        self.partner = self.company(self.client)
        self.approve_company(self.partner)
        self.job = self.publish(self.client, self.partner)
        self.user, _ = self.ready_student()
        self.student = self.client_class()
        self.sign_in_student(self.student, self.user)
        self.apply(self.student, self.job)
        self.app = Application.objects.get(internship=self.job)

    def decide(self, decision):
        self.client.post(reverse("partner_applicant_decide", args=[self.app.pk]), {"decision": decision})
        self.app.refresh_from_db()
        return self.app.status

    def complete(self):
        self.client.post(reverse("partner_applicant_complete", args=[self.app.pk]),
                         {"star_rating": 5, "review_text": "Great work, delivered on time and well documented."})
        self.app.refresh_from_db()

    def test_closing_keeps_completed_work_and_cv_access(self):
        self.decide("accept")
        self.complete()
        self.assertEqual(self.app.status, Application.STATUS_COMPLETED)
        self.client.post(reverse("partner_close_opportunity", args=[self.job.pk]))
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_COMPLETED)

    def test_finished_decisions_cannot_be_changed(self):
        self.decide("accept")
        self.complete()
        self.assertEqual(self.decide("fulfilled"), Application.STATUS_COMPLETED)
        self.assertEqual(self.decide("accept"), Application.STATUS_COMPLETED)

    def test_no_decisions_after_closing(self):
        self.client.post(reverse("partner_close_opportunity", args=[self.job.pk]))
        self.assertEqual(self.decide("accept"), Application.STATUS_FULFILLED)
        self.assertEqual(self.decide("shortlist"), Application.STATUS_FULFILLED)

    def test_accepting_directly_tells_the_student(self):
        sent = len(mail.outbox)
        self.assertEqual(self.decide("accept"), Application.STATUS_ACCEPTED)
        self.assertIsNotNone(self.app.shortlisted_at)
        self.assertEqual(len(mail.outbox), sent + 1)

    def test_staff_never_become_students_and_resend_needs_post(self):
        self.admin.get(reverse("profile"))
        self.assertFalse(StudentProfile.objects.filter(user=self.root).exists())
        self.assertEqual(self.student.get(reverse("resend_verification", args=["personal"])).status_code, 405)


class AdminJourney(E2EBase):
    def test_every_admin_page_renders(self):
        self.ready_student()
        failures = []
        for model, model_admin in django_admin.site._registry.items():
            info = (model._meta.app_label, model._meta.model_name)
            for view in ("changelist", "add"):
                url = reverse(f"admin:{info[0]}_{info[1]}_{view}")
                status = self.admin.get(url).status_code
                if status not in (200, 302, 403):
                    failures.append((url, status))
            obj = model._default_manager.first()
            if obj is not None:
                status = self.admin.get(reverse(f"admin:{info[0]}_{info[1]}_change", args=[obj.pk])).status_code
                if status not in (200, 302, 403):
                    failures.append((f"{info} change", status))
        self.assertEqual(failures, [])
        self.assertEqual(self.admin.get(reverse("skills_review_queue")).status_code, 200)


# ---------------------------------------------------------------- smoke crawl
def _named_patterns(resolver=None, prefix=""):
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            if entry.namespace == "admin" or str(entry.pattern).startswith(("admin", "oauth", "i18n")):
                continue
            yield from _named_patterns(entry, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern) and entry.name:
            yield entry.name, entry


SKIP = {"serve_media", "robots", "sitemap", "healthz", "async_task_status", "async_task_cancel", "logout",
        "skills_api_next", "skills_api_answer", "skills_api_event", "cv_builder_pdf", "experience_letter",
        "lounge_thread_poll", "checkout_return", "social_start"}


class SmokeCrawl(E2EBase):
    """GET every named page as each role, in both languages. A 500 (or an exception) anywhere fails the test."""

    def _kwargs(self, pattern, ids):
        names = re.findall(r"<(?:\w+:)?(\w+)>", str(pattern.pattern))
        kwargs = {}
        for name in names:
            if name not in ids:
                return None
            kwargs[name] = ids[name]
        return kwargs

    def test_no_page_errors_for_any_role(self):
        student_user, profile = self.ready_student()
        partner_user = User.objects.create_user("co", password=PASSWORD)
        partner = PartnerProfile.objects.create(user=partner_user, company_name="Co", partner_code="CO1", is_fully_verified=True)
        CompanyProfile.objects.create(partner=partner, industry="software", founded_year=2020, description="Co")
        job = Internship.objects.create(partner=partner, title="Smoke job", description="d", location="Cairo",
                                        required_majors="any", deadline=timezone.localdate() + timedelta(days=5))
        app = Application.objects.create(internship=job, applicant=student_user, contact_consent_at=timezone.now())
        ids = {"pk": job.pk, "internship_id": job.pk, "app_id": app.pk, "student_id": profile.pk,
               "student_skill_id": profile.skills.first().pk, "skill_id": profile.skills.first().skill_id,
               "role": "student", "provider": "google", "email_type": "personal", "section": "overview",
               "task_id": 1, "course_id": 1, "enrollment_id": 1, "order_id": 1, "outcome": "success", "gig_id": 1}
        roles = {"anonymous": None, "student": student_user, "company": partner_user, "staff": self.root}
        failures, statuses = [], {}
        for lang in ("en", "ar"):
            for role, user in roles.items():
                client = self.client_class()
                client.cookies["internest_lang"] = lang
                if user is student_user:
                    self.sign_in_student(client, user)
                elif user:
                    client.force_login(user)
                for name, pattern in _named_patterns():
                    if name in SKIP:
                        continue
                    kwargs = self._kwargs(pattern, ids)
                    if kwargs is None:
                        continue
                    try:
                        url = reverse(name, kwargs=kwargs)
                    except Exception:  # noqa: BLE001 - a route whose converter rejects the placeholder
                        continue
                    try:
                        status = client.get(url).status_code
                    except Exception as exc:  # noqa: BLE001 - collect every failure, not just the first
                        failures.append((lang, role, name, repr(exc)[:160]))
                        continue
                    statuses[(lang, role, name)] = status
                    if status >= 500:
                        failures.append((lang, role, name, status))
        self.assertEqual(failures, [])
        self.assertGreater(len(statuses), 500, "the crawl must actually visit the site (4 roles x 2 languages)")
        if os.getenv("E2E_REPORT"):
            print(f"smoke crawl: {len(statuses)} page loads")

        # Permission matrix: who must NOT get a page (anything but 200 is fine: login redirect, 403, 404, 405).
        forbidden = {
            "anonymous": ["profile", "my_applications", "skills_hub", "student_dashboard", "cv_builder", "account_delete",
                          "partner_dashboard", "partner_applicant", "partner_applicant_cv", "talent_pool", "checkout",
                          "skills_review_queue", "lounge_feed", "startup_upgrade"],
            "student": ["partner_dashboard", "partner_applicant", "partner_applicant_cv", "talent_pool", "checkout",
                        "startup_upgrade", "skills_review_queue", "skills_university_dashboard", "lounge_feed"],
            "company": ["profile", "my_applications", "skills_hub", "cv_builder", "account_delete", "skills_review_queue",
                        "skills_university_dashboard"],
        }
        leaks = [(lang, role, name, statuses[(lang, role, name)])
                 for lang in ("en", "ar") for role, names in forbidden.items() for name in names
                 if statuses.get((lang, role, name)) == 200]
        self.assertEqual(leaks, [])
        for name in ("profile", "partner_dashboard", "talent_pool", "account_delete"):
            self.assertEqual(statuses[("en", "anonymous", name)], 302, name)  # sent to sign in
        for role, name in (("student", "profile"), ("student", "my_applications"), ("company", "partner_dashboard"),
                           ("company", "partner_applicant"), ("staff", "skills_review_queue")):
            self.assertEqual(statuses[("en", role, name)], 200, (role, name))  # and the owners do get their pages
