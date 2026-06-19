import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.contrib.sites.models import Site
from django.core.mail import EmailMultiAlternatives
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.urls import reverse

from .models import EmailVerification, Internship, StudentProfile

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
    if not created:
        return

    try:
        internship = instance
        partner_name = internship.partner.company_name

        detail_url = _absolute_url(reverse("internship_detail", args=[internship.pk]))

        student_emails = list(
            StudentProfile.objects
            .filter(personal_email__isnull=False)
            .exclude(personal_email="")
            .values_list("personal_email", flat=True)
        )

        if not student_emails:
            logger.info("No student emails to notify about new internship %s", internship.pk)
            return

        subject = f"فرصة تدريب جديدة متاحة: {internship.title} من {partner_name}"
        html_content = render_to_string(
            "emails/new_internship_notification.html",
            {
                "internship_title": internship.title,
                "partner_name": partner_name,
                "internship_url": detail_url,
            },
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=html_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            bcc=student_emails,
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send(fail_silently=False)
        logger.info(
            "Sent new-internship notification for %s to %d recipients",
            internship.title,
            len(student_emails),
        )
    except Exception:
        logger.exception("Failed to send new-internship notification")


@receiver(user_logged_in)
def re_verify_emails_on_login(sender, request, user, **kwargs):
    """Every login resets verified flags + issues fresh OTPs so the student
    must re-confirm ownership of their personal/university emails."""
    from .email_helpers import issue_and_send_verification  # local: avoid circular import

    try:
        profile = user.studentprofile
    except StudentProfile.DoesNotExist:
        return

    touched = False
    if profile.personal_email and profile.personal_email_verified_at is not None:
        profile.personal_email_verified_at = None
        touched = True
    if profile.university_email and profile.university_email_verified_at is not None:
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
