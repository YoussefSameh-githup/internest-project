"""Free vs Pro: every row of the comparison table on /startups/upgrade/ is enforced in code.

| Feature                               | Free                         | Pro                         |
|---------------------------------------|------------------------------|-----------------------------|
| Opportunity posts                     | 1 per month                  | Unlimited                   |
| Applicant skills                      | Verified skill names only    | + scores and percentiles    |
| Shortlist a candidate for interview   | Yes                          | Yes                         |
| Talent Pool search + profile preview  | Locked                       | Yes                         |
| Direct invitations (skip 70% gate)    | 0                            | 3 per opportunity           |
| Pro badge + priority in listings      | No                           | Yes                         |
| Founders Network posts                | 3 per hour                   | Unlimited                   |
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, Internship, PartnerInternshipSubmission, PartnerProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_lounge.moderation import POSTS_PER_HOUR
from internest_skills.models import EmployerSubscription, Skill, StudentSkill

from .talent import INVITES_PER_OPPORTUNITY
from .tiers import FREE_POSTS_PER_MONTH, activate_pro, is_pro, monthly_quota_reached


class PlanMatrixTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.founder)
        self.student_user, self.student = _student("amr")
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit",
                                    status=StudentSkill.STATUS_VERIFIED, verified_via=StudentSkill.VIA_CHALLENGE, score=91, percentile=87)
        self.app = Application.objects.create(internship=self.gig, applicant=self.student_user)

    def _go_pro(self):
        activate_pro(self.partner, 1)
        self.partner.refresh_from_db()
        self.assertTrue(is_pro(self.partner))

    def _post_once(self):
        PartnerInternshipSubmission.objects.create(partner=self.partner, title="T", description="d", location="Cairo",
                                                   required_majors="any", deadline=timezone.now().date() + timedelta(days=9))

    # 1 · posts per month
    def test_free_gets_one_post_per_month_pro_unlimited(self):
        self.assertEqual(FREE_POSTS_PER_MONTH, 1)
        self.assertFalse(monthly_quota_reached(self.partner))
        self._post_once()
        self.assertTrue(monthly_quota_reached(self.partner))
        resp = self.client.get(reverse("partner_submit_internship"))
        self.assertRedirects(resp, reverse("partner_dashboard") + "?upsell=limit", fetch_redirect_response=False)
        self._go_pro()
        self._post_once()
        self.assertFalse(monthly_quota_reached(self.partner))
        self.assertEqual(self.client.get(reverse("partner_submit_internship")).status_code, 200)

    # 2 · scores and percentiles
    def test_free_sees_verified_skill_names_only_pro_sees_scores(self):
        url = reverse("partner_applicant", args=[self.app.pk])
        page = self.client.get(url)
        self.assertContains(page, "Python")
        self.assertNotContains(page, "91% · P87")
        self.assertContains(page, "Upgrade to see the detailed score report")
        self.assertNotContains(page, reverse("skills_student_report", args=[self.student.pk]))
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[self.student.pk])).status_code, 403)
        self._go_pro()
        page = self.client.get(url)
        self.assertContains(page, "91% · P87")
        self.assertContains(page, reverse("skills_student_report", args=[self.student.pk]))
        self.assertEqual(self.client.get(reverse("skills_student_report", args=[self.student.pk])).status_code, 200)

    def test_inbox_upsell_matches_the_table(self):
        page = self.client.get(reverse("partner_dashboard_section", args=["applicants"]))
        self.assertContains(page, "Detailed scores and percentiles on Internest Pro")
        self.assertNotContains(page, "Verified skills available on Internest Pro")

    # 3 · shortlist for interview — both plans
    def test_shortlist_works_on_free_and_pro(self):
        self.client.post(reverse("partner_applicant_decide", args=[self.app.pk]), {"decision": "shortlist"})
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_SHORTLISTED)

    # 4 · Talent Pool
    def test_talent_pool_locked_on_free_open_on_pro(self):
        self.assertRedirects(self.client.get(reverse("talent_pool")), reverse("startup_upgrade"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("talent_pool_candidate", args=[self.student.pk])), reverse("startup_upgrade"),
                             fetch_redirect_response=False)
        self._go_pro()
        self.assertEqual(self.client.get(reverse("talent_pool")).status_code, 200)

    # 5 · direct invitations
    def test_invitations_zero_on_free_three_per_opportunity_on_pro(self):
        self.assertEqual(INVITES_PER_OPPORTUNITY, 3)
        self.student.talent_pool_visible = True
        self.student.save(update_fields=["talent_pool_visible"])
        invite = reverse("talent_pool_invite", args=[self.student.pk])
        self.client.post(invite, {"internship": self.role.pk})
        self.assertFalse(self.role.talent_invitations.exists())  # Free: none
        self._go_pro()
        for name in ("s1", "s2", "s3", "s4"):
            _u, other = _student(name)
            other.talent_pool_visible = True
            other.save(update_fields=["talent_pool_visible"])
            StudentSkill.objects.create(student=other, skill=Skill.objects.get(slug="python"), source="explicit",
                                        status=StudentSkill.STATUS_VERIFIED, score=80)
            self.client.post(reverse("talent_pool_invite", args=[other.pk]), {"internship": self.role.pk})
        self.assertEqual(self.role.talent_invitations.count(), 3)

    # 6 · Pro badge + priority
    def test_pro_badge_and_priority_in_listings(self):
        other_user, _ = _student("viewer")
        rival_user = type(self.founder).objects.create_user("rival", password="pw")
        rival = PartnerProfile.objects.create(user=rival_user, company_name="Rival", partner_code="R1", is_fully_verified=True)
        newer = Internship.objects.create(partner=rival, title="Newer free gig", description="d", location="Cairo", required_majors="any",
                                          deadline=timezone.now().date() + timedelta(days=60))
        self.login_student(other_user)
        first = self.client.get(reverse("list")).context["internships"][0]
        self.assertEqual(first.pk, newer.pk)  # latest deadline first while nobody is Pro
        self._go_pro()
        page = self.client.get(reverse("list"))
        self.assertEqual(page.context["internships"][0].partner_id, self.partner.pk)
        self.assertContains(page, "ol-card--pro")

    # 7 · Founders Network posts
    def test_founders_network_post_limit(self):
        self.assertEqual(POSTS_PER_HOUR, 3)  # Pro bypass is covered in internest_lounge.tests

    # Pro ends when the subscription expires
    def test_expired_pro_falls_back_to_free(self):
        self._go_pro()
        EmployerSubscription.objects.filter(partner=self.partner).update(valid_until=timezone.now().date() - timedelta(days=1))
        self.partner = PartnerProfile.objects.get(pk=self.partner.pk)
        self.assertFalse(is_pro(self.partner))
        self.assertRedirects(self.client.get(reverse("talent_pool")), reverse("startup_upgrade"), fetch_redirect_response=False)
