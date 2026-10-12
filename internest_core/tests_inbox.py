"""Company dashboard: active-applicants badge and the opportunity-grouped applicant inbox."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import Application, Internship
from .tests_applications import ApplicationsBase, _student
from .views import active_applications

INBOX = reverse("partner_dashboard_section", args=["applicants"])


class InboxTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        today = timezone.now().date()
        self.closed = Internship.objects.create(partner=self.partner, title="Closed gig", description="d", location="Online",
                                                required_majors="any", deadline=today + timedelta(days=5), is_active=False)
        self.expired = Internship.objects.create(partner=self.partner, title="Expired gig", description="d", location="Online",
                                                 required_majors="any", deadline=today - timedelta(days=1))
        self.empty = Internship.objects.create(partner=self.partner, title="Empty gig", description="d", location="Online",
                                               required_majors="any", deadline=today + timedelta(days=9))
        statuses = [("amr", self.gig, "submitted"), ("sara", self.gig, "under_review"), ("lina", self.role, "shortlisted"),
                    ("omar", self.role, "accepted"), ("nour", self.role, "fulfilled"), ("hana", self.gig, "completed"),
                    ("ziad", self.closed, "submitted"), ("mona", self.expired, "under_review")]
        self.apps = {}
        for name, opp, status in statuses:
            user, _ = _student(name)
            self.apps[name] = Application.objects.create(internship=opp, applicant=user, status=status)
        self.client.force_login(self.founder)

    def test_badge_counts_only_undecided_applicants_on_open_opportunities(self):
        self.assertEqual(set(active_applications(self.partner).values_list("applicant__username", flat=True)), {"amr", "sara", "lina"})
        page = self.client.get(reverse("partner_dashboard"))
        self.assertEqual(page.context["applicant_count"], 3)
        self.assertContains(page, '<span class="dash-card__count">3</span>')
        self.assertContains(page, "Active applicants on your open opportunities")

    def test_inbox_groups_open_opportunities_with_their_own_applicants(self):
        page = self.client.get(INBOX)
        open_titles = [g["internship"].title for g in page.context["open_gigs"]]
        self.assertEqual(set(open_titles), {"Design gig", "Data role", "Empty gig"})
        self.assertEqual({g["internship"].title for g in page.context["archived_gigs"]}, {"Closed gig", "Expired gig"})
        design = next(g for g in page.context["open_gigs"] if g["internship"] == self.gig)
        self.assertEqual(({a.applicant.username for a in design["applications"]}, design["total"], design["active"]),
                         ({"amr", "sara", "hana"}, 3, 2))
        self.assertIn(page.context["selected_gig"], {str(self.gig.pk), str(self.role.pk)})  # one with undecided applicants
        self.assertContains(page, f'data-gig-panel="{self.empty.pk}"')
        self.assertContains(page, "No applicants for this opportunity yet")
        self.assertNotContains(page, "Closed gig")  # archived opportunities live in their own tab
        self.assertEqual(len(page.context["received_applicants"]), 8)

    def test_selected_gig_and_archive_tab(self):
        page = self.client.get(INBOX, {"gig": self.role.pk})
        self.assertEqual(page.context["selected_gig"], str(self.role.pk))
        self.assertContains(page, f'data-gig-panel="{self.role.pk}" role="tabpanel" >', html=False)
        archive = self.client.get(INBOX, {"view": "archive"})
        self.assertEqual(archive.context["inbox_view"], "archive")
        for marker in ("Closed gig", "Expired gig", "ix-state--closed", "ix-state--expired"):
            self.assertContains(archive, marker)

    def test_quick_actions(self):
        page = self.client.get(INBOX, {"gig": self.role.pk})
        self.assertContains(page, reverse("partner_applicant", args=[self.apps["lina"].pk]))
        self.assertContains(page, "View profile & CV")
        self.assertContains(page, f'href="mailto:{self.apps["lina"].applicant.studentprofile.personal_email}"')  # shortlisted: unlocked
        page = self.client.get(INBOX, {"gig": self.gig.pk})
        self.assertNotContains(page, f'mailto:{self.apps["amr"].applicant.studentprofile.personal_email}')  # still locked

    def test_status_change_from_inbox_returns_to_inbox(self):
        app = self.apps["amr"]
        target = f"{INBOX}?gig={self.gig.pk}"
        resp = self.client.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "shortlist", "next": target})
        self.assertRedirects(resp, target, fetch_redirect_response=False)
        app.refresh_from_db()
        self.assertEqual(app.status, Application.STATUS_SHORTLISTED)
        resp = self.client.post(reverse("partner_applicant_decide", args=[app.pk]), {"decision": "accept", "next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("partner_applicant", args=[app.pk]), fetch_redirect_response=False)

    def test_arabic_labels(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(INBOX)
        for text in ("الأرشيف / الفرص المغلقة", "لا يوجد متقدمون لهذه الفرصة حتى الآن", "عرض الملف والـ CV", "تغيير الحالة", "إرسال رسالة"):
            self.assertContains(page, text)
