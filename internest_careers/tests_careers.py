"""Career recommendations: role paths, coverage + why, gaps with courses and projects, best fit, cache, API, AI advice."""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from internest_ai import engine
from internest_ai.testing import MOCK_ONLY, NO_PROVIDERS
from internest_core.models import Internship
from internest_skills.models import Skill, StudentSkill
from internest_skills.tests import Base

from . import career_service as careers
from .career_service import normalize_title

MOCK = dict(NO_PROVIDERS, **MOCK_ONLY)


class TitleTests(SimpleTestCase):
    def test_normalize_title(self):
        self.assertEqual(normalize_title("Marketing Intern (Summer 2026)"), "marketing")
        self.assertEqual(normalize_title("Junior Data Analyst - Remote"), "data analyst")
        self.assertEqual(normalize_title("متدرب تسويق"), "تسويق")
        self.assertEqual(careers._display("ui ux designer"), "UI UX Designer")


class CareerFixtures(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.sql, self.excel, self.marketing, self.comms = (
            Skill.objects.get(name=n) for n in ("SQL", "Excel", "Digital Marketing", "Business Communication"))
        self.da1 = self._job("Data Analyst Intern", [self.sql, self.excel])
        self.da2 = self._job("Junior Data Analyst", [self.sql, self.excel])
        self.da3 = self._job("Data Analyst", [self.sql])
        self.mk = self._job("Marketing Trainee", [self.marketing, self.comms])
        self._job("Closed role", [self.sql], days=-2)
        StudentSkill.objects.create(student=self.student, skill=self.sql, source="explicit", status="verified", score=88)
        StudentSkill.objects.create(student=self.student, skill=self.comms, source="explicit", status="claimed")

    def _job(self, title, skills, days=10):
        job = Internship.objects.create(partner=self.pro, title=title, description="d", location="Cairo", required_majors="any",
                                        deadline=timezone.localdate() + timedelta(days=days))
        job.required_skills.set(skills)
        return job


class RoleProfileTests(CareerFixtures):
    def test_titles_are_grouped_and_core_skills_derived(self):
        roles = {r["key"]: r for r in careers.role_profiles()["roles"]}
        self.assertEqual(set(roles), {"data-analyst", "marketing"})
        analyst = roles["data-analyst"]
        self.assertEqual((analyst["title"], analyst["openings"]), ("Data Analyst", 3))
        self.assertEqual([(s["name"], s["demand"]) for s in analyst["skills"]], [("SQL", 3), ("Excel", 2)])
        self.assertEqual(analyst["discipline"], "computing")

    def test_same_core_skills_merge_across_titles(self):
        self._job("Growth Marketing Intern", [self.marketing, self.comms])
        roles = {r["key"]: r for r in careers.role_profiles()["roles"]}
        self.assertEqual(roles["marketing"]["openings"], 2)
        self.assertNotIn("growth-marketing", roles)


class RecommendationTests(CareerFixtures):
    def test_coverage_gaps_and_why(self):
        recs = careers.recommendations(self.student)
        analyst = recs["roles"][0]
        self.assertEqual(analyst["key"], "data-analyst")  # best covered first
        self.assertEqual(analyst["coverage"], 70)            # SQL 1.0 + Excel 0.4 (seeded relation) -> 70%
        self.assertEqual(analyst["readiness"], "ready")
        self.assertIn("asking for these skills: 3.", analyst["why"])
        self.assertIn("1 of 2 core skills", analyst["why"])
        self.assertEqual({s["name"]: s["state"] for s in analyst["skills"]}, {"SQL": "verified", "Excel": "missing"})
        gap = analyst["gaps"][0]
        self.assertEqual(gap["skill"], "Excel")
        self.assertIn("require it: 2 of 3.", gap["why"])
        self.assertTrue(gap["courses"] and gap["courses"][0]["url"])
        self.assertTrue(gap["project"]["title"] and "Excel" in gap["project"]["description"])
        self.assertIn("role=data-analyst", analyst["practice_url"])

    def test_claimed_skill_is_called_out(self):
        marketing = next(r for r in careers.recommendations(self.student)["roles"] if r["key"] == "marketing")
        self.assertEqual(marketing["to_verify"], 1)
        self.assertIn("already listed (1)", marketing["why"])
        comms = next(g for g in marketing["gaps"] if g["skill"] == "Business Communication")
        self.assertTrue(comms["claimed"])

    def test_best_fit_internships_with_reasons(self):
        fit = careers.recommendations(self.student)["internships"]
        self.assertEqual(fit[0]["id"], self.da3.pk)  # SQL only: 100%
        self.assertEqual(fit[0]["score"], 100)
        self.assertNotIn(self.mk.pk, [i["id"] for i in fit])  # 0% is not a fit
        partial = next(i for i in fit if i["id"] == self.da1.pk)
        self.assertIn("To close: Excel", partial["why"])

    def test_no_verified_skills_means_no_best_fit(self):
        self.assertEqual(careers.recommendations(self.other_student)["internships"], [])

    def test_cached_and_invalidated_by_skill_and_market_changes(self):
        careers.recommendations(self.student)
        with CaptureQueriesContext(connection) as ctx:
            careers.recommendations(self.student)
        self.assertEqual(len(ctx.captured_queries), 0)
        StudentSkill.objects.create(student=self.student, skill=self.excel, source="explicit", status="verified", score=80)
        analyst = careers.recommendations(self.student)["roles"][0]
        self.assertEqual(analyst["coverage"], 100)
        self._job("Data Analyst Trainee", [self.sql, self.excel])
        analyst = careers.recommendations(self.student)["roles"][0]
        self.assertEqual(analyst["openings"], 4)

    def test_role_by_key_feeds_interview_practice(self):
        from .interview_service import create_kit

        role = careers.role_by_key("data-analyst")
        self.assertEqual([s.name for s in role["skills"]], ["Excel", "SQL"])
        kit = create_kit(self.student_user, {"role": "data-analyst"})
        self.assertEqual(kit.role, "Data Analyst")
        self.assertEqual({q["skill"] for q in kit.questions if q["skill"]}, {"Excel", "SQL"})


class PageAndAPITests(CareerFixtures):
    def test_page_links_from_the_workspace_and_shows_paths(self):
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("career_paths"))
        self.assertContains(page, "Data Analyst")
        self.assertContains(page, "Best-fit internships")
        self.assertContains(page, reverse("interview_practice") + "?role=data-analyst")
        hub = self.client.get(reverse("my_applications"))
        self.assertContains(hub, reverse("career_paths"))
        self.assertContains(hub, reverse("interview_practice"))
        self.assertNotContains(page, "sh-stats")  # the workspace's stat widgets are not repeated here

    def test_api_returns_the_same_data(self):
        self.client.force_login(self.student_user)
        data = self.client.get(reverse("api_careers")).json()
        self.assertEqual(data["roles"][0]["key"], "data-analyst")
        self.assertEqual(data["verified_skills"], ["SQL"])

    def test_api_permissions(self):
        self.assertEqual(self.client.get(reverse("api_careers")).status_code, 401)
        self.client.force_login(self.pro_user)
        resp = self.client.get(reverse("api_careers"))
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "students_only"))
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("api_careers"))
        self.assertEqual((resp.status_code, resp.json()["code"]), (405, "method_not_allowed"))

    def test_query_ceiling(self):
        self.client.force_login(self.student_user)
        url = reverse("career_paths")
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 6)  # warm: session, user, profile; recommendations from cache

    @override_settings(**MOCK)
    def test_ai_advice_async_with_anonymous_payload(self):
        self.student_user.first_name = "Mona"
        self.student_user.save()
        self.client.force_login(self.student_user)
        with mock.patch.object(engine, "run", wraps=engine.run) as run:
            resp = self.client.post(reverse("api_career_advice"))
        self.assertEqual(resp.status_code, 202)
        result = resp.json()["result"]
        self.assertTrue(result["narrative"] and result["next_steps"])
        sent = json.dumps(run.call_args.kwargs["data"], ensure_ascii=False)
        self.assertIn("Data Analyst", sent)
        for private in ("Mona", "Cairo", "Finance"):
            self.assertNotIn(private, sent)

    @override_settings(**NO_PROVIDERS)
    def test_ai_advice_unavailable(self):
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("api_career_advice"))
        self.assertEqual((resp.status_code, resp.json()["code"]), (503, "ai_unavailable"))


class APIErrorShapeTests(CareerFixtures):
    @override_settings(RATELIMIT_ENABLED=True)
    def test_rate_limited_api_answers_json(self):
        from internest_core import ratelimit

        self.client.force_login(self.student_user)
        with mock.patch.dict(ratelimit.RULES, {"api_career_advice": (1, 3600)}):
            self.client.post(reverse("api_career_advice"))
            resp = self.client.post(reverse("api_career_advice"))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["code"], "rate_limited")
        self.assertTrue(resp["Retry-After"])
