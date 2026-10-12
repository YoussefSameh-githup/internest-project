"""Change-password pages use the Internest design and keep Django's PasswordChangeView behaviour."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from .models import PartnerProfile, StudentProfile

NEW = "Gr33n-Lantern!x"


class PasswordChangeTests(TestCase):
    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"
        self.user = User.objects.create_user("mona", password="old-Pass-123")
        StudentProfile.objects.create(user=self.user, university="Cairo", major="CS", study_level="3")
        self.client.force_login(self.user)

    def _post(self, old="old-Pass-123", new1=NEW, new2=NEW):
        return self.client.post(reverse("password_change"), {"old_password": old, "new_password1": new1, "new_password2": new2})

    def test_form_uses_the_internest_design(self):
        page = self.client.get(reverse("password_change"))
        self.assertTemplateUsed(page, "registration/password_change_form.html")
        self.assertTemplateNotUsed(page, "admin/base.html")
        for marker in ("Change password", "Enter your current password and a new one to keep your account safe.",
                       'name="old_password"', 'name="new_password1"', 'name="new_password2"', "data-pw-toggle",
                       "At least 8 characters", "Save the new password", f'{reverse("profile")}?tab=settings',
                       "Back to your Student Hub"):
            self.assertContains(page, marker)

    def test_inline_errors(self):
        page = self._post(old="wrong")
        self.assertContains(page, 'aria-invalid="true" aria-describedby="id_old_password-error"')
        self.assertContains(page, "Your old password was entered incorrectly")
        page = self._post(new2="Different-123!")
        self.assertContains(page, 'id="id_new_password2-error"')
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-Pass-123"))  # unchanged after failed attempts

    def test_success_keeps_the_user_signed_in(self):
        resp = self._post()
        self.assertRedirects(resp, reverse("password_change_done"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))
        done = self.client.get(reverse("password_change_done"))
        self.assertTemplateUsed(done, "registration/password_change_done.html")
        self.assertContains(done, "Your password was changed successfully!")
        self.assertContains(done, f'{reverse("profile")}?tab=settings')

    def test_partners_go_back_to_their_dashboard(self):
        partner = User.objects.create_user("founder", password="pw-Strong-1")
        PartnerProfile.objects.create(user=partner, company_name="Nile", partner_code="N1")
        self.client.force_login(partner)
        page = self.client.get(reverse("password_change"))
        self.assertContains(page, f'href="{reverse("partner_dashboard")}"')
        self.assertNotContains(page, "Back to your Student Hub")

    def test_arabic(self):
        self.client.cookies["internest_lang"] = "ar"
        page = self.client.get(reverse("password_change"))
        for text in ("تغيير كلمة المرور", "أدخل كلمة المرور الحالية وكلمة المرور الجديدة لحماية حسابك",
                     "حفظ كلمة المرور الجديدة", "الرجوع إلى مساحتك الطلابية"):
            self.assertContains(page, text)
        self._post()
        done = self.client.get(reverse("password_change_done"))
        self.assertContains(done, "تم تغيير كلمة المرور بنجاح!")
        self.assertContains(done, "العودة إلى مساحتك الطلابية")
