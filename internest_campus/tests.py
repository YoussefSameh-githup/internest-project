from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from internest_core.models import PartnerProfile, StudentProfile

from .forms import CampusVoteForm
from .models import CampusDemandVote

URL = "/universities/vote/"
DUPLICATE_EN = "Sorry, a vote has already been recorded with this student ID or account for this faculty."
VALID = {"university_name": "Ain Shams University", "faculty_name": "Computer Science",
         "department": "Information Systems", "student_number": "20210345"}


def _student(username, **profile):
    user = User.objects.create_user(username, password="pw")
    StudentProfile.objects.create(user=user, **profile)
    return user


class CampusVoteTests(TestCase):
    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.alice = _student("alice", university="Ain Shams University", major="Information Systems")
        self.bob = _student("bob")

    def vote(self, user, **overrides):
        self.client.force_login(user)
        return self.client.post(URL, {**VALID, **overrides})

    # --- form validation ---------------------------------------------------
    def test_form_validation(self):
        form = CampusVoteForm(data={**VALID, "student_number": "12", "faculty_name": " "})
        self.assertFalse(form.is_valid())
        self.assertIn("student_number", form.errors)
        self.assertIn("faculty_name", form.errors)
        self.assertFalse(CampusVoteForm(data={**VALID, "department": ""}).is_valid())
        form = CampusVoteForm(data={**VALID, "student_number": " ab-123 "})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["student_number"], "AB-123")

    # --- vote recording & card -------------------------------------------
    def test_vote_is_recorded_and_card_is_shown(self):
        resp = self.vote(self.alice)
        vote = CampusDemandVote.objects.get()
        self.assertEqual(vote.student.user, self.alice)
        self.assertRedirects(resp, f"{URL}?card={vote.referral_code}", fetch_redirect_response=False)
        page = self.client.get(resp.url)
        self.assertContains(page, "Student #1 demanding Internest at Ain Shams University — Computer Science!")
        self.assertContains(page, "20••••45")
        self.assertNotContains(page, "20210345")  # full ID never shown
        self.assertContains(page, "https://wa.me/?text=")
        self.assertContains(page, "https://t.me/share/url?")
        self.assertContains(page, "1</strong> / 500")

    def test_guest_cannot_vote_and_is_sent_to_login(self):
        resp = self.client.post(URL, VALID)
        self.assertTrue(resp.url.startswith(reverse("login")))
        self.assertFalse(CampusDemandVote.objects.exists())
        self.assertContains(self.client.get(URL + "?open=1"), "Log in to vote")

    def test_partner_cannot_vote(self):
        user = User.objects.create_user("acme", password="pw")
        PartnerProfile.objects.create(user=user, company_name="Acme", partner_code="A1")
        self.vote(user)
        self.assertFalse(CampusDemandVote.objects.exists())

    # --- duplicate prevention --------------------------------------------
    def test_one_vote_per_student_account_anywhere(self):
        self.vote(self.alice)
        for overrides in ({"university_name": "  ain   SHAMS university ", "student_number": "99999999"},
                          {"university_name": "Cairo University", "student_number": "99999999"}):
            resp = self.vote(self.alice, **overrides)
            self.assertContains(resp, DUPLICATE_EN)
        self.assertEqual(CampusDemandVote.objects.count(), 1)

    def test_student_number_cannot_be_reused_in_same_university(self):
        self.vote(self.alice)
        resp = self.vote(self.bob, faculty_name="Engineering")  # other account, same ID, same university
        self.assertContains(resp, DUPLICATE_EN)
        self.assertEqual(CampusDemandVote.objects.count(), 1)

    def test_duplicate_message_in_arabic(self):
        self.vote(self.alice)
        self.client.cookies["internest_lang"] = "ar"
        resp = self.vote(self.alice)
        self.assertContains(resp, "عفواً، تم تسجيل صوت بهذا الرقم الجامعي أو الحساب من قبل في هذه الكلية.")

    def test_database_enforces_one_vote_per_account(self):
        from django.db import IntegrityError, transaction
        self.vote(self.alice)
        first = CampusDemandVote.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusDemandVote.objects.create(student=first.student, university_name="Cairo University",
                                            faculty_name="Law", department="Law", student_number="11112222")

    # --- referral links ---------------------------------------------------
    def test_referral_link_generation_and_tracking(self):
        self.vote(self.alice)
        first = CampusDemandVote.objects.get()
        ref = first.share_ref
        self.assertRegex(ref, r"^[0-9A-F]{6}-\w+$")
        self.assertNotIn(first.student_number, ref)

        page = self.client.get(f"{URL}?card={first.referral_code}")
        link = page.context["share"]["link"]
        self.assertEqual(parse_qs(urlparse(link).query)["ref"], [ref])

        self.client.force_login(self.bob)
        page = self.client.get(link)
        self.assertContains(page, "data-autoopen")
        self.assertEqual(page.context["form"].initial["faculty_name"], "Computer Science")
        self.assertContains(page, "Join the campaign at Ain Shams University — Computer Science")

        self.client.post(URL, {**VALID, "student_number": "20219999", "ref": ref})
        second = CampusDemandVote.objects.get(student__user=self.bob)
        self.assertEqual(second.referred_by, first)
        self.assertEqual(second.voter_number, 2)
        self.assertEqual(second.campus_votes, 2)

    def test_logged_in_student_form_is_prefilled_from_profile(self):
        self.client.force_login(self.alice)
        initial = self.client.get(URL).context["form"].initial
        self.assertEqual((initial["university_name"], initial["department"]), ("Ain Shams University", "Information Systems"))

    def test_cta_only_on_student_dashboard(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("profile")), f"{URL}?open=1")
        self.client.logout()  # also clears cookies
        for lang, text in (("en", "Bring Internest to My Campus"), ("ar", "طالب بنسخة تجريبية لكليتك")):
            self.client.cookies["internest_lang"] = lang
            landing = self.client.get(reverse("landing"))
            self.assertNotContains(landing, text)
            self.assertNotContains(landing, URL)
            # Guests opening a shared link get the login prompt, not the CTA button.
            self.assertNotContains(self.client.get(URL), 'data-campus-open="vote-dialog"')

    def test_startup_does_not_see_cta(self):
        user = User.objects.create_user("acme2", password="pw")
        PartnerProfile.objects.create(user=user, company_name="Acme2", partner_code="A2", is_academic=True)
        self.client.force_login(user)
        self.assertNotContains(self.client.get(URL), 'data-campus-open="vote-dialog"')

    def test_arabic_button_text_on_student_profile(self):
        self.client.force_login(self.alice)
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("profile")), "طالب بنسخة تجريبية لكليتك")
