"""Resume builder: DOCX export, ATS score, modern PDF, engine polish without PII, fabrication guard, daily cap, API."""
import io
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from docx import Document
from pypdf import PdfReader

from internest_ai import engine
from internest_ai.errors import QuotaExceeded
from internest_ai.testing import MOCK_ONLY, NO_PROVIDERS
from internest_core.models import AsyncTask
from internest_skills.tests import Base

from . import cv_builder_service as svc
from .cv_docx import render_cv_docx
from .cv_pdf import render_cv_pdf
from .models import StudentCV
from .views import AI_USES_PER_DAY

MOCK = dict(NO_PROVIDERS, **MOCK_ONLY)
ASYNC = {"HTTP_PREFER": "respond-async"}
CV = {
    "name": "Mona Adel", "email": "mona@example.com", "phone": "+20 100 123 4567", "linkedin": "linkedin.com/in/mona-adel",
    "github": "github.com/mona-adel",
    "summary": "Finance student at Cairo University who builds data tools for student clubs and enjoys turning numbers into decisions.",
    "education": [{"university": "Cairo University", "major": "Finance", "start_date": "2023-09", "end_date": "2027"}],
    "experiences": [{"title": "Finance Intern", "organization": "Banque Misr",
                     "achievements": ["Built a Python dashboard used by 12 analysts", "Analyzed 3 years of branch data"]}],
    "projects": [{"title": "Budget Tracker", "description": "Personal finance app", "tech_stack": "Python, Excel",
                  "link": "github.com/mona-adel/budget"}],
    "skills": ["Python", "Excel", "SQL", "Financial Analysis", "Business Communication"],
}
PRIVATE = ("Mona", "Adel", "mona@example.com", "+20 100 123 4567", "linkedin.com", "github.com", "mona-adel")


class AtsScoreTests(SimpleTestCase):
    def setUp(self):
        from django.utils import translation

        translation.activate("en")  # English assertions: a request in an earlier test can leave Arabic active
        self.addCleanup(translation.deactivate)

    def test_complete_cv_scores_high(self):
        report = svc.ats_report(CV)
        self.assertGreaterEqual(report["score"], 90)
        self.assertEqual(report["grade"], "Excellent")
        self.assertEqual(sum(c["max"] for c in report["checks"]), 110)

    def test_weak_cv_gets_concrete_fixes_by_impact(self):
        report = svc.ats_report({"name": "Mona", "skills": ["Excel"],
                                 "experiences": [{"title": "Intern", "achievements": ["Responsible for reports", "[X%] growth"]}],
                                 "summary": "مرحبا"})
        self.assertLess(report["score"], 50)
        codes = {c["code"]: c for c in report["checks"]}
        self.assertFalse(codes["contact"]["ok"])
        self.assertIn("e-mail", codes["contact"]["fix"])
        self.assertEqual(codes["bullets"]["points"], 0)  # neither bullet starts with an action verb
        self.assertIn("[X%]", codes["placeholders"]["fix"])
        self.assertFalse(codes["language"]["ok"])
        failed = sorted((c for c in report["checks"] if not c["ok"]), key=lambda c: c["max"] - c["points"], reverse=True)
        self.assertEqual(report["fixes"], [c["fix"] for c in failed])  # highest impact first
        self.assertEqual(report["fixes"][0], codes["bullets"]["fix"])  # 15 points lost, the biggest gap

    def test_checks_that_do_not_apply_are_skipped(self):
        codes = {c["code"] for c in svc.ats_report({"name": "A", "projects": [{"title": "P"}]})["checks"]}
        self.assertNotIn("bullets", codes)
        self.assertIn("projects", codes)


class ExportTests(Base):
    def test_docx_is_valid_and_ats_friendly(self):
        doc = Document(io.BytesIO(render_cv_docx(svc.clean_cv(CV))))
        texts = [p.text for p in doc.paragraphs]
        self.assertEqual(texts[0], "Mona Adel")
        for heading in ("SUMMARY", "EDUCATION", "EXPERIENCE", "PROJECTS", "SKILLS"):
            self.assertIn(heading, texts)
        bullets = [p.text for p in doc.paragraphs if p.style.name == "List Bullet"]
        self.assertEqual(bullets, CV["experiences"][0]["achievements"])
        self.assertIn("linkedin.com/in/mona-adel", " ".join(texts))
        self.assertEqual((len(doc.tables), len(doc.inline_shapes)), (0, 0))  # parsers skip tables and images
        self.assertEqual(doc.core_properties.title, "Mona Adel - Resume")

    def test_arabic_docx_is_right_to_left(self):
        doc = Document(io.BytesIO(render_cv_docx(svc.clean_cv({**CV, "name": "منى عادل", "summary": "طالبة مالية"}))))
        self.assertEqual(doc.paragraphs[0].text, "منى عادل")
        self.assertIn("w:bidi", doc.paragraphs[0]._p.xml)

    def test_modern_pdf_template_keeps_text_and_arabic(self):
        cv = svc.clean_cv(CV)
        classic, modern = render_cv_pdf(cv), render_cv_pdf(cv, "modern")
        self.assertNotEqual(classic, modern)
        text = PdfReader(io.BytesIO(modern)).pages[0].extract_text()
        self.assertIn("Mona Adel", text)
        self.assertIn("EXPERIENCE", text)
        arabic = render_cv_pdf(svc.clean_cv({**CV, "name": "منى عادل"}), "modern")
        self.assertTrue(arabic.startswith(b"%PDF"))

    def test_download_endpoints(self):
        self.client.force_login(self.student_user)
        self.assertRedirects(self.client.get(reverse("cv_builder_docx")), reverse("cv_builder"))  # nothing saved yet
        self.client.post(reverse("cv_builder_save"), {"cv": CV}, content_type="application/json")
        docx = self.client.get(reverse("cv_builder_docx"))
        self.assertEqual(docx["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn('filename="mona-adel-cv.docx"', docx["Content-Disposition"])
        Document(io.BytesIO(docx.content))
        modern = self.client.get(reverse("cv_builder_pdf") + "?template=modern")
        self.assertIn("mona-adel-cv-modern.pdf", modern["Content-Disposition"])
        self.assertIn("mona-adel-cv.pdf", self.client.get(reverse("cv_builder_pdf") + "?template=evil")["Content-Disposition"])
        preview = self.client.get(reverse("cv_builder_preview"))
        self.assertContains(preview, "ATS score")
        self.assertContains(preview, reverse("cv_builder_docx"))


class PolishPrivacyTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_engine_never_receives_contacts_or_links(self):
        reply = {"summary": "Finance student who builds data tools.", "experiences": CV["experiences"],
                 "projects": [{"title": "Budget Tracker", "description": "A budgeting app", "tech_stack": "Python"}]}
        with mock.patch.object(engine, "run", return_value=reply) as run:
            polished = svc.polish_cv(CV, "My email is mona.voice@example.com", user=self.student_user.pk)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["private"])
        self.assertEqual(kwargs["known_names"], ["Mona Adel"])
        sent = json.dumps(kwargs["data"]["resume"], ensure_ascii=False) + kwargs["prompt"] + kwargs["system"]
        for value in PRIVATE:
            self.assertNotIn(value, sent)
        self.assertEqual(set(kwargs["data"]["resume"]), {"summary", "education", "experiences", "projects", "skills"})
        # merged back locally: the student's contacts and project link
        self.assertEqual((polished["name"], polished["email"]), ("Mona Adel", "mona@example.com"))
        self.assertEqual(polished["projects"][0]["link"], "https://github.com/mona-adel/budget")
        self.assertEqual(polished["summary"], "Finance student who builds data tools.")

    def test_mock_provider_path_through_the_view(self):
        with override_settings(**MOCK):
            resp = self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        cv = resp.json()["cv"]
        self.assertEqual(cv["summary"], "Motivated student who turns coursework into working projects.")
        self.assertEqual(cv["email"], "mona@example.com")

    def test_no_fabricated_entries_or_figures(self):
        reply = {"summary": "Analyst with 5 years of experience.",
                 "experiences": [{"title": "Finance Intern", "organization": "Banque Misr",
                                  "achievements": ["Built a Python dashboard used by 12 analysts, cutting time by 40%"]},
                                 {"title": "CEO", "organization": "Invented Corp", "achievements": ["Led 200 people"]}]}
        with mock.patch.object(engine, "run", return_value=reply):
            polished = svc.polish_cv(CV)
        self.assertEqual([x["organization"] for x in polished["experiences"]], ["Banque Misr"])  # no new employer
        self.assertEqual(polished["experiences"][0]["achievements"][0],
                         "Built a Python dashboard used by 12 analysts, cutting time by [X%]")  # 12 was given, 40% was not
        self.assertEqual(polished["summary"], "Analyst with [X] years of experience.")
        self.assertTrue(svc.placeholders(polished))  # the student must fill them before saving

    def test_voice_may_add_entries_and_contacts_are_found_locally(self):
        reply = {"experiences": [*CV["experiences"], {"title": "Volunteer", "organization": "Resala", "achievements": []}]}
        empty = {**CV, "email": "", "phone": ""}
        with mock.patch.object(engine, "run", return_value=reply) as run:
            polished = svc.polish_cv(empty, "I volunteered at Resala. Email me at mona.v@example.com or 01012345678")
        self.assertEqual(len(polished["experiences"]), 2)
        self.assertEqual((polished["email"], polished["phone"]), ("mona.v@example.com", "01012345678"))
        self.assertIn("voice", run.call_args.kwargs["data"])


class DailyCapTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _uses(self):
        record = StudentCV.objects.filter(student=self.student).first()
        return record.ai_uses if record else 0

    @override_settings(**MOCK)
    def test_success_costs_one_use(self):
        self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json", **ASYNC)
        self.assertEqual(self._uses(), 1)

    @override_settings(**MOCK)
    def test_an_empty_cv_is_refused_before_it_costs_a_use_or_a_model_call(self):
        with mock.patch.object(engine, "run") as run:
            for payload in ({"cv": {}}, {"cv": {"name": "Mona Adel", "email": "mona@example.com"}}, {}):
                resp = self.client.post(reverse("cv_builder_polish"), payload, content_type="application/json")
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["code"], "empty_cv")
        run.assert_not_called()
        self.assertEqual(self._uses(), 0)

    @override_settings(**MOCK, ASYNC_TASK_USER_MAX_ACTIVE=1)
    def test_throttled_request_costs_nothing(self):
        AsyncTask.objects.create(user=self.student_user, kind="cv_voice")
        resp = self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json", **ASYNC)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self._uses(), 0)

    @override_settings(**MOCK, ASYNC_TASK_BACKEND="db")
    def test_duplicate_of_a_running_task_costs_nothing(self):
        for _ in range(2):
            resp = self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json", **ASYNC)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self._uses(), 1)

    @override_settings(**MOCK)
    def test_engine_quota_gives_the_use_back(self):
        with mock.patch.object(engine, "run", side_effect=QuotaExceeded("user_daily", 600)):
            resp = self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json")
        self.assertEqual((resp.status_code, resp.json()["code"]), (429, "ai_quota"))
        self.assertEqual(resp.json()["cv"]["name"], "Mona Adel")
        self.assertEqual(self._uses(), 0)

    @override_settings(**MOCK)
    def test_cap_still_applies(self):
        StudentCV.objects.create(student=self.student, ai_day=timezone.localdate(), ai_uses=AI_USES_PER_DAY)
        resp = self.client.post(reverse("cv_builder_polish"), {"cv": CV}, content_type="application/json")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self._uses(), AI_USES_PER_DAY)


class ResumeAPITests(Base):
    def test_auth_roles_and_missing_cv(self):
        self.assertEqual(self.client.get(reverse("api_resume_ats")).status_code, 401)
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(reverse("api_resume_docx")).json()["code"], "students_only")
        self.client.force_login(self.student_user)
        resp = self.client.get(reverse("api_resume_pdf"))
        self.assertEqual((resp.status_code, resp.json()["code"]), (404, "no_cv"))

    @override_settings(**MOCK)
    def test_polish_is_async_and_exports_work(self):
        self.client.force_login(self.student_user)
        self.client.post(reverse("cv_builder_save"), {"cv": CV}, content_type="application/json")
        resp = self.client.post(reverse("api_resume_polish"), {}, content_type="application/json")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["result"]["cv"]["email"], "mona@example.com")
        self.assertEqual(self.client.get(reverse("api_resume_ats")).json()["grade"], "Excellent")
        self.assertEqual(self.client.get(reverse("api_resume_pdf") + "?template=modern")["Content-Type"], "application/pdf")
        Document(io.BytesIO(self.client.get(reverse("api_resume_docx")).content))
        self.assertEqual(self.client.post(reverse("api_resume_ats")).status_code, 405)


class ImportFromCVAnalysisTests(Base):
    """A student without a saved CV starts the builder from what their analysed CV said; the profile always wins."""

    FACTS = {
        "contacts": {"email": ["sara@example.com"], "phone": ["01012345678"]},
        "education": [{"degree": "BSc Computer Science", "institution": "Cairo University", "start": "2021-09", "end": "present"}],
        "experience": [{"title": "Backend Intern", "organization": "Acme", "start": "2024-06", "end": "2024-09", "months": 3}],
        "projects": ["Library system in Django"],
        "skills": [{"skill_id": 1, "name": "Django"}, {"skill_id": 2, "name": "SQL"}],
    }

    def _analysis(self, status="done", facts=None):
        from internest_skills.models import CVAnalysis
        return CVAnalysis.objects.create(user=self.student.user, file_sha256="a" * 64, status=status,
                                         facts=facts if facts is not None else self.FACTS)

    def test_empty_sections_come_from_the_latest_finished_analysis(self):
        self.student.university = ""
        self.student.projects.all().delete()
        self.student.skills.all().delete()
        self._analysis()
        cv = svc.initial_cv(self.student)
        self.assertEqual(cv["education"][0]["university"], "Cairo University")
        self.assertTrue(cv["education"][0]["is_current"])
        self.assertEqual((cv["experiences"][0]["title"], cv["experiences"][0]["organization"]), ("Backend Intern", "Acme"))
        self.assertEqual(cv["experiences"][0]["achievements"], [])  # nothing invented
        self.assertEqual(cv["projects"][0]["title"], "Library system in Django")
        self.assertEqual(cv["skills"], ["Django", "SQL"])

    def test_profile_data_wins_and_unfinished_analyses_are_ignored(self):
        self.student.university, self.student.major = "Ain Shams University", "Information Systems"
        self.student.user.email, self.student.personal_email = "owner@uni.edu.eg", ""
        self._analysis()
        cv = svc.initial_cv(self.student)
        self.assertEqual(cv["education"][0]["university"], "Ain Shams University")
        self.assertEqual(cv["email"], "owner@uni.edu.eg")  # the account's own address beats the one in the file

    def test_queued_analysis_or_none_changes_nothing(self):
        self.student.university = ""
        self.student.projects.all().delete()
        before = svc.initial_cv(self.student)
        self._analysis(status="queued")
        self.assertEqual(svc.initial_cv(self.student), before)

    def test_garbage_facts_are_safe(self):
        self.assertEqual(svc.cv_from_facts({"education": ["x"], "experience": None, "skills": [{"no": 1}]})["education"], [])
        self.assertEqual(svc.cv_from_facts(None)["skills"], [])
