"""Email backend that watches the real one (SendGrid via django-anymail, or Gmail SMTP) and reports its failures.

settings.EMAIL_BACKEND points here; settings.MONITORED_EMAIL_BACKEND is the backend that actually delivers. Every email the
site sends (verification codes, notifications, admin alerts) therefore gets the same alerting as the AI APIs.
"""
import logging

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend

from .api_monitor import report_failure, sending_alert

logger = logging.getLogger(__name__)


def provider_key(real_backend: str) -> str:
    return "sendgrid" if "anymail" in real_backend else "gmail_smtp"


class MonitoredEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.real_backend = settings.MONITORED_EMAIL_BACKEND
        # The inner backend always raises; this wrapper reports the failure and then honours fail_silently itself.
        self.inner = get_connection(self.real_backend, fail_silently=False, **kwargs)

    def open(self):
        return self.inner.open()

    def close(self):
        return self.inner.close()

    def send_messages(self, email_messages):
        try:
            return self.inner.send_messages(email_messages)
        except Exception as exc:
            if not sending_alert():  # a failing alert email must not raise another alert
                report_failure(provider_key(self.real_backend), "Transactional email", exc)
            if not self.fail_silently:
                raise
            return 0
