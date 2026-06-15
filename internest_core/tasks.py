import logging

from django.utils import timezone

from .models import Internship

logger = logging.getLogger(__name__)


def clean_expired_opportunities():
    """Deactivate internships whose deadline has passed.

    Schedule this daily via a cron job on the production server.
    """
    today = timezone.now().date()

    expired = Internship.objects.filter(deadline__lt=today, is_active=True)
    count = expired.update(is_active=False)

    if count:
        logger.info("Deactivated %d expired internships", count)
    else:
        logger.info("No expired internships found today")
