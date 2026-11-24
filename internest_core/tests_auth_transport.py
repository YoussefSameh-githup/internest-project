"""Login over plain http:// vs https://: the cookie flags must match how browsers reach the site.

Root cause of the "every login fails" incident (2026-11-24): production forced Secure CSRF/session cookies while the
EC2 server was reachable only over http://. Browsers drop Secure cookies on http://, so every POST (password login,
signup, logout, "Continue with Google") failed with 403 "CSRF cookie not set". DJANGO_HTTPS=False is the switch for
that phase; Google/LinkedIn/Microsoft are hidden then because they reject http:// callbacks.
"""
import os
import subprocess
import sys

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from internest_core.models import StudentProfile
from internest_core.tests_social_auth import KEYED

PASSWORD = "Strong-pass-2026!"


class BrowserLikeClient(Client):
    """Behaves like a browser on http://: a cookie marked Secure is never stored, so it is never sent back."""

    def request(self, **request):
        response = super().request(**request)
        if request.get("wsgi.url_scheme", "http") != "https":
            for name, morsel in list(response.cookies.items()):
                if morsel["secure"]:
                    self.cookies.pop(name, None)
        return response


class LoginOverHttpTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("mona", email="mona@example.com", password=PASSWORD)
        StudentProfile.objects.create(user=cls.user, university="Cairo", major="CS", study_level="3")

    def _login(self, client):
        page = client.get(reverse("login"))
        self.assertEqual(page.status_code, 200)
        token = page.context["csrf_token"]
        return client.post(reverse("login"), {"csrfmiddlewaretoken": str(token), "username": "mona", "password": PASSWORD})

    @override_settings(CSRF_COOKIE_SECURE=True, SESSION_COOKIE_SECURE=True)
    def test_secure_cookies_on_plain_http_reproduce_the_incident(self):
        response = self._login(BrowserLikeClient(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 403)  # "CSRF cookie not set": the bug the users saw

    @override_settings(CSRF_COOKIE_SECURE=False, SESSION_COOKIE_SECURE=False)
    def test_plain_http_cookies_let_a_password_login_through(self):
        client = BrowserLikeClient(enforce_csrf_checks=True)
        response = self._login(client)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("home_redirect"))
        self.assertEqual(int(client.session["_auth_user_id"]), self.user.pk)

    @override_settings(CSRF_COOKIE_SECURE=True, SESSION_COOKIE_SECURE=True)
    def test_secure_cookies_over_https_still_work(self):
        client = Client(enforce_csrf_checks=True)
        page = client.get(reverse("login"), secure=True)
        response = client.post(reverse("login"), {"csrfmiddlewaretoken": str(page.context["csrf_token"]),
                                                  "username": "mona", "password": PASSWORD},
                               secure=True, HTTP_REFERER="https://testserver/accounts/login/")
        self.assertEqual(response.status_code, 302)

    @override_settings(CSRF_COOKIE_SECURE=False, SESSION_COOKIE_SECURE=False)
    def test_inactive_accounts_are_still_refused(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        client = BrowserLikeClient(enforce_csrf_checks=True)
        response = self._login(client)
        self.assertEqual(response.status_code, 200)  # the form again, with its error
        self.assertTrue(response.context["form"].errors)
        self.assertNotIn("_auth_user_id", client.session)

    @override_settings(CSRF_COOKIE_SECURE=False, SESSION_COOKIE_SECURE=False)
    def test_logout_works_over_plain_http(self):
        client = BrowserLikeClient(enforce_csrf_checks=True)
        self.assertEqual(self._login(client).status_code, 302)
        self.assertIn("_auth_user_id", client.session)
        response = client.post(reverse("logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)


class SocialButtonsWithoutHttpsTests(TestCase):
    @override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, DEBUG=False, SITE_HTTPS=False)
    def test_https_only_providers_are_hidden_and_refused_on_plain_http(self):
        html = self.client.get(reverse("login")).content.decode()
        for provider in settings.SOCIAL_HTTPS_ONLY_PROVIDERS:
            self.assertNotIn(f'data-provider="{provider}"', html)
            self.assertEqual(self.client.post(reverse("social_start", args=["student", provider])).status_code, 404)
        self.assertIn('data-provider="github"', html)  # GitHub accepts http:// callbacks

    @override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, DEBUG=False, SITE_HTTPS=True)
    def test_every_provider_returns_with_https(self):
        html = self.client.get(reverse("login")).content.decode()
        for provider in ("google", "linkedin_oauth2", "github", "microsoft"):
            self.assertIn(f'data-provider="{provider}"', html)

    @override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, DEBUG=True, SITE_HTTPS=False)
    def test_local_development_keeps_google(self):  # Google allows http://localhost callbacks
        self.assertIn('data-provider="google"', self.client.get(reverse("login")).content.decode())


class SettingsSwitchTests(TestCase):
    """The production settings derived from DJANGO_HTTPS, read in a fresh interpreter (they are set at import)."""

    def _settings(self, **env):
        code = ("import django, os; os.environ['DJANGO_SETTINGS_MODULE']='internest_app_project.settings'; django.setup();"
                "from django.conf import settings as s;"
                "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_HSTS_SECONDS > 0, s.SECURE_SSL_REDIRECT,"
                " s.ACCOUNT_DEFAULT_HTTP_PROTOCOL)")
        clean = {k: v for k, v in os.environ.items() if k not in ("DJANGO_HTTPS", "DJANGO_SECURE_SSL_REDIRECT")}
        clean.update(DJANGO_DEBUG="False", **env)
        out = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=clean, capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return out.stdout.strip().splitlines()[-1]

    def test_https_is_the_production_default(self):
        self.assertEqual(self._settings(), "True True True True https")

    def test_plain_http_switch(self):
        self.assertEqual(self._settings(DJANGO_HTTPS="False"), "False False False False http")


class UnconfiguredProviderRoutesTests(TestCase):
    """allauth's own /oauth/<provider>/ routes: 404 for providers that are not offered, never a 500."""

    ROUTES = ("/oauth/{p}/login/", "/oauth/{p}/login/callback/")

    def test_no_keys_means_404_not_500(self):
        for provider in settings.SOCIAL_LOGIN_PROVIDER_ORDER:
            for route in self.ROUTES:
                self.assertEqual(self.client.get(route.format(p=provider)).status_code, 404, (provider, route))

    @override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, DEBUG=False, SITE_HTTPS=True)
    def test_configured_providers_still_reach_allauth(self):
        for provider in settings.SOCIAL_LOGIN_PROVIDER_ORDER:
            status = self.client.get(f"/oauth/{provider}/login/").status_code
            self.assertNotIn(status, (404, 500), provider)

    @override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, DEBUG=False, SITE_HTTPS=False)
    def test_https_only_providers_are_closed_on_plain_http(self):
        self.assertEqual(self.client.get("/oauth/google/login/").status_code, 404)
        self.assertNotEqual(self.client.get("/oauth/github/login/").status_code, 404)
