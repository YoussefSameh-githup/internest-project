import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.contrib.sites.models import Site
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import identity
from .models import EmailVerification, Internship, PartnerProfile, StudentProfile
from .notifications import schedule_new_internship_emails  # also registers the batched job

logger = logging.getLogger(__name__)


def _absolute_url(path: str) -> str:
    try:
        current_site = Site.objects.get_current()
        protocol = "https" if getattr(settings, "SECURE_SSL_REDIRECT", False) else "http"
        return f"{protocol}://{current_site.domain}{path}"
    except Site.DoesNotExist:
        return f"http://127.0.0.1:8000{path}"


@receiver(post_save, sender=Internship)
def send_new_internship_notification(sender, instance, created, **kwargs):
    """A new opportunity is announced to opted-in students off the request (batched job, see notifications.py)."""
    if not created:
        return
    try:
        schedule_new_internship_emails(instance)
    except Exception:
        logger.exception("Failed to schedule the new-internship notification")


@receiver(post_save, sender="internest_students.StudentCV")
def sync_education_from_cv(sender, instance, created, update_fields=None, **kwargs):
    """Creating, editing or deleting an education entry in the CV Builder updates StudentProfile.university / major."""
    if update_fields is not None and "data" not in update_fields:
        return  # AI-usage counters etc.
    from .student_hub import sync_profile_education

    sync_profile_education(instance.student, (instance.data or {}).get("education"))


@receiver(post_save, sender=StudentProfile)
def _remember_student(sender, instance, **kwargs):
    identity.remember(instance, identity.STUDENT_ATTR)


@receiver(post_save, sender=PartnerProfile)
def _remember_partner(sender, instance, **kwargs):
    identity.remember(instance, identity.PARTNER_ATTR)


@receiver(post_delete, sender=StudentProfile)
def _forget_student(sender, instance, **kwargs):
    identity.remember(instance, identity.STUDENT_ATTR, deleted=True)


@receiver(post_delete, sender=PartnerProfile)
def _forget_partner(sender, instance, **kwargs):
    identity.remember(instance, identity.PARTNER_ATTR, deleted=True)


@receiver(user_logged_in)
def re_verify_emails_on_login(sender, request, user, **kwargs):
    """Every login resets verified flags + issues fresh OTPs so the student
    must re-confirm ownership of their personal/university emails (except an address a social provider just vouched for)."""
    from .email_helpers import issue_and_send_verification  # local: avoid circular import

    try:
        profile = user.studentprofile
    except StudentProfile.DoesNotExist:
        return

    # A social login whose provider vouched for this very address (see SocialAccountAdapter.pre_social_login) keeps it verified.
    vouched = getattr(request, "social_verified_emails", set())
    touched = False
    if profile.personal_email and profile.personal_email_verified_at is not None and profile.personal_email.lower() not in vouched:
        profile.personal_email_verified_at = None
        touched = True
    if profile.university_email and profile.university_email_verified_at is not None and profile.university_email.lower() not in vouched:
        profile.university_email_verified_at = None
        touched = True

    if touched:
        profile.save(update_fields=[
            "personal_email_verified_at",
            "university_email_verified_at",
        ])

    if profile.personal_email and not profile.is_personal_email_verified:
        issue_and_send_verification(user, profile.personal_email, EmailVerification.EMAIL_TYPE_PERSONAL)
    if profile.university_email and not profile.is_university_email_verified:
        issue_and_send_verification(user, profile.university_email, EmailVerification.EMAIL_TYPE_UNIVERSITY)


@receiver([post_save, post_delete], sender="internest_core.TrustStory")
@receiver([post_save, post_delete], sender="internest_core.TeamMember")
def _refresh_trust_page(sender, **kwargs):
    """Unpublishing a story or a team member must not stay visible from a cached page."""
    from .page_cache import bump

    bump("trust")
    bump("public")
