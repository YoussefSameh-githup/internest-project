"""Email-verification helpers. Lives outside views.py so signals can import safely."""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EmailVerification

logger = logging.getLogger(__name__)


def send_verification_email(user, otp: EmailVerification) -> bool:
    label = "personal" if otp.email_type == EmailVerification.EMAIL_TYPE_PERSONAL else "university"
    subject = f"Internest — Verify your {label} email"
    context = {
        "user": user,
        "code": otp.code,
        "email_type_label": label.title(),
        "expires_minutes": int((otp.expires_at - timezone.now()).total_seconds() // 60) or 30,
    }
    html_body = render_to_string("emails/verification_email.html", context)
    text_body = render_to_string("emails/verification_email.txt", context)
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[otp.email],
    )
    msg.attach_alternative(html_body, "text/html")
    try:
        sent = msg.send(fail_silently=False)
        logger.info("Verification email sent to %s (result=%s)", otp.email, sent)
        return bool(sent)
    except Exception:
        logger.exception("Failed to send verification email to %s", otp.email)
        return False


def issue_and_send_verification(user, email: str, email_type: str) -> bool:
    if not email:
        return False
    otp = EmailVerification.issue(user=user, email=email, email_type=email_type)
    return send_verification_email(user, otp)
