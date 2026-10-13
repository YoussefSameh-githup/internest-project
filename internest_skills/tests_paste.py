"""Skill analysis from pasted LinkedIn / CV text, uploaded files, or both — through the same AI pipeline."""
import json
from unittest import mock

from django.test import override_settings
from django.urls import reverse

from .models import SkillProfile, StudentSkill
from .tests import Base, _docx_upload, _llm_reply

CHAT = "openai.resources.chat.completions.Completions.create"
LINKEDIN_TEXT = ("About\nFinance student who loves data.\nExperience\nIntern at Banque Misr — built monthly SQL reports "
                 "and pivot tables in Excel for the retail team.\nSkills\nSQL · Excel · Financial Analysis")


class PasteTextTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def test_pasted_text_is_analyzed_locally_without_ai(self):
        resp = self.client.post(reverse("skills_hub"), {"pasted_text": LINKEDIN_TEXT}, follow=True)
        self.assertContains(resp, "Analysis complete")
        self.assertTrue({"SQL", "Excel"} <= set(self.student.skills.values_list("skill__name", flat=True)))
        self.assertEqual(SkillProfile.objects.get(student=self.student).extraction_count_today, 1)

    @override_settings(AGENTROUTER_API_KEY="key-1", AGENTROUTER_BASE_URL="https://llm.test/v1", LLM_BACKUP_API_KEY="")
    def test_pasted_text_and_file_go_to_the_ai_together(self):
        reply = _llm_reply(json.dumps({"extracted_skills": ["Financial Analysis", "SQL"], "top_domains": ["business"]}))
        with mock.patch(CHAT, return_value=reply) as create:
            self.client.post(reverse("skills_hub"), {"pasted_text": LINKEDIN_TEXT, "cv_file": _docx_upload("Graduation project: Python stock dashboard.")})
        cv = create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("Banque Misr", cv)
        self.assertIn("stock dashboard", cv)
        self.assertEqual(set(StudentSkill.objects.filter(student=self.student).values_list("skill__name", flat=True)), {"Financial Analysis", "SQL"})

    def test_validation(self):
        resp = self.client.post(reverse("skills_hub"), {"pasted_text": "SQL, Excel"})
        self.assertContains(resp, "Paste a few more lines (at least 80 characters)")
        self.assertContains(resp, 'data-mode="text" aria-selected="true"')  # reopens on the text tab
        resp = self.client.post(reverse("skills_hub"), {})
        self.assertContains(resp, "Upload your CV or LinkedIn PDF, or paste your LinkedIn / CV text.")
        self.assertFalse(self.student.skills.exists())

    def test_dialog_returns_to_the_profile_and_keeps_university_sharing(self):
        SkillProfile.objects.create(student=self.student, partner_university=self.uni)
        target = reverse("profile") + "?tab=skills"
        resp = self.client.post(reverse("skills_hub"), {"pasted_text": LINKEDIN_TEXT, "next": target})
        self.assertRedirects(resp, target, fetch_redirect_response=False)
        self.assertEqual(SkillProfile.objects.get(student=self.student).partner_university, self.uni)
        resp = self.client.post(reverse("skills_hub"), {"pasted_text": LINKEDIN_TEXT, "next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("skills_hub"), fetch_redirect_response=False)

    def test_hub_page_offers_both_inputs(self):
        page = self.client.get(reverse("skills_hub"))
        for marker in ('data-mode="file"', 'data-mode="text"', 'name="cv_file"', 'name="pasted_text"', "Analyze my skills"):
            self.assertContains(page, marker)

    def test_arabic(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(reverse("skills_hub"))
        self.assertContains(page, "لصق نص")
        self.assertContains(page, "رفع ملف")
