"""Freelance importer: form validation, AI parsing with taxonomy matching, review gate and the unified badge."""
import json
from decimal import Decimal
from unittest import mock

import httpx
import openai
from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from django.urls import reverse

from . import freelance_service
from .models import Skill, StudentFreelanceGig, StudentSkill
from .tests import Base, _llm_reply

CHAT = "openai.resources.chat.completions.Completions.create"
AI = dict(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", LLM_BACKUP_API_KEY="")
REVIEW = ("تصميم لوحة تحكم مبيعات بالـ Excel وتحليل بيانات العملاء. Delivered a Power Query dashboard and SQL reports. "
          "Client review: Excellent work, delivered early! 5/5")
PARSED = {"project_title": "Sales dashboard for an e-commerce store", "client_feedback": "Excellent work, delivered early!",
          "rating": 5, "deliverables": ["Excel dashboard", "SQL reports"], "extracted_skills": ["MS Excel", "sql", "Basket Weaving"]}


def _post(client, **overrides):
    data = {"platform": "mostaql", "link": "https://mostaql.com/portfolio/123", "raw_text": REVIEW, **overrides}
    return client.post(reverse("freelance_importer"), data, follow=True)


class FormTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_page_renders_platforms_and_fields(self):
        page = self.client.get(reverse("freelance_importer"))
        for marker in ('value="mostaql"', 'value="khamsat"', 'value="upwork"', 'value="fiverr"', 'value="portfolio"',
                       'name="link"', 'name="raw_text"', 'name="proof"'):
            self.assertContains(page, marker)
        self.assertContains(self.client.get(reverse("profile")), reverse("freelance_importer"))  # entry point lives in the Student Hub

    def test_link_must_match_platform_and_text_is_required(self):
        self.assertContains(_post(self.client, link="https://evil.example/x"), "mostaql.com. Choose the right platform")
        self.assertContains(_post(self.client, platform="portfolio", raw_text="too short"), "at least 40 characters")
        self.assertFalse(StudentFreelanceGig.objects.exists())

    def test_duplicate_link_and_daily_cap(self):
        _post(self.client)
        self.assertContains(_post(self.client), "You already imported this link.")
        for i in range(4):
            _post(self.client, platform="portfolio", link=f"https://me.example/p{i}")
        self.assertContains(_post(self.client, platform="portfolio", link="https://me.example/p9"), "up to 5 gigs a day")
        self.assertEqual(StudentFreelanceGig.objects.count(), 5)

    def test_non_students_are_blocked(self):
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(reverse("freelance_importer")).status_code, 404)


@override_settings(**AI)
class ParsingTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_ai_parse_matches_taxonomy_and_waits_for_review(self):
        proof = SimpleUploadedFile("proof.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png")
        with mock.patch(CHAT, return_value=_llm_reply(json.dumps(PARSED))) as create:
            resp = _post(self.client, proof=proof)
        gig = StudentFreelanceGig.objects.get()
        self.assertEqual((gig.project_title, gig.rating, gig.status, gig.ai_parsed), (PARSED["project_title"], Decimal("5.0"), "pending", True))
        self.assertEqual(sorted(gig.skills.values_list("name", flat=True)), ["Excel", "SQL"])  # unknown skill dropped
        self.assertTrue(gig.proof.name.startswith("freelance_proofs/"))
        self.assertFalse(StudentSkill.objects.filter(student=self.student, status="verified").exists())
        self.assertContains(resp, "Our team reviews the link and proof")
        self.assertIn("https://mostaql.com/portfolio/123", create.call_args.kwargs["messages"][1]["content"])
        gig.proof.delete(save=False)

    def test_ai_down_falls_back_to_local_matcher(self):
        request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
        with mock.patch(CHAT, side_effect=openai.RateLimitError("q", response=httpx.Response(429, request=request), body=None)), \
                self.assertLogs("internest_skills", "WARNING"):
            data = freelance_service.parse_gig("mostaql", "https://mostaql.com/p/1", REVIEW)
        self.assertFalse(data["ai_parsed"])
        self.assertEqual(data["rating"], Decimal("5.0"))
        self.assertTrue({"Excel", "SQL"} <= {s.name for s in data["skills"]})

    @override_settings(FREELANCE_AUTO_VERIFY=True)
    def test_auto_verify_setting_verifies_immediately(self):
        with mock.patch(CHAT, return_value=_llm_reply(json.dumps(PARSED))):
            resp = _post(self.client, link="https://mostaql.com/u/stud")  # handle matches the username
        self.assertEqual(StudentFreelanceGig.objects.get().status, "verified")
        self.assertContains(resp, "Verified skill ✅")

    @override_settings(FREELANCE_AUTO_VERIFY=True)
    def test_auto_verify_never_skips_review_without_owner_match(self):
        with mock.patch(CHAT, return_value=_llm_reply(json.dumps(PARSED))):
            _post(self.client)  # portfolio link: no username to match
        self.assertEqual(StudentFreelanceGig.objects.get().status, "pending")


class VerificationTests(Base):
    def setUp(self):
        super().setUp()
        self.gig = StudentFreelanceGig.objects.create(student=self.student, platform="khamsat", link="https://khamsat.com/x/1",
                                                      raw_text=REVIEW, project_title="Dashboard")
        self.excel, self.sql = Skill.objects.get(name="Excel"), Skill.objects.get(name="SQL")
        self.gig.skills.set([self.excel, self.sql])

    def test_verify_upgrades_skills_and_keeps_challenge_scores(self):
        StudentSkill.objects.create(student=self.student, skill=self.sql, source="explicit", status="verified", score=88,
                                    percentile=70, verified_via="challenge")
        self.assertEqual(freelance_service.verify_gig(self.gig), 1)
        excel = StudentSkill.objects.get(student=self.student, skill=self.excel)
        self.assertTrue(excel.is_verified)
        self.assertEqual((excel.verified_via, excel.score), ("freelance", None))
        sql = StudentSkill.objects.get(student=self.student, skill=self.sql)
        self.assertEqual((sql.verified_via, sql.score), ("challenge", 88))

    def test_admin_actions(self):
        admin_user = User.objects.create_superuser("root", "r@x.com", "pw")
        request = RequestFactory().post("/")
        request.user = admin_user
        model_admin = site._registry[StudentFreelanceGig]
        with mock.patch.object(model_admin, "message_user"):
            model_admin.approve(request, StudentFreelanceGig.objects.all())
        self.assertEqual(StudentSkill.objects.filter(student=self.student, status="verified").count(), 2)
        other = StudentFreelanceGig.objects.create(student=self.student, platform="fiverr", link="https://fiverr.com/x", raw_text=REVIEW)
        with mock.patch.object(model_admin, "message_user"):
            model_admin.reject(request, StudentFreelanceGig.objects.filter(pk=other.pk))
        other.refresh_from_db()
        self.assertEqual(other.status, "rejected")

    def test_unified_badge_on_hub_profile_and_talent_pool(self):
        freelance_service.verify_gig(self.gig)
        self.client.force_login(self.student_user)
        hub = self.client.get(reverse("skills_hub"))
        self.assertContains(hub, 'class="vs-badge"', count=2)
        self.assertContains(hub, "Verified through reviewed freelance work")
        self.assertNotContains(hub, "None%")
        profile = self.client.get(reverse("profile"))
        self.assertContains(profile, "Verified skill ✅")
        self.assertNotContains(profile, "PNone")

        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("skills_hub")), "مهارة موثقة ✅")

        type(self.student).objects.filter(pk=self.student.pk).update(talent_pool_visible=True)
        self.client.force_login(self.pro_user)
        self.client.cookies["internest_lang"] = "en"
        pool = self.client.get(reverse("talent_pool"))
        self.assertContains(pool, "Verified skill ✅")
        self.assertContains(pool, "Excel")
