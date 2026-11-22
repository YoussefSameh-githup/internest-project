"""Bulk "new opportunity" email to opted-in students, run off the request through internest_core/async_tasks.

The post_save signal only enqueues (after the transaction commits); the job streams the recipient list with
iterator() and sends one BCC message per batch of NEW_INTERNSHIP_BATCH_SIZE (<= 500) addresses over a single
connection, so a large audience never builds one giant message or list in memory. Imported by signals.py at
startup, so web and worker processes both have the job registered.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string

from .async_tasks import register

logger = logging.getLogger(__name__)

NEW_INTERNSHIP_JOB = "new_internship_notification"
MAX_BATCH_SIZE = 500


def batch_size() -> int:
    return max(1, min(int(getattr(settings, "NEW_INTERNSHIP_BATCH_SIZE", MAX_BATCH_SIZE)), MAX_BATCH_SIZE))


def opted_in_recipients():
    """Opted-in students' personal emails, streamed (never materialised as one list)."""
    from .models import StudentProfile

    return (StudentProfile.objects
            .filter(personal_email__isnull=False, email_new_opportunities=True)
            .exclude(personal_email="")
            .order_by("pk")
            .values_list("personal_email", flat=True)
            .iterator(chunk_size=batch_size()))


def _batches(emails, size):
    batch = []
    for email in emails:
        batch.append(email)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def send_new_internship_emails(internship_id) -> dict:
    """Send the announcement in batches. Returns {"recipients": n, "batches": m}; never raises on a missing row."""
    from django.urls import reverse

    from .models import Internship
    from .signals import _absolute_url

    internship = Internship.objects.select_related("partner").filter(pk=internship_id).first()
    if internship is None:
        return {"recipients": 0, "batches": 0}
    partner_name = internship.partner.company_name
    subject = f"فرصة تدريب جديدة متاحة: {internship.title} من {partner_name}"
    html_content = render_to_string("emails/new_internship_notification.html", {
        "internship_title": internship.title,
        "partner_name": partner_name,
        "internship_url": _absolute_url(reverse("internship_detail", args=[internship.pk])),
    })
    recipients = batches = 0
    connection = get_connection()
    try:
        connection.open()
        for batch in _batches(opted_in_recipients(), batch_size()):
            msg = EmailMultiAlternatives(subject=subject, body=html_content, from_email=settings.DEFAULT_FROM_EMAIL,
                                         bcc=batch, connection=connection)
            msg.attach_alternative(html_content, "text/html")
            try:
                msg.send(fail_silently=False)
            except Exception:  # one failed batch must not stop the rest
                logger.exception("New-internship notification batch %d for internship %s failed", batches + 1, internship.pk)
            else:
                recipients += len(batch)
            batches += 1
    finally:
        connection.close()
    logger.info("Sent new-internship notification for internship %s to %d recipients in %d batches",
                internship.pk, recipients, batches)
    return {"recipients": recipients, "batches": batches}


@register(NEW_INTERNSHIP_JOB)
def new_internship_notification_job(user_id, internship_id):
    return 200, send_new_internship_emails(internship_id)


def schedule_new_internship_emails(internship) -> None:
    """Enqueue the announcement once the internship row is committed (the worker must be able to read it)."""
    from django.db import transaction

    from .async_tasks import enqueue

    partner_user_id = internship.partner.user_id
    internship_id = internship.pk

    def _enqueue():
        from django.contrib.auth import get_user_model

        try:
            enqueue(NEW_INTERNSHIP_JOB, get_user_model()(pk=partner_user_id), internship_id=internship_id)
        except Exception:
            logger.exception("Could not enqueue the new-internship notification for internship %s", internship_id)

    transaction.on_commit(_enqueue)
