"""Interview kits: deterministic generation, paid-access gating, IDOR, async + idempotency, AI quota, mock path, PII."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from internest_ai import engine
from internest_ai.errors import QuotaExceeded
from internest_ai.testing import MOCK_ONLY, NO_PROVIDERS
from internest_core.models import Application, Internship, PartnerProfile, StudentProfile
from internest_skills.models import Skill, StudentSkill
from internest_skills.tests import Base

from . import interview_service as svc
from .models import InterviewKit

MOCK = dict(NO_PROVIDERS, **MOCK_ONLY)
ASYNC = {"HTTP_PREFER": "respond-async"}


class KitFixtures(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.python, self.sql, self.excel, self.law = (Skill.objects.get(name=n) for n in ("Python", "SQL", "Excel", "Legal Research"))
        self.student_user.first_name, self.student_user.last_name = "Mona", "Adel"
        self.student_user.email = "mona@example.com"
        self.student_user.save()
        StudentProfile.objects.filter(pk=self.student.pk).update(phone_number="01012345678")
        StudentSkill.objects.create(student=self.student, skill=self.python, source="explicit", status="verified", score=91)
        StudentSkill.objects.create(student=self.student, skill=self.sql, source="explicit", status="lag", score=55,
                                    lag_sub_skills=[{"id": 1, "name": "Joins"}])
        StudentSkill.objects.create(student=self.student, skill=self.excel, source="explicit", status="claimed")
        self.pro_job = self._job(self.pro, "Data Analyst Intern", [self.python, self.sql, self.excel, self.law])
        self.free_job = self._job(self.free, "Data Analyst Intern", [self.python, self.sql, self.excel, self.law])
        self.pro_app = Application.objects.create(internship=self.pro_job, applicant=self.student_user)
        self.free_app = Application.objects.create(internship=self.free_job, applicant=self.student_user)

    def _job(self, partner, title, skills, days=10):
        job = Internship.objects.create(partner=partner, title=title, description="d", location="Cairo", required_majors="any",
                                        deadline=timezone.localdate() + timedelta(days=days))
        job.required_skills.set(skills)
        return job


class DeterministicTests(KitFixtures):
    def test_kinds_follow_the_candidate_evidence(self):
        target = svc.resolve_target(self.student_user, {"internship": self.pro_job.pk})
        questions = svc.build_questions(target, "intern")
        by_skill = {q["skill"]: q for q in questions if q["skill"]}
        self.assertEqual({k: q["kind"] for k, q in by_skill.items()},
                         {"Python": "depth", "SQL": "subskill", "Excel": "evidence", "Legal Research": "gap"})
        self.assertIn("Joins", by_skill["SQL"]["question"])
        self.assertEqual((questions[0]["kind"], questions[-1]["kind"]), ("behavioral", "behavioral"))
        for q in questions:
            self.assertTrue(q["question"] and q["purpose"] and q["expected_evidence"])
            self.assertEqual((len(q["criteria"]), len(q["follow_ups"])), (3, 2))
            self.assertIn(q["difficulty"], ("easy", "medium", "hard"))

    def test_same_input_same_output_and_seniority_shifts_difficulty(self):
        target = svc.resolve_target(self.student_user, {"internship": self.pro_job.pk})
        self.assertEqual(svc.build_questions(target, "junior"), svc.build_questions(target, "junior"))
        intern = {q["skill"]: q["difficulty"] for q in svc.build_questions(target, "intern") if q["skill"]}
        mid = {q["skill"]: q["difficulty"] for q in svc.build_questions(target, "mid") if q["skill"]}
        self.assertEqual((intern["Python"], mid["Python"]), ("medium", "hard"))
        self.assertEqual((intern["Legal Research"], mid["Legal Research"]), ("easy", "medium"))

    def test_discipline_bank_is_used(self):
        target = svc.resolve_target(self.student_user, {"internship": self.pro_job.pk})
        law = next(q for q in svc.build_questions(target, "intern") if q["skill"] == "Legal Research")
        self.assertIn("first assignment", law["question"])  # the law bank, not the computing one

    def test_generic_kit_for_an_opportunity_has_no_candidate(self):
        target = svc.resolve_target(self.pro_user, {"internship": self.pro_job.pk})
        kinds = {q["kind"] for q in svc.build_questions(target, "intern") if q["skill"]}
        self.assertEqual(kinds, {"evidence"})
        self.assertIsNone(target.student)

    def test_arabic_kit(self):
        from django.utils import translation

        target = svc.resolve_target(self.student_user, {"internship": self.pro_job.pk})
        with translation.override("ar"):
            first = svc.build_questions(target, "intern")[0]["question"]
        self.assertRegex(first, r"[؀-ۿ]")


class PaidAccessTests(KitFixtures):
    def test_free_company_sees_only_verified_skill_names(self):
        kit = svc.create_kit(self.free_user, {"application": self.free_app.pk})
        text = json.dumps(kit.questions)
        self.assertFalse(kit.includes_assessments)
        self.assertNotIn("Joins", text)  # flagged areas are paid detail
        kinds = {q["skill"]: q["kind"] for q in kit.questions if q["skill"]}
        self.assertEqual(kinds["Python"], "depth")
        self.assertEqual(kinds["Excel"], "gap")     # an unverified claim is not visible to a free account
        self.assertEqual(kinds["SQL"], "gap")       # the failed assessment is paid detail too

    def test_paid_company_gets_flagged_areas(self):
        kit = svc.create_kit(self.pro_user, {"application": self.pro_app.pk})
        self.assertTrue(kit.includes_assessments)
        self.assertIn("Joins", json.dumps(kit.questions))
        self.assertEqual(kit.partner, self.pro)


class AccessTests(KitFixtures):
    def setUp(self):
        super().setUp()
        self.kit = svc.create_kit(self.pro_user, {"application": self.pro_app.pk})
        self.practice = svc.create_kit(self.student_user, {"internship": self.pro_job.pk})

    def test_company_b_cannot_use_company_a_applicant_or_opportunity(self):
        self.client.force_login(self.free_user)
        self.assertEqual(self.client.get(reverse("interview_kit_new") + f"?application={self.pro_app.pk}").status_code, 404)
        self.assertEqual(self.client.get(reverse("interview_kit_new") + f"?internship={self.pro_job.pk}").status_code, 404)
        self.assertEqual(self.client.post(reverse("interview_kit_new"), {"application_id": self.pro_app.pk}).status_code, 404)
        resp = self.client.post(reverse("api_interview_kits"), {"application_id": self.pro_app.pk}, content_type="application/json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (404, "not_found"))
        self.assertEqual(InterviewKit.objects.filter(owner=self.free_user).count(), 0)

    def test_company_b_cannot_read_company_a_kit(self):
        self.client.force_login(self.free_user)
        for name in ("interview_kit", "interview_kit_print"):
            self.assertEqual(self.client.get(reverse(name, args=[self.kit.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("api_interview_kit", args=[self.kit.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("interview_kit_enrich", args=[self.kit.pk])).status_code, 404)

    def test_student_b_cannot_read_student_a_kit_and_students_cannot_target_applications(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(reverse("interview_kit", args=[self.practice.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("api_interview_kit", args=[self.practice.pk])).status_code, 404)
        with self.assertRaises(svc.KitDenied):
            svc.resolve_target(self.other_user, {"application": self.pro_app.pk})

    def test_students_cannot_practise_on_a_closed_opportunity_they_never_applied_to(self):
        closed = self._job(self.pro, "Old role", [self.python], days=-3)
        with self.assertRaises(svc.KitDenied):
            svc.resolve_target(self.other_user, {"internship": closed.pk})
        Application.objects.create(internship=closed, applicant=self.other_user)
        self.assertEqual(svc.resolve_target(self.other_user, {"internship": closed.pk}).internship, closed)

    def test_owner_reads_kit_and_print_view(self):
        self.client.force_login(self.pro_user)
        page = self.client.get(reverse("interview_kit", args=[self.kit.pk]))
        self.assertContains(page, "Scoring criteria")
        self.assertContains(page, "Mona Adel")
        self.assertContains(page, reverse("partner_profile"))  # company navigation, not the student one
        printed = self.client.get(reverse("interview_kit_print", args=[self.kit.pk]))
        self.assertContains(printed, "data-print")  # print button, wired by ui_behaviors.js (no inline handler: CSP)
        self.assertContains(printed, "js/ui_behaviors")
        self.assertContains(printed, "☐ 5")
        data = self.client.get(reverse("api_interview_kit", args=[self.kit.pk])).json()
        self.assertEqual((data["id"], len(data["questions"])), (self.kit.pk, len(self.kit.questions)))

    def test_anonymous_api_is_401_json(self):
        resp = self.client.get(reverse("api_interview_kit", args=[self.kit.pk]))
        self.assertEqual((resp.status_code, resp.json()["code"]), (401, "auth_required"))

    def test_applicant_page_links_to_the_kit_form(self):
        self.client.force_login(self.pro_user)
        page = self.client.get(reverse("partner_applicant", args=[self.pro_app.pk]))
        self.assertContains(page, reverse("interview_kit_new") + f"?application={self.pro_app.pk}")


class FlowTests(KitFixtures):
    def test_partner_async_form_returns_202_and_redirect(self):
        self.client.force_login(self.pro_user)
        resp = self.client.post(reverse("interview_kit_new"), {"application_id": self.pro_app.pk, "seniority": "junior"}, **ASYNC)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        kit = InterviewKit.objects.get(owner=self.pro_user)
        self.assertEqual((body["status"], body["result"]["kit_id"], kit.seniority), ("done", kit.pk, "junior"))
        self.assertEqual(body["result"]["redirect"], reverse("interview_kit", args=[kit.pk]))
        status = self.client.get(body["status_url"]).json()
        self.assertEqual(status["result"]["kit_id"], kit.pk)

    def test_student_practice_without_js_redirects_to_the_kit(self):
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("interview_practice"), {"target": f"internship:{self.pro_job.pk}"})
        kit = InterviewKit.objects.get(owner=self.student_user)
        self.assertRedirects(resp, reverse("interview_kit", args=[kit.pk]))
        self.assertTrue(kit.is_practice)
        page = self.client.get(reverse("interview_kit", args=[kit.pk]))
        self.assertContains(page, "See what a strong answer shows")
        self.assertContains(page, 'aria-current="page"')  # the hub's interview tab

    def test_api_post_is_idempotent(self):
        self.client.force_login(self.pro_user)
        body = json.dumps({"application_id": self.pro_app.pk, "seniority": "intern"})
        first = self.client.post(reverse("api_interview_kits"), body, content_type="application/json")
        second = self.client.post(reverse("api_interview_kits"), body, content_type="application/json")
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(first.json()["result"]["kit_id"], second.json()["result"]["kit_id"])
        self.assertEqual(InterviewKit.objects.filter(owner=self.pro_user).count(), 1)
        listing = self.client.get(reverse("api_interview_kits")).json()["kits"]
        self.assertEqual([k["id"] for k in listing], [first.json()["result"]["kit_id"]])

    @override_settings(ASYNC_TASK_BACKEND="db")
    def test_double_submit_while_queued_returns_the_same_task(self):
        self.client.force_login(self.pro_user)
        body = json.dumps({"internship_id": self.pro_job.pk})
        first = self.client.post(reverse("api_interview_kits"), body, content_type="application/json").json()
        second = self.client.post(reverse("api_interview_kits"), body, content_type="application/json").json()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["status"], "queued")

    def test_practice_page_lists_targets(self):
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("interview_practice") + f"?internship={self.pro_job.pk}")
        self.assertContains(page, f'value="internship:{self.pro_job.pk}" selected')


@override_settings(**MOCK)
class AITests(KitFixtures):
    def test_mock_provider_adds_ai_questions(self):
        kit = svc.create_kit(self.pro_user, {"application": self.pro_app.pk}, use_ai=True)
        self.assertEqual((kit.ai_status, kit.source), ("done", "ai"))
        ai = [q for q in kit.questions if q["source"] == "ai"]
        self.assertEqual(len(ai), 1)
        self.assertEqual((ai[0]["kind"], ai[0]["skill"]), ("ai", ""))

    def test_ai_payload_has_no_personal_data(self):
        with mock.patch.object(engine, "run", wraps=engine.run) as run:
            svc.create_kit(self.pro_user, {"application": self.pro_app.pk}, use_ai=True)
        sent = json.dumps({k: run.call_args.kwargs.get(k) for k in ("data", "prompt", "system")}, ensure_ascii=False)
        for private in ("Mona", "Adel", "mona@example.com", "01012345678", "ProCo", "Cairo"):
            self.assertNotIn(private, sent)
        self.assertEqual(run.call_args.kwargs["partner"], self.pro.pk)
        self.assertIn("Joins", sent)  # skill-level data is what the AI works from

    def test_ai_questions_about_other_skills_or_duplicates_are_dropped(self):
        reply = {"questions": [
            {"question": "Q about Rust?", "purpose": "p", "expected_evidence": "e", "criteria": ["a"], "follow_ups": ["b"],
             "difficulty": "hard", "skill": "Rust"},
            {"question": "Explain a SQL join to a teammate.", "purpose": "p", "expected_evidence": "e", "criteria": ["a"],
             "follow_ups": ["b"], "difficulty": "extreme", "skill": "sql"}]}
        with mock.patch.object(engine, "run", return_value=reply):
            kit = svc.create_kit(self.pro_user, {"application": self.pro_app.pk}, use_ai=True)
        ai = [q for q in kit.questions if q["source"] == "ai"]
        self.assertEqual([(q["skill"], q["difficulty"]) for q in ai], [("SQL", "medium")])

    @override_settings(AI_QUOTA_TASK_LIMITS={"interview_questions": 1})
    def test_quota_keeps_the_deterministic_kit(self):
        first = svc.create_kit(self.student_user, {"internship": self.pro_job.pk}, use_ai=True)
        second = svc.create_kit(self.student_user, {"internship": self.pro_job.pk}, seniority="junior", use_ai=True)
        self.assertEqual((first.ai_status, second.ai_status), ("done", "quota"))
        self.assertEqual(second.source, "deterministic")
        self.assertTrue(all(q["source"] == "bank" for q in second.questions))
        self.client.force_login(self.student_user)
        self.assertContains(self.client.get(reverse("interview_kit", args=[second.pk])), "today&#x27;s AI limit")

    def test_partner_quota_scope(self):
        with mock.patch.object(engine, "run", side_effect=QuotaExceeded("partner_daily", 3600)):
            kit = svc.create_kit(self.pro_user, {"internship": self.pro_job.pk}, use_ai=True)
        self.assertEqual(kit.ai_status, "quota")

    def test_enrich_endpoint_later(self):
        with mock.patch.object(engine, "run", side_effect=engine.AIUnavailable("down")):
            kit = svc.create_kit(self.student_user, {"internship": self.pro_job.pk}, use_ai=True)
        self.assertEqual(kit.ai_status, "unavailable")
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("interview_kit_enrich", args=[kit.pk]), **ASYNC)
        self.assertEqual(resp.status_code, 202)
        kit.refresh_from_db()
        self.assertEqual((kit.ai_status, kit.source), ("done", "ai"))

    def test_api_post_with_ai(self):
        self.client.force_login(self.pro_user)
        resp = self.client.post(reverse("api_interview_kits"), {"internship_id": self.pro_job.pk, "ai": True},
                                content_type="application/json")
        self.assertEqual(resp.json()["result"]["ai_status"], "done")


class QueryCeilingTests(KitFixtures):
    CEILINGS = {"kit (company)": 8, "kit print (company)": 6, "interview practice (student)": 12, "kits list (company)": 8}

    def _count(self, user, url):
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx.captured_queries)

    def test_ceilings(self):
        kit = svc.create_kit(self.pro_user, {"application": self.pro_app.pk})
        pages = {"kit (company)": (self.pro_user, reverse("interview_kit", args=[kit.pk])),
                 "kit print (company)": (self.pro_user, reverse("interview_kit_print", args=[kit.pk])),
                 "interview practice (student)": (self.student_user, reverse("interview_practice")),
                 "kits list (company)": (self.pro_user, reverse("interview_kits"))}
        for label, (user, url) in pages.items():
            with self.subTest(label):
                count = self._count(user, url)
                self.assertLessEqual(count, self.CEILINGS[label], f"{label}: {count} queries")

    def test_kit_list_does_not_grow_with_kits(self):
        for seniority in ("intern", "junior", "mid"):
            svc.create_kit(self.pro_user, {"internship": self.pro_job.pk}, seniority=seniority)
        few = self._count(self.pro_user, reverse("interview_kits"))
        for seniority in ("intern", "junior", "mid"):
            svc.create_kit(self.pro_user, {"application": self.pro_app.pk}, seniority=seniority)
        self.assertEqual(self._count(self.pro_user, reverse("interview_kits")), few)
