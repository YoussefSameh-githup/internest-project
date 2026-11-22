"""AI features 1-5: CV analysis (deterministic facts + grounded AI), skills provenance (M1), assessment quota and
answer-key verification (M2), advisory answer evaluation (L3), the /api/ai/ JSON API and the skills performance work."""
import io
import json
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from internest_ai import tasks as ai_tasks
from internest_ai.prompts import validate_shape
from internest_ai.testing import CREATE, reply
from internest_core import async_tasks
from internest_core.models import AsyncTask, Internship, PartnerProfile, StudentProfile

from . import challenge as engine
from . import cv_analysis, cv_facts, quizgen, review
from .extraction import ExtractionError, check_docx_archive, locate_evidence, read_document_text, relevant_known
from .models import (
    ChallengeAttempt, ChallengeItem, ChallengeResponse, CVAnalysis, Skill, SkillProfile, SkillSuggestion, StudentSkill,
)
from .suggestions import approve_suggestion, merge_suggestion
from .tests import Base, _docx_upload
from .tests_assessment import answer_all

AI_ON = dict(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", SKILLS_LLM_MODEL="", LLM_BACKUP_API_KEY="")
TODAY = date(2026, 11, 22)

EN_CV = """Mona Adel
mona.adel@example.com | +20 101 234 5678 | linkedin.com/in/mona-adel
Summary
Finance student who automates monthly reporting with Excel and SQL for the retail banking team at a large bank.
Education
Bachelor of Commerce, Cairo University
2021 - 2025 GPA 3.4/4.0
Experience
Finance Intern - Acme Bank | Jun 2023 - Aug 2023
Built a DCF valuation for a retail client and cut reporting time by 30%.
Prepared 2 years of SQL reporting history for the audit team and documented every query clearly.
Data Analyst Intern
Nile Retail
Jan 2024 - Present
Automated weekly dashboards for 5 store managers and trained two new interns on the reporting process.
Projects
Budget tracker web app that helps students plan monthly spending and savings goals.
Skills
Excel, SQL, Pottery Glazing
Certifications
Google Data Analytics Certificate 2023
Languages
Arabic - native, English - fluent (IELTS 7)
"""
AR_CV = """سارة محمود
sara@example.com
نبذة عني
طالبة محاسبة مهتمة بالتحليل المالي.
التعليم
بكالوريوس التجارة - جامعة عين شمس ٢٠٢٠ - ٢٠٢٤
الخبرات
محاسبة متدربة لدى شركة النيل | يناير ٢٠٢٣ - حتى الآن
إعداد التقارير الشهرية باستخدام اكسل
المهارات
اكسل، صياغة العقود
اللغات
العربية: اللغة الأم
الإنجليزية: جيد جدا
"""


def cv_reply(**overrides):
    data = {
        "skills": [
            {"name": "Excel", "evidence": "automates monthly reporting with Excel", "years": 5},
            {"name": "SQL", "evidence": "Prepared 2 years of SQL reporting history", "years": 2},
            {"name": "Python", "evidence": "Automated weekly dashboards for 5 store managers", "implied": True, "confidence": 0.9},
            {"name": "Machine Learning", "evidence": "Built neural networks at Google"},
            {"name": "Treasury Hedging", "evidence": "Built a DCF valuation for a retail client", "implied": True},
            {"name": "Blockchain", "evidence": ""},
        ],
        "summary": "Finance student who automates reporting.",
        "top_domains": ["business", "astrology"],
        "suggestions": [{"text": "Add a GitHub link with your dashboards."}, "Quantify the dashboard impact."],
        "roles": [{"title": "Finance Intern", "organization": "Acme Bank", "evidence": "Finance Intern - Acme Bank"},
                  {"title": "CFO", "organization": "Apple", "evidence": "CFO at Apple 2020"}],
        "degrees": [{"degree": "PhD in Physics", "institution": "MIT", "evidence": "PhD in Physics, MIT"}],
    }
    data.update(overrides)
    return reply(data)


def _zip_bomb(size=25 * 1024 * 1024, name="cv.docx"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", b"\0" * size)
    return SimpleUploadedFile(name, buf.getvalue())


def _blank_pdf(name="scan.pdf"):
    from pypdf import PdfWriter

    writer, buf = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=595, height=842)
    writer.write(buf)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="application/pdf")


# ---------------------------------------------------------------- feature 1: deterministic facts
class CVFactsTests(Base):
    def setUp(self):
        super().setUp()
        self.skills = list(Skill.objects.filter(is_active=True))

    def test_english_cv_sections_contacts_entries_and_score(self):
        facts = cv_facts.parse(EN_CV, self.skills, today=TODAY)
        self.assertEqual(facts["sections"], ["certifications", "education", "experience", "languages", "projects", "skills", "summary"])
        self.assertEqual(facts["contacts"]["email"], ["mona.adel@example.com"])
        self.assertEqual(facts["contacts"]["phone"], ["+20 101 234 5678"])
        self.assertIn("linkedin.com/in/mona-adel", facts["contacts"]["linkedin"])
        (edu,) = facts["education"]
        self.assertIn("Bachelor of Commerce", edu["degree"])
        self.assertEqual((edu["start"], edu["end"], edu["gpa"]), ("2021", "2025", "3.4/4.0"))
        first, second = facts["experience"]
        self.assertEqual((first["title"], first["organization"], first["start"], first["end"], first["months"], first["quantified"]),
                         ("Finance Intern", "Acme Bank", "2023-06", "2023-08", 3, True))
        self.assertEqual((second["title"], second["organization"], second["end"]), ("Data Analyst Intern", "Nile Retail", "present"))
        self.assertEqual(second["months"], 35)  # Jan 2024 → Nov 2026, both months included
        self.assertEqual(facts["languages"], [{"language": "Arabic", "level": "native"}, {"language": "English", "level": "fluent"}])
        self.assertEqual(facts["certifications"], ["Google Data Analytics Certificate 2023"])
        self.assertEqual(len(facts["projects"]), 1)
        found = {s["name"]: s["provenance"] for s in facts["skills"]}
        self.assertEqual((found["Excel"], found["SQL"]), ("declared", "declared"))
        self.assertIn("Financial Analysis", found)
        self.assertEqual(facts["unlisted_skills"], ["Pottery Glazing"])  # not in the taxonomy: kept as a fact, never a Skill
        self.assertEqual(facts["quality_score"], 100)
        self.assertEqual(facts["missing"], [])

    def test_arabic_cv_headings_digits_months_and_present(self):
        facts = cv_facts.parse(AR_CV, self.skills, today=TODAY)
        self.assertEqual(facts["sections"], ["education", "experience", "languages", "skills", "summary"])
        (edu,) = facts["education"]
        self.assertEqual((edu["start"], edu["end"]), ("2020", "2024"))
        (job,) = facts["experience"]
        self.assertEqual((job["title"], job["organization"], job["start"], job["end"]), ("محاسبة متدربة", "شركة النيل", "2023-01", "present"))
        self.assertEqual([l["language"] for l in facts["languages"]], ["Arabic", "English"])
        self.assertEqual({s["name"] for s in facts["skills"]}, {"Excel", "Contract Drafting"})
        self.assertIn("projects", facts["missing"])
        self.assertIn("phone", facts["missing"])
        self.assertLess(facts["quality_score"], 70)

    def test_thin_cv_gets_a_low_score_and_actionable_tips(self):
        facts = cv_facts.parse("Ahmed\nI like computers and want an internship.", self.skills, today=TODAY)
        self.assertLessEqual(facts["quality_score"], 10)
        codes = {row["code"] for row in cv_facts.describe(facts)}
        self.assertTrue({"email", "education", "experience", "too_short", "summary"} <= codes)
        self.assertTrue(all(row["suggestion"] for row in cv_facts.describe(facts)))

    def test_date_ranges(self):
        self.assertEqual(cv_facts.date_range("Intern | Jun 2023 – Aug 2023", TODAY)["months"], 3)
        self.assertEqual(cv_facts.date_range("2020 - 2024", TODAY), {"start": "2020", "end": "2024", "months": 60})
        self.assertEqual(cv_facts.date_range("06/2021 - 09/2021", TODAY)["months"], 4)
        self.assertEqual(cv_facts.date_range("يناير ٢٠٢٥ - حتى الآن", TODAY)["end"], "present")
        self.assertIsNone(cv_facts.date_range("Call me on 0101 234 5678", TODAY))

    def test_contacts_never_reach_the_ai_text(self):
        safe = cv_facts.ai_safe_text(EN_CV)
        for secret in ("mona.adel@example.com", "+20 101 234 5678", "linkedin.com"):
            self.assertNotIn(secret, safe)
        self.assertIn("2021 - 2025", safe)  # dates are not phone numbers
        self.assertIn("Finance Intern - Acme Bank", safe)


class UploadGuardTests(Base):
    def test_docx_zip_bomb_is_refused_before_python_docx_parses_it(self):
        with mock.patch("docx.Document") as parse:
            with self.assertRaisesMessage(ExtractionError, "unusually large once unpacked"):
                read_document_text(_zip_bomb())
        parse.assert_not_called()

    def test_absurd_compression_ratio_is_refused(self):
        with self.assertRaises(ExtractionError):
            check_docx_archive(_zip_bomb(size=3 * 1024 * 1024).read())

    def test_normal_docx_passes(self):
        upload = _docx_upload("Skills: SQL and Excel for monthly finance reporting at the bank.")
        check_docx_archive(upload.read())
        upload.seek(0)
        self.assertIn("SQL", read_document_text(upload))

    def test_scanned_pdf_says_no_text_found(self):
        with self.assertRaisesMessage(ExtractionError, "no selectable text"):
            read_document_text(_blank_pdf())

    def test_hub_shows_the_reason_and_uses_no_daily_try(self):
        self.client.force_login(self.student_user)
        resp = self.client.post(reverse("skills_hub"), {"cv_file": _zip_bomb()}, follow=True)
        self.assertContains(resp, "unusually large once unpacked")
        self.assertFalse(CVAnalysis.objects.exists())
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 0)


# ---------------------------------------------------------------- feature 1+2: AI enrichment, grounding, M1
@override_settings(**AI_ON)
class GroundingTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _upload(self, text=EN_CV, **create):
        create = create or {"return_value": cv_reply()}
        with mock.patch(CREATE, **create) as call:
            resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(text)}, follow=True)
        return resp, call

    def test_facts_and_ai_are_stored_apart_and_only_grounded_items_survive(self):
        skills_before = Skill.objects.count()
        resp, call = self._upload()
        analysis = CVAnalysis.objects.get()
        self.assertEqual((analysis.status, analysis.user, analysis.quality_score), ("done", self.student_user, 100))
        self.assertEqual(analysis.facts["contacts"]["email"], ["mona.adel@example.com"])  # facts keep contacts (owner only)
        ai = analysis.ai
        self.assertEqual(ai["status"], "ok")
        self.assertEqual(ai["top_domains"], ["business"])
        self.assertEqual(ai["suggestions"], ["Add a GitHub link with your dashboards.", "Quantify the dashboard impact."])
        self.assertEqual([r["title"] for r in ai["roles"]], ["Finance Intern"])  # "CFO at Apple" is not in the CV
        self.assertEqual(ai["degrees"], [])                                       # neither is the PhD
        self.assertEqual([s["name"] for s in ai["skills_to_confirm"]], ["Python"])
        self.assertLessEqual(ai["skills_to_confirm"][0]["confidence"], cv_analysis.INFERRED_MAX_CONFIDENCE)
        self.assertIn("Automated weekly dashboards", ai["skills_to_confirm"][0]["evidence"])
        # Saved now: declared / evidenced only. Years only when the quoted evidence states them.
        saved = {s.skill.name: s for s in StudentSkill.objects.filter(student=self.student).select_related("skill")}
        self.assertEqual(set(saved), {"Excel", "SQL", "Financial Analysis"})
        self.assertEqual(saved["SQL"].years_experience, Decimal("2.0"))
        self.assertIsNone(saved["Excel"].years_experience)
        self.assertTrue(all(s.provenance != StudentSkill.PROVENANCE_INFERRED for s in saved.values()))
        # M1: AI names never become skills; a grounded unknown one waits for staff, an invented one disappears.
        self.assertEqual(Skill.objects.count(), skills_before)
        self.assertEqual(list(SkillSuggestion.objects.values_list("name", flat=True)), ["Treasury Hedging"])
        sent = call.call_args.kwargs["messages"][1]["content"]
        for private in ("mona.adel@example.com", "101 234 5678", "linkedin.com/in/mona-adel"):
            self.assertNotIn(private, sent)
        self.assertContains(resp, "Skills to confirm")
        self.assertContains(resp, "Your CV analysis")

    def test_confirming_inferred_skills_saves_only_offered_ones_unverified(self):
        self._upload()
        analysis = CVAnalysis.objects.get()
        python, sql = Skill.objects.get(name="Python"), Skill.objects.get(name="Web Development")
        resp = self.client.post(reverse("skills_cv_confirm", args=[analysis.pk]), {"skill_ids": [python.pk, sql.pk]}, follow=True)
        ss = StudentSkill.objects.get(student=self.student, skill=python)
        self.assertEqual((ss.provenance, ss.status, ss.source), (StudentSkill.PROVENANCE_INFERRED, "claimed", "implicit"))
        self.assertFalse(StudentSkill.objects.filter(student=self.student, skill=sql).exists())  # not offered
        analysis.refresh_from_db()
        self.assertEqual(analysis.ai["skills_to_confirm"], [])
        self.assertContains(resp, "Suggested by AI")
        # never shown as verified: the badge is the provenance chip in the claimed list, not the verified section
        page = self.client.get(reverse("skills_hub")).content.decode()
        self.assertNotIn("skill-tile--verified", page)

    def test_someone_elses_analysis_cannot_be_confirmed(self):
        self._upload()
        analysis = CVAnalysis.objects.get()
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.post(reverse("skills_cv_confirm", args=[analysis.pk]), {"skill_ids": ["1"]}).status_code, 404)

    def test_known_list_is_capped_to_the_relevant_skills(self):
        Skill.objects.bulk_create([Skill(name=f"Filler Skill {i}", slug=f"filler-{i}", discipline="general") for i in range(120)])
        skills = list(Skill.objects.filter(is_active=True))
        known = relevant_known(EN_CV, skills, found_ids=[Skill.objects.get(name="SQL").pk])
        self.assertEqual(len(known), 60)
        self.assertEqual(known[0].name, "SQL")
        with mock.patch(CREATE, return_value=cv_reply()) as call:
            cv_analysis.analyze_cv(EN_CV)
        sent = call.call_args.kwargs["messages"][1]["content"]
        listed = sent.split("[known_skills]\n", 1)[1].split("\n", 1)[0].split(", ")
        self.assertEqual(len(listed), 60)
        self.assertIn("Excel", listed)

    def test_reply_that_breaks_the_schema_keeps_the_local_result(self):
        with mock.patch(CREATE, return_value=reply({"skills": "Excel, SQL"})), self.assertLogs("internest_skills", "WARNING"):
            found = cv_analysis.analyze_cv(EN_CV)
        self.assertEqual(found.ai_status, cv_analysis.AI_UNAVAILABLE)
        self.assertTrue({"Excel", "SQL"} <= {e.skill.name for e in found.skills})

    def test_locate_evidence(self):
        self.assertIn("Acme Bank", locate_evidence("finance intern  -  ACME bank", EN_CV))
        self.assertEqual(locate_evidence("Built neural networks", EN_CV), "")
        self.assertIn("DCF", locate_evidence("Built a DCF … cut reporting time", EN_CV))
        self.assertEqual(locate_evidence("ok", EN_CV), "")  # too short to prove anything

    def test_engine_quota_on_cv_analysis_is_explained_and_refunded(self):
        from internest_ai.errors import QuotaExceeded

        with mock.patch("internest_skills.cv_analysis.ai_enrich", side_effect=QuotaExceeded("user_daily", 60, 15)):
            resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)}, follow=True)
        self.assertContains(resp, "reached today&#x27;s AI limit")
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 0)


class SuggestionAdminTests(Base):
    def test_approve_and_merge(self):
        new = SkillSuggestion.objects.create(key="rust", name="Rust", discipline="computing")
        skill = approve_suggestion(new)
        self.assertTrue(skill.is_active)
        self.assertEqual((new.status, new.skill), ("approved", skill))
        alias = SkillSuggestion.objects.create(key="ms access", name="MS Access", discipline="business")
        excel = Skill.objects.get(name="Excel")
        merge_suggestion(alias, excel)
        excel.refresh_from_db()
        self.assertIn("MS Access", excel.alias_list)
        self.assertEqual(alias.status, "merged")

    def test_admin_actions(self):
        admin = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(admin)
        suggestion = SkillSuggestion.objects.create(key="figma", name="Figma", discipline="media")
        url = reverse("admin:internest_skills_skillsuggestion_changelist")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"action": "approve", "_selected_action": [suggestion.pk]})
        self.assertTrue(Skill.objects.get(name="Figma").is_active)
        self.assertEqual(self.client.get(reverse("admin:internest_skills_cvanalysis_changelist")).status_code, 200)


# ---------------------------------------------------------------- feature 1: queue, idempotency, daily cap
class AnalysisFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_upload_runs_the_job_and_the_hub_shows_results(self):
        resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)}, follow=True)
        analysis = CVAnalysis.objects.get()
        self.assertEqual(analysis.status, "done")
        self.assertEqual(len(analysis.file_sha256), 64)
        self.assertEqual(AsyncTask.objects.get(kind="cv_analysis").idempotency_key, analysis.file_sha256)
        self.assertEqual(SkillProfile.objects.get(student=self.student).last_analysis, analysis)
        self.assertContains(resp, "Analysis complete")
        self.assertContains(resp, "CV completeness")
        self.assertContains(resp, "/100")
        self.assertTrue(analysis.counted)

    def test_same_file_twice_reuses_the_result_and_the_daily_try(self):
        content = _docx_upload(EN_CV).read()  # the idempotency key is the hash of the uploaded bytes
        for _ in range(2):
            self.client.post(reverse("skills_hub"), {"cv_file": SimpleUploadedFile("cv.docx", content)})
        self.assertEqual(CVAnalysis.objects.count(), 1)
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 1)

    @override_settings(ASYNC_TASK_BACKEND="db")
    def test_queued_analysis_shows_progress_and_deduplicates(self):
        content = _docx_upload(EN_CV).read()
        resp = self.client.post(reverse("skills_hub"), {"cv_file": SimpleUploadedFile("cv.docx", content)}, follow=True)
        self.assertContains(resp, "data-cva-poll")
        self.assertContains(resp, "Analyzing")
        self.client.post(reverse("skills_hub"), {"cv_file": SimpleUploadedFile("cv.docx", content)})  # double submit
        self.assertEqual((CVAnalysis.objects.count(), AsyncTask.objects.filter(kind="cv_analysis").count()), (1, 1))
        self.assertTrue(async_tasks.run_next())
        analysis = CVAnalysis.objects.get()
        self.assertEqual(analysis.status, "done")
        self.assertEqual(self.client.get(reverse("skills_api_cv_analysis_detail", args=[analysis.pk])).json()["status"], "done")

    def test_throttled_request_does_not_use_a_daily_try(self):
        refused = async_tasks.TaskThrottled(429, "too_many_tasks", 10, "You already have tasks in progress.")
        with mock.patch("internest_core.async_tasks.enqueue", side_effect=refused):
            resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)}, follow=True)
        self.assertContains(resp, "You already have tasks in progress.")
        self.assertFalse(CVAnalysis.objects.exists())
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 0)

    def test_daily_limit(self):
        SkillProfile.objects.create(student=self.student, extraction_day=timezone.localdate(),
                                    extraction_count_today=cv_analysis.MAX_ANALYSES_PER_DAY)
        resp = self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)}, follow=True)
        self.assertContains(resp, "Daily analysis limit reached.")
        self.assertFalse(AsyncTask.objects.filter(kind="cv_analysis").exists())

    def test_a_new_day_resets_the_count(self):
        SkillProfile.objects.create(student=self.student, extraction_day=timezone.localdate() - timedelta(days=1),
                                    extraction_count_today=cv_analysis.MAX_ANALYSES_PER_DAY)
        self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)})
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 1)

    def test_job_failure_is_reported_and_refunded(self):
        with mock.patch("internest_skills.cv_facts.parse", side_effect=RuntimeError("boom")), self.assertLogs("internest_skills", "ERROR"):
            self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)})
        analysis = CVAnalysis.objects.get()
        self.assertEqual(analysis.status, "failed")
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 0)
        self.assertContains(self.client.get(reverse("skills_hub")), "The analysis failed.")

    def test_hub_query_count_does_not_grow_with_an_analysis(self):
        self._claim()
        url = reverse("skills_hub")
        self.client.get(url)
        with CaptureQueriesContext(connection) as without:
            self.client.get(url)
        self.client.post(url, {"cv_file": _docx_upload(EN_CV)})
        self.client.get(url)
        with CaptureQueriesContext(connection) as with_analysis:
            self.client.get(url)
        self.assertLessEqual(len(with_analysis), len(without))


# ---------------------------------------------------------------- feature 6: JSON API
class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_anonymous_and_non_students(self):
        anonymous = Client()
        for name in ("skills_api_cv_analysis", "skills_api_skills_extract", "skills_api_assessments"):
            resp = anonymous.post(reverse(name))
            self.assertEqual((resp.status_code, resp.json()["error"]), (401, "login_required"))
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.post(reverse("skills_api_cv_analysis")).status_code, 403)

    def test_cv_analysis_create_and_owner_only_detail(self):
        resp = self.client.post(reverse("skills_api_cv_analysis"), {"cv_file": _docx_upload(EN_CV)})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertTrue(body["task_id"])
        self.assertEqual(body["status_url"], reverse("skills_api_cv_analysis_detail", args=[body["analysis_id"]]))
        detail = self.client.get(body["status_url"]).json()
        self.assertEqual((detail["status"], detail["quality_score"]), ("done", 100))
        self.assertTrue(detail["ai"]["advisory"])
        self.assertEqual(self.client.post(reverse("skills_api_cv_analysis"), {"pasted_text": EN_CV}).status_code, 202)
        self.assertEqual(self.client.post(reverse("skills_api_cv_analysis"), {"pasted_text": EN_CV}).status_code, 200)  # reused
        self.client.force_login(self.other_user)  # IDOR: someone else's analysis id is a 404, not a 403 oracle
        self.assertEqual(self.client.get(body["status_url"]).status_code, 404)
        self.assertEqual(Client().get(body["status_url"]).status_code, 401)

    def test_cv_analysis_errors_are_json(self):
        resp = self.client.post(reverse("skills_api_cv_analysis"), {"cv_file": _zip_bomb()})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "unreadable_file"))
        resp = self.client.post(reverse("skills_api_cv_analysis"), {"pasted_text": "too short"})
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, "invalid"))
        SkillProfile.objects.update_or_create(student=self.student, defaults={"extraction_day": timezone.localdate(),
                                                                               "extraction_count_today": 5})
        resp = self.client.post(reverse("skills_api_cv_analysis"), {"pasted_text": EN_CV})
        self.assertEqual((resp.status_code, resp.json()["error"]), (429, "daily_limit"))
        self.assertTrue(int(resp["Retry-After"]) > 0)

    def test_skills_extract_returns_a_task_and_saves_nothing(self):
        resp = self.client.post(reverse("skills_api_skills_extract"), json.dumps({"text": EN_CV}), content_type="application/json")
        self.assertEqual(resp.status_code, 202)
        result = self.client.get(resp.json()["status_url"]).json()["result"]
        self.assertTrue({"Excel", "SQL"} <= {s["name"] for s in result["skills"]})
        self.assertFalse(StudentSkill.objects.filter(student=self.student).exists())
        self.assertEqual(self.client.post(reverse("skills_api_skills_extract"), {"text": "short"}).status_code, 400)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student_user)
        self.assertEqual(client.post(reverse("skills_api_skills_extract"), {"text": EN_CV}).status_code, 403)

    @override_settings(RATELIMIT_ENABLED=True)
    def test_rate_limit_answers_json(self):
        from internest_skills import api

        with mock.patch.dict(api.API_RULES, {"skills_api_skills_extract": (1, 3600)}):
            self.client.post(reverse("skills_api_skills_extract"), {"text": EN_CV})
            resp = self.client.post(reverse("skills_api_skills_extract"), {"text": EN_CV})
        self.assertEqual((resp.status_code, resp.json()["error"]), (429, "rate_limited"))
        self.assertEqual(resp["Retry-After"], "3600")

    def test_assessments_only_for_claimed_or_required_skills(self):
        python = Skill.objects.get(slug="python")
        resp = self.client.post(reverse("skills_api_assessments"), {"skill_id": python.pk})
        self.assertEqual((resp.status_code, resp.json()["error"]), (403, "not_testable"))
        self.assertFalse(StudentSkill.objects.filter(student=self.student, skill=python).exists())
        self._claim(skill=python)
        resp = self.client.post(reverse("skills_api_assessments"), {"skill_id": python.pk})
        self.assertEqual(resp.status_code, 202)
        result = self.client.get(resp.json()["status_url"]).json()
        self.assertIn("/app/skills/challenge/", result["result"]["redirect"])
        self.assertEqual(self.client.post(reverse("skills_api_assessments"), {"skill_id": 999999}).status_code, 404)

    def test_answer_evaluation_is_staff_only_and_saved(self):
        attempt = answer_all(engine.start_attempt(self._claim(skill=Skill.objects.get(slug="python"))))
        practical = attempt.responses.get(item__kind="practical")
        url = reverse("skills_api_answer_evaluate", args=[practical.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        staff = User.objects.create_user("rev", password="pw", is_staff=True)
        self.client.force_login(staff)
        rows = review.criteria(practical.item)
        grading = {"criteria": [{"criterion": r["criterion"], "met": True, "note": "ok"} for r in rows], "rationale": "fine"}
        with override_settings(**AI_ON), mock.patch(CREATE, return_value=reply(grading)) as call:
            resp = self.client.post(url)
        self.assertEqual(resp.status_code, 202)
        practical.refresh_from_db()
        self.assertEqual((practical.ai_suggestion["credit"], practical.ai_suggestion["advisory"]), (1.0, True))
        user_message = call.call_args.kwargs["messages"][1]["content"]
        prompt_part, data_part = user_message.split("BEGIN UNTRUSTED DATA", 1)
        self.assertNotIn(practical.answer_text[:40], prompt_part)  # L3: the answer never sits among the instructions
        self.assertIn(practical.answer_text[:40], data_part)
        mcq = attempt.responses.filter(item__kind="mcq").first()
        self.assertEqual(self.client.post(reverse("skills_api_answer_evaluate", args=[mcq.pk])).status_code, 400)


# ---------------------------------------------------------------- feature 3: M2 quota, restriction, key verification
def _quiz(n=10, prefix="Gen"):
    return reply({"q": [{"s": "Forecasting", "d": "emh"[i % 3], "t": f"Topic {i}", "p": f"{prefix} question number {i} on cash flow?",
                         "k": "", "c": [f"A{i}", f"B{i}", f"C{i}", f"D{i}"], "a": 1} for i in range(n)]})


@override_settings(**AI_ON)
class AssessmentGuardTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_unclaimed_unrequired_skill_cannot_be_tested(self):
        python = Skill.objects.get(slug="python")
        with mock.patch(CREATE) as call:
            resp = self.client.post(reverse("skills_challenge_start_for_skill", args=[python.pk]), follow=True)
        call.assert_not_called()
        self.assertContains(resp, "skills an open opportunity requires")
        self.assertFalse(StudentSkill.objects.filter(student=self.student, skill=python).exists())

    def test_required_by_an_open_opportunity_is_testable(self):
        python = Skill.objects.get(slug="python")
        job = Internship.objects.create(partner=self.pro, title="Dev", description="d", location="Cairo", required_majors="any",
                                        deadline=timezone.localdate() + timedelta(days=5))
        job.required_skills.add(python)
        with mock.patch(CREATE, return_value=_quiz()):
            resp = self.client.post(reverse("skills_challenge_start_for_skill", args=[python.pk]))
        attempt = ChallengeAttempt.objects.get(student_skill__skill=python)
        self.assertRedirects(resp, reverse("skills_challenge", args=[attempt.token]), fetch_redirect_response=False)

    @override_settings(AI_QUOTA_TASK_LIMITS={"question_generation": 1})
    def test_daily_generation_quota_falls_back_to_the_bank(self):
        first, second = self._claim(), self._claim(skill=Skill.objects.get(slug="python"))
        with mock.patch(CREATE, return_value=_quiz()) as call:
            self.client.post(reverse("skills_challenge_start", args=[first.pk]))
            self.client.post(reverse("skills_challenge_start", args=[second.pk]))
        self.assertEqual(call.call_count, 1)  # the second quiz did not call the AI
        bank = set(Skill.objects.get(slug="python").items.values_list("id", flat=True))
        self.assertTrue(set(second.attempts.get().item_ids) <= bank)

    def test_generated_items_are_unreviewed_and_disputed_keys_are_dropped(self):
        ss = self._claim()
        calls = []

        def provider(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _quiz()
            questions = json.loads(kwargs["messages"][1]["content"].split("[questions]\n", 1)[1].split("\n", 1)[0])
            answers = []
            for q in questions:
                key = ChallengeItem.objects.get(prompt=q["question"]).correct_index
                answers.append({"i": q["i"], "answer": key if q["i"] >= 2 else (key + 1) % 4})  # disputes the first two
            return reply({"answers": answers})

        with mock.patch(CREATE, side_effect=provider), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        self.assertEqual(len(calls), 2)
        self.assertIn("independent exam checker", calls[1]["messages"][0]["content"])
        generated = ChallengeItem.objects.filter(prompt__startswith="Gen question")
        self.assertEqual(generated.count(), 10)
        self.assertFalse(generated.filter(reviewed=True).exists())
        disputed = generated.filter(key_verified=False)
        self.assertEqual(disputed.count(), 2)
        self.assertFalse(disputed.filter(is_active=True).exists())
        self.assertEqual(generated.filter(key_verified=True).count(), 8)
        attempt = ss.attempts.get()
        self.assertFalse(set(disputed.values_list("id", flat=True)) & set(attempt.item_ids))
        self.assertTrue(AsyncTask.objects.filter(kind="quiz_verify", user=None, status="done").exists())

    def test_answered_disputed_question_does_not_count(self):
        ss = self._claim(skill=Skill.objects.get(slug="python"))
        attempt = answer_all(engine.start_attempt(ss), correct=False)
        responses = list(attempt.responses.select_related("item__sub_skill"))
        before, _ = engine.score_responses(responses)
        mcqs = [r for r in responses if r.item.kind == "mcq"]
        ChallengeItem.objects.filter(pk__in=[r.item_id for r in mcqs]).update(key_verified=False)
        for r in mcqs:
            r.item.key_verified = False
        after, breakdown = engine.score_responses(responses)
        self.assertGreaterEqual(after, before)
        self.assertTrue(all(r.item.kind != "mcq" or not engine.counts_in_score(r) for r in responses))

    def test_verifier_outage_keeps_items_unchecked(self):
        from internest_ai.errors import AIUnavailable

        item = ChallengeItem.objects.filter(skill=self.skill, kind="mcq").first()
        ChallengeItem.objects.filter(pk=item.pk).update(reviewed=False)
        with mock.patch("internest_ai.engine.run", side_effect=AIUnavailable("down")), self.assertRaises(AIUnavailable):
            quizgen.verify_items([item.pk])
        item.refresh_from_db()
        self.assertIsNone(item.key_verified)
        self.assertTrue(item.is_active)


# ---------------------------------------------------------------- mocks for load tests (AI_MOCK_PROVIDER=1)
@override_settings(AI_MOCK_PROVIDER=True, AI_MOCK_ALLOW_PRODUCTION=True, AI_MOCK_LATENCY_MS=0, AGENTROUTER_API_KEY="")
class MockProviderTests(Base):
    def test_every_task_mock_matches_its_schema(self):
        for name in ("cv_analysis", "question_generation", "quiz_verify", "answer_evaluation", "freelance_gig_analysis"):
            spec = ai_tasks.get_task(name)
            messages = [{"role": "system", "content": ""}, {"role": "user", "content": "exactly 4 NEW\n[questions]\n[]"}]
            with self.subTest(task=name):
                validate_shape(ai_tasks.mock_response(name, messages), spec.schema)

    def test_full_flow_runs_offline(self):
        self.client.force_login(self.student_user)
        self.client.post(reverse("skills_hub"), {"cv_file": _docx_upload(EN_CV)})
        self.assertEqual(CVAnalysis.objects.get().ai["status"], "ok")
        ss = self._claim(skill=Skill.objects.create(name="Mock Skill", slug="mock-skill", discipline="general"))
        with mock.patch.object(quizgen, "llm_enabled", return_value=True), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("skills_challenge_start", args=[ss.pk]))
        attempt = ss.attempts.get()
        self.assertEqual(len(attempt.item_ids), 10)
        self.assertEqual(ChallengeItem.objects.filter(skill=ss.skill, key_verified=True).count(), 10)


# ---------------------------------------------------------------- feature 5: performance
class SkillsPerformanceTests(Base):
    def test_leaderboard_rank_is_one_count_query(self):
        python = Skill.objects.get(slug="python")
        for i, score in enumerate((90, 70, 70, 50)):
            user = User.objects.create_user(f"peer{i}", password="pw")
            student = StudentProfile.objects.create(user=user, university="U", major="M", study_level="1", personal_email=f"p{i}@x.com")
            StudentSkill.objects.create(student=student, skill=python, source="explicit", score=score)
        mine = StudentSkill.objects.create(student=self.student, skill=python, source="explicit", score=70, percentile=50)
        with self.assertNumQueries(1):
            board = engine.leaderboard_position(mine)
        self.assertEqual((board["rank"], board["peers"]), (2, 5))
        with self.assertNumQueries(1):
            self.assertEqual(engine._percentile(python, 70, mine.pk), 50)

    def test_market_demand_is_cached_and_invalidated_by_opportunity_changes(self):
        from .market_fit import market_demand

        python = Skill.objects.get(slug="python")
        self.assertEqual(market_demand(), [])
        with self.assertNumQueries(0):
            market_demand()
        job = Internship.objects.create(partner=self.pro, title="Dev", description="d", location="Cairo", required_majors="any",
                                        deadline=timezone.localdate() + timedelta(days=5))
        job.required_skills.add(python)
        self.assertEqual([(s.name, n) for s, n in market_demand()], [("Python", 1)])
        Internship.objects.filter(pk=job.pk).update(is_active=False)  # no signal: still cached ...
        self.assertEqual([(s.name, n) for s, n in market_demand()], [("Python", 1)])
        job.is_active = False
        job.save()                                                   # ... until a save bumps the group
        self.assertEqual(market_demand(), [])

    def test_university_dashboard_is_paginated_with_a_flat_query_count(self):
        def students(n, offset):
            for i in range(n):
                user = User.objects.create_user(f"u{offset + i}", password="pw")
                student = StudentProfile.objects.create(user=user, university="Cairo", major="CS", study_level="2",
                                                        personal_email=f"u{offset + i}@x.com")
                SkillProfile.objects.create(student=student, partner_university=self.uni)

        self.client.force_login(self.uni_user)
        url = reverse("skills_university_dashboard")
        students(3, 0)
        self.client.get(url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(url)
        students(40, 100)
        with CaptureQueriesContext(connection) as large:
            page = self.client.get(url)
        self.assertEqual(len(large), len(small))
        self.assertEqual(len(page.context["students_page"].object_list), 25)
        self.assertContains(page, 'rel="next"')
        self.assertEqual(len(self.client.get(url + "?page=2").context["students_page"].object_list), 18)

    def test_partner_skill_relations_run_as_a_background_job(self):
        from .jobs import relate_new_skills

        skill = Skill.objects.create(name="Brand Voice", slug="brand-voice", discipline="media")
        with mock.patch("internest_skills.semantic.relate_skill_ai", return_value=2) as relate:
            relate_new_skills([skill.pk])
        relate.assert_called_once()
        self.assertEqual(AsyncTask.objects.get(kind="skills_relate").user_id, None)
        refused = async_tasks.TaskThrottled(503, "queue_full", 30, "busy")
        with mock.patch("internest_skills.jobs.enqueue", side_effect=refused), self.assertLogs("internest_skills", "WARNING"):
            relate_new_skills([skill.pk])  # a full queue never breaks the partner's submission

    def test_partner_submission_no_longer_calls_the_ai_inline(self):
        import inspect

        from internest_core import views as core_views

        source = inspect.getsource(core_views.partner_submit_internship)
        self.assertNotIn("relate_skill_ai(", source)
        self.assertIn("relate_new_skills(", source)
