"""AI provider diagnostics: exact HTTP errors in the logs, the direct Gemini key, and `test_ai_connection`."""
import io
import json
from unittest import mock

import httpx
import openai
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from .llm import describe_error, parse_json_object, providers

CHAT = "openai.resources.chat.completions.Completions.create"
REQ = httpx.Request("POST", "https://agentrouter.org/v1/chat/completions")


def _status_error(code, body):
    resp = httpx.Response(code, request=REQ, json=body)
    return openai.APIStatusError("boom", response=resp, body=body)


def _reply(content):
    from types import SimpleNamespace
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")])


QUIZ = {"q": [{"s": "Network security", "d": "emh"[i % 3], "t": f"topic {i}", "p": f"A real incident scenario number {i}?",
               "k": "", "c": ["Isolate host", "Reboot", "Ignore", "Email users"], "a": 0} for i in range(10)]}


class DescribeErrorTests(SimpleTestCase):
    def test_status_code_and_body_are_reported_with_secrets_masked(self):
        exc = _status_error(401, {"error": {"message": "unauthorized client detected", "key": "sk-abcdef1234567890"}})
        text = describe_error(exc)
        self.assertIn("HTTP 401", text)
        self.assertIn("unauthorized client detected", text)
        self.assertNotIn("abcdef1234567890", text)

    def test_connection_and_timeout(self):
        self.assertIn("timeout", describe_error(openai.APITimeoutError(request=REQ)))
        self.assertIn("connection failed", describe_error(openai.APIConnectionError(request=REQ)))

    def test_fenced_json_is_parsed(self):
        self.assertEqual(parse_json_object('```json\n{"q": []}\n```'), {"q": []})

    @override_settings(AGENTROUTER_API_KEY="k1", AGENTROUTER_BASE_URL="https://agentrouter.org/v1", LLM_BACKUP_API_KEY="")
    def test_failures_are_logged_with_status_and_body(self):
        from .llm import LLMUnavailable, chat_json
        with mock.patch(CHAT, side_effect=_status_error(404, {"error": {"message": "models/gemini-1.5-flash is not found"}})), \
                self.assertLogs("internest_skills.llm", "WARNING") as logs, self.assertRaises(LLMUnavailable):
            chat_json("sys", "user")
        self.assertIn("HTTP 404", logs.output[0])
        self.assertIn("is not found", logs.output[0])
        self.assertIn("agentrouter.org", logs.output[0])


class GeminiKeyTests(SimpleTestCase):
    @override_settings(AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="", GEMINI_API_KEY="g-key", GEMINI_MODEL="",
                       GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/")
    def test_direct_gemini_key_uses_a_current_model(self):
        [p] = providers()
        self.assertEqual((p.label, p.model), ("GEMINI_API_KEY", "gemini-2.5-flash"))
        self.assertIn("generativelanguage.googleapis.com", p.base_url)

    @override_settings(AGENTROUTER_API_KEY="k1", AGENTROUTER_BASE_URL="https://agentrouter.org/v1", LLM_BACKUP_API_KEY="",
                       GEMINI_API_KEY="g-key", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/")
    def test_gemini_comes_first_then_agentrouter(self):
        self.assertEqual([p.label for p in providers()], ["GEMINI_API_KEY", "API_KEY_1"])
        calls = []

        def fake(**kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise _status_error(401, {"error": {"message": "unauthorized client detected"}})
            return _reply(json.dumps({"ok": True}))

        from .llm import chat_json
        with mock.patch(CHAT, side_effect=fake), self.assertLogs("internest_skills.llm", "WARNING"):
            self.assertEqual(chat_json("sys", "user"), {"ok": True})
        self.assertEqual(calls, ["gemini-2.5-flash", "gpt-4o-mini"])


@override_settings(AGENTROUTER_API_KEY="k1", AGENTROUTER_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
                   SKILLS_LLM_MODEL="", LLM_BACKUP_API_KEY="", GEMINI_API_KEY="")
class TestAIConnectionCommandTests(TestCase):
    def test_prints_ten_questions(self):
        replies = [_reply('{"ok": true}'), _reply("```json\n" + json.dumps(QUIZ) + "\n```")]
        out = io.StringIO()
        with mock.patch(CHAT, side_effect=replies):
            call_command("test_ai_connection", "--skill", "Cybersecurity", stdout=out)
        text = out.getvalue()
        self.assertIn("200 OK", text)
        self.assertIn("model=gemini-2.5-flash", text)
        self.assertIn("10/10 valid questions", text)
        self.assertIn("A real incident scenario number 9?", text)
        self.assertIn("technical (code)", text)  # "security" → technical style for Cybersecurity

    def test_reports_the_exact_provider_error(self):
        out = io.StringIO()
        with mock.patch(CHAT, side_effect=_status_error(400, {"error": {"message": "Invalid JSON payload received"}})), \
                self.assertRaisesMessage(CommandError, "Every AI key failed"):
            call_command("test_ai_connection", stdout=out)
        self.assertIn("HTTP 400", out.getvalue())
        self.assertIn("Invalid JSON payload received", out.getvalue())

    @override_settings(AGENTROUTER_API_KEY="", GEMINI_API_KEY="")
    def test_no_key_configured(self):
        with self.assertRaisesMessage(CommandError, "No AI key configured"):
            call_command("test_ai_connection", stdout=io.StringIO())
