"""CV parser: API key failover, taxonomy normalization, structured JSON, text cleanup and the inline outage notice."""
import json
from decimal import Decimal
from unittest import mock

import httpx
import openai
from django.test import TestCase, override_settings
from django.urls import reverse

from . import llm
from .extraction import analyze_cv, sanitize_text
from .llm import GROQ_DEFAULT_MODEL, LLMUnavailable, chat_json, providers
from .models import Skill, SkillProfile, StudentSkill
from .taxonomy import TaxonomyIndex
from .tests import Base, _docx_upload, _llm_reply

CREATE = "openai.resources.chat.completions.Completions.create"
TWO_KEYS = dict(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", SKILLS_LLM_MODEL="",
                LLM_BACKUP_API_KEY="key-2", LLM_BACKUP_BASE_URL="", LLM_BACKUP_MODEL="")
CV_TEXT = "Finance intern. Skills: SQL and pivot tables for monthly reporting."


def _rate_limited():
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    return openai.RateLimitError("quota exceeded", response=httpx.Response(429, request=request), body=None)


def _profile(**overrides):
    data = {"candidate_name": "Mona Adel", "summary": "Finance student who automates reports with Python.",
            "extracted_skills": ["Python3", "sql", "Excel"], "implied_skills": ["Excel"],
            "years_experience_by_skill": {"Python3": 2, "sql": "1.5", "Excel": "lots"},
            "top_domains": ["business", "computing", "astrology"]}
    data.update(overrides)
    return _llm_reply(json.dumps(data))


@override_settings(**TWO_KEYS)
class FailoverTests(TestCase):
    def test_rate_limited_primary_fails_over_to_second_key(self):
        with mock.patch(CREATE, side_effect=[_rate_limited(), _llm_reply('{"ok": true}')]) as create, \
                self.assertLogs("internest_skills.llm", "WARNING") as logs:
            self.assertEqual(chat_json("s", "u"), {"ok": True})
        self.assertEqual(create.call_count, 2)
        self.assertIn("LLM failover: API_KEY_1 [https://llm.test/v1", logs.output[0])
        self.assertIn("failed: HTTP 429 RateLimitError: quota exceeded; switching to API_KEY_2", logs.output[0])
        self.assertNotIn("key-1", " ".join(logs.output))  # keys are never logged

    def test_timeout_http_error_and_bad_json_all_fail_over(self):
        for failure in (httpx.ReadTimeout("slow"), openai.APIConnectionError(request=httpx.Request("POST", "https://x")),
                        _llm_reply("not json")):
            first = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
            with self.subTest(failure=type(failure).__name__), self.assertLogs("internest_skills.llm", "WARNING"):
                calls = iter([failure, _llm_reply('{"ok": 1}')])

                def fake(*args, **kwargs):
                    item = next(calls)
                    if isinstance(item, Exception):
                        raise item
                    return item
                with mock.patch(CREATE, side_effect=fake):
                    self.assertEqual(chat_json("s", "u"), {"ok": 1})

    def test_each_key_uses_its_own_client_and_no_sdk_retry_before_failover(self):
        with mock.patch.object(llm, "_client", wraps=llm._client) as client, \
                mock.patch(CREATE, side_effect=[_rate_limited(), _llm_reply("{}")]), self.assertLogs("internest_skills.llm", "WARNING"):
            chat_json("s", "u")
        (first, second) = client.call_args_list
        self.assertEqual((first.args[1].api_key, first.args[2]), ("key-1", 0))
        self.assertEqual((second.args[1].api_key, second.args[2]), ("key-2", 1))

    @override_settings(LLM_BACKUP_BASE_URL="https://api.groq.com/openai/v1")
    def test_backup_can_be_another_provider(self):
        chain = providers()
        self.assertEqual([p.label for p in chain], ["API_KEY_1", "API_KEY_2"])
        self.assertEqual((chain[1].base_url, chain[1].model), ("https://api.groq.com/openai/v1", GROQ_DEFAULT_MODEL))
        with mock.patch(CREATE, side_effect=[_rate_limited(), _llm_reply("{}")]) as create, self.assertLogs("internest_skills.llm"):
            chat_json("s", "u")
        self.assertEqual([c.kwargs["model"] for c in create.call_args_list], ["gpt-4o-mini", GROQ_DEFAULT_MODEL])

    def test_all_keys_failing_raises_unavailable(self):
        with mock.patch(CREATE, side_effect=_rate_limited()) as create, self.assertLogs("internest_skills.llm", "WARNING"), \
                mock.patch("openai._base_client.SyncAPIClient._calculate_retry_timeout", return_value=0):
            with self.assertRaises(LLMUnavailable) as ctx:
                chat_json("s", "u")
        self.assertEqual(str(ctx.exception), "API_KEY_1: HTTP 429 RateLimitError: quota exceeded; API_KEY_2: HTTP 429 RateLimitError: quota exceeded")
        self.assertEqual(create.call_count, 2)

    @override_settings(AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="")
    def test_no_keys_means_disabled(self):
        self.assertEqual(providers(), [])
        with self.assertRaises(LLMUnavailable):
            chat_json("s", "u")

    @override_settings(AGENTROUTER_API_KEY="")
    def test_backup_alone_still_works(self):
        self.assertEqual([p.label for p in providers()], ["API_KEY_2"])


class TaxonomyTests(Base):
    def setUp(self):
        super().setUp()
        self.index = TaxonomyIndex(list(Skill.objects.all()))

    def test_variants_map_to_the_standard_skill(self):
        python = Skill.objects.get(name="Python")
        for name in ("Python", "python3", "Python 3.11", "py", "python developer", "Python programming", "Pythn", "PYTHON  "):
            with self.subTest(name=name):
                self.assertEqual(self.index.match(name), python)
        self.assertEqual(self.index.match("MS Excel"), Skill.objects.get(name="Excel"))
        self.assertEqual(self.index.match("ui/ux"), Skill.objects.get(name="UI/UX Design"))

    def test_unrelated_or_short_names_do_not_match(self):
        for name in ("Java", "R", "Pottery", "Rust developer", ""):
            with self.subTest(name=name):
                self.assertIsNone(self.index.match(name))

    def test_canonical_name_for_new_skills(self):
        self.assertEqual(self.index.canonical_name("Rust developer"), "Rust")
        self.assertEqual(self.index.canonical_name("Angular v17 "), "Angular")
        self.assertEqual(self.index.canonical_name("Game Development"), "Game Development")


@override_settings(**TWO_KEYS)
class StructuredAnalysisTests(Base):
    def test_structured_profile_links_to_existing_skills(self):
        before = Skill.objects.count()
        with mock.patch(CREATE, return_value=_profile()) as create:
            analysis = analyze_cv(CV_TEXT)
        self.assertEqual(Skill.objects.count(), before)  # every name matched the taxonomy
        self.assertEqual([(e.skill.name, e.source) for e in analysis.skills],
                         [("Python", "explicit"), ("SQL", "explicit"), ("Excel", "implicit")])
        self.assertEqual((analysis.candidate_name, analysis.top_domains), ("Mona Adel", ["business", "computing"]))
        python, sql = Skill.objects.get(name="Python"), Skill.objects.get(name="SQL")
        self.assertEqual(analysis.years_by_skill, {python.pk: Decimal("2.0"), sql.pk: Decimal("1.5")})
        self.assertTrue(analysis.ai_used and not analysis.ai_failed)
        prompt = create.call_args.kwargs["messages"]
        self.assertIn('"years_experience_by_skill"', prompt[0]["content"])
        self.assertIn("Known: ", prompt[1]["content"])

    def test_unknown_skill_is_created_once_with_domain_discipline(self):
        reply = _profile(extracted_skills=["Supply Chain Planning", "supply chain planning skills"], top_domains=["business"])
        with mock.patch(CREATE, return_value=reply):
            analysis = analyze_cv(CV_TEXT)
        self.assertEqual([e.skill.name for e in analysis.skills], ["Supply Chain Planning"])
        self.assertEqual(Skill.objects.get(name="Supply Chain Planning").discipline, "business")

    def test_missing_skill_list_falls_back_and_is_flagged(self):
        with mock.patch(CREATE, return_value=_llm_reply('{"skills": []}')), self.assertLogs("internest_skills", "WARNING"):
            analysis = analyze_cv(CV_TEXT)
        self.assertTrue(analysis.ai_failed)
        self.assertEqual({e.skill.name for e in analysis.skills}, {"SQL", "Excel"})


class SanitizeTests(TestCase):
    def test_cleans_noisy_pdf_text(self):
        raw = ("Data ﬁltering (cid:12) and manage-\nment\r\n• Python​ scripts\n▪▪▪ ─── ═══\n"
               "ﻳﺎﺳﺭ skills\x00\n\n\t  Excel   reports  ")
        self.assertEqual(sanitize_text(raw).splitlines(),
                         ["Data filtering and management", "Python scripts", "ياسر skills", "Excel reports"])


@override_settings(**TWO_KEYS)
class HubFeedbackTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)
        patcher = mock.patch("openai._base_client.SyncAPIClient._calculate_retry_timeout", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upload(self, text=CV_TEXT, **create):
        with mock.patch(CREATE, **create), self.assertNoLogs("django.request", "ERROR"):
            return self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(text)}, follow=True)

    def test_success_saves_summary_domains_and_years(self):
        resp = self._upload(return_value=_profile())
        profile = SkillProfile.objects.get(student=self.student)
        self.assertEqual(profile.top_domains, ["business", "computing"])
        self.assertEqual(profile.extraction_count_today, 1)
        self.assertEqual(StudentSkill.objects.get(student=self.student, skill__name="Python").years_experience, Decimal("2.0"))
        self.assertContains(resp, "Finance student who automates reports with Python.")
        self.assertContains(resp, "2 yrs experience")
        self.assertContains(resp, "Business &amp; Finance")
        self.assertNotContains(resp, 'role="alert"')

    def test_all_keys_down_shows_inline_notice_and_keeps_local_skills(self):
        with self.assertLogs("internest_skills", "WARNING"):
            resp = self._upload(side_effect=_rate_limited())
        self.assertContains(resp, "Our AI analysis is temporarily unavailable.")
        self.assertContains(resp, "some skills may be missing")
        self.assertEqual(set(self.student.skills.values_list("skill__name", flat=True)), {"SQL", "Excel"})
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 0)
        self.assertNotContains(self.client.get(reverse("skills_hub")), "temporarily unavailable")  # shown once

    def test_all_keys_down_and_nothing_found_explains_instead_of_dropping(self):
        with self.assertLogs("internest_skills", "WARNING"):
            resp = self._upload("Hello world, this is a plain document with nothing relevant in it at all.", side_effect=_rate_limited())
        self.assertContains(resp, "ai-notice--empty")
        self.assertContains(resp, "found no skills in it")
        self.assertNotContains(resp, "Analysis complete")

    def test_notice_is_translated(self):
        self.client.cookies["internest_lang"] = "ar"
        with self.assertLogs("internest_skills", "WARNING"):
            resp = self._upload(side_effect=_rate_limited())
        self.assertContains(resp, "التحليل بالذكاء الاصطناعي غير متاح مؤقتاً")
