"""Drain the async task queue (ASYNC_TASK_BACKEND=db): python manage.py run_async_tasks [--once] [--idle 0.5]

Run it as a PythonAnywhere always-on task or a systemd service next to Gunicorn (deploy/azure/internest-worker.service).
Highest priority first; tasks waiting for a retry (next_run_at) are skipped until due; running tasks whose lease expired
(a worker was killed mid-job) are re-claimed at start and every --reclaim-every seconds."""
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from internest_core import async_tasks


class Command(BaseCommand):
    help = "Run queued async tasks (AI polish, voice transcription, quiz preparation)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run what is queued now, then exit.")
        parser.add_argument("--idle", type=float, default=0.5, help="Seconds to sleep when the queue is empty.")
        parser.add_argument("--reclaim-every", type=float, default=30.0, help="Seconds between stale-lease checks.")

    def handle(self, *args, **options):
        self._stop = False
        previous = None
        try:  # systemd stops the service with SIGTERM: finish the current task, then exit
            previous = signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        except (ValueError, AttributeError):  # not the main thread / platform without SIGTERM
            pass
        try:
            self._drain(options)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)

    def _drain(self, options):
        done, last_purge, last_reclaim = 0, time.monotonic(), None
        while not self._stop:
            close_old_connections()
            if last_reclaim is None or time.monotonic() - last_reclaim > options["reclaim_every"]:
                reclaimed = async_tasks.reclaim_stale()
                if reclaimed:
                    self.stdout.write(f"{reclaimed} stale task(s) re-claimed")
                last_reclaim = time.monotonic()
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
