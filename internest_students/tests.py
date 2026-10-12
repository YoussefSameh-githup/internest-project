import io
import json
from types import SimpleNamespace
from unittest import mock

import httpx
import openai
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from internest_skills.models import Skill, StudentSkill
from internest_skills.tests import Base, _llm_reply

from . import cv_builder_service as svc
from .cv_pdf import render_cv_pdf
from .models import StudentCV, StudentProject
from .views import AI_USES_PER_DAY

CHAT = "openai.resources.chat.completions.Completions.create"
WHISPER = "openai.resources.audio.transcriptions.Transcriptions.create"
AI = dict(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", SKILLS_LLM_MODEL="",
          LLM_BACKUP_API_KEY="", WHISPER_API_KEY="whisper-key", WHISPER_BASE_URL="https://api.groq.com/openai/v1",
          WHISPER_MODEL="whisper-large-v3")
TRANSCRIPT = "انا اسمي منى، بدرس Finance في جامعة القاهرة، اشتغلت intern في بنك مصر وعملت dashboard بالـ Python."

CV = {
    "name": "Mona Adel", "email": "mona@example.com", "phone": "+20 100 000 0000", "linkedin": "linkedin.com/in/mona",
    "github": "", "summary": "Finance student.",
    "education": [{"university": "Cairo University", "major": "Finance", "graduation_year": "2027"}],
    "experiences": [{"title": "Intern", "organization": "Banque Misr", "achievements": ["Built a Python dashboard", ""]}],
    "projects": [{"title": "Budget Tracker", "description": "Personal finance app", "tech_stack": "Python, Excel", "link": "github.com/mona/budget"}],
    "skills": ["python3", "Excel", "Underwater Basket Weaving"],
}
POLISHED = {
    **CV, "email": "invented@example.com", "summary": "Finance student who builds data tools.",
    "experiences": [{"title": "Finance Intern", "organization": "Banque Misr",
                     "achievements": ["Developed a Python dashboard that cut reporting time by [X%]", "Analyzed monthly branch data"]}],
    "skills": ["py", "MS Excel", "Financial Analysis"],
}


def _audio(content_type="audio/webm", size=2048):
    return SimpleUploadedFile("voice.webm", b"\x1aE\xdf\xa3" + b"0" * size, content_type=content_type)


def _rate_limited():
    request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    return openai.RateLimitError("quota", response=httpx.Response(429, request=request), body=None)


class CleaningTests(TestCase):
    def test_clean_cv_coerces_types_and_drops_junk(self):
        cv = svc.clean_cv({
            "name": "  Mona   Adel ", "email": "not-an-email", "linkedin": "linkedin.com/in/mona", "github": "javascript:alert(1)",
            "summary": ["not", "text"], "education": "Cairo", "experiences": [
                {"title": "Intern", "achievements": "• Led a team of 4\n- Built reports\n\n"}, {"title": "", "organization": ""}, "junk"],
            "projects": [{"title": "A", "link": "ftp://x"}], "skills": ["Python", "python", "", 5, None],
        })
        self.assertEqual((cv["name"], cv["email"], cv["linkedin"], cv["github"], cv["summary"]),
                         ("Mona Adel", "", "https://linkedin.com/in/mona", "", ""))
        self.assertEqual(cv["education"], [])
        self.assertEqual(cv["experiences"], [{"title": "Intern", "organization": "", "achievements": ["Led a team of 4", "Built reports"]}])
        self.assertEqual(cv["projects"][0]["link"], "")
        self.assertEqual(cv["skills"], ["Python", "5"])
        self.assertEqual(svc.clean_cv(None), svc.EMPTY_CV)

    def test_placeholders_are_found(self):
        self.assertEqual(svc.placeholders({"a": "Cut costs by [X%] for [N] users", "b": "[Link]"}), ["[N]", "[X%]"])


class TaxonomyTests(Base):
    def test_skills_standardized_and_only_taxonomy_matches_returned(self):
        names, matched = svc.standardize_skills(["python3", "Py", "MS Excel", "Underwater Basket Weaving"])
        self.assertEqual(names, ["Python", "Excel", "Underwater Basket Weaving"])
        self.assertEqual([s.name for s in matched], ["Python", "Excel"])


class BuilderPageTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_page_prefills_from_profile_and_lists_taxonomy(self):
        page = self.client.get(reverse("cv_builder"))
        self.assertEqual(page.status_code, 200)
        cv = page.context["cv"]
        self.assertEqual((cv["name"], cv["education"][0]["university"]), ("stud", "Cairo"))
        for marker in ("Record your details by voice", "data-rec-start", "MediaRecorder", 'data-goto="4"', '<option value="Python">',
                       'id="cv-data"', reverse("cv_builder_voice")):
            self.assertContains(page, marker)
        self.assertContains(page, "data-rec-start disabled")  # no Whisper key configured in tests

    def test_arabic_labels(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(reverse("cv_builder"))
        self.assertContains(page, "سجّل بياناتك بصوتك 🎙️")
        self.assertContains(page, "تحميل الـ CV (PDF)")

    def test_partners_cannot_open_it(self):
        self.client.force_login(self.pro_user)
        self.assertEqual(self.client.get(reverse("cv_builder")).status_code, 404)
        self.assertEqual(self.client.post(reverse("cv_builder_save"), {}, content_type="application/json").status_code, 403)

    def test_profile_shows_builder_card(self):
        StudentProject.objects.create(student=self.student, title="Budget Tracker", tech_stack="Python")
        page = self.client.get(reverse("profile"))
        self.assertContains(page, reverse("cv_builder"))
        self.assertContains(page, "Budget Tracker")


@override_settings(**AI)
class PolishTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _polish(self, cv=CV):
        return self.client.post(reverse("cv_builder_polish"), {"cv": cv}, content_type="application/json")

    def test_polish_standardizes_skills_and_keeps_typed_contacts(self):
        with mock.patch(CHAT, return_value=_llm_reply(json.dumps(POLISHED))) as create:
            resp = self._polish()
        data = resp.json()["cv"]
        self.assertEqual(data["skills"], ["Python", "Excel", "Financial Analysis"])
        self.assertEqual(data["email"], "mona@example.com")  # the AI can't replace what the student typed
        self.assertTrue(data["experiences"][0]["achievements"][0].startswith("Developed"))
        system, user = (m["content"] for m in create.call_args.kwargs["messages"])
        self.assertIn("strong past-tense action verb", system)
        self.assertIn("Never invent", system)
        self.assertIn("Known: ", user)
        self.assertIn("VOICE: (none)", user)

    def test_ai_down_keeps_student_text(self):
        with mock.patch(CHAT, side_effect=_rate_limited()), self.assertLogs("internest_skills.llm", "WARNING"):
            resp = self._polish()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["cv"]["name"], "Mona Adel")
        self.assertIn("kept as is", resp.json()["error"])

    @override_settings(AGENTROUTER_API_KEY="")
    def test_ai_not_configured(self):
        resp = self._polish()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["cv"]["name"], "Mona Adel")

    def test_daily_ai_cap(self):
        StudentCV.objects.create(student=self.student, ai_day=timezone.localdate(), ai_uses=AI_USES_PER_DAY)
        with mock.patch(CHAT) as create:
            resp = self._polish()
        self.assertEqual(resp.status_code, 429)
        create.assert_not_called()


@override_settings(**AI)
class VoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _send(self, audio=None, cv=CV):
        return self.client.post(reverse("cv_builder_voice"), {"audio": audio or _audio(), "cv": json.dumps(cv)})

    def test_voice_note_is_transcribed_and_structured(self):
        with mock.patch(WHISPER, return_value=SimpleNamespace(text=TRANSCRIPT)) as whisper, \
                mock.patch(CHAT, return_value=_llm_reply(json.dumps(POLISHED))) as chat:
            resp = self._send()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["transcript"], TRANSCRIPT)
        self.assertEqual(body["cv"]["skills"][0], "Python")
        kwargs = whisper.call_args.kwargs
        self.assertEqual(kwargs["model"], "whisper-large-v3")
        self.assertEqual(kwargs["file"][0], "voice.webm")
        self.assertIn("العربية", kwargs["prompt"])
        self.assertIn(TRANSCRIPT, chat.call_args.kwargs["messages"][1]["content"])

    def test_page_enables_the_mic_when_whisper_is_configured(self):
        self.assertNotContains(self.client.get(reverse("cv_builder")), "data-rec-start disabled")

    @override_settings(LLM_BACKUP_API_KEY="groq-key", LLM_BACKUP_BASE_URL="https://api.groq.com/openai/v1")
    def test_transcription_fails_over_to_groq_backup_key(self):
        self.assertEqual([p.label for p in svc.transcription_providers()], ["WHISPER_API_KEY", "API_KEY_2"])
        with mock.patch(WHISPER, side_effect=[_rate_limited(), SimpleNamespace(text=TRANSCRIPT)]) as whisper, \
                mock.patch(CHAT, return_value=_llm_reply(json.dumps(POLISHED))), self.assertLogs("internest_students", "WARNING"):
            self.assertEqual(self._send().status_code, 200)
        self.assertEqual(whisper.call_count, 2)

    def test_rejects_wrong_type_and_empty_speech(self):
        self.assertEqual(self._send(_audio("text/plain")).status_code, 400)
        with mock.patch(WHISPER, return_value=SimpleNamespace(text="  ")):
            resp = self._send()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("couldn't hear enough", resp.json()["error"])

    def test_transcription_down_is_a_clear_503(self):
        with mock.patch(WHISPER, side_effect=_rate_limited()), self.assertLogs("internest_students", "WARNING"):
            resp = self._send()
        self.assertEqual(resp.status_code, 503)
        self.assertIn("voice note", resp.json()["error"])

    def test_ai_down_after_transcription_returns_transcript(self):
        with mock.patch(WHISPER, return_value=SimpleNamespace(text=TRANSCRIPT)), \
                mock.patch(CHAT, side_effect=_rate_limited()), self.assertLogs("internest_skills.llm", "WARNING"):
            resp = self._send()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["transcript"], TRANSCRIPT)
        self.assertEqual(resp.json()["cv"]["name"], "Mona Adel")

    @override_settings(WHISPER_API_KEY="")
    def test_no_transcription_key(self):
        self.assertFalse(svc.voice_enabled())  # the primary chat key isn't Groq/OpenAI, so it can't transcribe
        self.assertEqual(self._send().status_code, 503)


class SaveAndPdfTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _save(self, cv=CV):
        return self.client.post(reverse("cv_builder_save"), {"cv": cv}, content_type="application/json")

    def test_save_syncs_taxonomy_skills_and_projects(self):
        manual = StudentProject.objects.create(student=self.student, title="Manual", source="manual")
        before = Skill.objects.count()
        resp = self._save()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["synced"], {"skills": 2, "new_skills": 2, "projects": 1})
        self.assertEqual(Skill.objects.count(), before)  # unknown skills stay on the CV only
        claimed = StudentSkill.objects.filter(student=self.student)
        self.assertEqual(set(claimed.values_list("skill__name", flat=True)), {"Python", "Excel"})
        self.assertEqual(StudentCV.objects.get(student=self.student).data["skills"], ["Python", "Excel", "Underwater Basket Weaving"])
        project = StudentProject.objects.get(student=self.student, source="cv_builder")
        self.assertEqual(project.link, "https://github.com/mona/budget")

        self._save({**CV, "projects": [{"title": "New One"}, {"title": "Two"}]})
        self.assertEqual(list(self.student.projects.filter(source="cv_builder").values_list("title", flat=True)), ["New One", "Two"])
        self.assertTrue(StudentProject.objects.filter(pk=manual.pk).exists())

    def test_verified_skill_is_never_downgraded(self):
        python = Skill.objects.get(name="Python")
        StudentSkill.objects.create(student=self.student, skill=python, source="explicit", status=StudentSkill.STATUS_VERIFIED, score=90)
        self._save()
        self.assertEqual(StudentSkill.objects.get(student=self.student, skill=python).status, StudentSkill.STATUS_VERIFIED)

    def test_save_requires_name_and_no_placeholders(self):
        resp = self._save({**CV, "name": ""})
        self.assertEqual((resp.status_code, resp.json()["step"]), (400, 0))
        resp = self._save(POLISHED)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("[X%]", resp.json()["error"])
        self.assertFalse(StudentCV.objects.filter(student=self.student).exists())

    def test_pdf_download_is_ats_friendly(self):
        self.assertRedirects(self.client.get(reverse("cv_builder_pdf")), reverse("cv_builder"), fetch_redirect_response=False)
        self._save()
        resp = self.client.get(reverse("cv_builder_pdf"))
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn('filename="mona-adel-cv.pdf"', resp["Content-Disposition"])
        reader = PdfReader(io.BytesIO(resp.content))
        text = reader.pages[0].extract_text()
        for heading in ("Mona Adel", "SUMMARY", "EDUCATION", "EXPERIENCE", "PROJECTS", "SKILLS", "Built a Python dashboard"):
            self.assertIn(heading, text)
        self.assertEqual(len(reader.pages), 1)

    def test_preview_page(self):
        self._save()
        page = self.client.get(reverse("cv_builder_preview"))
        self.assertContains(page, "mona@example.com | +20 100 000 0000 | https://linkedin.com/in/mona")
        self.assertContains(page, "<h2>Experience</h2>", html=False)

    def test_arabic_cv_renders(self):
        cv = svc.clean_cv({**CV, "name": "منى عادل", "summary": "طالبة مالية"})
        self.assertTrue(render_cv_pdf(cv).startswith(b"%PDF"))
