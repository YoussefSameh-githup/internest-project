"""student_hub service + shared components: tab resolution, graceful degradation, one-query skills, reusable partials."""
from unittest import mock

from django.db import DatabaseError
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from internest_core import student_hub
from internest_core.forms import ProfileForm
from internest_core.models import Application, Internship
from internest_skills.models import Skill, StudentSkill
from internest_skills.tests import Base


class TabResolutionTests(SimpleTestCase):
    def test_known_legacy_and_unknown_tabs(self):
        for requested, expected in (("overview", "overview"), ("skills", "skills"), ("activity", "activity"), ("edit", "edit"),
                                    ("details", "edit"), ("settings", "edit"), ("portfolio", "activity"),
                                    (None, "overview"), ("", "overview"), ("<script>", "overview")):
            self.assertEqual(student_hub.resolve_tab(requested), expected)

    def test_form_errors_force_the_edit_tab(self):
        self.assertEqual(student_hub.resolve_tab("skills", has_errors=True), "edit")

    def test_every_form_section_field_exists_on_the_profile_form(self):
        fields = set(ProfileForm().fields)
        for section, names in student_hub.FORM_SECTIONS.items():
            self.assertLessEqual(set(names), fields, section)


class DbSafeTests(SimpleTestCase):
    def test_returns_the_default_and_logs_when_the_database_fails(self):
        calls = []

        @student_hub.db_safe(list)
        def broken():
            calls.append(1)
            raise DatabaseError("no such table")

        with self.assertLogs("internest_core.student_hub", "ERROR"), mock.patch("internest_core.student_hub.transaction.atomic"):
            first, second = broken(), broken()
        self.assertEqual((first, second, len(calls)), ([], [], 2))
        self.assertIsNot(first, second)  # a factory default: callers never share one mutable object

    def test_scalar_default(self):
        @student_hub.db_safe(0)
        def broken():
            raise DatabaseError("x")

        with self.assertLogs("internest_core.student_hub", "ERROR"), mock.patch("internest_core.student_hub.transaction.atomic"):
            self.assertEqual(broken(), 0)


class ProfileDataTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)
        self.request = RequestFactory().get("/profile/")
        self.request.user = self.student_user

    def _skill(self, slug, status, **kw):
        return StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug=slug), source=kw.pop("source", "explicit"),
                                           status=status, **kw)

    def test_skills_are_grouped_once_with_verified_first(self):
        self._skill("python", "claimed")
        self._skill("sql", "lag", source="implicit")
        self._skill("excel", "verified", score=90)
        skills = student_hub.student_skills(self.student)
        self.assertEqual([s.skill.slug for s in skills][0], "excel")
        groups = student_hub.group_skills(skills)
        self.assertEqual([len(groups[k]) for k in ("verified", "unverified", "explicit", "inferred")], [1, 2, 1, 1])

    def test_profile_context_reads_the_skills_once(self):
        for slug, status in (("python", "claimed"), ("excel", "verified")):
            self._skill(slug, status)
        form = ProfileForm(instance=self.student)
        with mock.patch.object(student_hub, "student_skills", wraps=student_hub.student_skills) as skills,                 mock.patch.object(student_hub, "verified_skills_count") as count:
            ctx = student_hub.profile_context(self.request, self.student, form)
        self.assertEqual(skills.call_count, 1)
        count.assert_not_called()  # the verified count comes from the same list, not a second query
        self.assertEqual(ctx["verified_skills_count"], 1)
        self.assertEqual(len(ctx["skill_groups"]["unverified"]), 1)

    def test_applications_selectors_feed_the_applications_page(self):
        internship = Internship.objects.create(partner=self.pro, title="Analyst", description="d", location="Cairo", required_majors="any",
                                               deadline=timezone.localdate() + timezone.timedelta(days=3))
        Application.objects.create(internship=internship, applicant=self.student_user)
        self.assertEqual([a.internship_id for a in student_hub.student_applications(self.student_user)], [internship.pk])
        self.assertEqual(student_hub.open_invitations(self.student_user, {internship.pk}), [])

    def test_profile_context_has_no_workspace_data(self):
        ctx = student_hub.profile_context(self.request, self.student, ProfileForm(instance=self.student))
        for key in ("applications", "invitations", "applications_total", "certificates_count"):
            self.assertNotIn(key, ctx)

    def test_skills_share_is_none_without_verified_skills_and_names_at_most_four(self):
        self.assertIsNone(student_hub.skills_share(self.request, []))
        for slug in ("python", "sql", "excel"):
            self._skill(slug, "verified")
        share = student_hub.skills_share(self.request, student_hub.group_skills(student_hub.student_skills(self.student))["verified"])
        self.assertTrue(share["url"].endswith(reverse("landing")))
        self.assertIn("AI-verified in", share["text"])


class ComponentTests(SimpleTestCase):
    def test_progress_ring_draws_the_value(self):
        html = render_to_string("components/progress_ring.html", {"value": 72, "label": "Readiness"})
        self.assertIn('aria-valuenow="72"', html)
        self.assertIn('stroke-dasharray="72 100"', html)
        self.assertIn("72%", html)

    def test_avatar_falls_back_to_the_initial_and_shows_the_tick(self):
        html = render_to_string("components/avatar.html", {"picture": None, "name": "youssef", "verified": True})
        self.assertRegex(html, r">\s*Y\s*")
        self.assertIn("px-avatar__check", html)
        self.assertNotIn("px-avatar__check", render_to_string("components/avatar.html", {"name": "youssef"}))

    def test_empty_state_action_is_optional(self):
        self.assertNotIn("<a ", render_to_string("components/empty_state.html", {"text": "Nothing here"}))
        html = render_to_string("components/empty_state.html", {"text": "Nothing", "href": "/x/", "cta": "Go"})
        self.assertIn('<a href="/x/" class="px-ghost">Go</a>', html)

    def test_card_head_renders_title_and_optional_action(self):
        html = render_to_string("components/card_head.html", {"icon": "bi-x", "title": "Title"})
        self.assertIn("Title", html)
        self.assertNotIn("<a ", html)
        html = render_to_string("components/card_head.html", {"icon": "bi-x", "title": "T", "href": "/h/", "action": "Edit", "tab": "edit"})
        self.assertIn('href="/h/" class="px-ghost" data-tab-link="edit"', html)
