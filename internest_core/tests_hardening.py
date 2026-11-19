"""Phase 7 security hardening: upload content checks, random file names, malware-scan hook, headers, rate limits,
audit trail, and student self-service export / deletion."""
import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta
from unittest import mock

from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from internest_skills.models import SkillCertificate, StudentFreelanceGig
from internest_startups.models import ProUpgradeRequest

from . import upload_security
from .models import Application, EmailVerification, Internship, PartnerProfile, SecurityEvent, StudentProfile
from .ratelimit import RULES
from .upload_security import RandomName, scan_upload, validate_file_signature

MEDIA = tempfile.mkdtemp()
PDF = b"%PDF-1.4\n% real enough\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def docx(with_document=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if with_document:
            z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


class SignatureTests(TestCase):
    def _check(self, name, content):
        validate_file_signature(SimpleUploadedFile(name, content))

    def test_real_files_pass(self):
        for name, content in (("cv.pdf", PDF), ("a.png", PNG), ("a.jpg", JPG), ("a.JPEG", JPG), ("a.webp", WEBP),
                              ("cv.docx", docx()), ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8)):
            self._check(name, content)

    def test_renamed_files_are_rejected(self):
        for name, content in (("cv.pdf", b"MZ\x90\x00 an exe"), ("a.png", JPG), ("a.jpg", b"<svg onload=alert(1)>"),
                              ("a.webp", b"RIFF\x00\x00\x00\x00AVI LIST"), ("cv.docx", docx(with_document=False)),
                              ("cv.docx", b"PK\x03\x04 broken zip"), ("cv.pdf", b"")):
            with self.subTest(name=name, head=content[:8]), self.assertRaises(ValidationError):
                self._check(name, content)

    def test_file_position_is_kept(self):
        f = SimpleUploadedFile("cv.pdf", PDF)
        f.seek(3)
        validate_file_signature(f)
        self.assertEqual(f.tell(), 3)

    def test_every_upload_field_runs_the_checks(self):
        fields = [StudentProfile._meta.get_field("cv_file"), StudentProfile._meta.get_field("profile_picture"),
                  PartnerProfile._meta.get_field("logo"), StudentFreelanceGig._meta.get_field("proof"),
                  SkillCertificate._meta.get_field("file"), ProUpgradeRequest._meta.get_field("payment_proof")]
        for field in fields:
            with self.subTest(field=str(field)):
                self.assertIn(validate_file_signature, field.validators)
                self.assertIn(scan_upload, field.validators)


@override_settings(MEDIA_ROOT=MEDIA)
class StoredFileTests(TestCase):
    """Review finding 1: the checks run on new uploads only, never on files already stored."""

    def test_editing_a_profile_with_an_old_mismatched_file_still_works(self):
        user, profile = _student("legacy")
        os.makedirs(os.path.join(MEDIA, "profile_pics"), exist_ok=True)
        with open(os.path.join(MEDIA, "profile_pics", "old.jpg"), "wb") as fh:
            fh.write(PNG)  # a PNG saved as .jpg years ago
        StudentProfile.objects.filter(pk=profile.pk).update(profile_picture="profile_pics/old.jpg")
        profile.refresh_from_db()
        with override_settings(CLAMAV_HOST="clamd.local"),                 mock.patch.object(upload_security.socket, "create_connection", side_effect=OSError("down")):
            profile.full_clean()  # no re-check, no re-scan

    def test_a_new_bad_upload_is_rejected_through_the_model(self):
        _user, profile = _student("fresh")
        profile.cv_file = SimpleUploadedFile("cv.pdf", b"MZ not a pdf")
        with self.assertRaises(ValidationError) as ctx:
            profile.full_clean()
        self.assertIn("cv_file", ctx.exception.message_dict)

    def test_oversized_files_are_left_to_the_size_check(self):
        big = SimpleUploadedFile("cv.docx", b"x" * (5 * 1024 * 1024 + 1))
        validate_file_signature(big)  # not read or unzipped here
        with mock.patch.object(upload_security.socket, "create_connection") as conn, override_settings(CLAMAV_HOST="c"):
            scan_upload(big)
        conn.assert_not_called()


class RequestSizeTests(TestCase):
    @override_settings(MAX_REQUEST_BYTES=1000)
    def test_oversized_bodies_get_413(self):
        response = self.client.post(reverse("contact"), {"message": "x" * 2000})
        self.assertEqual(response.status_code, 413)


class RandomNameTests(TestCase):
    def test_name_is_random_folder_and_extension_kept(self):
        upload_to = RandomName("cvs")
        a, b = upload_to(None, "Nour Ahmed CV.PDF"), upload_to(None, "Nour Ahmed CV.PDF")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^cvs/[0-9a-f]{32}\.pdf$")
        self.assertNotIn("Nour", a)

    def test_private_folders_use_random_names(self):
        for model, field in ((StudentProfile, "cv_file"), (StudentProfile, "profile_picture"), (StudentFreelanceGig, "proof"),
                             (SkillCertificate, "file"), (ProUpgradeRequest, "payment_proof")):
            self.assertIsInstance(model._meta.get_field(field).upload_to, RandomName, field)


class ScanTests(TestCase):
    def test_not_configured_is_a_no_op(self):
        with override_settings(CLAMAV_HOST=""), mock.patch.object(upload_security.socket, "create_connection") as conn:
            scan_upload(SimpleUploadedFile("cv.pdf", PDF))
        conn.assert_not_called()

    def _fake_clamd(self, reply):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.return_value = reply
        return sock

    @override_settings(CLAMAV_HOST="clamd.local")
    def test_infected_file_is_rejected_and_clean_file_passes(self):
        with mock.patch.object(upload_security.socket, "create_connection", return_value=self._fake_clamd(b"stream: OK\0")):
            scan_upload(SimpleUploadedFile("cv.pdf", PDF))
        with mock.patch.object(upload_security.socket, "create_connection",
                               return_value=self._fake_clamd(b"stream: Eicar-Signature FOUND\0")):
            with self.assertRaises(ValidationError):
                scan_upload(SimpleUploadedFile("cv.pdf", PDF))

    @override_settings(CLAMAV_HOST="clamd.local")
    def test_error_or_empty_replies_fail_closed(self):
        for reply in (b"INSTREAM size limit exceeded. ERROR\0", b"Can't allocate memory ERROR\0", b""):
            with self.subTest(reply=reply),                     mock.patch.object(upload_security.socket, "create_connection", return_value=self._fake_clamd(reply)):
                with self.assertRaises(ValidationError):
                    scan_upload(SimpleUploadedFile("cv.pdf", PDF))

    @override_settings(CLAMAV_HOST="clamd.local")
    def test_unreachable_scanner_fails_closed(self):
        with mock.patch.object(upload_security.socket, "create_connection", side_effect=OSError("refused")):
            with self.assertRaises(ValidationError):
                scan_upload(SimpleUploadedFile("cv.pdf", PDF))


class HeaderTests(TestCase):
    def test_permissions_policy_and_csp(self):
        response = self.client.get(reverse("landing"))
        policy = response["Permissions-Policy"]
        for rule in ("camera=()", "geolocation=()", "microphone=(self)", "payment=()"):
            self.assertIn(rule, policy)
        csp = response["Content-Security-Policy"]
        for rule in ("object-src 'none'", "base-uri 'self'", "frame-ancestors 'none'"):
            self.assertIn(rule, csp)

    @override_settings(MEDIA_ROOT=MEDIA)
    def test_media_keeps_its_sandbox_policy(self):
        os.makedirs(os.path.join(MEDIA, "partner_logos"), exist_ok=True)
        with open(os.path.join(MEDIA, "partner_logos", "l.png"), "wb") as fh:
            fh.write(PNG)
        self.client.force_login(User.objects.create_user("viewer", password="pw"))
        response = self.client.get(reverse("serve_media", args=["partner_logos/l.png"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("sandbox", response["Content-Security-Policy"])


class RateLimitRuleTests(TestCase):
    def test_public_and_abuse_prone_forms_are_limited(self):
        for name in ("verify_email", "startup_register", "contact", "university_inquiry", "contact_partner", "apply",
                     "skills_challenge_start", "screening_submit", "founding_claim", "account_export", "account_delete"):
            self.assertIn(name, RULES)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_contact_form_is_limited(self):
        from django.core.cache import cache
        cache.clear()
        limit = RULES["contact"][0]
        codes = [self.client.post(reverse("contact"), {}).status_code for _ in range(limit + 1)]
        self.assertNotIn(429, codes[:limit])
        self.assertEqual(codes[-1], 429)
        cache.clear()


def _student(username, email=None, password="pw-Strong-123"):
    user = User.objects.create_user(username, email=email or f"{username}@example.com", password=password)
    profile = StudentProfile.objects.create(user=user, university="Cairo", major="CS", study_level="3")
    return user, profile


@override_settings(MEDIA_ROOT=MEDIA)
class PrivacySelfServiceTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.user, self.profile = _student("nour")
        self.profile.cv_file = SimpleUploadedFile("Nour CV.pdf", PDF)
        self.profile.save()
        partner_user = User.objects.create_user("co", password="pw")
        self.partner = PartnerProfile.objects.create(user=partner_user, company_name="Co", partner_code="C1")
        self.internship = Internship.objects.create(partner=self.partner, title="Intern", description="d", location="Cairo",
                                                    required_majors="any", deadline=timezone.now().date())
        Application.objects.create(internship=self.internship, applicant=self.user, contact_consent_at=timezone.now())
        EmailVerification.objects.create(user=self.user, email_type=EmailVerification.EMAIL_TYPE_PERSONAL,
                                         email=self.user.email, code="424242",
                                         expires_at=timezone.now() + timedelta(minutes=30))

    def test_export_is_post_only_and_has_the_students_data_without_secrets(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_export")).status_code, 405)
        response = self.client.post(reverse("account_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        body = response.content.decode()
        data = json.loads(body)
        self.assertEqual(data["records"]["auth.user"][0]["username"], "nour")
        self.assertEqual(data["records"]["internest_core.application"][0]["internship"], self.internship.pk)
        self.assertTrue(data["records"]["internest_core.studentprofile"][0]["cv_file"].startswith("cvs/"))
        self.assertNotIn("pbkdf2", body)
        self.assertNotIn("424242", body)
        self.assertNotIn('"password"', body)
        self.assertNotIn("internest_core.partnerprofile", data["records"])  # the company's own data is not theirs
        self.assertTrue(SecurityEvent.objects.filter(action="data_export", actor=self.user).exists())

    def test_integrity_signals_and_reviewer_notes_are_not_exported(self):
        from django.apps import apps

        from .account_privacy import HIDDEN_FIELDS
        for label, names in HIDDEN_FIELDS.items():  # guards against a renamed field silently leaking again
            fields = {f.name for f in apps.get_model(label)._meta.concrete_fields}
            self.assertLessEqual(names, fields, label)
        with mock.patch("internest_core.account_privacy.collect_owned") as owned:
            from internest_skills.models import ChallengeAttempt
            attempt = ChallengeAttempt(pk=7, integrity_events=[{"type": "paste"}], review_note="internal", score_pct=80)
            owned.return_value = {ChallengeAttempt: [attempt]}
            from .account_privacy import export_student_data
            row = export_student_data(self.user)["records"]["internest_skills.challengeattempt"][0]
        self.assertEqual(row["score_pct"], 80)
        for hidden in ("integrity_events", "review_note", "review_reasons", "focus_warnings", "reviewed_by", "token"):
            self.assertNotIn(hidden, row)

    def test_companies_staff_and_visitors_cannot_use_it(self):
        self.assertEqual(self.client.post(reverse("account_export")).status_code, 302)  # sign in first
        self.client.force_login(self.partner.user)
        self.assertEqual(self.client.post(reverse("account_export")).status_code, 404)
        self.assertEqual(self.client.get(reverse("account_delete")).status_code, 404)
        staff, _ = _student("staffer")
        staff.is_staff = True
        staff.save()
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("account_delete")).status_code, 404)

    def test_wrong_password_or_no_tick_keeps_the_account(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("account_delete")), "Delete my account permanently")
        r = self.client.post(reverse("account_delete"), {"password": "nope", "confirm": "on"})
        self.assertContains(r, "That password isn&#x27;t right.")
        r = self.client.post(reverse("account_delete"), {"password": "pw-Strong-123"})
        self.assertContains(r, "Tick the box to confirm.")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_delete_removes_rows_and_files_and_signs_out(self):
        path = os.path.join(MEDIA, self.profile.cv_file.name)
        self.assertTrue(os.path.exists(path))
        self.client.force_login(self.user)
        opened = self.client.get(reverse("serve_media", args=[self.profile.cv_file.name]))  # a trail entry naming the file
        b"".join(opened.streaming_content)
        opened.close()  # as the server does; an open handle would block the delete on Windows
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("account_delete"), {"password": "pw-Strong-123", "confirm": "on"})
        self.assertRedirects(r, reverse("landing"), fetch_redirect_response=False)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(StudentProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertFalse(Application.objects.filter(internship=self.internship).exists())
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(SecurityEvent.objects.filter(target=self.profile.cv_file.name).count(), 0)
        event = SecurityEvent.objects.get(action="account_deleted")
        self.assertEqual((event.actor, event.target), (None, f"user:{self.user.pk}"))  # the trail outlives the account
        self.assertTrue(Internship.objects.filter(pk=self.internship.pk).exists())  # the company keeps its posting

    def test_social_only_account_confirms_with_its_email(self):
        user, _ = _student("gina", email="gina@uni.edu")
        user.set_unusable_password()
        user.save()
        self.client.force_login(user)
        r = self.client.post(reverse("account_delete"), {"email": "someone@else.com", "confirm": "on"})
        self.assertContains(r, "Type the email address of this account exactly.")
        self.client.post(reverse("account_delete"), {"email": "GINA@uni.edu ", "confirm": "on"})
        self.assertFalse(User.objects.filter(pk=user.pk).exists())

    def test_unverified_student_can_still_leave(self):
        self.profile.personal_email = self.user.email
        self.profile.personal_email_verified_at = None
        self.profile.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_delete")).status_code, 200)

    def test_profile_offers_both_actions(self):
        self.client.force_login(self.user)
        html = self.client.get(reverse("profile") + "?tab=edit").content.decode()
        self.assertIn(f'action="{reverse("account_export")}"', html)
        self.assertIn(f'href="{reverse("account_delete")}"', html)


@override_settings(MEDIA_ROOT=MEDIA)
class AuditTrailTests(TestCase):
    def test_private_file_access_is_recorded(self):
        user, profile = _student("sara")
        profile.cv_file = SimpleUploadedFile("cv.pdf", PDF)
        profile.save()
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("serve_media", args=[profile.cv_file.name])).status_code, 200)
        event = SecurityEvent.objects.get(action="private_file_view")
        self.assertEqual((event.actor, event.target, event.ip), (user, profile.cv_file.name, "127.0.0.1"))

    def test_public_files_are_not_logged(self):
        os.makedirs(os.path.join(MEDIA, "partner_logos"), exist_ok=True)
        with open(os.path.join(MEDIA, "partner_logos", "x.png"), "wb") as fh:
            fh.write(PNG)
        self.client.force_login(User.objects.create_user("viewer", password="pw"))
        self.assertEqual(self.client.get(reverse("serve_media", args=["partner_logos/x.png"])).status_code, 200)
        self.assertFalse(SecurityEvent.objects.exists())

    def test_recording_never_breaks_the_page(self):
        from . import audit
        with mock.patch.object(SecurityEvent.objects, "create", side_effect=RuntimeError("db locked")):
            audit.record(RequestFactory().get("/"), "data_export")  # no exception

    def test_admin_is_read_only(self):
        model_admin = site._registry[SecurityEvent]
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("root", password="pw")
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))

    @override_settings(SECURITY_EVENT_RETENTION_DAYS=30)
    def test_old_events_are_purged(self):
        old = SecurityEvent.objects.create(action="data_export")
        SecurityEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))
        recent = SecurityEvent.objects.create(action="data_export")
        call_command("purge_security_events", stdout=io.StringIO())
        self.assertEqual(list(SecurityEvent.objects.values_list("pk", flat=True)), [recent.pk])
