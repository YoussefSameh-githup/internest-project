"""Applicant counters, empathetic status lifecycle, duplicate prevention, semantic matching, posting form, badges."""
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from internest_core.models import Application, Internship, PartnerInternshipSubmission, PartnerProfile, StudentProfile
from internest_skills.matching import skill_match
from internest_skills.models import Skill, SkillRelation, StudentSkill
from internest_skills.semantic import aggregate_score
from internest_startups.models import CompanyProfile
from internest_startups.tiers import activate_pro


CONSENT = {"contact_consent": "on"}


def _student(username):
    user = User.objects.create_user(username, password="pw", first_name=username.title())
    profile = StudentProfile.objects.create(user=user, university="Cairo", major="CS", study_level="3",
                                            personal_email=f"{username}@uni.edu", profile_completion_score=100)
    return user, profile


@override_settings(AGENTROUTER_API_KEY="")
class ApplicationsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_skills", stdout=io.StringIO())
        founder = User.objects.create_user("founder", password="pw")
        cls.partner = PartnerProfile.objects.create(user=founder, company_name="NileCode", partner_code="N1",
                                                    is_fully_verified=True, official_email="hi@nilecode.io",
                                                    official_website="https://nilecode.io", linkedin_url="https://linkedin.com/x")
        CompanyProfile.objects.create(partner=cls.partner, industry="software", founded_year=2022, description="d",
                                      founder_email="f@nilecode.io")
        cls.partner.calculate_completion()
        cls.founder = founder
        cls.gig = Internship.objects.create(partner=cls.partner, title="Design gig", description="Long description " * 40,
                                            location="Online", required_majors="any",
                                            deadline=timezone.now().date() + timedelta(days=20))
        cls.role = Internship.objects.create(partner=cls.partner, title="Data role", description="d", location="Giza",
                                             required_majors="any", deadline=timezone.now().date() + timedelta(days=20))

    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"

    def login_student(self, user):
        self.client.force_login(user)
        StudentProfile.objects.filter(user=user).update(personal_email_verified_at=timezone.now())  # login signal resets it


class CounterAndLifecycleTests(ApplicationsBase):
    def test_inbox_counter_matches_published_list(self):
        for name, opp in (("amr", self.gig), ("sara", self.gig), ("lina", self.role)):
            user, _ = _student(name)
            Application.objects.create(internship=opp, applicant=user)
        self.client.force_login(self.founder)
        page = self.client.get(reverse("partner_dashboard"))
        self.assertEqual(page.context["applicant_count"], 3)
        self.assertEqual(sum(i.applicants_count for i in page.context["published_internships"]), 3)
        self.assertContains(page, '<span class="dash-card__count">3</span>')
        inbox = self.client.get(reverse("partner_dashboard_section", args=["applicants"]))
        self.assertEqual(len(inbox.context["received_applicants"]), 3)

    def test_status_lifecycle(self):
        user, _ = _student("amr")
        self.login_student(user)
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        app = Application.objects.get()
        self.assertEqual(app.status, Application.STATUS_SUBMITTED)
        self.assertContains(self.client.get(reverse("my_applications")), "Submitted")

        self.client.force_login(self.founder)
        self.client.get(reverse("partner_applicant", args=[app.pk]))  # startup opens the file
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_UNDER_REVIEW)
        self.assertIsNotNone(app.reviewed_at)

        page = self.client.get(reverse("partner_applicant", args=[app.pk]))
        self.assertNotContains(page, "amr@uni.edu")  # contact hidden until accepted
        self.client.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "accept"})
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_ACCEPTED)
        self.assertContains(self.client.get(reverse("partner_applicant", args=[app.pk])), "amr@uni.edu")

    def test_decline_and_close_show_position_filled_never_rejected(self):
        a_user, _ = _student("amr")
        b_user, _ = _student("sara")
        a = Application.objects.create(internship=self.gig, applicant=a_user)
        b = Application.objects.create(internship=self.gig, applicant=b_user, status=Application.STATUS_ACCEPTED)
        self.client.force_login(self.founder)
        self.client.post(reverse("partner_close_opportunity", args=[self.gig.pk]))
        a.refresh_from_db(); b.refresh_from_db(); self.gig.refresh_from_db()
        self.assertEqual((a.status, b.status, self.gig.is_active), (Application.STATUS_FULFILLED, Application.STATUS_ACCEPTED, False))
        self.login_student(a_user)
        page = self.client.get(reverse("my_applications"))
        self.assertContains(page, "Position filled")
        self.assertNotContains(page, "Rejected")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("my_applications")), "تم الاكتفاء")

    def test_other_startups_cannot_open_applicants(self):
        user, _ = _student("amr")
        app = Application.objects.create(internship=self.gig, applicant=user)
        other = User.objects.create_user("other", password="pw")
        p = PartnerProfile.objects.create(user=other, company_name="Other", partner_code="O1", is_fully_verified=True)
        CompanyProfile.objects.create(partner=p, industry="software", founded_year=2022, description="d")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("partner_applicant", args=[app.pk])).status_code, 404)
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_SUBMITTED)


class DuplicateAndCardTests(ApplicationsBase):
    def test_duplicate_application_prevented_and_button_locked(self):
        user, _ = _student("amr")
        self.login_student(user)
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        resp = self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        self.assertRedirects(resp, reverse("internship_detail", args=[self.gig.pk]), fetch_redirect_response=False)
        self.assertEqual(Application.objects.count(), 1)
        listing = self.client.get(reverse("list"))
        self.assertContains(listing, "Already applied")
        self.assertContains(listing, "Apply now")  # the other opportunity
        detail = self.client.get(reverse("internship_detail", args=[self.gig.pk]))
        self.assertContains(detail, "Already applied")
        self.assertNotContains(detail, f'href="{reverse("apply", args=[self.gig.pk])}"')

    def test_pro_badge_only_for_pro_and_no_premium_badge(self):
        user, _ = _student("amr")
        self.login_student(user)
        Internship.objects.update(is_premium=True)
        listing = self.client.get(reverse("list"))
        self.assertNotContains(listing, "Premium")
        self.assertNotContains(listing, ">PRO<")
        activate_pro(self.partner)
        self.assertContains(self.client.get(reverse("list")), ">PRO<", count=2)

    def test_description_clamped_on_cards_and_online_label(self):
        user, _ = _student("amr")
        self.login_student(user)
        listing = self.client.get(reverse("list"))
        self.assertContains(listing, "listing-card__desc--clamp")
        self.assertContains(listing, "💻")
        detail = self.client.get(reverse("internship_detail", args=[self.role.pk]))
        self.assertContains(detail, "maps.google.com/maps?q=Giza")


class SemanticMatchTests(ApplicationsBase):
    def _skill(self, name):
        return Skill.objects.create(name=name, slug=name.lower().replace(" ", "-"), discipline="media")

    def test_related_verified_skills_unlock_requested_skill(self):
        _, student = _student("amr")
        designer = self._skill("Graphic Designer Pro")
        tools = [self._skill(n) for n in ("Adobe Photoshop", "Adobe Illustrator", "UI Design Basics")]
        for tool, w in zip(tools, (0.7, 0.7, 0.5)):
            SkillRelation.objects.create(skill=designer, related=tool, weight=w, source="ai")
            StudentSkill.objects.create(student=student, skill=tool, source="explicit", status=StudentSkill.STATUS_VERIFIED)
        self.gig.required_skills.set([designer])
        match = skill_match(student, self.gig)
        self.assertGreaterEqual(match.score, 80)  # 1 - .3*.3*.5 = 95.5%
        self.assertTrue(match.unlocked)

    def test_weak_relation_alone_stays_locked(self):
        _, student = _student("amr")
        designer = self._skill("Motion Designer")
        ui = self._skill("UI Sketching")
        SkillRelation.objects.create(skill=designer, related=ui, weight=0.5)
        StudentSkill.objects.create(student=student, skill=ui, source="explicit", status=StudentSkill.STATUS_VERIFIED)
        self.gig.required_skills.set([designer])
        self.assertEqual(skill_match(student, self.gig).score, 50)
        self.assertFalse(skill_match(student, self.gig).unlocked)

    def test_name_variant_counts_as_equivalent(self):
        required = Skill(id=1, name="Graphic Designer")
        verified = [Skill(id=2, name="Graphic Design")]
        self.assertGreaterEqual(aggregate_score([required], verified, weights={})[0], 80)

    def test_relations_work_in_both_directions_and_apply_gate(self):
        user, student = _student("amr")
        req = self._skill("Brand Identity")
        have = self._skill("Logo Design")
        SkillRelation.objects.create(skill=have, related=req, weight=0.9)  # stored the other way round
        StudentSkill.objects.create(student=student, skill=have, source="explicit", status=StudentSkill.STATUS_VERIFIED)
        self.gig.required_skills.set([req])
        self.login_student(user)
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        self.assertTrue(Application.objects.filter(internship=self.gig, applicant=user).exists())


def _llm_reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class PostingFormTests(ApplicationsBase):
    BASE = {"title": "Design gig", "description": "Brand work", "required_majors": "Design",
            "deadline": (timezone.now().date() + timedelta(days=10)).isoformat()}

    def setUp(self):
        super().setUp()
        self.client.force_login(self.founder)

    def test_online_and_onsite_locations(self):
        self.client.post(reverse("partner_submit_internship"), {**self.BASE, "location_mode": "online"})
        self.assertEqual(PartnerInternshipSubmission.objects.get().location, "Online")
        PartnerInternshipSubmission.objects.all().delete()
        resp = self.client.post(reverse("partner_submit_internship"), {**self.BASE, "location_mode": "onsite", "address": ""})
        self.assertContains(resp, "Enter the office address for on-site opportunities.")
        self.client.post(reverse("partner_submit_internship"), {**self.BASE, "location_mode": "onsite", "address": "Smart Village, Giza"})
        self.assertEqual(PartnerInternshipSubmission.objects.get().location, "Smart Village, Giza")

    @override_settings(AGENTROUTER_API_KEY="k", AGENTROUTER_BASE_URL="https://llm.test/v1")
    def test_custom_skills_created_and_ai_related(self):
        photoshop = Skill.objects.create(name="Adobe Photoshop", slug="adobe-photoshop", discipline="media")
        reply = _llm_reply(json.dumps({"r": [{"n": "Adobe Photoshop", "w": 0.8}, {"n": "Nonexistent", "w": 0.9}]}))
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply):
            self.client.post(reverse("partner_submit_internship"), {
                **self.BASE, "location_mode": "online",
                "required_skills": [Skill.objects.get(slug="python").pk], "custom_skills": "Graphic Designer, graphic designer ,Brand Strategy",
            })
        sub = PartnerInternshipSubmission.objects.get()
        self.assertEqual(sorted(s.name for s in sub.required_skills.all()), ["Brand Strategy", "Graphic Designer", "Python"])
        rel = SkillRelation.objects.get(skill__name="Graphic Designer", related=photoshop)
        self.assertEqual((rel.weight, rel.source), (0.8, "ai"))

    def test_form_page_renders_new_ui(self):
        page = self.client.get(reverse("partner_submit_internship"))
        for marker in ('name="location_mode"', 'value="onsite"', "data-tag-input", 'name="custom_skills"', "opp-card", "data-map"):
            self.assertContains(page, marker)


class CourseRecommendationTests(ApplicationsBase):
    def test_external_suggestions_when_no_partner_course(self):
        user, student = _student("amr")
        skill = Skill.objects.get(slug="financial-analysis")
        sub = skill.sub_skills.first()
        StudentSkill.objects.create(student=student, skill=skill, source="explicit", status=StudentSkill.STATUS_LAG,
                                    lag_sub_skills=[{"id": sub.id, "name": sub.name, "accuracy": 40}])
        self.login_student(user)
        page = self.client.get(reverse("course_list"))
        self.assertContains(page, "Recommended for you")
        self.assertContains(page, "coursera.org")
        self.assertTrue(page.context["recommended"]["external"])
        self.assertEqual(page.context["recommended"]["internal"], [])

    def test_partner_course_preferred_when_available(self):
        from internest_core.models import PartnerCourseSubmission
        user, student = _student("amr")
        skill = Skill.objects.get(slug="financial-analysis")
        sub = skill.sub_skills.get(name="Ratio analysis")
        PartnerCourseSubmission.objects.create(partner=self.partner, title="Financial ratios bootcamp", description="ratio analysis",
                                               price=100, instructor_name="Dr X", points_awarded=5, status="Approved")
        StudentSkill.objects.create(student=student, skill=skill, source="explicit", status=StudentSkill.STATUS_LAG,
                                    lag_sub_skills=[{"id": sub.id, "name": sub.name, "accuracy": 40}])
        self.login_student(user)
        page = self.client.get(reverse("course_list"))
        self.assertEqual([r["title"] for r in page.context["recommended"]["internal"]], ["Financial ratios bootcamp"])
        self.assertEqual(page.context["recommended"]["external"], [])
