"""Forgot password with a 6-digit email code: every role, anti-enumeration, expiry, attempts, reuse, strength,
sessions, rate limits, audit, and the pages in Arabic and English (with the accessibility audit)."""
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from . import password_reset as service
from .models import (
    EmailVerification, PartnerProfile, PasswordResetCode, PasswordResetThrottle, SecurityEvent, StudentProfile,
)
from .ratelimit import RULES
from .tests_accessibility import audit

STRONG = "Nile-Sunset-2026!"
OLD = "Old-Password-2025?"
UNIFORM_EN = "is registered with us, we've sent a 6-digit verification code to it."


def code_from_mail(index=-1):
    return re.search(r"\b(\d{6})\b", mail.outbox[index].body).group(1)


class ResetBase(TestCase):
    def setUp(self):
        cache.clear()
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.student = User.objects.create_user("sara", email="", password=OLD)
        StudentProfile.objects.create(user=self.student, personal_email="sara@uni.edu", personal_email_verified_at=timezone.now())
        EmailVerification.objects.create(user=self.student, email="sara@uni.edu", email_type=EmailVerification.EMAIL_TYPE_PERSONAL,
                                         code="000000", expires_at=timezone.now(), verified_at=timezone.now())  # proven once
        self.startup = User.objects.create_user("mona@startupx.io", email="mona@startupx.io", password=OLD)
        PartnerProfile.objects.create(user=self.startup, company_name="StartupX", partner_code="SX1")
        self.uni = User.objects.create_user("career@cu.edu.eg", email="career@cu.edu.eg", password=OLD)
        PartnerProfile.objects.create(user=self.uni, company_name="Cairo University", partner_code="CU1", is_academic=True)

    def skip_cooldown(self):
        PasswordResetThrottle.objects.update(last_sent_at=None)

    def ask(self, email, client=None):
        return (client or self.client).post(reverse("password_reset"), {"email": email}, follow=True)

    def enter(self, code, client=None):
        return (client or self.client).post(reverse("password_reset_verify"), {"code": code})

    def choose(self, p1, p2=None, client=None):
        return (client or self.client).post(reverse("password_reset_new"), {"new_password1": p1, "new_password2": p2 or p1})

    def full_reset(self, email, password=STRONG, client=None):
        self.ask(email, client)
        self.assertEqual(self.enter(code_from_mail(), client).status_code, 302)
        return self.choose(password, client=client)


class EveryRoleTests(ResetBase):
    def test_student_startup_and_university_can_reset_and_sign_in(self):
        for user, email, home in ((self.student, "Sara@Uni.edu ", "list"), (self.startup, "mona@startupx.io", "partner_dashboard"),
                                  (self.uni, "career@cu.edu.eg", "partner_dashboard")):
            with self.subTest(user=user.username):
                cache.clear()
                client = Client()
                r = self.full_reset(email, client=client)
                self.assertRedirects(r, f"{reverse('login')}?next={reverse(home).replace('/', '%2F')}", fetch_redirect_response=False)
                user.refresh_from_db()
                self.assertTrue(user.check_password(STRONG))
                self.assertFalse(user.check_password(OLD))
                self.assertTrue(Client().login(username=user.username, password=STRONG))
                self.assertTrue(SecurityEvent.objects.filter(action="password_reset", actor=user).exists())

    def test_code_is_never_stored_or_logged_in_clear(self):
        self.ask("sara@uni.edu")
        code = code_from_mail()
        row = PasswordResetCode.objects.get()
        self.assertNotIn(code, row.code_hash + row.salt + row.email)
        self.assertEqual(len(row.code_hash), 64)
        self.enter(code)
        self.choose(STRONG)
        event = SecurityEvent.objects.get(action="password_reset")
        self.assertNotIn(code, f"{event.target} {event.detail}")

    def test_email_is_bilingual_and_tells_the_username(self):
        self.ask("sara@uni.edu")
        msg = mail.outbox[-1]
        self.assertEqual(msg.to, ["sara@uni.edu"])
        html = msg.alternatives[0][0]
        self.assertIn("Reset your password", html)
        self.assertIn("أعد تعيين كلمة المرور", html)
        self.assertIn('dir="rtl"', html)
        self.assertIn("sara", msg.body)
        self.assertIn("10", msg.body)

    def test_social_only_account_can_set_a_first_password(self):
        self.student.set_unusable_password()
        self.student.save()
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())
        self.assertContains(self.client.get(reverse("password_reset_new")), "Set a password for your account")
        self.choose(STRONG)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(STRONG))


class AntiEnumerationTests(ResetBase):
    def test_unknown_staff_and_inactive_addresses_get_the_same_answer_and_no_email(self):
        staff = User.objects.create_user("boss", email="boss@internest.io", password=OLD, is_staff=True)
        User.objects.create_user("gone", email="gone@x.io", password=OLD, is_active=False)
        known = self.ask("sara@uni.edu")
        sent = len(mail.outbox)
        for email in ("nobody@nowhere.io", "boss@internest.io", "gone@x.io"):
            with self.subTest(email=email):
                cache.clear()
                r = self.ask(email)
                self.assertContains(r, UNIFORM_EN)
                self.assertEqual(r.redirect_chain, known.redirect_chain)
                self.assertEqual(len(mail.outbox), sent)  # nothing sent
                self.assertContains(self.enter("123456"), "That code is incorrect or has expired")
        staff.refresh_from_db()
        self.assertTrue(staff.check_password(OLD))

    def test_shared_address_sends_nothing(self):
        User.objects.create_user("twin", email="sara@uni.edu", password=OLD)  # two accounts claim one address
        self.ask("sara@uni.edu")
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_and_expired_codes_read_the_same(self):
        self.ask("sara@uni.edu")
        wrong = self.enter("000000" if code_from_mail() != "000000" else "111111").content.decode()
        PasswordResetCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        expired = self.enter(code_from_mail()).content.decode()
        msg = "That code is incorrect or has expired"
        self.assertIn(msg, wrong)
        self.assertIn(msg, expired)
        self.assertNotIn("attempt", wrong.lower().split("that code")[1][:200])

    def test_minimum_response_time_applies_to_every_request(self):
        with override_settings(PASSWORD_RESET_MIN_SECONDS=0.2), mock.patch.object(service.time, "sleep") as sleep:
            service.request_code("nobody@nowhere.io")
        self.assertTrue(sleep.called)


class CodeRulesTests(ResetBase):
    def test_expired_code_fails(self):
        self.ask("sara@uni.edu")
        code = code_from_mail()
        PasswordResetCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.enter(code).status_code, 200)
        self.assertRedirects(self.client.get(reverse("password_reset_new")), reverse("password_reset"), fetch_redirect_response=False)

    def test_five_wrong_tries_lock_the_code(self):
        self.ask("sara@uni.edu")
        code = code_from_mail()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(service.MAX_ATTEMPTS):
            self.enter(wrong)
        row = PasswordResetCode.objects.get()
        self.assertEqual(row.attempts, service.MAX_ATTEMPTS)
        self.assertIsNotNone(row.used_at)
        self.assertEqual(self.enter(code).status_code, 200)  # even the right code no longer works
        self.assertIsNone(service.verified_code(row.pk))

    def test_code_cannot_be_reused_and_new_code_replaces_old(self):
        self.ask("sara@uni.edu")
        first = code_from_mail()
        self.skip_cooldown()
        self.ask("sara@uni.edu")
        second = code_from_mail()
        if first != second:
            self.assertEqual(self.enter(first).status_code, 200)  # the old code was voided by the new one
        self.enter(second)
        self.choose(STRONG)
        other = Client()
        other.post(reverse("password_reset"), {"email": "sara@uni.edu"})
        self.assertEqual(self.enter(second, other).status_code, 200)  # spent
        self.assertRedirects(self.client.get(reverse("password_reset_new")), reverse("password_reset"), fetch_redirect_response=False)

    def test_verified_code_is_bound_to_the_browser(self):
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())
        intruder = Client()
        self.assertRedirects(intruder.get(reverse("password_reset_new")), reverse("password_reset"), fetch_redirect_response=False)
        self.assertEqual(intruder.post(reverse("password_reset_new"), {"new_password1": STRONG, "new_password2": STRONG}).status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(OLD))

    def test_verified_window_expires(self):
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())
        PasswordResetCode.objects.update(verified_at=timezone.now() - service.VERIFIED_WINDOW - timedelta(seconds=1))
        self.assertRedirects(self.client.get(reverse("password_reset_new")), reverse("password_reset"), fetch_redirect_response=False)


class PasswordRulesTests(ResetBase):
    def setUp(self):
        super().setUp()
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())

    def test_weak_and_mismatched_passwords_are_refused_in_both_languages(self):
        r = self.choose("123")
        self.assertContains(r, "This password is too short")
        self.assertContains(self.choose(STRONG, STRONG + "x"), "didn’t match")
        self.client.cookies["internest_lang"] = "ar"
        r = self.choose("password")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'lang="ar"')
        self.assertContains(r, "شائعة")  # Django's Arabic "too common" message
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(OLD))

    def test_a_login_elsewhere_does_not_block_the_reset(self):
        """Every student login clears the profile's verified flag; the address was still proven before."""
        Client().force_login(self.student)
        self.assertEqual(self.choose(STRONG).status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(STRONG))

    def test_other_sessions_end(self):
        elsewhere = Client()
        elsewhere.force_login(self.student)
        page = reverse("account_delete")  # reachable by a signed-in student even before the email-code gate
        self.assertEqual(elsewhere.get(page).status_code, 200)
        self.choose(STRONG)
        r = elsewhere.get(page)
        self.assertEqual(r.status_code, 302)  # signed out: the old session no longer matches the password
        self.assertNotIn("_auth_user_id", elsewhere.session)


class RateLimitTests(ResetBase):
    def test_cooldown_and_hourly_limit_per_email(self):
        self.ask("sara@uni.edu")
        r = self.client.post(reverse("password_reset_resend"), follow=True)
        self.assertContains(r, "Please wait a minute")
        self.assertEqual(len(mail.outbox), 1)
        for _ in range(service.MAX_CODES_PER_EMAIL_PER_HOUR - 1):
            self.skip_cooldown()
            self.client.post(reverse("password_reset_resend"))
        self.assertEqual(len(mail.outbox), service.MAX_CODES_PER_EMAIL_PER_HOUR)
        self.skip_cooldown()
        r = self.client.post(reverse("password_reset_resend"), follow=True)
        self.assertContains(r, "Too many codes")
        self.assertEqual(len(mail.outbox), service.MAX_CODES_PER_EMAIL_PER_HOUR)

    def test_rejected_requests_are_not_counted_and_the_window_rolls(self):
        for _ in range(service.MAX_CODES_PER_EMAIL_PER_HOUR):
            self.skip_cooldown()
            service.request_code("sara@uni.edu")
        for _ in range(10):  # someone keeps hammering: refused, but never extends the lock
            self.skip_cooldown()
            self.assertEqual(service.request_code("sara@uni.edu"), service.LIMITED)
        row = PasswordResetThrottle.objects.get()
        self.assertEqual(row.sends, service.MAX_CODES_PER_EMAIL_PER_HOUR)
        PasswordResetThrottle.objects.update(window_start=timezone.now() - timedelta(hours=1, seconds=1), last_sent_at=None)
        self.assertEqual(service.request_code("sara@uni.edu"), service.SENT)

    def test_unknown_addresses_have_the_same_cooldown(self):
        self.ask("nobody@nowhere.io")
        self.assertContains(self.client.post(reverse("password_reset_resend"), follow=True), "Please wait a minute")
        self.assertFalse(PasswordResetThrottle.objects.filter(email_key__contains="nobody").exists())  # only a hash is kept

    def test_daily_wrong_code_budget_per_address(self):
        self.ask("sara@uni.edu")
        PasswordResetThrottle.objects.update(failures=service.MAX_FAILURES_PER_DAY)
        self.assertEqual(self.enter(code_from_mail()).status_code, 200)  # even the right code: budget spent
        PasswordResetThrottle.objects.update(fail_window_start=timezone.now() - timedelta(days=1, seconds=1))
        self.assertEqual(self.enter(code_from_mail()).status_code, 302)  # a new day

    def test_no_code_no_lockout_and_the_owner_is_alerted(self):
        stranger = Client()
        stranger.post(reverse("password_reset"), {"email": "sara@uni.edu"})
        PasswordResetCode.objects.all().delete()  # no live code: wrong guesses are not counted
        for _ in range(service.MAX_FAILURES_PER_DAY + 5):
            self.enter("123456", stranger)
        self.assertEqual(PasswordResetThrottle.objects.get().failures, 0)
        self.skip_cooldown()
        self.ask("sara@uni.edu")
        PasswordResetThrottle.objects.update(failures=service.MAX_FAILURES_PER_DAY - 1)
        sent = len(mail.outbox)
        code = code_from_mail()
        self.enter("000000" if code != "000000" else "111111")  # the 20th wrong try
        self.assertEqual(len(mail.outbox), sent + 1)
        self.assertIn("Many wrong codes were entered", mail.outbox[-1].body)
        self.assertNotIn(code, mail.outbox[-1].body)

    def test_idle_limit_rows_are_purged(self):
        self.ask("nobody@nowhere.io")
        old = timezone.now() - timedelta(days=3)
        PasswordResetThrottle.objects.update(window_start=old, fail_window_start=old, last_sent_at=old)
        self.assertEqual(service.purge_stale(), 1)
        self.assertFalse(PasswordResetThrottle.objects.exists())

    def test_attempts_are_claimed_before_comparing(self):
        self.ask("sara@uni.edu")
        PasswordResetCode.objects.update(attempts=service.MAX_ATTEMPTS)  # parallel requests already used them up
        self.assertIsNone(service.verify_code("sara@uni.edu", code_from_mail()))

    @override_settings(RATELIMIT_ENABLED=True)
    def test_per_ip_limits(self):
        for name in ("password_reset", "password_reset_resend", "password_reset_verify", "password_reset_new"):
            with self.subTest(rule=name):
                cache.clear()
                limit = RULES[name][0]
                codes = [self.client.post(reverse(name), {"email": f"x{i}@y.io", "code": "123456"}).status_code
                         for i in range(limit + 1)]
                self.assertNotIn(429, codes[:limit])
                self.assertEqual(codes[-1], 429)
        cache.clear()

    def test_limiter_window_survives_file_cache(self):
        """Django's file cache resets a key's timeout to 300 s on incr(); the limiter must keep the real window."""
        import tempfile

        from django.core.cache import caches

        from .ratelimit import hit
        folder = tempfile.mkdtemp()
        with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                                                   "LOCATION": folder, "TIMEOUT": 300}}):
            caches["default"].clear()
            with mock.patch("internest_core.ratelimit.time.time", return_value=1_000_000.0):
                self.assertTrue(hit("rl:test", 2, 3600))
                self.assertTrue(hit("rl:test", 2, 3600))
                self.assertFalse(hit("rl:test", 2, 3600))
            with mock.patch("internest_core.ratelimit.time.time", return_value=1_000_000.0 + 1000):
                self.assertFalse(hit("rl:test", 2, 3600))  # 1000 s later, same hour: still limited
            with mock.patch("internest_core.ratelimit.time.time", return_value=1_000_000.0 + 3601):
                self.assertTrue(hit("rl:test", 2, 3600))  # new window

    def test_resend_needs_post_and_a_started_flow(self):
        self.assertEqual(self.client.get(reverse("password_reset_resend")).status_code, 405)
        self.assertRedirects(Client().post(reverse("password_reset_resend")), reverse("password_reset"), fetch_redirect_response=False)
        self.assertRedirects(Client().get(reverse("password_reset_verify")), reverse("password_reset"), fetch_redirect_response=False)


class ReviewFixTests(ResetBase):
    def test_unverified_personal_email_gets_nothing(self):
        EmailVerification.objects.filter(user=self.student).update(verified_at=None)  # never proven
        self.ask("sara@uni.edu")
        self.assertEqual(len(mail.outbox), 0)

    def test_unverified_duplicate_cannot_block_the_real_owner(self):
        squatter = User.objects.create_user("squat", password=OLD)
        StudentProfile.objects.create(user=squatter, personal_email="mona@startupx.io")  # never verified
        self.ask("mona@startupx.io")
        self.assertEqual(mail.outbox[-1].to, ["mona@startupx.io"])

    def test_a_verified_code_survives_a_new_request_elsewhere(self):
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())
        self.skip_cooldown()
        Client().post(reverse("password_reset"), {"email": "sara@uni.edu"})  # someone else asks again
        self.assertEqual(self.choose(STRONG).status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(STRONG))

    def test_account_is_rechecked_when_saving(self):
        self.ask("sara@uni.edu")
        self.enter(code_from_mail())
        User.objects.filter(pk=self.student.pk).update(is_active=False)
        r = self.choose(STRONG)
        self.assertRedirects(r, reverse("password_reset"), fetch_redirect_response=False)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password(OLD))

    def test_owner_gets_a_password_changed_notice(self):
        self.full_reset("mona@startupx.io")
        notice = mail.outbox[-1]
        self.assertEqual(notice.to, ["mona@startupx.io"])
        self.assertIn("Your password was changed", notice.body)
        self.assertNotRegex(notice.body, r"\b\d{6}\b")  # no code in the notice

    def test_partner_login_template_links_to_the_reset(self):
        from pathlib import Path

        from django.conf import settings
        text = (Path(settings.BASE_DIR) / "internest_core/templates/registration/partner_login.html").read_text(encoding="utf-8")
        self.assertIn("{% url 'password_reset' %}", text)

    def test_allauth_password_pages_lead_here(self):
        self.assertRedirects(self.client.get("/oauth/password/reset/"), reverse("password_reset"), fetch_redirect_response=False)


class PagesTests(ResetBase):
    def test_login_pages_link_to_forgot_password_in_both_languages(self):
        for lang, text in (("en", "Forgot password?"), ("ar", "نسيت كلمة المرور؟")):
            self.client.cookies["internest_lang"] = lang
            for url in (reverse("login"),):
                self.assertContains(self.client.get(url), f'href="{reverse("password_reset")}"')
                self.assertContains(self.client.get(url), text)
        self.client.force_login(self.student)
        StudentProfile.objects.filter(user=self.student).update(personal_email_verified_at=timezone.now())
        self.assertContains(self.client.get(reverse("password_change")), f'href="{reverse("password_reset")}"')

    def test_old_django_reset_links_lead_into_the_new_flow(self):
        for path in ("/accounts/reset/MQ/abc-123/", "/accounts/password_reset/done/", "/accounts/reset/done/"):
            self.assertRedirects(self.client.get(path), reverse("password_reset"), fetch_redirect_response=False)

    def test_pages_pass_the_accessibility_audit_in_both_languages(self):
        for lang in ("en", "ar"):
            client = Client()
            client.cookies["internest_lang"] = lang
            cache.clear()
            self.skip_cooldown()
            pages = [client.get(reverse("password_reset"))]
            client.post(reverse("password_reset"), {"email": "sara@uni.edu"})
            pages.append(client.get(reverse("password_reset_verify")))
            client.post(reverse("password_reset_verify"), {"code": code_from_mail()})
            pages.append(client.get(reverse("password_reset_new")))
            for page in pages:
                with self.subTest(lang=lang, url=page.request["PATH_INFO"]):
                    self.assertEqual(page.status_code, 200)
                    self.assertEqual(audit(page.content.decode(), lang), [])

    def test_arabic_uniform_message(self):
        self.client.cookies["internest_lang"] = "ar"
        r = self.ask("nobody@nowhere.io")
        self.assertContains(r, "مسجلاً لدينا، فقد أرسلنا رمز تحقق مكوناً من 6 أرقام")
