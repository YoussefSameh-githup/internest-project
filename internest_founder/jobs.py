"""Founder center jobs on the existing DB queue (registered by internest_core.async_tasks.autodiscover)."""
from internest_core.async_tasks import register

from . import events


@register(events.DELIVER_JOB)
def founder_webhook_job(user_id, event_pk):
    return events.deliver(event_pk)


@register(events.EMAIL_JOB)
def founder_email_job(user_id, dedupe_key):
    return events.send_alert_email(dedupe_key)


@register("founder_probe")
def founder_probe_job(user_id):
    from . import perf

    saved = perf.probe()
    return 200, {"measured": len(saved)}
