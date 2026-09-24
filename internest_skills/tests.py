import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import docx
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import (
    Application, Internship, PartnerApplicantData, PartnerCourseSubmission, PartnerProfile, StudentProfile,
)

from . import challenge as engine
from .extraction import extract_skills
from .integrity import analyze_typing
from .models import ChallengeAttempt, ChallengeItem, EmployerSubscription, Skill, SkillProfile, StudentSkill
from .permissions import skill_view_role
from .recommendations import recommendations_for

HUMAN_STATS = {"keystrokes": 80, "max_single_insert": 1, "intervals": [120, 95, 210, 80, 150] * 5}


def _docx_upload(text, name="cv.docx"):
    buf = io.BytesIO()
    d = docx.Document()
    for line in text.split("\n"):
        d.add_paragraph(line)
    d.save(buf)
    return SimpleUploadedFile(name, buf.getvalue(),
                              content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@override_settings(AGENTROUTER_API_KEY="")  # never hit the real LLM from tests
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_skills", stdout=io.StringIO())
        cls.student_user = User.objects.create_user("stud", password="pw")
        cls.student = StudentProfile.objects.create(user=cls.student_user, university="Cairo", major="Finance", study_level="3")
        cls.other_user = User.objects.create_user("other", password="pw")
        cls.other_student = StudentProfile.objects.create(user=cls.other_user, university="Ain Shams", major="Law", study_level="2")

        cls.uni_user = User.objects.create_user("uni", password="pw")
        cls.uni = PartnerProfile.objects.create(user=cls.uni_user, company_name="Cairo Uni", partner_code="U1",
                                                is_academic=True, is_fully_verified=True)
        cls.pro_user = User.objects.create_user("pro", password="pw")
        cls.pro = PartnerProfile.objects.create(user=cls.pro_user, company_name="ProCo", partner_code="P1")
        EmployerSubscription.objects.create(partner=cls.pro, plan=EmployerSubscription.PLAN_PRO)
        cls.free_user = User.objects.create_user("free", password="pw")
        cls.free = PartnerProfile.objects.create(user=cls.free_user, company_name="FreeCo", partner_code="F1")

        cls.skill = Skill.objects.get(slug="financial-analysis")

    def _claim(self, student=None, skill=None):
        return StudentSkill.objects.create(student=student or self.student, skill=skill or self.skill,
                                           source=StudentSkill.SOURCE_EXPLICIT)

    def _forward(self, partner, student):
        internship = Internship.objects.create(partner=partner, title="T", description="d", location="Cairo",
                                               required_majors="any", deadline=timezone.now().date())
        app = Application.objects.create(internship=internship, applicant=student.user)
        PartnerApplicantData.objects.create(partner=partner, student=student, internship=internship, application=app)


class ExtractionTests(Base):
    def test_explicit_implicit_and_arabic_across_disciplines(self):
        text = ("Built a DCF valuation for a retail client.\n"
                "Moot court finalist; drafted memoranda.\n"
                "Designed logos for the student union.\n"
                "مهارات: اكسل وصياغة العقود")
        found = {e.skill.name: e.source for e in extract_skills(text)}
        self.assertEqual(found["Financial Analysis"], "explicit")
        self.assertEqual(found["Legal Research"], "implicit")
        self.assertEqual(found["Graphic Design"], "implicit")
        self.assertEqual(found["Excel"], "explicit")
        self.assertEqual(found["Contract Drafting"], "explicit")
        self.assertNotIn("Python", found)

    def test_symbol_terms_and_word_boundaries(self):
        found = {e.skill.name for e in extract_skills("Tools: node.js and t-sql. I love pythonic ideas.")}
        self.assertIn("Web Development", found)
        self.assertIn("SQL", found)
        self.assertNotIn("Python", found)

    def test_hub_upload_marks_claimed_and_never_downgrades_verified(self):
        verified = self._claim(skill=Skill.objects.get(slug="excel"))
        verified.status = StudentSkill.STATUS_VERIFIED
        verified.save()
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("skills_hub"), {
            "cv_file": _docx_upload("Experience: financial modeling and Excel pivot tables at a bank."),
            "partner_university": self.uni.pk,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(StudentSkill.objects.get(student=self.student, skill=self.skill).status, "claimed")
        verified.refresh_from_db()
        self.assertEqual(verified.status, "verified")
        self.assertEqual(SkillProfile.objects.get(student=self.student).partner_university, self.uni)

    def test_linkedin_url_only_is_saved_without_extraction(self):
        self.client.force_login(self.student_user)
        self.client.post(reverse("skills_hub"), {"linkedin_url": "https://www.linkedin.com/in/someone"})
        self.assertEqual(self.student.skill_profile.linkedin_url, "https://www.linkedin.com/in/someone")
        self.assertFalse(self.student.skills.exists())

    def test_rejects_non_linkedin_url(self):
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("skills_hub"), {"linkedin_url": "https://evil.example/in/x"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(SkillProfile.objects.filter(student=self.student, linkedin_url__contains="evil").exists())


class IntegrityTests(TestCase):
    def test_human_typing_passes(self):
        self.assertEqual(analyze_typing("x" * 60, HUMAN_STATS), [])

    def test_injected_text_flagged(self):
        flags = analyze_typing("x" * 300, {"keystrokes": 10, "max_single_insert": 290, "intervals": []})
        self.assertIn("bulk_text_injection", flags)
        self.assertIn("large_single_insert", flags)

    def test_robotic_rhythm_and_missing_telemetry(self):
        flags = analyze_typing("x" * 40, {"keystrokes": 40, "max_single_insert": 1, "intervals": [15] * 39})
        self.assertIn("robotic_uniform_rhythm", flags)
        self.assertIn("superhuman_typing_speed", flags)
        self.assertEqual(analyze_typing("x" * 40, {}), ["missing_telemetry"])


class ChallengeFlowTests(Base):
    def _answer_all(self, attempt, correct=True):
        while attempt.is_active:
            payload = engine.serve_next(attempt)
            if payload is None:
                break
            item = ChallengeItem.objects.get(pk=payload["item_id"])
            if item.kind == ChallengeItem.KIND_MCQ:
                choice = item.correct_index if correct else (item.correct_index + 1) % len(item.choices)
                engine.submit_answer(attempt, item.id, choice_index=choice)
            else:
                text = " ".join(g[0] for g in item.rubric) + " padding words" if correct else "no idea at all honestly"
                engine.submit_answer(attempt, item.id, answer_text=text,
                                     typing_stats={**HUMAN_STATS, "keystrokes": len(text)})
            attempt.refresh_from_db()
        return attempt

    def test_pass_awards_verified_badge_and_percentile(self):
        ss = self._claim()
        attempt = self._answer_all(engine.start_attempt(ss))
        ss.refresh_from_db()
        self.assertEqual(attempt.state, "completed")
        self.assertEqual(ss.status, "verified")
        self.assertEqual(ss.score, 100)
        self.assertIsNotNone(ss.percentile)

    def test_fail_sets_skill_lag_with_cooldown_and_recommendations(self):
        PartnerCourseSubmission.objects.create(
            partner=self.uni, title="Ratio analysis bootcamp", description="Financial ratios in practice",
            price=100, instructor_name="Dr X", points_awarded=5, status="Approved")
        ss = self._claim()
        self._answer_all(engine.start_attempt(ss), correct=False)
        ss.refresh_from_db()
        self.assertEqual(ss.status, "lag")
        self.assertTrue(ss.status_label.startswith("Skill Lag Detected in "))
        self.assertNotIn("fail", ss.status_label.lower())
        self.assertAlmostEqual(ss.cooldown_seconds_remaining, 14 * 86400, delta=60)
        data = recommendations_for(ss)
        self.assertTrue(data["recommendations"])
        providers = {r["provider_type"] for r in data["recommendations"]}
        self.assertIn("internest_partner", providers)
        self.assertTrue(all({"title", "provider", "expected_outcome", "url"} <= r.keys() for r in data["recommendations"]))
        with self.assertRaises(engine.ChallengeError):
            engine.start_attempt(ss)

    def test_timeout_is_enforced_server_side(self):
        attempt = engine.start_attempt(self._claim())
        payload = engine.serve_next(attempt)
        ChallengeAttempt.objects.filter(pk=attempt.pk).update(
            current_served_at=timezone.now() - timedelta(seconds=payload["time_limit"] + 10))
        attempt.refresh_from_db()
        engine.submit_answer(attempt, payload["item_id"], choice_index=0, answer_text="x")
        resp = attempt.responses.get(item_id=payload["item_id"])
        self.assertTrue(resp.timed_out)
        self.assertEqual(resp.credit, 0)

    def test_third_focus_loss_terminates(self):
        ss = self._claim()
        attempt = engine.start_attempt(ss)
        for _ in range(2):
            engine.record_focus_loss(attempt)
        self.assertTrue(attempt.is_active)
        engine.record_focus_loss(attempt)
        self.assertEqual(attempt.state, "terminated")
        ss.refresh_from_db()
        self.assertTrue(ss.in_cooldown)
        self.assertEqual(ss.status, "claimed")

    def test_api_flow_hides_answers_and_blocks_other_students(self):
        ss = self._claim()
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        attempt = ss.attempts.get()
        self.assertRedirects(resp, reverse("skills_challenge", args=[attempt.token]))
        self.assertEqual(self.client.get(resp.url).status_code, 200)
        data = self.client.post(reverse("skills_api_next", args=[attempt.token])).json()
        self.assertEqual(data["state"], "active")
        self.assertNotIn("correct_index", json.dumps(data))
        self.assertNotIn("rubric", json.dumps(data))
        ev = self.client.post(reverse("skills_api_event", args=[attempt.token]), '{"type":"tab_hidden"}',
                              content_type="application/json").json()
        self.assertEqual(ev["focus_warnings"], 1)

        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(reverse("skills_api_next", args=[attempt.token])).status_code, 404)


class AccessControlTests(Base):
    def test_roles(self):
        SkillProfile.objects.create(student=self.student, partner_university=self.uni)
        self._forward(self.pro, self.student)
        self._forward(self.free, self.student)
        self.assertEqual(skill_view_role(self.student_user, self.student), "self")
        self.assertEqual(skill_view_role(self.uni_user, self.student), "university")
        self.assertEqual(skill_view_role(self.pro_user, self.student), "employer_pro")
        self.assertIsNone(skill_view_role(self.free_user, self.student))
        self.assertIsNone(skill_view_role(self.other_user, self.student))
        self.assertIsNone(skill_view_role(self.uni_user, self.other_student))  # not affiliated
        self.assertIsNone(skill_view_role(self.pro_user, self.other_student))  # never applied

    def test_expired_pro_loses_access(self):
        self._forward(self.pro, self.student)
        self.pro.subscription.valid_until = timezone.now().date() - timedelta(days=1)
        self.pro.subscription.save()
        self.assertIsNone(skill_view_role(self.pro_user, self.student))

    def test_report_views(self):
        self._forward(self.pro, self.student)
        self._forward(self.free, self.student)
        url = reverse("skills_student_report", args=[self.student.pk])
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.free_user)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "Internest Pro", status_code=403)

    def test_university_dashboard_only_for_verified_university(self):
        SkillProfile.objects.create(student=self.student, partner_university=self.uni)
        ss = self._claim()
        ss.status = StudentSkill.STATUS_VERIFIED
        ss.percentile = 80
        ss.save()
        self.client.force_login(self.uni_user)
        resp = self.client.get(reverse("skills_university_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["employability_rate"], 100)
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(reverse("skills_university_dashboard")).status_code, 403)

    def test_profile_cta_and_partner_dashboard_render(self):
        self.client.force_login(self.student_user)
        self.assertContains(self.client.get(reverse("profile")), "Analyze & Verify Skills")
        self._forward(self.pro, self.student)
        self.client.force_login(self.pro_user)
        self.assertContains(self.client.get(reverse("partner_dashboard")),
                            reverse("skills_student_report", args=[self.student.pk]))
        self.client.force_login(self.free_user)
        self.assertNotContains(self.client.get(reverse("partner_dashboard")),
                               reverse("skills_student_report", args=[self.student.pk]))

    def test_recommendations_api_is_self_only(self):
        ss = self._claim()
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(reverse("skills_api_recommendations", args=[ss.pk])).status_code, 403)
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("skills_api_recommendations", args=[ss.pk])).status_code, 200)


def _llm_reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@override_settings(AGENTROUTER_API_KEY="k", AGENTROUTER_BASE_URL="https://llm.test/v1")
class LLMExtractionTests(Base):
    def test_llm_maps_known_skills_and_creates_new_ones(self):
        reply = _llm_reply('{"skills":[{"n":"python","t":"i","d":"c"},{"n":"Supply Chain Planning","t":"e","d":"b"},'
                           '{"n":"Excel","t":"e","d":"b"},{"n":"","t":"e"},{"n":"x","t":"e"}]}')
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply) as create:
            found = {e.skill.name: e.source for e in extract_skills("Automated reports for the finance team.")}
        self.assertEqual(found, {"Python": "implicit", "Supply Chain Planning": "explicit", "Excel": "explicit"})
        self.assertEqual(Skill.objects.get(name="Supply Chain Planning").discipline, "business")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini")
        self.assertEqual(kwargs["max_tokens"], 300)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertIn("ESCO / O*NET", kwargs["messages"][0]["content"])

    def test_api_result_is_authoritative_no_local_merge(self):
        reply = _llm_reply('{"skills":[{"n":"Data Analysis","t":"e","d":"c"}]}')
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply):
            found = {e.skill.name for e in extract_skills("Skills: SQL and pivot tables")}
        self.assertEqual(found, {"Data Analysis"})

    def test_cv_upload_attaches_api_skills_to_student(self):
        reply = _llm_reply('{"skills":[{"n":"Supply Chain Management","t":"e","d":"b"},{"n":"SQL","t":"i","d":"c"}]}')
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply):
            self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload("Led procurement and logistics at a retail firm.")})
        self.assertEqual(set(self.student.skills.values_list("skill__name", flat=True)), {"Supply Chain Management", "SQL"})

    def test_falls_back_to_regex_on_error_or_bad_json(self):
        for effect in (TimeoutError("slow"), None):
            kw = {"side_effect": effect} if effect else {"return_value": _llm_reply("not json")}
            with mock.patch("openai.resources.chat.completions.Completions.create", **kw):
                found = {e.skill.name for e in extract_skills("Skills: SQL and pivot tables")}
            self.assertEqual(found, {"SQL", "Excel"})


class GateAndNavTests(Base):
    def test_incomplete_profile_redirects_to_profile(self):
        StudentProfile.objects.filter(pk=self.student.pk).update(major="")
        self.client.force_login(self.student_user)
        self.assertRedirects(self.client.get(reverse("skills_hub")), reverse("profile"), fetch_redirect_response=False)
        ss = self._claim()
        resp = self.client.get(reverse("skills_api_recommendations", args=[ss.pk]))
        self.assertEqual(resp.json()["error"], "profile_incomplete")

    def test_partners_and_universities_blocked_and_no_nav_button(self):
        for user in (self.pro_user, self.uni_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("skills_hub")).status_code, 404)
            self.assertNotContains(self.client.get(reverse("partner_dashboard")), reverse("skills_hub"))

    def test_student_sees_nav_button(self):
        self.client.force_login(self.student_user)
        self.assertContains(self.client.get(reverse("list")), "Analyze & Verify Skills")


class NewUserRobustnessTests(Base):
    def test_new_user_without_profile_gets_redirected_not_500(self):
        fresh = User.objects.create_user("fresh", password="pw")
        self.client.force_login(fresh)
        resp = self.client.get(reverse("skills_hub"))
        self.assertRedirects(resp, reverse("profile"), fetch_redirect_response=False)
        self.assertTrue(StudentProfile.objects.filter(user=fresh).exists())
        profile = self.client.get(reverse("profile"))
        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, "complete your basic profile")
        self.assertContains(profile, "Analyze & Verify Skills")

    def test_existing_user_with_null_fields_loads_both_pages(self):
        StudentProfile.objects.filter(pk=self.student.pk).update(university=None, major=None, study_level=None)
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        self.assertEqual(self.client.get(reverse("skills_hub")).status_code, 302)

    def test_complete_profile_loads_skills_hub(self):
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        self.assertEqual(self.client.get(reverse("skills_hub")).status_code, 200)

    def test_profile_renders_when_skills_tables_unavailable(self):
        self.client.force_login(self.student_user)
        with mock.patch.object(StudentSkill._meta, "db_table", "missing_skills_table"):
            self.assertEqual(self.client.get(reverse("profile")).status_code, 200)

    def test_admin_does_not_see_nav_button(self):
        admin = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(admin)
        self.assertNotContains(self.client.get(reverse("list")), "Analyze & Verify Skills")


@override_settings(AGENTROUTER_API_KEY="k", AGENTROUTER_BASE_URL="https://llm.test/v1")
class QuizGenerationTests(Base):
    # Difficulties deliberately out of order ("hhhmmmmeee"): the engine must serve easy → hard.

    QUIZ = json.dumps({"q": [
        {"s": "Forecasting" if i % 2 else "Inventory", "d": "hhhmmmmeee"[i], "t": f"Topic {i}",
         "p": f"Question number {i} about demand planning?", "k": "print(sum([1, 2]))" if i == 0 else "",
         "c": ["A", "B", "C", "D"], "a": i % 4}
        for i in range(10)
    ] + [{"s": "Bad", "p": "short", "c": ["A"], "a": 5}]})

    def setUp(self):
        self.new_skill = Skill.objects.create(name="Supply Chain Planning", slug="supply-chain-planning", discipline="business")

    def _start(self, ss):
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_llm_reply(self.QUIZ)) as create:
            self.assertContains(self.client.get(reverse("skills_hub")), "Start challenge")
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        return resp, create

    def test_generates_ten_progressive_questions_with_full_timer(self):
        ss = self._claim(skill=self.new_skill)
        resp, create = self._start(ss)
        attempt = ss.attempts.get()
        self.assertRedirects(resp, reverse("skills_challenge", args=[attempt.token]), fetch_redirect_response=False)
        self.assertEqual(self.new_skill.items.count(), 10)
        self.assertIn("workplace scenarios", create.call_args.kwargs["messages"][0]["content"])
        self.assertContains(self.client.get(resp.url), 'data-role="begin" disabled')

        served = []
        for _ in range(10):
            item = self.client.post(reverse("skills_api_next", args=[attempt.token])).json()["item"]
            self.assertEqual(item["remaining_seconds"], item["time_limit"])
            self.assertNotIn("correct_index", item)
            served.append(item["difficulty"])
            self.client.post(reverse("skills_api_answer", args=[attempt.token]),
                             json.dumps({"item_id": item["item_id"], "choice_index": 0}), content_type="application/json")
        order = [ChallengeItem.DIFFICULTY_ORDER[d] for d in served]
        self.assertEqual(order, sorted(order))
        hard_code = ChallengeItem.objects.get(skill=self.new_skill, topic="Topic 0")
        self.assertEqual((hard_code.difficulty, hard_code.code_snippet, hard_code.time_limit_seconds), ("hard", "print(sum([1, 2]))", 60))

    def test_technical_skill_prompt_requests_code(self):
        from .quizgen import build_system_prompt
        self.assertIn("snippet", build_system_prompt(Skill.objects.get(slug="python")))
        self.assertIn("snippet", build_system_prompt(Skill(name="Cybersecurity", discipline="general")))
        self.assertIn("workplace scenarios", build_system_prompt(Skill.objects.get(slug="project-management")))

    def test_generation_failure_redirects_with_message(self):
        ss = self._claim(skill=self.new_skill)
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create", side_effect=TimeoutError()):
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]), follow=True)
        self.assertContains(resp, "prepare a challenge")
        self.assertFalse(ss.attempts.exists())

    def test_live_quiz_is_primary_even_when_saved_questions_exist(self):
        seeded_ids = set(self.skill.items.values_list("id", flat=True))
        ss = self._claim()
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_llm_reply(self.QUIZ)) as create:
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(create.call_count, 1)
        attempt = ss.attempts.get()
        self.assertEqual(len(attempt.item_ids), 10)
        self.assertFalse(seeded_ids & set(attempt.item_ids))

    def test_resuming_an_active_attempt_does_not_regenerate(self):
        ss = self._claim()
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_llm_reply(self.QUIZ)) as create:
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(create.call_count, 1)
        self.assertEqual(ss.attempts.count(), 1)

    def test_api_failure_falls_back_to_saved_questions(self):
        seeded_ids = set(self.skill.items.values_list("id", flat=True))
        ss = self._claim()
        self.client.force_login(self.student_user)
        for failure in (TimeoutError("down"), _llm_reply("not json"), _llm_reply('{"q": []}')):
            kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            ss.attempts.all().delete()
            with mock.patch("openai.resources.chat.completions.Completions.create", **kw) as create:
                resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
            self.assertTrue(create.called)
            attempt = ss.attempts.get()
            self.assertRedirects(resp, reverse("skills_challenge", args=[attempt.token]), fetch_redirect_response=False)
            self.assertTrue(set(attempt.item_ids) <= seeded_ids)

    @override_settings(AGENTROUTER_API_KEY="")
    def test_no_api_key_uses_saved_questions_without_calling_api(self):
        ss = self._claim()
        self.client.force_login(self.student_user)
        with mock.patch("openai.resources.chat.completions.Completions.create") as create:
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        create.assert_not_called()
        self.assertTrue(ss.attempts.exists())


class FeedbackAndLeaderboardTests(ChallengeFlowTests):
    def test_missed_answers_become_actionable_topics_and_rank_is_shown(self):
        peer = self._claim(student=self.other_student)
        peer.score, peer.percentile = 90, 50
        peer.save()
        ss = self._claim()
        attempt = self._answer_all(engine.start_attempt(ss), correct=False)
        self.assertTrue(attempt.improvement_topics)
        first = attempt.improvement_topics[0]
        self.assertTrue({"topic", "sub_skill", "difficulty", "action", "reason"} <= first.keys())
        self.assertTrue(first["action"].startswith("Review "))
        ss.refresh_from_db()
        board = engine.leaderboard_position(ss)
        self.assertEqual((board["rank"], board["peers"]), (2, 2))
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("skills_result", args=[attempt.token]))
        self.assertContains(page, "What to improve")
        self.assertContains(page, "#2")
        data = self.client.get(reverse("skills_api_recommendations", args=[ss.pk])).json()
        self.assertEqual(data["improvement_topics"], attempt.improvement_topics)

    def test_perfect_run_has_no_topics(self):
        attempt = self._answer_all(engine.start_attempt(self._claim()))
        self.assertEqual(attempt.improvement_topics, [])


class CVCompactionTests(TestCase):
    def test_drops_contacts_urls_and_duplicates_but_keeps_sections(self):
        from .extraction import compact_cv_text
        cv = chr(10).join([
            "Jane Doe", "jane@x.com", "+20 100 123 4567", "https://github.com/jane", "EXPERIENCE",
            "• Built ETL in Python", "• Built ETL in Python", "Page 1 of 2", "PROJECTS", "Chatbot",
        ])
        self.assertEqual(compact_cv_text(cv).splitlines(),
                         ["Jane Doe", "EXPERIENCE", "Built ETL in Python", "PROJECTS", "Chatbot"])
