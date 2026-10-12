"""Freelance import ownership: usernames parsed from platform links and cross-matched with the student's identity."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse

from internest_students.models import StudentCV

from .freelance_service import ownership_check, student_identifiers
from .models import StudentFreelanceGig
from .tests import Base
from .utils import OWNER_MATCH_THRESHOLD, best_owner_match, extract_platform_username, handle_match_score

REVIEW = "Designed a full brand identity and social media kit in Photoshop and Illustrator. Client: great work 5/5"
WARNING_EN = "the username in the platform link doesn"  # banner is raw, flash message escapes the apostrophe
WARNING_AR = "تنبيه: اسم المستخدم في رابط المنصة لا يطابق اسم صاحب الحساب الحالي بشكل مباشر"


class ExtractUsernameTests(SimpleTestCase):
    def test_profile_links(self):
        cases = {
            "https://mostaql.com/u/youssef_sameh": "youssef_sameh",
            "mostaql.com/u/YoussefSameh/portfolio": "youssefsameh",
            "https://khamsat.com/user/ysameh": "ysameh",
            "https://www.upwork.com/freelancers/youssefsameh?viewMode=1": "youssefsameh",
            "https://www.fiverr.com/youssef_design/design-a-modern-logo?context_referrer=x": "youssef_design",
            "https://github.com/YoussefSameh-githup/internest-project": "youssefsameh-githup",
            "https://eg.linkedin.com/in/youssef-sameh-448719256/": "youssef-sameh-448719256",
            "http://github.com/@octocat": "octocat",
        }
        for url, handle in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_platform_username(url), handle)

    def test_links_without_a_username(self):
        for url in ("https://mostaql.com/portfolio/123-logo", "https://khamsat.com/design/12345-logo",
                    "https://www.upwork.com/freelancers/~01a2b3c4d5", "https://www.fiverr.com/categories/graphics-design",
                    "https://github.com/orgs/django", "https://github.com/", "https://evil.example/u/youssef",
                    "https://notmostaql.com/u/youssef", "", None, 12):
            with self.subTest(url=url):
                self.assertIsNone(extract_platform_username(url))


class MatchScoreTests(SimpleTestCase):
    def test_matching_handles(self):
        for handle in ("youssefsameh", "youssef_sameh", "sameh-youssef", "ysameh", "youssefs", "youssef.sameh.dev",
                       "youssefsameh95", "Youssef-Sameh-448719256"):
            with self.subTest(handle=handle):
                self.assertGreaterEqual(handle_match_score(handle, "Youssef Sameh"), OWNER_MATCH_THRESHOLD)

    def test_mismatched_handles(self):
        for handle in ("ahmed_ali", "designpro", "youssefdesigns", "ys", "omarsameh_x"):
            with self.subTest(handle=handle):
                self.assertLess(handle_match_score(handle, "Youssef Sameh"), OWNER_MATCH_THRESHOLD)

    def test_best_match_uses_any_identifier(self):
        self.assertGreaterEqual(best_owner_match("ysdev", ["Youssef Sameh", "ysdev@gmail.com".split("@")[0]]), OWNER_MATCH_THRESHOLD)
        self.assertEqual(best_owner_match("anyone", []), 0.0)


class OwnershipFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.student_user.first_name, self.student_user.last_name = "Youssef", "Sameh"
        self.student_user.email = "y.sameh@gmail.com"
        self.student_user.save()
        self.client.force_login(self.student_user)

    def _post(self, link, **extra):
        data = {"platform": "mostaql", "link": link, "raw_text": REVIEW, **extra}
        return self.client.post(reverse("freelance_importer"), data, follow=True)

    def test_identifiers_include_linkedin_and_cv_github(self):
        type(self.student).objects.filter(pk=self.student.pk).update(linkedin_url="https://linkedin.com/in/joe-sam-1")
        self.student.refresh_from_db()
        StudentCV.objects.create(student=self.student, data={"github": "https://github.com/jsam-codes"})
        ids = student_identifiers(self.student)
        for expected in ("Youssef Sameh", "stud", "y.sameh", "joe-sam-1", "jsam-codes"):
            self.assertIn(expected, ids)
        self.assertEqual(ownership_check(self.student, "https://khamsat.com/user/jsam-codes"), ("jsam-codes", True))

    def test_matching_link_is_flagged_owner_matched(self):
        resp = self._post("https://mostaql.com/u/youssef_sameh")
        gig = StudentFreelanceGig.objects.get()
        self.assertEqual((gig.platform_username, gig.owner_matched, gig.status), ("youssef_sameh", True, "pending"))
        self.assertNotContains(resp, WARNING_EN)
        self.assertContains(resp, "Account matches you")

    def test_mismatch_without_proof_shows_soft_warning_first(self):
        resp = self._post("https://mostaql.com/u/ahmed_designs")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, WARNING_EN)
        self.assertContains(resp, 'name="confirm_unmatched"')
        self.assertFalse(StudentFreelanceGig.objects.exists())  # nothing saved until they confirm or add proof

        resp = self._post("https://mostaql.com/u/ahmed_designs", confirm_unmatched="1")
        gig = StudentFreelanceGig.objects.get()
        self.assertEqual((gig.owner_matched, gig.status), (False, "needs_proof"))
        self.assertContains(resp, WARNING_EN)  # repeated as a flash message
        self.assertContains(resp, "Needs proof review")

    def test_mismatch_with_screenshot_imports_for_proof_review(self):
        proof = SimpleUploadedFile("proof.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png")
        self._post("https://mostaql.com/u/ahmed_designs", proof=proof)
        gig = StudentFreelanceGig.objects.get()
        self.assertEqual(gig.status, "needs_proof")
        gig.proof.delete(save=False)

    def test_links_without_username_skip_the_check(self):
        resp = self._post("https://mostaql.com/portfolio/77-logo")
        gig = StudentFreelanceGig.objects.get()
        self.assertEqual((gig.platform_username, gig.owner_matched, gig.status), ("", False, "pending"))
        self.assertNotContains(resp, WARNING_EN)

    def test_arabic_warning_text(self):
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self._post("https://mostaql.com/u/ahmed_designs"), WARNING_AR)
