"""Queue (or send now) an IndexNow submission for the public pages and open opportunities.

    manage.py indexnow_submit                 # public pages + sitemap + every open opportunity, via the worker
    manage.py indexnow_submit --pages         # public pages + sitemap only
    manage.py indexnow_submit URL [URL ...]   # these URLs (non-public ones are dropped)
    manage.py indexnow_submit --now ...       # send in this process and print the IndexNow status

HTTP 200/202 means IndexNow accepted the URLs, not that a search engine indexed them.
"""
from django.core.management.base import BaseCommand, CommandError

from internest_core import indexnow
from internest_core.models import Internship


class Command(BaseCommand):
    help = "Submit public Internest URLs to IndexNow (queued on the async worker unless --now)."

    def add_arguments(self, parser):
        parser.add_argument("urls", nargs="*")
        parser.add_argument("--pages", action="store_true", help="only the public pages and the sitemap")
        parser.add_argument("--now", action="store_true", help="send now instead of queueing")

    def handle(self, *args, urls, pages, now, **options):
        if not indexnow.is_configured():
            raise CommandError("IndexNow is not configured: set INDEXNOW_API_KEY and an https SITE_URL in .env.")
        if not urls:
            urls = indexnow.public_page_urls()
            if not pages:
                urls += [indexnow.internship_url(pk) for pk in Internship.objects.open().values_list("pk", flat=True)]
        kept = indexnow.clean(urls)
        if not kept:
            raise CommandError("No public Internest URL to submit.")
        if now:
            status, body = indexnow.submit(kept)
            self.stdout.write(f"{len(kept)} URL(s): {body}")
            if status != 200:
                raise CommandError("IndexNow did not accept the submission.")
            return
        indexnow.queue_urls(kept)
        self.stdout.write(f"Queued {len(kept)} URL(s) for IndexNow (job {indexnow.JOB}).")
