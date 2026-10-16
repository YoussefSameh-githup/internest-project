"""Opportunity expiry is computed live, so nothing has to run for a post to close.

`Internship.objects.open()` (active + deadline not passed) feeds every student list, `Internship.status`
reports open / expired / closed for dashboards and the admin. Expired posts keep `is_active=True` so
"expired" (deadline passed) stays distinguishable from "closed" (the company filled the role).
"""
import logging

from .models import Internship

logger = logging.getLogger(__name__)


def clean_expired_opportunities():
    """Report how many opportunities expired. Safe to schedule; it changes nothing."""
    count = Internship.objects.expired().count()
    logger.info("%d active opportunities are past their deadline (hidden from students automatically)", count)
    return count
