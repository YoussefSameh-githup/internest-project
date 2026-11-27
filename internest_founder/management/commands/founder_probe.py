"""Synthetic timing of the public pages through Cloudflare (6 sequential GETs, at most once per 5 minutes).

    manage.py founder_probe            # schedule every 30 minutes
"""
from django.core.management.base import BaseCommand

from internest_founder import perf


class Command(BaseCommand):
    help = "Measure public page response times through Cloudflare (rate-limited)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="ignore the 5-minute rate limit")

    def handle(self, *args, force=False, **options):
        rows = perf.probe(force=force)
        if not rows:
            self.stdout.write("Skipped: a probe ran less than 5 minutes ago.")
        for r in rows:
            self.stdout.write(f"{r.data.get('path')}: {r.status_code or r.error} ttfb={r.ttfb_ms}ms total={r.total_ms}ms")
