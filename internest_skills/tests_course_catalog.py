"""Skill-gap course catalogue: partner courses first, open courses as fallback, badges and admin."""
from django.contrib.auth.models import User
from django.urls import reverse

from internest_core.models import PartnerCourseSubmission

from .models import Course, Skill, StudentSkill
from .recommendations import recommendations_for, recommendations_for_student
from .tests import Base


class CourseCatalogTests(Base):
    def setUp(self):
        super().setUp()
        sub = self.skill.sub_skills.first()
        self.gap = StudentSkill.objects.create(student=self.student, skill=self.skill, source="explicit", status=StudentSkill.STATUS_LAG,
                                               lag_sub_skills=[{"id": sub.id, "name": sub.name, "accuracy": 40}])

    def _course(self, title, partner, priority=0, **extra):
        return Course.objects.create(title=title, skill=self.skill, provider_name="Academy", url=f"https://learn.example/{priority}-{partner}",
                                     is_partner=partner, priority=priority, **extra)

    def _titles(self):
        return [r["title"] for r in recommendations_for(self.gap)["recommendations"]]

    def test_partner_courses_first_by_priority_and_hide_open_ones(self):
        self._course("Open finance basics", False, priority=99)
        self._course("Partner valuation", True, priority=1)
        self._course("Partner modelling", True, priority=5, discount_note="20% off with code INTERNEST20")
        self._course("Inactive partner", True, priority=50, is_active=False)
        self.assertEqual(self._titles(), ["Partner modelling", "Partner valuation"])
        rec = recommendations_for(self.gap)["recommendations"][0]
        self.assertEqual((rec["is_partner"], rec["discount_note"], rec["opens_externally"]), (True, "20% off with code INTERNEST20", True))

    def test_open_courses_when_no_partner_course_matches(self):
        self._course("Open finance basics", False, priority=1)
        self._course("Open advanced finance", False, priority=3)
        Course.objects.create(title="Partner course for another skill", skill=Skill.objects.get(slug="python"), provider_name="X",
                              url="https://x.example", is_partner=True)
        self.assertEqual(self._titles(), ["Open advanced finance", "Open finance basics"])
        result = recommendations_for_student(self.student)
        self.assertEqual(result["internal"], [])
        self.assertEqual([r["title"] for r in result["external"]], ["Open advanced finance", "Open finance basics"])

    def test_partner_submissions_also_beat_open_courses(self):
        self._course("Open finance basics", False)
        sub = self.skill.sub_skills.first()
        PartnerCourseSubmission.objects.create(partner=self.pro, title="Bootcamp", description=sub.name, price=100,
                                               instructor_name="Dr X", points_awarded=5, status="Approved")
        self.assertEqual(self._titles(), ["Bootcamp"])

    def test_empty_catalogue_keeps_the_external_fallback(self):
        self.assertTrue(any("coursera.org" in r["url"] for r in recommendations_for(self.gap)["recommendations"]))

    def test_student_hub_cards_show_badge_and_discount(self):
        self._course("Partner modelling", True, priority=5, discount_note="20% off with code INTERNEST20")
        self.client.force_login(self.student_user)
        page = self.client.get(reverse("course_list"))
        self.assertContains(page, "Certified partner")
        self.assertContains(page, "20% off with code INTERNEST20")
        self.assertContains(page, 'href="https://learn.example/5-True" target="_blank" rel="noopener"')
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("course_list")), "شريك معتمد")

    def test_admin_manages_courses(self):
        root = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(root)
        course = self._course("Open finance basics", False)
        self.assertEqual(self.client.get(reverse("admin:internest_skills_course_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:internest_skills_course_add")).status_code, 200)
        self.client.post(reverse("admin:internest_skills_course_changelist"), {"action": "mark_partner", "_selected_action": [course.pk]})
        course.refresh_from_db()
        self.assertTrue(course.is_partner)
