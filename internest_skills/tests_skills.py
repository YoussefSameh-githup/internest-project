"""LLM outages (PythonAnywhere proxy 403s, connection errors) must fall back locally and never cause a 500."""
import json
from unittest import mock

import httpx
import openai
from django.test import override_settings
from django.urls import reverse

from .extraction import extract_skills
from .llm import LLMUnavailable, chat_json
from .models import Skill
from .quizgen import QUIZ_LENGTH, QuizGenerationError, quiz_item_ids_for
from .semantic import relate_skill_ai
from .tests import Base, _docx_upload, _llm_reply

CV_TEXT = "Finance intern. Skills: SQL and pivot tables for monthly reporting."
LOCAL_SKILLS = {"SQL", "Excel"}  # what the regex matcher finds in CV_TEXT


def _proxy_403(*args, **kwargs):
    raise httpx.ProxyError("403 Forbidden")


# Fails at the transport layer, exactly like the PythonAnywhere free-account proxy.
proxy_blocked = mock.patch("httpx.Client.send", side_effect=_proxy_403)
no_backoff = mock.patch("openai._base_client.SyncAPIClient._calculate_retry_timeout", return_value=0)

RAW_ERRORS = (
    httpx.ProxyError("403 Forbidden"),
    openai.APIConnectionError(request=httpx.Request("POST", "https://llm.test/v1/chat/completions")),
    RuntimeError("event loop is closed"),
)


@override_settings(AGENTROUTER_API_KEY="test-key", AGENTROUTER_BASE_URL="https://llm.test/v1")
class LLMOutageFallbackTests(Base):
    def setUp(self):
        super().setUp()
        no_backoff.start()
        self.addCleanup(no_backoff.stop)

    def test_chat_json_wraps_proxy_error(self):
        with proxy_blocked, self.assertRaises(LLMUnavailable):
            chat_json("sys", "user")

    def test_extraction_falls_back_to_local_matcher(self):
        with proxy_blocked:
            self.assertEqual({e.skill.name for e in extract_skills(CV_TEXT)}, LOCAL_SKILLS)
        for exc in RAW_ERRORS:
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("openai.resources.chat.completions.Completions.create", side_effect=exc):
                self.assertEqual({e.skill.name for e in extract_skills(CV_TEXT)}, LOCAL_SKILLS)

    def test_cv_upload_view_returns_200_when_proxy_blocks(self):
        self.client.force_login(self.student_user)
        with proxy_blocked:
            resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(CV_TEXT)}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Analysis complete")
        self.assertEqual(set(self.student.skills.values_list("skill__name", flat=True)), LOCAL_SKILLS)

    def test_quiz_generation_falls_back_to_saved_bank(self):
        saved = set(self.skill.items.values_list("id", flat=True))
        for exc in RAW_ERRORS:
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("openai.resources.chat.completions.Completions.create", side_effect=exc):
                ids = quiz_item_ids_for(self.skill)
                self.assertTrue(ids and set(ids) <= saved)

    def test_challenge_start_view_returns_200_when_proxy_blocks(self):
        saved = set(self.skill.items.values_list("id", flat=True))
        ss = self._claim()
        self.client.force_login(self.student_user)
        with proxy_blocked:
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        attempt = ss.attempts.get()
        self.assertEqual(resp.redirect_chain[-1][0], reverse("skills_challenge", args=[attempt.token]))
        self.assertTrue(attempt.item_ids and set(attempt.item_ids) <= saved)

    def test_skill_without_saved_questions_shows_message_not_500(self):
        ss = self._claim(skill=Skill.objects.create(name="Quantum Origami", slug="quantum-origami", discipline="general"))
        self.client.force_login(self.student_user)
        with proxy_blocked:
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Not enough questions are available for this challenge right now.")
        self.assertFalse(ss.attempts.exists())

    def test_ai_skill_relations_return_zero(self):
        with proxy_blocked:
            self.assertEqual(relate_skill_ai(Skill.objects.create(name="Brand Voice", slug="brand-voice")), 0)


def _quiz(n, prefix="Q"):
    return json.dumps({"q": [
        {"s": "Forecasting", "d": "emh"[i % 3], "t": f"Topic {i}", "p": f"{prefix} question number {i} on cash flow?",
         "k": "", "c": ["A", "B", "C", "D"], "a": i % 4}
        for i in range(n)
    ]})


@override_settings(AGENTROUTER_API_KEY="test-key", AGENTROUTER_BASE_URL="https://llm.test/v1")
class TenQuestionQuizTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_ai_quiz_has_ten_questions(self):
        self.assertEqual(QUIZ_LENGTH, 10)
        self.assertEqual(Skill.objects.create(name="Budgeting", slug="budgeting").challenge_length, 10)
        ss = self._claim()
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_llm_reply(_quiz(10))) as create:
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(ss.attempts.get().item_ids), 10)
        self.assertIn("exactly 10 NEW, distinct questions", create.call_args.kwargs["messages"][1]["content"])

    def test_short_ai_quiz_is_topped_up_from_saved_bank(self):
        saved = set(self.skill.items.values_list("id", flat=True))
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_llm_reply(_quiz(5, "Short"))):
            ids = quiz_item_ids_for(self.skill)
        self.assertEqual(len(ids), min(10, 5 + len(saved)))
        self.assertEqual(len(set(ids) & saved), len(ids) - 5)

    def test_quiz_generation_error_falls_back_to_saved_items_with_200(self):
        saved = set(self.skill.items.values_list("id", flat=True))
        ss = self._claim()
        with mock.patch("internest_skills.quizgen.live_quiz_item_ids", side_effect=QuizGenerationError("Connection error.")):
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        attempt = ss.attempts.get()
        self.assertEqual(resp.redirect_chain[-1][0], reverse("skills_challenge", args=[attempt.token]))
        self.assertTrue(attempt.item_ids and set(attempt.item_ids) <= saved)

    def test_no_questions_anywhere_renders_arabic_message_with_200(self):
        ss = self._claim(skill=Skill.objects.create(name="Kite Design", slug="kite-design"))
        self.client.cookies["internest_lang"] = "ar"
        with mock.patch("internest_skills.quizgen.live_quiz_item_ids", side_effect=QuizGenerationError("Connection error.")):
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "لا تتوفر أسئلة كافية لهذا الاختبار حالياً، يرجى المحاولة لاحقاً")

    def test_unexpected_error_while_preparing_quiz_never_500s(self):
        ss = self._claim()
        with mock.patch("internest_skills.views.quiz_item_ids_for", side_effect=KeyError("boom")):
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ss.attempts.exists())
