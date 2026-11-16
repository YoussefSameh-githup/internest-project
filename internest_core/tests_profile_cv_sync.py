"""CV Builder education is the single source of truth for StudentProfile.university / major, plus the avatar lightbox."""
import importlib

from django.apps import apps
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from internest_core import student_hub
from internest_skills.tests import Base
from internest_students.cv_builder_service import primary_education
from internest_students.models import StudentCV

AIN = {"university": "Ain Shams University", "major": "Cybersecurity", "start_date": "2023-09", "end_date": "", "is_current": True}
CAIRO = {"university": "Cairo University", "major": "Statistics", "start_date": "2018-09", "end_date": "2022-06", "is_current": False}
GUC = {"university": "GUC", "major": "Business", "start_date": "2022-09", "end_date": "2027", "is_current": False}


class PrimaryEducationTests(SimpleTestCase):
    def test_current_entry_wins_over_a_later_end_date(self):
        self.assertEqual(primary_education([GUC, AIN, CAIRO]), AIN)

    def test_latest_end_date_wins_when_nothing_is_current(self):
        self.assertEqual(primary_education([CAIRO, GUC]), GUC)
        self.assertEqual(primary_education([GUC, CAIRO]), GUC)

    def test_bare_year_and_missing_dates(self):
        self.assertEqual(primary_education([{"university": "A", "end_date": "2024"}, {"university": "B", "end_date": "2024-05"}])["university"], "A")
        self.assertEqual(primary_education([{"university": "A"}, {"university": "B", "start_date": "2020-01"}])["university"], "B")

    def test_first_row_wins_a_tie(self):
        self.assertEqual(primary_education([{"university": "A", "end_date": "2025-06"}, {"university": "B", "end_date": "2025-06"}])["university"], "A")

    def test_nothing_usable(self):
        for value in (None, [], [{}], [{"university": "", "major": ""}], ["junk"]):
            self.assertIsNone(primary_education(value))


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)  # login resets email verification, so verify afterwards
        type(self.student).objects.filter(pk=self.student.pk).update(
            university="zagazig", major="business", personal_email="y@x.com", personal_email_verified_at=timezone.now())

    def _profile(self):
        self.student.refresh_from_db()
        return self.student.university, self.student.major

    def test_creating_a_cv_education_updates_the_profile(self):
        StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        self.assertEqual(self._profile(), ("Ain Shams University", "Cybersecurity"))

    def test_editing_and_deleting_entries_follow_the_primary_one(self):
        record = StudentCV.objects.create(student=self.student, data={"education": [CAIRO, AIN]})
        self.assertEqual(self._profile()[0], "Ain Shams University")
        record.data = {"education": [CAIRO, {**AIN, "major": "Network Security"}]}
        record.save(update_fields=["data", "updated_at"])
        self.assertEqual(self._profile(), ("Ain Shams University", "Network Security"))
        record.data = {"education": [CAIRO]}  # the current entry was deleted
        record.save(update_fields=["data", "updated_at"])
        self.assertEqual(self._profile(), ("Cairo University", "Statistics"))

    def test_a_cv_without_education_never_blanks_the_profile(self):
        record = StudentCV.objects.create(student=self.student, data={"education": []})
        record.data = {"education": [{"university": "", "major": ""}]}
        record.save()
        self.assertEqual(self._profile(), ("zagazig", "business"))

    def test_saves_that_do_not_touch_the_data_do_not_resync(self):
        record = StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        type(self.student).objects.filter(pk=self.student.pk).update(university="Manual")
        record.ai_uses = 2
        record.save(update_fields=["ai_uses"])
        self.assertEqual(self._profile()[0], "Manual")

    def test_readiness_score_counts_the_synced_degree(self):
        type(self.student).objects.filter(pk=self.student.pk).update(university="", major="", profile_completion_score=0)
        StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        self.student.refresh_from_db()
        self.assertGreaterEqual(self.student.profile_completion_score, 20)

    def test_sidebar_falls_back_to_the_cv_when_the_profile_is_stale(self):
        record = StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        type(self.student).objects.filter(pk=self.student.pk).update(university="zagazig", major="business")  # out of sync
        page = self.client.get(reverse("profile")).content.decode()
        aside = page[page.index('<aside class="px-side"'):page.index("</aside>")]
        self.assertIn("Ain Shams University", aside)
        self.assertIn("Cybersecurity", aside)
        self.assertNotIn("zagazig", aside)
        record.delete()
        aside = self.client.get(reverse("profile")).content.decode()
        self.assertIn("zagazig", aside)  # no CV education: the profile fields are shown again

    def test_context_exposes_the_displayed_degree(self):
        StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        ctx = self.client.get(reverse("profile")).context
        self.assertEqual((ctx["display_university"], ctx["display_major"], ctx["education_locked"]), ("Ain Shams University", "Cybersecurity", True))

    def test_form_fields_are_locked_only_while_the_cv_has_education(self):
        data = {"personal_email": "y@x.com", "university": "Hacked", "major": "Hacked", "study_level": "3", "phone_number": "",
                "linkedin_url": "", "bio": "", "portfolio_url": ""}
        self.client.post(reverse("profile"), data)
        self.assertEqual(self._profile(), ("Hacked", "Hacked"))  # no CV education: editable as before
        StudentCV.objects.create(student=self.student, data={"education": [AIN]})
        self.client.post(reverse("profile"), {**data, "university": "Other", "major": "Other"})
        self.assertEqual(self._profile(), ("Ain Shams University", "Cybersecurity"))
        page = self.client.get(reverse("profile") + "?tab=edit").content.decode()
        self.assertIn("Synced from your CV education", page)
        self.assertIn("disabled", page[page.index('name="university"'):][:200])

    def test_one_time_migration_syncs_existing_profiles_and_is_idempotent(self):
        StudentCV.objects.create(student=self.student, data={"education": [CAIRO, AIN]})
        type(self.student).objects.filter(pk=self.student.pk).update(university="zagazig", major="business")  # pre-sync data
        migration = importlib.import_module("internest_students.migrations.0002_sync_profile_education")
        for _ in range(2):
            migration.sync_existing_profiles(apps, None)
            self.assertEqual(self._profile(), ("Ain Shams University", "Cybersecurity"))

    def test_sync_helper_reports_whether_anything_changed(self):
        self.assertTrue(student_hub.sync_profile_education(self.student, [AIN]))
        self.assertFalse(student_hub.sync_profile_education(self.student, [AIN]))
        self.assertFalse(student_hub.sync_profile_education(self.student, []))


class LightboxTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _set_picture(self, name):
        type(self.student).objects.filter(pk=self.student.pk).update(profile_picture=name)

    def test_avatar_with_a_photo_is_a_button_that_opens_the_lightbox(self):
        self._set_picture("profile_pics/me.png")
        page = self.client.get(reverse("profile")).content.decode()
        self.assertIn('class="px-avatar__btn"', page)
        self.assertIn('data-lightbox="/media/profile_pics/me.png"', page)
        self.assertIn('aria-haspopup="dialog"', page)
        self.assertIn('aria-label="View profile photo"', page)
        self.assertIn('id="photo-lightbox"', page)
        self.assertIn("data-lightbox-close", page)

    def test_without_a_photo_there_is_no_button_and_no_dialog(self):
        page = self.client.get(reverse("profile")).content.decode()
        self.assertNotIn("px-avatar__btn\"", page.split("</style>")[-1])
        self.assertNotIn('id="photo-lightbox"', page)

    def test_lightbox_image_is_contained_uncropped_and_responsive(self):
        self._set_picture("profile_pics/me.png")
        page = self.client.get(reverse("profile")).content.decode()
        rule = page[page.index(".px-lightbox__img {"):]
        rule = rule[:rule.index("}")]
        for expected in ("max-width: 90vw", "max-height: 85vh", "object-fit: contain", "border-radius: 16px"):
            self.assertIn(expected, rule)
        self.assertIn(".px-lightbox::backdrop", page)
        self.assertIn("backdrop-filter: blur", page)
        self.assertIn("cursor: pointer", page[page.index(".px-avatar__btn {"):][:300])
        self.assertIn(".px-avatar__btn:hover .px-avatar__img", page)

    def test_script_closes_on_outside_click_and_relies_on_native_escape(self):
        self._set_picture("profile_pics/me.png")
        page = self.client.get(reverse("profile")).content.decode()
        self.assertIn("lightbox.showModal()", page)
        self.assertIn("e.target !== lightboxImg", page)  # a click anywhere but the photo closes it
        self.assertIn('data-lightbox-close', page)

    def test_avatar_component_zoom_flag(self):
        class Pic:
            url = "/media/a.png"

        zoom = render_to_string("components/avatar.html", {"picture": Pic(), "name": "Y", "zoom": True})
        plain = render_to_string("components/avatar.html", {"picture": Pic(), "name": "Y"})
        self.assertIn("px-avatar__btn", zoom)
        self.assertNotIn("px-avatar__btn", plain)
        self.assertNotIn("px-avatar__btn", render_to_string("components/avatar.html", {"picture": None, "name": "Y", "zoom": True}))
