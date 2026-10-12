"""LLM replies: fenced, chatty or truncated JSON is still parsed; extraction uses a longer timeout and compact output."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from . import llm
from .extraction import LLM_TIMEOUT, analyze_cv
from .llm import parse_json_object
from .tests import Base

CREATE = "openai.resources.chat.completions.Completions.create"
AI = dict(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", SKILLS_LLM_MODEL="", LLM_BACKUP_API_KEY="")


def _reply(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)])


class ParseJsonTests(SimpleTestCase):
    def test_fences_and_surrounding_text(self):
        for raw in ('```json\n{"a": 1}\n```', '```\n{"a": 1}```', 'Sure! Here it is: {"a": 1} Hope this helps.', '{"a": 1}}\n'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_json_object(raw), {"a": 1})

    def test_truncated_replies_are_repaired(self):
        cases = {
            '{"extracted_skills":["Python","SQL","Exc': {"extracted_skills": ["Python", "SQL"]},
            '{"extracted_skills":["Python"],"summary":"Finance student who bui': {"extracted_skills": ["Python"]},
            '{"extracted_skills":["Py': {"extracted_skills": []},
            '{"a":{"b":["x","y\\"z': {"a": {"b": ["x"]}},
            '{"extracted_skills":["Python", "SQL"],': {"extracted_skills": ["Python", "SQL"]},
            '```json\n{"skills": ["C#", "a, b", "c': {"skills": ["C#", "a, b"]},
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_json_object(raw), expected)

    def test_unusable_replies_raise(self):
        for raw in ("", "no json here", "[1, 2]", "```json```"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_json_object(raw)


@override_settings(**AI)
class ExtractionCallTests(Base):
    def test_truncated_extraction_still_returns_skills(self):
        cut = '{"extracted_skills":["Python","SQL","Excel"],"implied_skills":["Excel"],"candidate_name":"Mona","summary":"Fin'
        with mock.patch(CREATE, return_value=_reply(cut, "length")), self.assertLogs("internest_skills.llm", "WARNING") as logs:
            analysis = analyze_cv("Finance intern. Skills: SQL and pivot tables.")
        self.assertTrue(analysis.ai_used and not analysis.ai_failed)
        self.assertEqual([(e.skill.name, e.source) for e in analysis.skills],
                         [("Python", "explicit"), ("SQL", "explicit"), ("Excel", "implicit")])
        self.assertIn("hit max_tokens", logs.output[0])

    def test_extraction_uses_long_timeout_compact_output_and_json_mode(self):
        with mock.patch.object(llm, "_client", wraps=llm._client) as client, \
                mock.patch(CREATE, return_value=_reply('{"extracted_skills":["SQL"]}')) as create:
            analyze_cv("Skills: SQL " + "x" * 50_000)
        self.assertEqual((LLM_TIMEOUT, client.call_args.args[0]), (45.0, 45.0))
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["max_tokens"], 700)
        self.assertIn("no markdown", kwargs["messages"][0]["content"])
        self.assertTrue(kwargs["messages"][0]["content"].index('"extracted_skills"') < kwargs["messages"][0]["content"].index('"summary"'))
        self.assertLess(len(kwargs["messages"][1]["content"]), 12_000)  # long CVs are capped
        self.assertNotIn("reasoning_effort", kwargs)

    @override_settings(AGENTROUTER_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/")
    def test_gemini_flash_thinking_is_turned_off(self):
        with mock.patch(CREATE, return_value=_reply('{"ok": true}')) as create:
            llm.chat_json("s", "u")
        self.assertEqual(create.call_args.kwargs["reasoning_effort"], "none")

    @override_settings(SKILLS_LLM_TIMEOUT=30.0)
    def test_default_timeout(self):
        with mock.patch.object(llm, "_client", wraps=llm._client) as client, mock.patch(CREATE, return_value=_reply("{}")):
            llm.chat_json("s", "u")
        self.assertEqual(client.call_args.args[0], 30.0)
