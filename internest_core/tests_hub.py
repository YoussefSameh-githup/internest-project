"""Student hub: shared tabs, application stepper, skill quizzes and learning paths."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill


class StudentHubTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        self.login_student(self.user)

    def _skill(self, slug, status, score=None, **extra):
        return StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug=slug), source="explicit",
                                           status=status, score=score, **extra)

    def test_tabs_on_all_three_pages(self):
        for name, active in (("my_applications", "📄"), ("task_list", "🎯"), ("course_list", "📚")):
            page = self.client.get(reverse(name))
            self.assertEqual(page.status_code, 200)
            for url in ("my_applications", "task_list", "course_list"):
                self.assertContains(page, f'href="{reverse(url)}"')
            self.assertContains(page, f'is-active" aria-current="page">{active}')
            self.assertContains(page, "Your student hub")

    def test_application_card_stepper_and_invited_pill(self):
        Application.objects.create(internship=self.gig, applicant=self.user, status=Application.STATUS_SHORTLISTED,
                                   source=Application.SOURCE_INVITED)
        Application.objects.create(internship=self.role, applicant=self.user, status=Application.STATUS_FULFILLED)
        page = self.client.get(reverse("my_applications"))
        self.assertContains(page, '<li class="is-done">Interview</li>')
        self.assertContains(page, '<li class="">Accepted</li>')
        self.assertContains(page, "🎟️ Invited")
        self.assertContains(page, "This position has been filled.")
        self.assertEqual(page.context["hub_stats"]["applications"], 2)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("my_applications")), "تم ترشيحك للمقابلة 🎉")

    def test_skill_quiz_cards(self):
        claimed = self._skill("python", "claimed")
        lag = self._skill("excel", "lag", score=55)
        self._skill("legal-research", "lag", score=40, cooldown_until=timezone.now() + timedelta(days=3))
        self._skill("project-management", "verified", score=92, percentile=81)
        page = self.client.get(reverse("task_list"))
        self.assertContains(page, reverse("skills_challenge_start", args=[claimed.pk]))
        self.assertContains(page, "Start quiz")
        self.assertContains(page, reverse("skills_challenge_start", args=[lag.pk]))
        self.assertContains(page, "Retake quiz")
        self.assertContains(page, "Retest available")
        self.assertContains(page, "Percentile P81")
        self.assertEqual(page.context["hub_stats"]["verified"], 1)

    def test_learning_paths_from_quiz_results(self):
        skill = Skill.objects.get(slug="financial-analysis")
        sub = skill.sub_skills.first()
        self._skill("financial-analysis", "lag", score=48, lag_sub_skills=[{"id": sub.id, "name": sub.name, "accuracy": 30}])
        self._skill("python", "claimed")  # untested: no learning path
        page = self.client.get(reverse("course_list"))
        tracks = page.context["tracks"]
        self.assertEqual([t["record"].skill.slug for t in tracks], ["financial-analysis"])
        self.assertTrue(tracks[0]["recs"])
        self.assertContains(page, "Close your skill gap")
        self.assertContains(page, "Start learning")
        self.assertContains(page, "Recommended for you")
