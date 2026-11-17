"""Drain the async task queue (ASYNC_TASK_BACKEND=db): python manage.py run_async_tasks [--once] [--idle 0.5]

Run it as a PythonAnywhere always-on task or a systemd service next to Gunicorn."""
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from internest_core import async_tasks


class Command(BaseCommand):
    help = "Run queued async tasks (AI polish, voice transcription, quiz preparation)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run what is queued now, then exit.")
        parser.add_argument("--idle", type=float, default=0.5, help="Seconds to sleep when the queue is empty.")

    def handle(self, *args, **options):
        done, last_purge = 0, time.monotonic()
        while True:
            close_old_connections()
            if async_tasks.run_next():
                done += 1
                continue
            if options["once"]:
                break
            if time.monotonic() - last_purge > 3600:
                async_tasks.purge_finished()
                last_purge = time.monotonic()
            time.sleep(options["idle"])
        self.stdout.write(f"{done} task(s) run")
