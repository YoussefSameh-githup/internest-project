"""Audit fixes (perf-security branch): batched opportunity emails, paginated feeds, cache-key normalisation,
verified-identity changes (M3), promo-code rate limits (L1), paid course content (L2) and log hygiene (L6)."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from internest_skills.models import Skill, StudentSkill
from internest_skills.tests import Base

from . import notifications
from .models import (AsyncTask, GamificationTask, Internship, PartnerCourseSubmission, StudentEnrollment, StudentProfile,
                     TaskQuestion)


def _opportunity(partner, title="Role", location="Cairo", days=10):
    return Internship.objects.create(partner=partner, title=title, description="d", location=location, required_majors="any",
                                     deadline=timezone.localdate() + timedelta(days=days))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NewOpportunityEmailTests(Base):
    def _students(self, n, opted_in=True):
        for i in range(n):
            user = User.objects.create_user(f"bulk{opted_in}{i}", password="pw")
            StudentProfile.objects.create(user=user, university="Cairo", major="CS", study_level="3",
                                          personal_email=f"bulk{opted_in}{i}@uni.edu", email_new_opportunities=opted_in)

    def test_nothing_is_sent_inside_the_save(self):
        self._students(2)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            _opportunity(self.pro)
        self.assertEqual(mail.outbox, [])  # the request only schedules the job
        self.assertEqual(len(callbacks), 1)

    @override_settings(NEW_INTERNSHIP_BATCH_SIZE=3)
    def test_opted_in_students_are_emailed_in_batches_by_a_background_job(self):
        self._students(7)
        self._students(2, opted_in=False)
        with self.captureOnCommitCallbacks(execute=True):
            job = _opportunity(self.pro, title="Analyst")
        self.assertEqual([len(m.bcc) for m in mail.outbox], [3, 3, 1])
        recipients = {address for m in mail.outbox for address in m.bcc}
        self.assertEqual(len(recipients), 7)
        self.assertFalse(any(address.startswith("bulkFalse") for address in recipients))  # opt-out respected
        self.assertTrue(all(m.to == [] and "Analyst" in m.subject for m in mail.outbox))  # nobody sees the list
        task = AsyncTask.objects.get(kind=notifications.NEW_INTERNSHIP_JOB)
        self.assertEqual((task.user_id, task.status, task.result), (self.pro_user.pk, AsyncTask.STATUS_DONE,
                                                                    {"recipients": 7, "batches": 3}))
        mail.outbox.clear()
        job.title = "Edited"
        with self.captureOnCommitCallbacks(execute=True):
            job.save()
        self.assertEqual(mail.outbox, [])  # edits never re-announce

    @override_settings(NEW_INTERNSHIP_BATCH_SIZE=10_000)
    def test_batch_size_never_exceeds_500(self):
        self.assertEqual(notifications.batch_size(), 500)

    def test_a_failed_batch_does_not_stop_the_others(self):
        self._students(2)
        with override_settings(NEW_INTERNSHIP_BATCH_SIZE=1), \
                mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=[Exception("smtp"), 1]):
            job = _opportunity(self.pro)
            result = notifications.send_new_internship_emails(job.pk)
        self.assertEqual(result, {"recipients": 1, "batches": 2})


class OpportunityFeedPaginationTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_twenty_per_page_and_only_the_page_is_scored(self):
        from . import views

        for i in range(views.INTERNSHIPS_PER_PAGE + 5):
            _opportunity(self.free, title=f"Role {i}", days=5 + i)
        with mock.patch.object(views, "bulk_match_scores", wraps=views.bulk_match_scores) as scorer:
            first = self.client.get(reverse("list"))
        self.assertEqual(len(first.context["internships"]), views.INTERNSHIPS_PER_PAGE)
        self.assertEqual(len(scorer.call_args.args[0]), views.INTERNSHIPS_PER_PAGE)  # not the whole feed
        self.assertContains(first, f"<strong>{views.INTERNSHIPS_PER_PAGE + 5}</strong>")
        self.assertContains(first, 'href="?page=2"')
        second = self.client.get(reverse("list"), {"page": 2})
        self.assertEqual(len(second.context["internships"]), 5)
        self.assertEqual(self.client.get(reverse("list"), {"page": "999"}).context["page"].number, 2)  # clamps

    def test_filters_are_kept_in_page_links_and_mode_is_filtered_in_sql(self):
        from . import views

        for i in range(views.INTERNSHIPS_PER_PAGE + 1):
            _opportunity(self.free, title=f"Remote {i}", location="Remote (Egypt)")
        _opportunity(self.free, title="Blank location", location="  ")
        _opportunity(self.free, title="Arabic online", location="عن بعد")
        _opportunity(self.free, title="Office", location="Smart Village")
        online = self.client.get(reverse("list"), {"mode": "online", "q": "o"})
        self.assertEqual(online.context["page"].paginator.count, views.INTERNSHIPS_PER_PAGE + 3)
        self.assertContains(online, 'href="?mode=online&amp;q=o&amp;page=2"')
        onsite = self.client.get(reverse("list"), {"mode": "onsite"})
        self.assertEqual([i.title for i in onsite.context["internships"]], ["Office"])
        self.assertTrue(all(i.is_online for i in online.context["internships"]))  # SQL filter == Internship.is_online

    def test_verified_badge_comes_from_the_joined_partner(self):
        type(self.free).objects.filter(pk=self.free.pk).update(is_fully_verified=True)
        _opportunity(self.free)
        self.assertContains(self.client.get(reverse("list")), 'title="Verified account"')


class StudentListsTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _count(self, url):
        self.client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx.captured_queries)

    def test_task_list_question_counts_are_one_query(self):
        def add(n):
            for i in range(n):
                task = GamificationTask.objects.create(title=f"T{GamificationTask.objects.count()}", description="d")
                for q in range(3):
                    TaskQuestion.objects.create(task=task, text=f"q{q}")

        add(2)
        few = self._count(reverse("task_list"))
        add(5)
        self.assertEqual(self._count(reverse("task_list")), few)  # no task.questions.count per card
        self.assertContains(self.client.get(reverse("task_list")), "Questions: 3")

    def test_course_list_is_paginated(self):
        from . import views

        for i in range(views.COURSES_PER_PAGE + 3):
            PartnerCourseSubmission.objects.create(partner=self.free, title=f"Course {i}", description="d", price=0,
                                                   instructor_name="X", status="Approved", skill=self.skill)
        page = self.client.get(reverse("course_list"))
        self.assertEqual(len(page.context["courses"]), views.COURSES_PER_PAGE)
        self.assertContains(page, 'href="?page=2"')
        self.assertEqual(len(self.client.get(reverse("course_list"), {"page": 2}).context["courses"]), 3)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "cache-key-tests"}})
class PublicPageCacheKeyTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_random_query_strings_share_the_canonical_copy_and_are_never_stored(self):
        miss = self.client.get(reverse("landing"), {"x": "random-1"})
        self.assertNotIn("X-Page-Cache", miss)  # rendered, not stored under its own key
        self.assertEqual(self.client.get(reverse("landing"))["X-Page-Cache"], "MISS")
        for junk in ("random-2", "random-3"):
            self.assertEqual(self.client.get(reverse("landing"), {"utm_source": junk})["X-Page-Cache"], "HIT")
        page_keys = [k for k in cache._cache if ":page:" in k]
        self.assertEqual(len(page_keys), 1)

    def test_declared_params_get_their_own_copy(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        from .page_cache import cache_public_page

        @cache_public_page(ttl=60, vary_on=("tag",))
        def view(request):
            return HttpResponse(request.GET.get("tag", "none"))

        factory = RequestFactory()

        def get(**params):
            request = factory.get("/tags/", params)
            request.user = mock.Mock(is_authenticated=False)
            request.COOKIES = {}
            return view(request)

        self.assertEqual(get(tag="a")["X-Page-Cache"], "MISS")
        self.assertEqual(get(tag="b")["X-Page-Cache"], "MISS")
        hit = get(tag="a", utm="z")
        self.assertEqual((hit["X-Page-Cache"], hit.content), ("HIT", b"a"))


class VerifiedIdentityChangeTests(Base):
    """Audit M3: a verified badge vouches for a name and an official email; changing either clears it."""

    def _post(self, **overrides):
        data = {"company_name": self.uni.company_name, "official_website": "", "official_email": self.uni.official_email or "",
                "linkedin_url": "", "facebook_url": "", "twitter_url": "", "instagram_url": "", **overrides}
        self.client.force_login(self.uni_user)
        return self.client.post(reverse("partner_profile"), data, follow=True)

    def _verified(self):
        return type(self.uni).objects.get(pk=self.uni.pk).is_fully_verified

    def test_social_links_keep_the_badge(self):
        self._post(linkedin_url="https://linkedin.com/school/cairo")
        self.assertTrue(self._verified())

    def test_renaming_clears_the_badge(self):
        resp = self._post(company_name="Totally Different University")
        self.assertFalse(self._verified())
        self.assertContains(resp, "the verified badge is paused until our team re-checks it")

    def test_new_official_email_clears_the_badge(self):
        self._post(official_email="admissions@elsewhere.edu")
        self.assertFalse(self._verified())


@override_settings(RATELIMIT_ENABLED=True)
class PromoCodeRateLimitTests(Base):
    """Audit L1: promo / discount codes cannot be brute-forced (10 tries an hour), other buttons are not counted."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_course_discount_attempts_are_limited(self):
        from .ratelimit import RULES

        course = PartnerCourseSubmission.objects.create(partner=self.free, title="C", description="d", price=100,
                                                        instructor_name="X", status="Approved")
        url = reverse("course_checkout", args=[course.pk])
        self.client.force_login(self.student_user)
        limit = RULES["course_checkout"][0]
        codes = [self.client.post(url, {"apply_discount": "1", "discount_code": f"GUESS{i}"}).status_code for i in range(limit + 1)]
        self.assertNotIn(429, codes[:limit])
        self.assertEqual(codes[-1], 429)
        self.assertNotEqual(self.client.post(url, {"confirm_purchase": "1"}).status_code, 429)  # buying is not a guess

    def test_startup_promo_attempts_are_limited(self):
        from .ratelimit import RULES

        self.client.force_login(self.free_user)
        url = reverse("startup_upgrade")
        limit = RULES["startup_upgrade"][0]
        codes = [self.client.post(url, {"action": "apply", "promo_code": f"GUESS{i}"}).status_code for i in range(limit + 1)]
        self.assertNotIn(429, codes[:limit])
        self.assertEqual(codes[-1], 429)
        self.assertNotEqual(self.client.post(url, {"action": "remove"}).status_code, 429)


class PaidCourseContentTests(Base):
    """Audit L2: no online course payment exists, so a paid seat is reserved: content hidden, skill not verifiable."""

    VIDEO = "https://videos.example/secret-course"

    def setUp(self):
        super().setUp()
        type(self.free).objects.filter(pk=self.free.pk).update(is_fully_verified=True)  # accredited partner
        self.course = PartnerCourseSubmission.objects.create(partner=self.free, title="Paid", description="d", price=500,
                                                             instructor_name="X", status="Approved", skill=self.skill,
                                                             video_link=self.VIDEO)
        self.client.force_login(self.student_user)

    def _checkout(self, course=None):
        resp = self.client.post(reverse("course_checkout", args=[(course or self.course).pk]), {"confirm_purchase": "1"}, follow=True)
        return resp, StudentEnrollment.objects.get(student=self.student, course=course or self.course)

    def test_paid_seat_is_pending_and_hides_the_content(self):
        resp, enrollment = self._checkout()
        self.assertEqual(enrollment.status, StudentEnrollment.STATUS_PENDING_PAYMENT)
        self.assertNotContains(resp, self.VIDEO)
        self.assertContains(resp, "The course opens as soon as your payment is confirmed.")
        listing = self.client.get(reverse("course_list"))
        self.assertNotContains(listing, self.VIDEO)
        self.assertContains(listing, "Awaiting payment")

    def test_unpaid_seat_cannot_be_completed_or_verify_the_skill(self):
        _resp, enrollment = self._checkout()
        self.client.force_login(self.free_user)
        self.client.post(reverse("partner_course_complete", args=[enrollment.pk]))
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.is_completed)
        self.assertFalse(StudentSkill.objects.filter(student=self.student, skill=self.skill,
                                                     status=StudentSkill.STATUS_VERIFIED).exists())
        from .courses import complete_enrollment
        self.assertFalse(complete_enrollment(enrollment))  # the service refuses too (admin action path)
        enrollment.refresh_from_db()
        self.assertFalse(enrollment.is_completed)

    def test_staff_payment_confirmation_unlocks_the_course(self):
        _resp, enrollment = self._checkout()
        admin = User.objects.create_superuser("boss", "boss@example.com", "pw")
        self.client.force_login(admin)
        self.client.post(reverse("admin:internest_core_studentenrollment_changelist"),
                         {"action": "confirm_payment", "_selected_action": [enrollment.pk]})
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, StudentEnrollment.STATUS_ENROLLED)
        self.client.force_login(self.student_user)
        self.assertContains(self.client.get(reverse("course_list")), self.VIDEO)

    def test_free_course_opens_immediately(self):
        free = PartnerCourseSubmission.objects.create(partner=self.free, title="Free", description="d", price=Decimal("0.00"),
                                                      instructor_name="X", status="Approved", video_link=self.VIDEO + "-free")
        resp, enrollment = self._checkout(free)
        self.assertEqual(enrollment.status, StudentEnrollment.STATUS_ENROLLED)
        self.assertContains(resp, self.VIDEO + "-free")


class LogHygieneTests(Base):
    """Audit L6: logs carry user ids, never email addresses."""

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_verification_email_log_has_no_address(self):
        from .email_helpers import issue_and_send_verification
        from .models import EmailVerification

        with self.assertLogs("internest_core.email_helpers", level="INFO") as logs:
            issue_and_send_verification(self.student_user, "private.person@uni.edu", EmailVerification.EMAIL_TYPE_PERSONAL)
        text = "\n".join(logs.output)
        self.assertNotIn("private.person", text)
        self.assertIn(f"user {self.student_user.pk}", text)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_failed_verification_email_log_has_no_address(self):
        from .email_helpers import issue_and_send_verification
        from .models import EmailVerification

        with self.assertLogs("internest_core.email_helpers", level="ERROR") as logs, \
                mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=Exception("smtp down")):
            issue_and_send_verification(self.student_user, "private.person@uni.edu", EmailVerification.EMAIL_TYPE_PERSONAL)
        self.assertNotIn("private.person", "\n".join(logs.output))


class TalentPoolCacheTests(Base):
    """talent.filter_options is cached pool-wide and invalidated when a candidate or skill changes."""

    def test_options_are_cached_and_bumped_on_save(self):
        from internest_startups import talent

        cache.clear()
        StudentProfile.objects.filter(pk=self.student.pk).update(talent_pool_visible=True, university="Cairo")
        ss = self._claim()
        StudentSkill.objects.filter(pk=ss.pk).update(status=StudentSkill.STATUS_VERIFIED)  # .update(): no signal
        from internest_core.page_cache import bump
        bump(talent.POOL_CACHE_GROUP)
        first = talent.filter_options()
        self.assertEqual(first["universities"], ["Cairo"])
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(talent.filter_options(), first)
        self.assertEqual(len(ctx.captured_queries), 0)  # served from the cache
        self.student.refresh_from_db()
        self.student.university = "Alexandria"
        self.student.save()  # post_save bumps the pool group
        self.assertEqual(talent.filter_options()["universities"], ["Alexandria"])
        self.assertEqual([s.slug for s in talent.filter_options()["skills"]], [Skill.objects.get(pk=self.skill.pk).slug])
