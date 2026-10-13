"""Admin panel (django-unfold): every page renders, grouped sidebar, KPI dashboard, badges and bulk actions."""
import io
from datetime import timedelta

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone, translation

from internest_core.admin_base import ModelAdmin
from internest_skills.models import StudentFreelanceGig

from .models import Application, Internship, PartnerProfile, StudentProfile


class AdminPanelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_skills", stdout=io.StringIO())
        cls.root = User.objects.create_superuser("root", "root@x.com", "pw")
        cls.student_user = User.objects.create_user("mona", email="mona@x.com", first_name="Mona")
        cls.student = StudentProfile.objects.create(user=cls.student_user, university="Cairo", major="CS", study_level="3",
                                                    personal_email="mona@x.com")
        cls.partner = PartnerProfile.objects.create(user=User.objects.create_user("founder"), company_name="Nile", partner_code="N1")
        cls.gig = Internship.objects.create(partner=cls.partner, title="Designer", description="d", location="Online",
                                            required_majors="any", deadline=timezone.now().date() + timedelta(days=5))
        Application.objects.create(internship=cls.gig, applicant=cls.student_user)
        StudentFreelanceGig.objects.create(student=cls.student, platform="mostaql", link="https://mostaql.com/u/x",
                                           raw_text="x" * 50, status="needs_proof", platform_username="x")

    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.client.force_login(self.root)

    def test_every_registered_model_uses_unfold_and_renders(self):
        for model, model_admin in admin.site._registry.items():
            with self.subTest(model=model.__name__):
                self.assertIsInstance(model_admin, ModelAdmin)
                info = (model._meta.app_label, model._meta.model_name)
                self.assertEqual(self.client.get(reverse("admin:%s_%s_changelist" % info)).status_code, 200)
                obj = model_admin.get_queryset(self._request()).first()
                if obj is not None:
                    self.assertEqual(self.client.get(reverse("admin:%s_%s_change" % info, args=[obj.pk])).status_code, 200)
                if model_admin.has_add_permission(self._request()):
                    try:
                        self.assertEqual(self.client.get(reverse("admin:%s_%s_add" % info)).status_code, 200)
                    except NoReverseMatch:
                        pass

    def _request(self):
        from django.test import RequestFactory
        request = RequestFactory().get("/admin/")
        request.user = self.root
        return request

    def test_dashboard_and_grouped_sidebar(self):
        page = self.client.get(reverse("admin:index"))
        self.assertEqual(page.status_code, 200)
        for marker in ("Platform at a glance", "Review queues", "Freelance work needing proof", "🎓 Students &amp; skills",
                       "🏢 Companies &amp; opportunities", "💼 Freelance import", "📊 Analytics &amp; system", "Audit log",
                       "?status__exact=needs_proof", "img/internest_logo.jpg"):
            self.assertContains(page, marker)
        kpis = {k["title"]: k["value"] for k in page.context["kpis"]}
        self.assertEqual((kpis["Students"], kpis["Open opportunities"]), (1, 1))
        self.assertEqual({q["title"]: q["value"] for q in page.context["queues"]}["Freelance work needing proof"], 1)

    def test_status_badges_in_lists(self):
        page = self.client.get(reverse("admin:internest_skills_studentfreelancegig_changelist"))
        self.assertContains(page, "bg-orange-100")  # amber badge for "Needs proof review"
        self.assertContains(page, "Needs proof review")
        page = self.client.get(reverse("admin:internest_core_internship_changelist"))
        self.assertContains(page, "bg-green-100")
        self.assertContains(page, "Open for applications")

    def test_bulk_actions(self):
        url = reverse("admin:internest_core_studentprofile_changelist")
        self.client.post(url, {"action": "verify_emails", "_selected_action": [self.student.pk]})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_personal_email_verified)
        self.client.post(reverse("admin:internest_core_partnerprofile_changelist"),
                         {"action": "verify_partners", "_selected_action": [self.partner.pk]})
        self.partner.refresh_from_db()
        self.assertTrue(self.partner.is_fully_verified)
        self.client.post(reverse("admin:internest_core_internship_changelist"), {"action": "close_internships", "_selected_action": [self.gig.pk]})
        self.gig.refresh_from_db()
        self.assertFalse(self.gig.is_active)

    def test_audit_log_is_read_only(self):
        self.assertEqual(self.client.get(reverse("admin:admin_logentry_add")).status_code, 403)

    def test_arabic_admin(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(reverse("admin:index"))
        self.assertContains(page, 'dir="rtl"')
        self.assertContains(page, "إدارة الطلاب والمهارات")
