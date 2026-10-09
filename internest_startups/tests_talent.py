"""Talent Pool: Pro-only access, opt-in candidates, filters, privacy and invitations."""
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.urls import reverse

from internest_core.models import Application
from internest_core.tests_applications import CONSENT, ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill

from .models import TalentInvitation
from .tiers import activate_pro

URL = reverse("talent_pool")


def _skill(student, slug, status=StudentSkill.STATUS_VERIFIED, score=90):
    return StudentSkill.objects.create(student=student, skill=Skill.objects.get(slug=slug), source="explicit",
                                       status=status, score=score, percentile=70)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TalentPoolTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.amr_user, self.amr = _student("amr")
        StudentProfile = type(self.amr)
        StudentProfile.objects.filter(pk=self.amr.pk).update(
            talent_pool_visible=True, phone_number="01012345678", university="Cairo University", major="Computer Science",
            bio="Built a stock dashboard in Django")
        _skill(self.amr, "python", score=95)
        self.sara_user, self.sara = _student("sara")
        StudentProfile.objects.filter(pk=self.sara.pk).update(talent_pool_visible=True, university="Ain Shams", major="Finance")
        _skill(self.sara, "financial-analysis", status=StudentSkill.STATUS_LAG, score=82)  # not verified but ≥ 80
        hidden_user, self.hidden = _student("hidden")  # verified but did not opt in
        _skill(self.hidden, "python")
        weak_user, self.weak = _student("weak")  # opted in, only a claimed/low skill
        StudentProfile.objects.filter(pk=self.weak.pk).update(talent_pool_visible=True)
        _skill(self.weak, "excel", status=StudentSkill.STATUS_CLAIMED, score=None)

    def _pro(self):
        activate_pro(self.partner)
        self.client.force_login(self.founder)

    def _names(self, resp):
        return {c.user.username for c in resp.context["page"]}

    def test_free_startup_redirected_to_upgrade_with_message(self):
        self.client.force_login(self.founder)
        resp = self.client.get(URL, follow=True)
        self.assertEqual(resp.redirect_chain[-1][0], reverse("startup_upgrade"))
        self.assertContains(resp, "Upgrade to Internest Pro to access the Talent Pool.")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(URL, follow=True), "ترقية الحساب لباقة Pro مطلوبة للوصول لقاعدة بيانات المواهب")
        self.assertEqual(self.client.post(reverse("talent_pool_invite", args=[self.amr.pk])).status_code, 302)
        self.assertFalse(TalentInvitation.objects.exists())

    def test_students_and_anonymous_cannot_access(self):
        self.login_student(self.amr_user)
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_pool_shows_only_opted_in_top_profiles(self):
        self._pro()
        resp = self.client.get(URL)
        self.assertEqual(self._names(resp), {"amr", "sara"})
        self.assertEqual(resp.context["page"][0].user.username, "amr")  # best score first
        self.assertContains(resp, "Python · 95%")
        self.assertContains(resp, "Cairo University")

    def test_filters(self):
        self._pro()
        python = Skill.objects.get(slug="python").pk
        self.assertEqual(self._names(self.client.get(URL, {"skill": python})), {"amr"})
        self.assertEqual(self._names(self.client.get(URL, {"min_score": "90"})), {"amr"})
        self.assertEqual(self._names(self.client.get(URL, {"university": "Ain Shams"})), {"sara"})
        self.assertEqual(self._names(self.client.get(URL, {"major": "Computer Science"})), {"amr"})
        self.assertEqual(self._names(self.client.get(URL, {"q": "dashboard"})), {"amr"})
        self.assertEqual(self._names(self.client.get(URL, {"q": "financial"})), {"sara"})
        self.assertEqual(self._names(self.client.get(URL, {"skill": python, "min_score": "99"})), set())
        resp = self.client.get(URL)
        self.assertIn("Ain Shams", resp.context["options"]["universities"])
        self.assertEqual({s.slug for s in resp.context["options"]["skills"]}, {"python", "financial-analysis"})

    def test_contact_details_never_shown(self):
        self._pro()
        resp = self.client.get(URL)
        for secret in ("amr@uni.edu", "01012345678", "sara@uni.edu"):
            self.assertNotContains(resp, secret)
        self.assertContains(resp, "Invite to apply / contact")

    def test_invite_sends_email_and_shows_to_student(self):
        self._pro()
        resp = self.client.post(reverse("talent_pool_invite", args=[self.amr.pk]),
                                {"internship": self.gig.pk, "message": "Loved your Django dashboard"}, follow=True)
        self.assertContains(resp, "Invitation sent.")
        inv = TalentInvitation.objects.get()
        self.assertEqual((inv.student, inv.internship, inv.message), (self.amr, self.gig, "Loved your Django dashboard"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["amr@uni.edu"])
        self.assertIn(reverse("internship_detail", args=[self.gig.pk]), mail.outbox[0].body)
        self.assertContains(self.client.get(URL), "Invited")

        resp = self.client.post(reverse("talent_pool_invite", args=[self.amr.pk]), {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "already invited")
        self.assertEqual(TalentInvitation.objects.count(), 1)

        self.login_student(self.amr_user)
        page = self.client.get(reverse("my_applications"))
        self.assertContains(page, "Startups invited you to apply")
        self.assertContains(page, "Loved your Django dashboard")
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        self.assertTrue(Application.objects.filter(applicant=self.amr_user).exists())
        self.assertNotContains(self.client.get(reverse("my_applications")), "Startups invited you to apply")

    def test_invite_validation(self):
        self._pro()
        other = type(self.gig).objects.create(partner=type(self.partner).objects.create(
            user=type(self.founder).objects.create_user("x"), company_name="X", partner_code="X1"),
            title="Theirs", description="d", location="Online", required_majors="any", deadline=self.gig.deadline)
        self.client.post(reverse("talent_pool_invite", args=[self.amr.pk]), {"internship": other.pk})
        self.assertEqual(self.client.post(reverse("talent_pool_invite", args=[self.hidden.pk]), {"internship": self.gig.pk}).status_code, 404)
        self.assertEqual(self.client.post(reverse("talent_pool_invite", args=[self.weak.pk]), {"internship": self.gig.pk}).status_code, 404)
        self.assertFalse(TalentInvitation.objects.exists())
        with mock.patch("internest_startups.talent.DAILY_INVITATIONS", 1):
            self.client.post(reverse("talent_pool_invite", args=[self.amr.pk]), {"internship": self.gig.pk})
            resp = self.client.post(reverse("talent_pool_invite", args=[self.sara.pk]), {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "today&#x27;s limit")
        self.assertEqual(TalentInvitation.objects.count(), 1)

    def test_dashboard_card_and_profile_opt_in(self):
        self._pro()
        self.assertContains(self.client.get(reverse("partner_dashboard")), URL)
        self.login_student(self.amr_user)
        page = self.client.get(reverse("profile"))
        self.assertContains(page, 'name="talent_pool_visible"')
        self.assertContains(page, "never your phone, email or CV")


class TalentPoolProfileSettingTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.profile = _student("lina")
        self.login_student(self.user)

    def _save(self, **extra):
        data = {"personal_email": "lina@uni.edu", "university": "Cairo", "major": "CS", "study_level": "3",
                "phone_number": "01000000000", "linkedin_url": "", "bio": "", **extra}
        return self.client.post(reverse("profile"), data)

    def test_checkbox_saves_both_ways(self):
        self.assertRedirects(self._save(talent_pool_visible="on"), reverse("profile"), fetch_redirect_response=False)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.talent_pool_visible)
        self._save()  # unchecked boxes are simply absent from the POST
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.talent_pool_visible)

    def test_checkbox_visible_to_all_with_eligibility_hint(self):
        page = self.client.get(reverse("profile"))
        self.assertContains(page, 'name="talent_pool_visible"')
        self.assertContains(page, "A verified skill or a score of 80%+ is required to appear in the talent pool.")
        _skill(self.profile, "python")
        page = self.client.get(reverse("profile"))
        self.assertContains(page, 'name="talent_pool_visible"')
        self.assertNotContains(page, "A verified skill or a score of 80%+ is required")
        self._save(talent_pool_visible="on")
        self.assertContains(self.client.get(reverse("profile")), "Visible to Pro startups")
