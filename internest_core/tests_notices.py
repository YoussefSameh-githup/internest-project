"""In-app notices: the 2026-11-18 Talent Pool privacy notice for students who opted in earlier."""
import importlib

from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from internest_core import notices
from internest_core.models import StudentProfile
from internest_core.tests_applications import ApplicationsBase, _student

KEY = notices.TALENT_POOL_SUMMARY
URL = reverse("student_notice", args=[KEY])
TITLE = "We updated our privacy policy"
migration = importlib.import_module("internest_core.migrations.0024_student_pending_notices")


class NoticeTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.profile = _student("opted")
        StudentProfile.objects.filter(pk=self.profile.pk).update(talent_pool_visible=True)
        self.other_user, self.other = _student("notopted")
        migration.queue_notice(apps, None)
        self.profile.refresh_from_db()
        self.other.refresh_from_db()

    def test_migration_queues_only_opted_in_students_once(self):
        self.assertEqual(self.profile.pending_notices, [KEY])
        self.assertEqual(self.other.pending_notices, [])
        migration.queue_notice(apps, None)  # re-running never duplicates
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pending_notices, [KEY])

    def test_banner_shown_until_dismissed(self):
        self.login_student(self.user)
        page = self.client.get(reverse("list"))
        self.assertContains(page, TITLE)
        self.assertContains(page, "They never see your name, university, photo, phone, email or CV.")
        self.assertContains(page, f'href="{reverse("privacy")}"')
        resp = self.client.post(URL, {"action": "dismiss", "next": reverse("profile")})
        self.assertRedirects(resp, reverse("profile"), fetch_redirect_response=False)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pending_notices, [])
        self.assertTrue(self.profile.talent_pool_visible)  # "keep me visible" changes nothing else
        self.assertNotContains(self.client.get(reverse("list")), TITLE)

    def test_hide_from_pool_turns_visibility_off(self):
        self.login_student(self.user)
        resp = self.client.post(URL, {"action": "hide_from_pool"}, follow=True)
        self.assertContains(resp, "You are no longer shown in the Talent Pool.")
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.talent_pool_visible)
        self.assertEqual(self.profile.pending_notices, [])

    def test_students_without_the_notice_never_see_it(self):
        self.login_student(self.other_user)
        self.assertNotContains(self.client.get(reverse("list")), TITLE)
        self.client.post(URL, {"action": "hide_from_pool"})  # acting on a notice you never had is harmless
        self.other.refresh_from_db()
        self.assertFalse(self.other.talent_pool_visible)
        self.assertEqual(self.other.pending_notices, [])

    def test_only_post_known_key_student_and_safe_redirect(self):
        self.login_student(self.user)
        self.assertEqual(self.client.get(URL).status_code, 405)
        self.assertEqual(self.client.post(reverse("student_notice", args=["unknown"])).status_code, 404)
        resp = self.client.post(URL, {"action": "dismiss", "next": "https://evil.example/x"})
        self.assertRedirects(resp, reverse("list"), fetch_redirect_response=False)
        self.client.force_login(self.founder)
        self.assertEqual(self.client.post(URL).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(URL).status_code, 302)  # login
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.talent_pool_visible)

    def test_partner_pages_never_show_it(self):
        self.client.force_login(self.founder)
        self.assertNotContains(self.client.get(reverse("partner_dashboard")), TITLE)

    def test_showing_the_notice_costs_no_query(self):
        self.login_student(self.user)
        self.client.get(reverse("list"))
        with CaptureQueriesContext(connection) as with_notice:
            self.assertContains(self.client.get(reverse("list")), TITLE)
        StudentProfile.objects.filter(pk=self.profile.pk).update(pending_notices=[])
        self.client.get(reverse("list"))
        with CaptureQueriesContext(connection) as without:
            self.assertNotContains(self.client.get(reverse("list")), TITLE)
        self.assertEqual(len(with_notice), len(without))

    def test_unknown_stored_keys_are_ignored(self):
        StudentProfile.objects.filter(pk=self.profile.pk).update(pending_notices=["removed_notice", KEY])
        self.profile.refresh_from_db()
        self.assertEqual(notices.pending(self.profile), [KEY])

    def test_add_helper(self):
        self.assertEqual(notices.add(StudentProfile.objects.filter(pk=self.other.pk), KEY), 1)
        self.assertEqual(notices.add(StudentProfile.objects.filter(pk=self.other.pk), KEY), 0)

    def test_arabic(self):
        self.login_student(self.user)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("list")), "حدّثنا سياسة الخصوصية")
