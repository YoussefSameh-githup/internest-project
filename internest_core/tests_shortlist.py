"""Skills-first funnel: consent, locked CV/contacts until shortlisted, notifications, remote map logic."""
import shutil
import tempfile

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from internest_core.models import Application, Internship
from internest_core.tests_applications import CONSENT, ApplicationsBase, _student

TEMP_MEDIA = tempfile.mkdtemp()


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=TEMP_MEDIA, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ShortlistFunnelTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.profile = _student("amr")
        self.profile.phone_number = "01012345678"
        self.profile.linkedin_url = "https://www.linkedin.com/in/amr"
        self.profile.bio = "Built a budgeting app used by 300 students."
        self.profile.cv_file.save("amr_cv.pdf", SimpleUploadedFile("amr_cv.pdf", b"%PDF-1.4 cv"), save=True)
        self.cv_media_url = self.profile.cv_file.url

    def _apply(self):
        self.login_student(self.user)
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        mail.outbox = []  # drop login verification emails
        return Application.objects.get(applicant=self.user)

    def test_apply_requires_consent(self):
        self.login_student(self.user)
        page = self.client.get(reverse("apply", args=[self.gig.pk]))
        self.assertContains(page, 'name="contact_consent"')
        self.assertContains(page, "my CV and contact details will be shared with the company")
        resp = self.client.post(reverse("apply", args=[self.gig.pk]), {})
        self.assertContains(resp, "Please agree to sharing your CV and contact details to apply.")
        self.assertFalse(Application.objects.exists())
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        self.assertIsNotNone(Application.objects.get().contact_consent_at)

    def test_cv_and_contacts_locked_before_shortlist(self):
        app = self._apply()
        self.client.force_login(self.founder)
        page = self.client.get(reverse("partner_applicant", args=[app.pk]))
        self.assertContains(page, "Built a budgeting app")  # skills-first profile
        self.assertContains(page, "the CV and contact details unlock when you shortlist")
        for secret in ("amr@uni.edu", "01012345678", "linkedin.com/in/amr", "wa.me", self.cv_media_url,
                       reverse("partner_applicant_cv", args=[app.pk])):
            self.assertNotContains(page, secret)
        self.assertEqual(self.client.get(reverse("partner_applicant_cv", args=[app.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.cv_media_url).status_code, 404)  # no bypass via /media/

    def test_shortlist_unlocks_and_notifies_student(self):
        app = self._apply()
        self.client.force_login(self.founder)
        self.client.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "shortlist"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_SHORTLISTED)
        self.assertIsNotNone(app.shortlisted_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["amr@uni.edu"])
        self.assertIn("تم ترشيحك للمقابلة", mail.outbox[0].subject)

        page = self.client.get(reverse("partner_applicant", args=[app.pk]))
        for shown in ("mailto:amr@uni.edu", "tel:01012345678", "https://wa.me/201012345678", "linkedin.com/in/amr",
                      reverse("partner_applicant_cv", args=[app.pk])):
            self.assertContains(page, shown)
        self.assertNotContains(page, 'value="shortlist"')
        cv = self.client.get(reverse("partner_applicant_cv", args=[app.pk]))
        self.assertEqual(cv.status_code, 200)
        self.assertEqual(b"".join(cv.streaming_content), b"%PDF-1.4 cv")
        self.assertEqual(self.client.get(self.cv_media_url).status_code, 200)

        self.login_student(self.user)
        self.assertContains(self.client.get(reverse("my_applications")), "You&#x27;re shortlisted for an interview 🎉")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("my_applications")), "تم ترشيحك للمقابلة 🎉")

    def test_cannot_shortlist_after_position_filled(self):
        app = self._apply()
        Application.objects.filter(pk=app.pk).update(status=Application.STATUS_FULFILLED)
        self.client.force_login(self.founder)
        self.client.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "shortlist"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_FULFILLED)
        self.assertEqual(mail.outbox, [])

    def test_cv_media_privacy(self):
        self.client.logout()
        self.assertIn(self.client.get(self.cv_media_url).status_code, (302, 404))  # anonymous: login redirect, never the file
        stranger, _ = _student("stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.cv_media_url).status_code, 404)  # another student
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.cv_media_url).status_code, 200)  # the owner
        self.client.force_login(self.founder)
        self.assertEqual(self.client.get(self.cv_media_url).status_code, 404)  # startup without an application
        self.assertEqual(self.client.get(self.cv_media_url.replace("cvs/", "cvs/../cvs/")).status_code, 404)


class RemoteLocationTests(ApplicationsBase):
    def test_is_online_keywords(self):
        for loc in ("Online", "online", "Remotely", "REMOTE", "عن بعد", "عن بُعد", "أونلاين", ""):
            self.assertTrue(Internship(location=loc).is_online, loc)
        for loc in ("Giza", "Smart Village, Giza", "المعادي، القاهرة"):
            self.assertFalse(Internship(location=loc).is_online, loc)

    def test_detail_map_only_for_physical_address(self):
        user, _ = _student("amr")
        self.login_student(user)
        for loc in ("Remotely", "عن بعد"):
            Internship.objects.filter(pk=self.gig.pk).update(location=loc)
            page = self.client.get(reverse("internship_detail", args=[self.gig.pk]))
            self.assertNotContains(page, "maps.google.com")
            self.assertContains(page, "💻 Remote / online opportunity")
        page = self.client.get(reverse("internship_detail", args=[self.role.pk]))
        self.assertContains(page, "maps.google.com/maps?q=Giza")
        self.assertNotContains(page, "remote-badge")
