"""LLM outages (PythonAnywhere proxy 403s, connection errors) must fall back locally and never cause a 500."""
from unittest import mock

import httpx
import openai
from django.test import override_settings
from django.urls import reverse

from .extraction import extract_skills
from .llm import LLMUnavailable, chat_json
from .models import Skill
from .quizgen import quiz_item_ids_for
from .semantic import relate_skill_ai
from .tests import Base, _docx_upload

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
        for exc in RAW_ERRORS:
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("openai.resources.chat.completions.Completions.create", side_effect=exc):
                self.assertIsNone(quiz_item_ids_for(self.skill))

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
            resp = self.client.post(reverse("skills_challenge_start", args=[ss.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "couldn")  # "We couldn't prepare a challenge…" (apostrophe is HTML-escaped)
        self.assertFalse(ss.attempts.exists())

    def test_ai_skill_relations_return_zero(self):
        with proxy_blocked:
            self.assertEqual(relate_skill_ai(Skill.objects.create(name="Brand Voice", slug="brand-voice")), 0)
