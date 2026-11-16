"""Social login (Google, LinkedIn, GitHub, Microsoft): buttons, role-aware provisioning, account-takeover guards,
avatar SSRF / file hardening, open-redirect and OAuth-state protection."""
import io
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
from allauth.account.middleware import AccountMiddleware
from allauth.account.models import EmailAddress
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.messages import get_messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.files.base import ContentFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation
from PIL import Image

from internest_core import adapters, social_avatar
from internest_core.models import PartnerProfile, StudentProfile

KEYED = {name: {**cfg, "APP": {"client_id": f"{name}-id", "secret": f"{name}-secret", "key": ""}}
         for name, cfg in settings.SOCIALACCOUNT_PROVIDERS.items()}
with_providers = override_settings(SOCIALACCOUNT_PROVIDERS=KEYED, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


def jpeg(size=(40, 60), color="#7c3aed", fmt="JPEG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


class ButtonTests(TestCase):
    @with_providers
    def test_student_pages_offer_every_configured_provider(self):
        for url in (reverse("login_or_signup"), reverse("login"), reverse("signup")):
            html = self.client.get(url).content.decode()
            for provider, label in (("google", "Google"), ("linkedin_oauth2", "LinkedIn"), ("github", "GitHub"), ("microsoft", "Microsoft")):
                self.assertIn(f'href="{reverse("social_start", args=["student", provider])}"', html, (url, provider))
            self.assertNotIn("coming soon", html)
        html = self.client.get(reverse("login_or_signup")).content.decode()
        self.assertLess(html.index('data-provider="google"'), html.index('data-provider="linkedin_oauth2"'))  # Google first
        self.assertIn("social-btn--primary", html.split('data-provider="linkedin_oauth2"')[0])

    @with_providers
    def test_startup_registration_uses_the_partner_flow(self):
        html = self.client.get(reverse("startup_register")).content.decode()
        self.assertIn(f'href="{reverse("social_start", args=["partner", "google"])}"', html)
        self.assertNotIn("/oauth-start/student/", html)

    def test_no_keys_means_no_buttons_and_no_placeholder(self):
        html = self.client.get(reverse("login_or_signup")).content.decode()
        self.assertNotIn("social-auth", html)
        self.assertNotIn("coming soon", html)
        self.assertEqual(self.client.get(reverse("social_start", args=["student", "google"])).status_code, 404)

    @with_providers
    def test_only_providers_with_keys_are_shown(self):
        partial = {**KEYED, "github": {k: v for k, v in KEYED["github"].items() if k != "APP"}}
        with override_settings(SOCIALACCOUNT_PROVIDERS=partial):
            html = self.client.get(reverse("login_or_signup")).content.decode()
        self.assertIn('data-provider="google"', html)
        self.assertNotIn('data-provider="github"', html)

    @with_providers
    def test_labels_are_translated(self):
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("login_or_signup")), "المتابعة عبر Google")


@with_providers
class StartViewTests(TestCase):
    def _start(self, role="student", provider="google", **params):
        return self.client.get(reverse("social_start", args=[role, provider]), params)

    def test_remembers_the_role_and_goes_to_allauth(self):
        response = self._start("partner")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/oauth/google/login/?"))
        self.assertEqual(parse_qs(urlparse(response.url).query)["process"], ["login"])
        self.assertEqual(self.client.session[adapters.ROLE_SESSION_KEY]["role"], "partner")

    def test_unknown_role_or_provider_is_404(self):
        self.assertEqual(self._start("admin").status_code, 404)
        self.assertEqual(self._start("student", "facebook").status_code, 404)
        self.assertNotIn(adapters.ROLE_SESSION_KEY, self.client.session)

    def test_open_redirect_targets_are_dropped(self):
        for evil in ("https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "http://testserver.evil.example/"):
            query = parse_qs(urlparse(self._start(next=evil).url).query)
            self.assertNotIn("next", query, evil)

    def test_safe_next_is_kept(self):
        self.assertEqual(parse_qs(urlparse(self._start(next="/list/").url).query)["next"], ["/list/"])

    def test_post_is_not_allowed_and_signed_in_users_skip_it(self):
        self.assertEqual(self.client.post(reverse("social_start", args=["student", "google"])).status_code, 405)
        self.client.force_login(User.objects.create_user("already", password="x"))
        self.assertRedirects(self._start(), reverse("home_redirect"), fetch_redirect_response=False)

    def test_provider_redirect_carries_an_oauth_state(self):
        response = self.client.get(self._start().url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://accounts.google.com/"))
        query = parse_qs(urlparse(response.url).query)
        self.assertTrue(query["state"][0])
        self.assertEqual(query["client_id"], ["google-id"])
        self.assertIn("email", query["scope"][0])
        self.assertTrue(self.client.session["socialaccount_states"])

    def test_a_forged_state_is_rejected(self):
        self.client.get(self._start().url)
        response = self.client.get("/oauth/google/login/callback/", {"code": "abc", "state": "forged"})
        self.assertRedirects(response, reverse("login_or_signup"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(SocialAccount.objects.count(), 0)

    def test_allauth_own_account_pages_are_not_exposed(self):
        for path in ("/oauth/login/", "/oauth/signup/", "/oauth/password/reset/", "/oauth/email/", "/oauth/logout/"):
            self.assertRedirects(self.client.get(path), reverse("login_or_signup"), fetch_redirect_response=False, msg_prefix=path)


@with_providers
class ProvisioningBase(TestCase):
    """Runs the real allauth completion pipeline (lookup, pre_social_login, signup, login) for a fake provider answer."""

    def setUp(self):
        photo = mock.patch.object(adapters, "fetch_avatar", return_value=None)
        self.fetch = photo.start()
        self.addCleanup(photo.stop)
        language = translation.override("en")
        language.__enter__()
        self.addCleanup(language.__exit__, None, None, None)

    def complete(self, *, email="nour@uni.edu", verified=True, uid="u-1", provider="google", role=None, first="Nour", last="Ali",
                 extra=None, next_url=None):
        addresses = [EmailAddress(email=email, verified=verified, primary=True)] if email else []
        account = SocialAccount(provider=provider, uid=uid, extra_data=extra or {"picture": "https://lh3.googleusercontent.com/a/photo"})
        login = SocialLogin(user=User(first_name=first, last_name=last, email=email or ""), account=account, email_addresses=addresses)
        login.state = {"process": "login", "next": next_url} if next_url else {"process": "login"}
        request = RequestFactory().get(f"/oauth/{provider}/login/callback/")
        SessionMiddleware(lambda r: None).process_request(request)
        MessageMiddleware(lambda r: None).process_request(request)
        request.user = AnonymousUser()
        if role:
            adapters.remember_role(request, role)
        response = AccountMiddleware(lambda req: complete_social_login(req, login))(request)
        return request, response

    def flashes(self, request):
        return [str(m) for m in get_messages(request)]


class RoleAwareProvisioningTests(ProvisioningBase):
    def test_student_flow_creates_a_verified_student_profile(self):
        self.fetch.return_value = ContentFile(jpeg(), name="avatar.jpg")
        request, response = self.complete(role="student", extra={"picture": "https://lh3.googleusercontent.com/a/p", "email_verified": True})
        user = User.objects.get(email="nour@uni.edu")
        self.assertEqual((user.first_name, user.last_name), ("Nour", "Ali"))
        self.assertFalse(user.has_usable_password())
        profile = user.studentprofile
        self.assertEqual(profile.personal_email, "nour@uni.edu")
        self.assertIsNotNone(profile.personal_email_verified_at)  # the provider vouched for it: no OTP step
        self.assertTrue(profile.profile_picture.name.startswith("profile_pics/"))
        self.assertGreaterEqual(profile.profile_completion_score, 20)
        self.assertFalse(PartnerProfile.objects.filter(user=user).exists())
        self.assertTrue(request.user.is_authenticated)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("home_redirect"))
        self.fetch.assert_called_once_with("https://lh3.googleusercontent.com/a/p")

    def test_partner_flow_creates_a_company_account_for_onboarding(self):
        self.fetch.return_value = ContentFile(jpeg(), name="avatar.jpg")
        request, response = self.complete(role="partner", email="founder@startup.io", first="Salma", last="Hany")
        user = User.objects.get(email="founder@startup.io")
        partner = user.partnerprofile
        self.assertTrue(partner.company_name.startswith("__pending__"))  # the mandatory company-profile step names it
        self.assertEqual(partner.official_email, "founder@startup.io")
        self.assertTrue(partner.logo.name.startswith("partner_logos/"))
        self.assertTrue(partner.partner_code)
        self.assertFalse(StudentProfile.objects.filter(user=user).exists())
        self.assertEqual((user.first_name, user.last_name), ("Salma", "Hany"))
        self.assertEqual(response.url, reverse("home_redirect"))
        # home_redirect sends partners to the dashboard, where the company-profile gate takes over
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse("home_redirect")), reverse("startup_company_profile_edit"), fetch_redirect_response=False)

    def test_github_profile_url_goes_to_the_portfolio_link(self):
        self.complete(provider="github", email="dev@uni.edu", uid="g1", extra={"html_url": "https://github.com/nour", "avatar_url": "https://avatars.githubusercontent.com/u/1"})
        self.assertEqual(User.objects.get(email="dev@uni.edu").studentprofile.portfolio_url, "https://github.com/nour")
        self.fetch.assert_called_once_with("https://avatars.githubusercontent.com/u/1")

    def test_a_non_github_url_is_never_stored(self):
        self.complete(provider="github", email="dev@uni.edu", uid="g2", extra={"html_url": "https://evil.example/x"})
        self.assertEqual(User.objects.get(email="dev@uni.edu").studentprofile.portfolio_url, "")

    def test_missing_or_stale_role_means_student_and_is_consumed_once(self):
        self.complete(role=None)
        self.assertTrue(StudentProfile.objects.filter(user__email="nour@uni.edu").exists())
        for bad in ({"role": "admin", "ts": int(timezone.now().timestamp())}, {"role": "partner", "ts": 1}, "partner", None):
            request = RequestFactory().get("/")
            SessionMiddleware(lambda r: None).process_request(request)
            request.session[adapters.ROLE_SESSION_KEY] = bad
            self.assertEqual(adapters.take_role(request), "student")
            self.assertNotIn(adapters.ROLE_SESSION_KEY, request.session)

    def test_avatar_failure_never_blocks_the_sign_in(self):
        self.fetch.return_value = None
        request, response = self.complete()
        self.assertTrue(request.user.is_authenticated)
        self.assertFalse(User.objects.get(email="nour@uni.edu").studentprofile.profile_picture)

    def test_safe_next_is_honoured(self):
        _request, response = self.complete(next_url="/list/")
        self.assertEqual(response.url, "/list/")

    def test_unverified_provider_email_creates_an_account_that_still_needs_otp(self):
        request, response = self.complete(provider="microsoft", email="student@mu.edu.eg", verified=False, uid="m1", extra={"mail": "student@mu.edu.eg"})
        profile = User.objects.get(email="student@mu.edu.eg").studentprofile
        self.assertIsNone(profile.personal_email_verified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("student@mu.edu.eg", mail.outbox[0].to)
        self.client.force_login(profile.user)
        self.assertRedirects(self.client.get(reverse("home_redirect")), reverse("verify_email"), fetch_redirect_response=False)


class AccountTakeoverTests(ProvisioningBase):
    def _student(self, username="existing", email="nour@uni.edu", verified=True, **user_kw):
        user = User.objects.create_user(username, password="pw", **user_kw)
        StudentProfile.objects.create(user=user, personal_email=email, personal_email_verified_at=timezone.now() if verified else None,
                                      university="Cairo", major="CS")
        return user

    def test_unverified_provider_email_never_links_to_an_existing_account(self):
        existing = self._student()
        request, response = self.complete(email="nour@uni.edu", verified=False, provider="microsoft", uid="evil")
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertFalse(request.user.is_authenticated)
        self.assertEqual(User.objects.count(), 1)
        self.assertRedirects(response, reverse("login_or_signup"), fetch_redirect_response=False)
        self.assertTrue(any("already uses this email" in m for m in self.flashes(request)))
        existing.refresh_from_db()
        self.assertTrue(existing.has_usable_password())

    def test_verified_provider_email_connects_to_a_verified_student_without_touching_it(self):
        existing = self._student()
        request, response = self.complete(email="NOUR@uni.edu", verified=True, first="Someone", last="Else")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(request.user.pk, existing.pk)
        self.assertTrue(SocialAccount.objects.filter(user=existing, provider="google").exists())
        existing.refresh_from_db()
        self.assertEqual((existing.first_name, existing.last_name), ("", ""))  # names are NOT overwritten
        profile = existing.studentprofile
        self.assertEqual((profile.university, profile.major), ("Cairo", "CS"))
        self.assertEqual(StudentProfile.objects.count(), 1)
        self.assertFalse(PartnerProfile.objects.exists())
        self.assertIsNotNone(profile.personal_email_verified_at)  # the provider vouched for it: no re-verification
        self.assertEqual(len(mail.outbox), 0)

    def test_verified_provider_email_does_not_link_to_an_unverified_local_email(self):
        self._student(verified=False)  # an attacker pre-registered the victim's address and never verified it
        request, response = self.complete(email="nour@uni.edu", verified=True)
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertFalse(request.user.is_authenticated)
        self.assertRedirects(response, reverse("login_or_signup"), fetch_redirect_response=False)

    def test_a_company_is_never_matched_by_email_and_keeps_its_role(self):
        company = User.objects.create_user("acme@startup.io", email="acme@startup.io", password="pw")
        PartnerProfile.objects.create(user=company, company_name="Acme", partner_code="c1")
        request, response = self.complete(email="acme@startup.io", verified=True, role="student")
        self.assertFalse(request.user.is_authenticated)
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertFalse(StudentProfile.objects.filter(user=company).exists())  # no role downgrade / second profile
        self.assertTrue(PartnerProfile.objects.filter(user=company).exists())
        self.assertRedirects(response, reverse("login_or_signup"), fetch_redirect_response=False)

    def test_staff_accounts_are_not_matched_either(self):
        staff = User.objects.create_user("boss", email="boss@internest.app", password="pw", is_staff=True)
        request, _response = self.complete(email="boss@internest.app", verified=True)
        self.assertFalse(request.user.is_authenticated)
        self.assertFalse(SocialAccount.objects.filter(user=staff).exists())

    def test_the_university_email_of_a_verified_student_matches_too(self):
        user = self._student(email="personal@gmail.com")
        StudentProfile.objects.filter(user=user).update(university_email="s1@mu.edu.eg", university_email_verified_at=timezone.now())
        request, _response = self.complete(provider="microsoft", email="s1@mu.edu.eg", verified=True, uid="m2")
        self.assertEqual(request.user.pk, user.pk)

    def test_a_different_profile_email_is_still_re_verified_on_login(self):
        user = self._student(email="other@gmail.com")
        StudentProfile.objects.filter(user=user).update(university_email="nour@uni.edu", university_email_verified_at=timezone.now())
        request, _response = self.complete(email="nour@uni.edu", verified=True)
        self.assertEqual(request.user.pk, user.pk)
        profile = StudentProfile.objects.get(user=user)
        self.assertIsNotNone(profile.university_email_verified_at)  # vouched by the provider
        self.assertIsNone(profile.personal_email_verified_at)  # the usual per-login OTP policy still applies to the other address

    def test_a_provider_without_an_email_is_refused(self):
        request, response = self.complete(email="", provider="github", uid="noemail")
        self.assertFalse(request.user.is_authenticated)
        self.assertEqual(User.objects.count(), 0)
        self.assertTrue(any("did not share an email" in m for m in self.flashes(request)))

    def test_a_returning_social_user_keeps_their_profile(self):
        self.complete()
        user = User.objects.get(email="nour@uni.edu")
        StudentProfile.objects.filter(user=user).update(university="Ain Shams", bio="mine")
        User.objects.filter(pk=user.pk).update(first_name="Edited")
        request, _response = self.complete(first="Changed", last="ByProvider", role="partner")
        self.assertEqual(request.user.pk, user.pk)
        user.refresh_from_db()
        self.assertEqual((user.first_name, StudentProfile.objects.get(user=user).university), ("Edited", "Ain Shams"))
        self.assertFalse(PartnerProfile.objects.filter(user=user).exists())  # the partner flow cannot re-role an existing account
        self.assertEqual(SocialAccount.objects.count(), 1)


class AvatarHardeningTests(TestCase):
    """fetch_avatar treats the provider's URL as hostile."""

    URL = "https://lh3.googleusercontent.com/a/photo"

    def setUp(self):
        self.requests = []
        patch = mock.patch.object(social_avatar.socket, "getaddrinfo", side_effect=lambda host, *a, **k: self.dns.get(host, []))
        patch.start()
        self.addCleanup(patch.stop)
        self.dns = {"lh3.googleusercontent.com": [(2, 1, 6, "", ("142.250.1.1", 443))], "cdn.example.com": [(2, 1, 6, "", ("93.184.216.34", 443))]}
        self.real_client = httpx.Client

    def serve(self, handler):
        def factory(**kwargs):
            def recording(request):
                self.requests.append(request)
                return handler(request)
            return self.real_client(transport=httpx.MockTransport(recording), **kwargs)
        patch = mock.patch.object(social_avatar.httpx, "Client", side_effect=factory)
        patch.start()
        self.addCleanup(patch.stop)

    def ok(self, body=None, content_type="image/jpeg", **headers):
        return lambda request: httpx.Response(200, content=body if body is not None else jpeg(), headers={"content-type": content_type, **headers})

    def test_a_good_photo_is_downloaded_pinned_and_re_encoded(self):
        self.serve(self.ok(jpeg((800, 1200), fmt="PNG"), "image/png"))
        result = social_avatar.fetch_avatar(self.URL)
        self.assertEqual(result.name, "avatar.jpg")
        data = result.read()
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))  # a fresh JPEG, not the downloaded PNG
        self.assertLessEqual(max(Image.open(io.BytesIO(data)).size), 512)
        request = self.requests[0]
        self.assertEqual(request.url.host, "142.250.1.1")  # connected to the address that was validated
        self.assertEqual(request.headers["host"], "lh3.googleusercontent.com")

    def test_only_https_urls_without_credentials_or_odd_ports(self):
        self.serve(self.ok())
        for url in ("http://lh3.googleusercontent.com/a.jpg", "ftp://x/a.jpg", "file:///etc/passwd", "//lh3.googleusercontent.com/a.jpg",
                    "https://user:pw@lh3.googleusercontent.com/a.jpg", "https://lh3.googleusercontent.com:8443/a.jpg", "", None, "not a url"):
            self.assertIsNone(social_avatar.fetch_avatar(url), url)
        self.assertEqual(self.requests, [])

    def test_private_loopback_and_metadata_addresses_are_never_contacted(self):
        self.serve(self.ok())
        for host, ip in (("localhost", "127.0.0.1"), ("internal.example", "10.0.0.5"), ("lan.example", "192.168.1.9"),
                         ("meta.example", "169.254.169.254"), ("v6.example", "::1"), ("ula.example", "fd00::1"), ("zero.example", "0.0.0.0")):
            self.dns[host] = [(2, 1, 6, "", (ip, 443))]
            self.assertIsNone(social_avatar.fetch_avatar(f"https://{host}/a.jpg"), host)
        self.assertEqual(self.requests, [])

    def test_a_name_that_resolves_to_both_public_and_private_addresses_is_rejected(self):
        self.dns["rebind.example"] = [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]
        self.serve(self.ok())
        self.assertIsNone(social_avatar.fetch_avatar("https://rebind.example/a.jpg"))
        self.assertEqual(self.requests, [])

    def test_unresolvable_hosts_fail_quietly(self):
        self.serve(self.ok())
        self.assertIsNone(social_avatar.fetch_avatar("https://nxdomain.example/a.jpg"))

    def test_redirects_are_revalidated_and_limited(self):
        self.dns["internal.example"] = [(2, 1, 6, "", ("10.0.0.5", 443))]
        self.serve(lambda r: httpx.Response(302, headers={"location": "https://internal.example/secret"}))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))
        self.assertEqual(len(self.requests), 1)  # never followed into the private network

        self.requests.clear()
        self.serve(lambda r: httpx.Response(302, headers={"location": "https://cdn.example.com/next"}))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))
        self.assertEqual(len(self.requests), social_avatar.MAX_REDIRECTS + 1)

    def test_a_safe_redirect_to_a_public_host_works(self):
        def handler(request):
            if request.headers["host"] == "lh3.googleusercontent.com":
                return httpx.Response(302, headers={"location": "https://cdn.example.com/real.jpg"})
            return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})
        self.serve(handler)
        self.assertIsNotNone(social_avatar.fetch_avatar(self.URL))

    def test_wrong_content_types_are_rejected(self):
        for content_type in ("text/html", "application/octet-stream", "image/svg+xml", "application/pdf", ""):
            self.serve(self.ok(jpeg(), content_type))
            self.assertIsNone(social_avatar.fetch_avatar(self.URL), content_type)

    def test_bytes_that_are_not_an_image_are_rejected_even_with_an_image_header(self):
        for payload in (b"<html><script>alert(1)</script></html>", b"MZ" + b"\x00" * 100, b"GIF89a-not-really", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b""):
            self.serve(self.ok(payload, "image/png"))
            self.assertIsNone(social_avatar.fetch_avatar(self.URL), payload[:12])

    def test_a_truncated_or_corrupt_image_is_rejected(self):
        self.serve(self.ok(jpeg()[:60], "image/jpeg"))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_oversized_downloads_are_rejected(self):
        self.serve(self.ok(jpeg(), "image/jpeg", **{"content-length": str(social_avatar.MAX_BYTES + 1)}))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))
        self.serve(self.ok(b"\xff\xd8\xff" + b"0" * (social_avatar.MAX_BYTES + 10), "image/jpeg"))  # no content-length: counted while streaming
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_huge_pixel_dimensions_are_rejected(self):
        self.serve(self.ok(jpeg((5000, 5000)), "image/jpeg"))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_animated_or_exotic_formats_are_not_stored(self):
        out = io.BytesIO()
        Image.new("P", (20, 20)).save(out, "GIF")
        self.serve(self.ok(out.getvalue(), "image/gif"))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_errors_and_timeouts_never_raise(self):
        for response in (httpx.ConnectTimeout("slow"), httpx.ReadError("boom")):
            self.serve(lambda request, response=response: (_ for _ in ()).throw(response))
            self.assertIsNone(social_avatar.fetch_avatar(self.URL))
        self.serve(lambda request: httpx.Response(404))
        self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_the_five_second_budget_is_enforced(self):
        self.assertEqual(social_avatar.TIMEOUT_SECONDS, 5.0)
        self.assertEqual(social_avatar.MAX_BYTES, 2 * 1024 * 1024)
        with mock.patch.object(social_avatar.time, "monotonic", side_effect=[0.0, 99.0, 99.0, 99.0]):
            self.serve(self.ok())
            self.assertIsNone(social_avatar.fetch_avatar(self.URL))

    def test_clean_image_strips_metadata_and_caps_size(self):
        out = io.BytesIO()
        image = Image.new("RGB", (1500, 900), "red")
        exif = Image.Exif()
        exif[0x010E] = "secret description"
        image.save(out, "JPEG", exif=exif)
        cleaned = social_avatar.clean_image(out.getvalue()).read()
        self.assertNotIn(b"secret description", cleaned)
        self.assertLessEqual(max(Image.open(io.BytesIO(cleaned)).size), 512)


class ConfigurationTests(TestCase):
    def test_one_click_and_email_link_settings(self):
        self.assertTrue(settings.SOCIALACCOUNT_LOGIN_ON_GET)
        self.assertTrue(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION)
        self.assertTrue(settings.SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT)
        self.assertFalse(settings.SOCIALACCOUNT_STORE_TOKENS)
        self.assertEqual(settings.SOCIALACCOUNT_ADAPTER, "internest_core.adapters.SocialAccountAdapter")
        self.assertEqual(settings.AUTHENTICATION_BACKENDS if hasattr(settings, "AUTHENTICATION_BACKENDS") else None,
                         ["django.contrib.auth.backends.ModelBackend"])

    def test_cookie_hardening(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(settings.CSRF_COOKIE_HTTPONLY)
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")  # Strict would drop the session on the provider's redirect back

    def test_provider_scopes(self):
        providers = settings.SOCIALACCOUNT_PROVIDERS
        self.assertEqual(providers["google"]["SCOPE"], ["profile", "email"])
        self.assertEqual(providers["linkedin_oauth2"]["SCOPE"], ["openid", "profile", "email"])
        self.assertEqual(providers["github"]["SCOPE"], ["user:email", "read:user"])
        self.assertEqual(providers["microsoft"]["TENANT"], "common")  # university (work/school) and personal accounts

    def test_credentials_come_from_the_environment_only(self):
        from internest_app_project import settings as project_settings

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(project_settings._social_app("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), {})
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "abc", "GOOGLE_CLIENT_SECRET": "xyz"}):
            self.assertEqual(project_settings._social_app("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
                             {"APP": {"client_id": "abc", "secret": "xyz", "key": ""}})
        with mock.patch.dict("os.environ", {"GOOGLE_CLIENT_ID": "abc"}, clear=True):  # half a pair is not enough
            self.assertEqual(project_settings._social_app("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), {})

    def test_no_secret_is_hardcoded_in_settings_or_adapters(self):
        import re
        from pathlib import Path

        root = Path(settings.BASE_DIR)
        for name in ("internest_app_project/settings.py", "internest_core/adapters.py", "internest_core/social_views.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?i)(client_secret|secret)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", text), name)
            self.assertIsNone(re.search(r"GOCSPX-|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}", text), name)

    def test_the_rate_limiter_covers_the_button_endpoint(self):
        from internest_core.ratelimit import RULES

        self.assertIn("social_start", RULES)
        self.assertEqual(resolve_name(reverse("social_start", args=["student", "google"])), "social_start")


def resolve_name(path):
    from django.urls import resolve

    return resolve(path).url_name
