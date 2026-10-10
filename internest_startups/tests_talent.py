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
class PoolBase(ApplicationsBase):
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



class TalentPoolTests(PoolBase):
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


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class InvitationBypassAndPreviewTests(PoolBase):
    def setUp(self):
        super().setUp()
        self.gig.required_skills.set([Skill.objects.get(slug="legal-research")])  # amr has none of these
        self._pro()

    def _invite(self, student=None):
        self.client.post(reverse("talent_pool_invite", args=[(student or self.amr).pk]), {"internship": self.gig.pk})
        return TalentInvitation.objects.get(student=student or self.amr, internship=self.gig)

    def test_invite_token_bypasses_skill_gate(self):
        inv = self._invite()
        self.assertIn(f"invite_token={inv.token}", mail.outbox[-1].body)
        self.login_student(self.amr_user)
        apply_url = reverse("apply", args=[self.gig.pk])

        self.client.post(apply_url, CONSENT)  # no token: still locked by the 80% match
        self.assertFalse(Application.objects.exists())
        detail = self.client.get(inv.apply_url)
        self.assertContains(detail, "invited you, so the skill match requirement is waived")
        self.assertContains(detail, f"{apply_url}?invite_token={inv.token}")
        page = self.client.get(f"{apply_url}?invite_token={inv.token}")
        self.assertContains(page, f'name="invite_token" value="{inv.token}"')
        self.client.post(apply_url, {**CONSENT, "invite_token": str(inv.token)})

        app = Application.objects.get()
        self.assertEqual(app.source, Application.SOURCE_INVITED)
        inv.refresh_from_db()
        self.assertEqual(inv.status, TalentInvitation.STATUS_ACCEPTED)
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(reverse("partner_applicant", args=[app.pk])), "🎟️ Invited")
        self.assertContains(self.client.get(reverse("partner_dashboard_section", args=["applicants"])), "🎟️ Invited")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("partner_applicant", args=[app.pk])), "🎟️ بدعوة خاصة (Invited)")

    def test_token_only_works_for_the_invited_student(self):
        inv = self._invite()
        self.login_student(self.sara_user)  # someone else's token
        self.client.post(reverse("apply", args=[self.gig.pk]), {**CONSENT, "invite_token": str(inv.token)})
        self.login_student(self.amr_user)
        for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            self.client.post(reverse("apply", args=[self.gig.pk]), {**CONSENT, "invite_token": bad})
        self.assertFalse(Application.objects.exists())

    def test_regular_applications_are_direct(self):
        self.gig.required_skills.clear()
        self.login_student(self.amr_user)
        self.client.post(reverse("apply", args=[self.gig.pk]), CONSENT)
        self.assertEqual(Application.objects.get().source, Application.SOURCE_DIRECT)

    def test_three_invitations_per_opportunity(self):
        for i in range(3):
            u, p = _student(f"extra{i}")
            TalentInvitation.objects.create(partner=self.partner, student=p, internship=self.gig)
        resp = self.client.post(reverse("talent_pool_invite", args=[self.amr.pk]), {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "at most 3 invitations")
        self.assertFalse(TalentInvitation.objects.filter(student=self.amr).exists())
        self.assertContains(self.client.get(URL), "· 3/3</option>")

    def test_profile_preview_hides_contacts(self):
        type(self.amr).objects.filter(pk=self.amr.pk).update(
            linkedin_url="https://www.linkedin.com/in/amr-secret", portfolio_url="https://github.com/amr-dev")
        self.assertContains(self.client.get(URL), reverse("talent_pool_candidate", args=[self.amr.pk]))
        resp = self.client.get(reverse("talent_pool_candidate", args=[self.amr.pk]))
        for shown in ("Built a stock dashboard in Django", "https://github.com/amr-dev", "Python", "95%", "P70",
                      "Cairo University", "Computer Science", "Invite to apply / contact"):
            self.assertContains(resp, shown)
        for secret in ("amr@uni.edu", "01012345678", "amr-secret", "cvs/"):
            self.assertNotContains(resp, secret)
        self.assertEqual(self.client.get(reverse("talent_pool_candidate", args=[self.hidden.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("talent_pool_candidate", args=[self.weak.pk])).status_code, 404)

    def test_preview_is_pro_only(self):
        from internest_skills.models import EmployerSubscription

        EmployerSubscription.objects.filter(partner=self.partner).update(plan=EmployerSubscription.PLAN_FREE)
        resp = self.client.get(reverse("talent_pool_candidate", args=[self.amr.pk]))
        self.assertRedirects(resp, reverse("startup_upgrade"), fetch_redirect_response=False)


class ProfileRedesignTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.user, self.profile = _student("nour")
        self.login_student(self.user)

    def test_sections_dropzones_and_pool_switch(self):
        page = self.client.get(reverse("profile"))
        for marker in ("Personal and university details", "Skills and professional links", "Attachments (photo and CV)",
                       "data-dropzone", 'name="cv_file"', 'name="profile_picture"', "Max 5 MB", 'class="pf-switch"',
                       "Your phone, email and CV are shared only if you apply"):
            self.assertContains(page, marker)

    def test_cv_upload_through_dropzone_field(self):
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        data = {"personal_email": "nour@uni.edu", "university": "Cairo", "major": "CS", "study_level": "3",
                "phone_number": "01000000000", "linkedin_url": "", "bio": "",
                "cv_file": SimpleUploadedFile("nour.pdf", b"%PDF-1.4", content_type="application/pdf")}
        with override_settings(MEDIA_ROOT=media):
            self.client.post(reverse("profile"), data)
            self.profile.refresh_from_db()
            self.assertTrue(self.profile.cv_file.name.startswith("cvs/"))
            self.assertContains(self.client.get(reverse("profile")), 'name="cv_file-clear"')
