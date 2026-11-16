"""Social login (django-allauth) wired into Internest's roles.

Rules, in order of importance:
1. A provider email that is NOT verified never links to, or logs into, an existing account.
2. Even a verified provider email only auto-connects to an account whose email Internest itself verified (a student with a
   verified personal/university email). Companies and staff have no email verification here, so they are never matched by
   email: that blocks "pre-hijacking" (registering someone else's address first, then waiting for them to sign in with Google).
3. Existing users keep their role and profile untouched. Only brand-new users get a StudentProfile (student flow) or a
   PartnerProfile (partner flow), chosen by the role stored in the server-side session by `social_start`.
"""
import logging
import secrets
import time

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import PartnerProfile, StudentProfile
from .social_avatar import fetch_avatar

logger = logging.getLogger(__name__)

ROLE_SESSION_KEY = "social_role"
ROLES = ("student", "partner")
ROLE_TTL_SECONDS = 15 * 60  # a sign-in has to finish within 15 minutes of the click


def remember_role(request, role: str) -> None:
    request.session[ROLE_SESSION_KEY] = {"role": role, "ts": int(time.time())}


def take_role(request) -> str:
    """The role chosen on the page the user clicked from (consumed once). Anything missing, stale or odd means student."""
    data = request.session.pop(ROLE_SESSION_KEY, None)
    if isinstance(data, dict) and data.get("role") in ROLES and time.time() - int(data.get("ts", 0)) <= ROLE_TTL_SECONDS:
        return data["role"]
    return "student"


def users_with_email(email: str):
    """Every local account that already uses this address (account email, a student's personal/university email, or a username)."""
    User = get_user_model()
    return User.objects.filter(
        Q(email__iexact=email) | Q(username__iexact=email) | Q(studentprofile__personal_email__iexact=email)
        | Q(studentprofile__university_email__iexact=email)
    ).distinct()


def local_email_is_verified(user, email: str) -> bool:
    """Did Internest verify this address for this user? Only students have that (OTP); companies and staff do not."""
    profile = getattr(user, "studentprofile", None)
    if profile is None:
        return False
    email = email.lower()
    return bool((profile.personal_email or "").lower() == email and profile.personal_email_verified_at
                or (profile.university_email or "").lower() == email and profile.university_email_verified_at)


def provider_email(sociallogin):
    """(email, verified) the provider vouches for: the verified primary one if any, else the first one it gave us."""
    addresses = list(sociallogin.email_addresses)
    ranked = sorted(addresses, key=lambda a: (not a.verified, not a.primary))
    if not ranked:
        return "", False
    return ranked[0].email.strip().lower(), bool(ranked[0].verified)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return False  # allauth's own sign-up form is never used; accounts come from Internest's forms or social_start


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def _block(self, request, message):
        messages.error(request, message)
        raise ImmediateHttpResponse(redirect("login_or_signup"))

    def authenticate_by_email(self, sociallogin):
        """Auto-connect by email: provider-verified address AND an account whose email Internest verified."""
        for address in sociallogin.email_addresses:
            if not address.verified:
                continue
            for user in users_with_email(address.email):
                if user.is_active and local_email_is_verified(user, address.email):
                    return user, address.email
        return None

    def pre_social_login(self, request, sociallogin):
        email, verified = provider_email(sociallogin)
        if verified:  # tells the "re-verify on every login" signal that the provider already vouched for this address
            request.social_verified_emails = {email}
        if sociallogin.is_existing:  # already connected (or just auto-connected above): plain sign-in, nothing is touched
            return
        provider = sociallogin.account.get_provider().name
        if not email:
            self._block(request, _("%(provider)s did not share an email address with us. Use another sign-in method.") % {"provider": provider})
        if users_with_email(email).exists():
            self._block(request, _("An Internest account already uses this email. Sign in with your password (and verify the email) first, "
                                   "then try again."))

    def is_open_for_signup(self, request, sociallogin):
        return True  # pre_social_login already rejected every case that must not create an account

    def is_auto_signup_allowed(self, request, sociallogin):
        return True

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        logger.warning("Social login error (%s): %s", getattr(provider, "id", provider), error)
        self._block(request, _("We couldn't sign you in with %(provider)s. Please try again or use another method.")
                    % {"provider": getattr(provider, "name", "")})

    def save_user(self, request, sociallogin, form=None):
        role = take_role(request)
        email, verified = provider_email(sociallogin)
        user = sociallogin.user
        user.email = email
        with transaction.atomic():
            user = super().save_user(request, sociallogin, form)
            profile = self._create_partner(user, email) if role == "partner" else self._create_student(user, email, verified, sociallogin)
            self._store_avatar(profile, role, sociallogin)
        # A student whose address the provider did not vouch for gets the normal OTP from the login signal.
        return user

    @staticmethod
    def _create_student(user, email, verified, sociallogin):
        github = (sociallogin.account.extra_data or {}).get("html_url") or ""
        return StudentProfile.objects.create(
            user=user, personal_email=email, personal_email_verified_at=timezone.now() if verified else None,
            portfolio_url=github if github.startswith("https://github.com/") else "",
        )

    @staticmethod
    def _create_partner(user, email):
        from internest_startups.completion import PLACEHOLDER_PREFIX

        # Same shape as startup registration; the company name is filled in on the mandatory company-profile step.
        return PartnerProfile.objects.create(user=user, company_name=f"{PLACEHOLDER_PREFIX}{secrets.token_hex(6)}",
                                             partner_code=secrets.token_urlsafe(9), official_email=email)

    @staticmethod
    def _store_avatar(profile, role, sociallogin):
        photo = fetch_avatar(sociallogin.account.get_avatar_url())
        if photo is None:
            return
        field = profile.logo if role == "partner" else profile.profile_picture
        field.save(f"{profile.user_id}.jpg", photo, save=False)
        profile.save()
        if role == "student":
            profile.calculate_completion()
