"""Phase 3: the public trust page shows only true, live facts; candidate evidence report."""
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, StudentProfile, TeamMember, TrustStory
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import ChallengeAttempt, ChallengeItem, Skill, StudentSkill

URL = reverse("trust")


class TrustPageTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_public_in_both_languages_with_every_required_section(self):
        page = self.client.get(URL)
        self.assertEqual(page.status_code, 200)
        for anchor in ("method", "assessment", "integrity", "limits", "privacy", "evidence", "numbers", "people"):
            self.assertContains(page, f'id="{anchor}"')
        for text in ("A high score alone never counts", "No online test is cheat-proof",
                     "Skill tests never turn on the camera or microphone", "we don't guarantee hires",
                     "We don't claim formal compliance", "Law No. 151 of 2020",
                     "No person reviews these automatic decisions.", "AI-written questions and answer keys can contain mistakes.",
                     "Generated questions are added to the bank without a person checking each one first."):
            self.assertContains(page, text)
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(URL)
        self.assertContains(page, "ليه تثق في Internest؟")
        self.assertContains(page, "اختبارات المهارات مش بتشغّل الكاميرا أو الميكروفون")

    def test_rules_shown_are_the_rules_enforced(self):
        from internest_skills.models import RETEST_COOLDOWN, ChallengeAttempt as Attempt

        page = self.client.get(URL)
        low, high = ChallengeItem.QUICK_SECONDS
        self.assertContains(page, f"{low}–{high} seconds per question")
        self.assertContains(page, f"more than {Attempt.MAX_FOCUS_WARNINGS} times, end the attempt")
        self.assertContains(page, f"after {int(RETEST_COOLDOWN.total_seconds() // 3600)} hours")
        self.assertContains(page, "13 of the 13 skills with a test end with a 15-minute real task")  # counted, per skill
        for overclaim in ("a flag alone never fails anyone", "Each tested skill ends with", "drawn from questions the student has not seen"):
            self.assertNotContains(page, overclaim)

    def test_numbers_are_live(self):
        page = self.client.get(URL)
        self.assertEqual(page.context["numbers"]["candidates"], 0)
        user, student = _student("amr")
        StudentProfile.objects.filter(pk=student.pk).update(talent_pool_visible=True)
        StudentSkill.objects.create(student=student, skill=Skill.objects.get(slug="python"), source="explicit", status="verified", score=90)
        Application.objects.create(internship=self.gig, applicant=user, status=Application.STATUS_ACCEPTED, shortlisted_at=timezone.now())
        Application.objects.create(internship=self.role, applicant=user, status=Application.STATUS_ACCEPTED, shortlisted_at=timezone.now())
        cache.clear()
        numbers = self.client.get(URL).context["numbers"]
        # two accepted applications from one student = one candidate
        self.assertEqual((numbers["candidates"], numbers["verified_skills"], numbers["accepted"], numbers["shortlisted"]), (1, 1, 1, 1))
        self.assertEqual(numbers["verified_companies"], 1)  # NileCode from the fixtures
        self.assertEqual(numbers["practical_tasks"], 13)

    def test_nothing_invented_when_there_is_no_team_or_story(self):
        page = self.client.get(URL)
        self.assertContains(page, "Team profiles will be added here.")
        self.assertContains(page, "No testimonials yet. We publish only real ones")
        self.assertContains(page, "No case studies yet.")
        self.assertNotContains(page, "<blockquote")

    def test_stories_need_consent_and_publishing(self):
        TrustStory.objects.create(person_name="Draft Person", person_role="Founder", body="Unpublished words", source_note="call")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrustStory.objects.create(person_name="No Consent", person_role="Founder", body="x", source_note="y", is_published=True)
        TrustStory.objects.create(person_name="Mona Adel", person_role="Founder, Kemet Labs", body="We hired two interns in a week.",
                                  source_note="email 2026-11-10", consent_given_at=timezone.now(), is_published=True)
        TeamMember.objects.create(name="Hidden Member", role="CTO")
        TeamMember.objects.create(name="Youssef", role="Founder", is_published=True)
        cache.clear()
        page = self.client.get(URL)
        self.assertContains(page, "We hired two interns in a week.")
        self.assertContains(page, "Youssef")
        for hidden in ("Unpublished words", "No Consent", "Hidden Member", "email 2026-11-10"):
            self.assertNotContains(page, hidden)

    def test_linked_from_footer_preview_checkout_and_skills_hub(self):
        self.assertContains(self.client.get(reverse("landing")), f'href="{URL}"')
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(reverse("talent_pool_preview")), f'href="{URL}"')
        self.assertContains(self.client.get(reverse("checkout")), f'href="{URL}"')

    def test_anonymous_page_is_cached(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(URL)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(URL)
        self.assertEqual(len(ctx.captured_queries), 0)


    def test_unpublishing_a_story_reaches_cached_visitors_at_once(self):
        story = TrustStory.objects.create(person_name="Mona Adel", person_role="Founder", body="Quick hires.", source_note="call",
                                          consent_given_at=timezone.now(), is_published=True)
        self.assertContains(self.client.get(URL), "Quick hires.")
        story.is_published = False
        story.save()
        self.assertNotContains(self.client.get(URL), "Quick hires.")

    def test_freelance_wording_follows_the_setting(self):
        from django.test import override_settings

        self.assertContains(self.client.get(URL), "Freelance work the student did, reviewed by our team.")
        with override_settings(FREELANCE_AUTO_VERIFY=True):
            cache.clear()
            self.assertContains(self.client.get(URL), "checked automatically against the student")


class EvidenceReportTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        from internest_startups import entitlements

        self.user, self.student = _student("amr")
        self.app = Application.objects.create(internship=self.gig, applicant=self.user)
        python = Skill.objects.get(slug="python")
        self.ss = StudentSkill.objects.create(student=self.student, skill=python, source="explicit", status="verified", score=88,
                                              percentile=75, verified_via="challenge", verified_at=timezone.now())
        practical = python.items.get(kind="practical")
        ChallengeAttempt.objects.create(student_skill=self.ss, item_ids=[practical.pk], state="completed", finished_at=timezone.now(),
                                        review_status="approved", reviewed_at=timezone.now())
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="excel"), source="explicit", status="verified",
                                    verified_via="certificate", verified_at=timezone.now())
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="sql"), source="explicit", status="lag", score=50)
        entitlements.create_pass(self.partner, self.gig, source="admin")
        self.url = reverse("skills_student_report", args=[self.student.pk])

    def test_paid_employer_sees_how_each_verified_skill_was_verified(self):
        self.client.force_login(self.founder)
        page = self.client.get(self.url)
        self.assertContains(page, "Evidence report")
        rows = {r["skill"]: r for r in page.context["evidence"]}
        self.assertEqual(set(rows), {"Python", "Excel"})  # only verified skills are evidence
        self.assertTrue(rows["Python"]["practical"] and rows["Python"]["human_reviewed"])
        self.assertEqual(rows["Excel"]["via"], "Reviewed certificate")
        self.assertTrue(rows["Excel"]["human_reviewed"])
        self.assertContains(page, "Print or save as PDF")

    def test_unrelated_company_cannot_see_it(self):
        from internest_startups.tests_hiring_pass import _company

        rival, _p = _company("rival")
        self.client.force_login(rival)
        self.assertEqual(self.client.get(self.url).status_code, 403)

class EvidenceAccuracyTests(EvidenceReportTests):
    def test_old_failed_test_score_is_not_shown_as_certificate_evidence(self):
        StudentSkill.objects.filter(student=self.student, skill__slug="excel").update(score=41, percentile=12)
        self.client.force_login(self.founder)
        rows = {r["skill"]: r for r in self.client.get(self.url).context["evidence"]}
        self.assertEqual((rows["Excel"]["score"], rows["Excel"]["percentile"], rows["Excel"]["practical"]), (None, None, False))
        self.assertEqual(rows["Python"]["score"], 88)

    def test_hand_set_verification_is_labelled(self):
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="excel").__class__.objects.get(slug="web-development"),
                                    source="explicit", status="verified")
        self.client.force_login(self.founder)
        rows = {r["skill"]: r for r in self.client.get(self.url).context["evidence"]}
        self.assertEqual((rows["Web Development"]["via"], rows["Web Development"]["human_reviewed"]), ("Set by our team", False))
