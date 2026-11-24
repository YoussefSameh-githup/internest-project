"""Async task reliability: idempotency, per-user cap (429), backpressure (503), priorities, retries, dead letter, re-claim."""
import io
import json
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core import async_tasks
from internest_core.models import AsyncTask
from internest_core.tests_async_ai import LLM_KEYS
from internest_skills.tests import Base

ASYNC = {"HTTP_PREFER": "respond-async"}
DB = dict(ASYNC_TASK_BACKEND="db", ASYNC_TASK_USER_MAX_ACTIVE=2, ASYNC_TASK_MAX_QUEUED=500, ASYNC_TASK_MAX_ATTEMPTS=3,
          ASYNC_TASK_RETRY_BASE_SECONDS=5, ASYNC_TASK_RETRY_MAX_SECONDS=300, ASYNC_TASK_LEASE_SECONDS=300)


class _Jobs:
    """Test-only job kinds, registered for the duration of one test."""

    def __init__(self, testcase):
        self.calls = []
        self.outcomes = []
        jobs = {"t_echo": self.echo, "t_flaky": self.flaky}
        for kind, fn in jobs.items():
            async_tasks.REGISTRY[kind] = fn
            testcase.addCleanup(async_tasks.REGISTRY.pop, kind, None)

    def echo(self, user_id, **kwargs):
        self.calls.append((user_id, kwargs))
        return 200, {"user_id": user_id, **kwargs}

    def flaky(self, user_id, **kwargs):
        self.calls.append((user_id, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "retry":
            raise async_tasks.RetryTask("provider busy")
        if outcome == "network":
            import httpx
            raise httpx.ConnectError("refused")
        return 200, {"ok": True}


@override_settings(**DB)
class AdmissionTests(Base):
    def setUp(self):
        super().setUp()
        self.jobs = _Jobs(self)

    def test_same_key_while_active_returns_the_existing_task(self):
        first = async_tasks.enqueue("t_echo", self.student_user, idempotency_key="k1", n=1)
        again = async_tasks.enqueue("t_echo", self.student_user, idempotency_key="k1", n=1)
        self.assertEqual((again.pk, again.deduplicated, first.deduplicated), (first.pk, True, False))
        self.assertEqual(AsyncTask.objects.count(), 1)
        other = async_tasks.enqueue("t_echo", self.other_user, idempotency_key="k1", n=1)  # another user: its own task
        self.assertNotEqual(other.pk, first.pk)
        async_tasks.run(first.pk)
        fresh = async_tasks.enqueue("t_echo", self.student_user, idempotency_key="k1", n=1)  # finished: a new run is fine
        self.assertNotEqual(fresh.pk, first.pk)

    def test_a_stale_duplicate_does_not_block_a_new_task(self):
        old = async_tasks.enqueue("t_echo", self.student_user, idempotency_key="k2")
        AsyncTask.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=2))
        new = async_tasks.enqueue("t_echo", self.student_user, idempotency_key="k2")
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(AsyncTask.objects.get(pk=old.pk).status, "failed")

    @override_settings(**LLM_KEYS)
    def test_view_double_submit_is_deduplicated_on_the_arguments(self):
        self.client.force_login(self.student_user)
        body = json.dumps({"cv": {"name": "Raw", "summary": "Finance student"}})
        first = self.client.post(reverse("cv_builder_polish"), data=body, content_type="application/json", **ASYNC).json()
        second = self.client.post(reverse("cv_builder_polish"), data=body, content_type="application/json", **ASYNC).json()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(AsyncTask.objects.filter(kind="cv_polish").count(), 1)

    def test_per_user_active_cap(self):
        async_tasks.enqueue("t_echo", self.student_user, n=1)
        async_tasks.enqueue("t_echo", self.student_user, n=2)
        with self.assertRaises(async_tasks.TaskThrottled) as ctx:
            async_tasks.enqueue("t_echo", self.student_user, n=3)
        self.assertEqual((ctx.exception.status, ctx.exception.code, ctx.exception.retry_after), (429, "too_many_tasks", 10))
        async_tasks.enqueue("t_echo", self.other_user, n=1)  # the cap is per user
        stale = AsyncTask.objects.filter(user=self.student_user).first()
        AsyncTask.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(hours=2))
        async_tasks.enqueue("t_echo", self.student_user, n=4)  # a stale row no longer counts

    @override_settings(**LLM_KEYS)
    def test_per_user_cap_answers_429_with_retry_after_from_the_view(self):
        self.client.force_login(self.student_user)
        for n in range(2):
            async_tasks.enqueue("t_echo", self.student_user, n=n)
        resp = self.client.post(reverse("cv_builder_polish"), data=json.dumps({"cv": {"name": "Raw", "summary": "Finance student"}}),
                                content_type="application/json", **ASYNC)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual((resp["Retry-After"], resp["Cache-Control"]), ("10", "no-store"))
        self.assertEqual((resp.json()["code"], resp.json()["retry_after"]), ("too_many_tasks", 10))
        self.assertIn("tasks in progress", resp.json()["error"])

    @override_settings(ASYNC_TASK_MAX_QUEUED=2, ASYNC_TASK_USER_MAX_ACTIVE=0)
    def test_global_backpressure_answers_503(self):
        async_tasks.enqueue("t_echo", self.student_user, n=1)
        async_tasks.enqueue("t_echo", self.other_user, n=1)
        resp = async_tasks.enqueue_response("t_echo", self.student_user, n=2)
        self.assertEqual((resp.status_code, resp["Retry-After"]), (503, "30"))
        self.assertEqual(json.loads(resp.content)["code"], "queue_full")

    @override_settings(ASYNC_TASK_BACKEND="sync", ASYNC_TASK_MAX_QUEUED=1)
    def test_sync_backend_has_no_queue_to_fill(self):
        AsyncTask.objects.create(user=self.other_user, kind="t_echo")
        task = async_tasks.enqueue("t_echo", self.student_user, n=1)
        self.assertEqual((task.status, task.result["n"]), ("done", 1))

    def test_system_jobs_have_no_user(self):
        task = async_tasks.enqueue("t_echo", None, idempotency_key="nightly", job="nightly")
        self.assertEqual(async_tasks.enqueue("t_echo", None, idempotency_key="nightly", job="nightly").pk, task.pk)
        call_command("run_async_tasks", "--once", stdout=io.StringIO())
        task.refresh_from_db()
        self.assertEqual((task.status, task.user_id, task.result), ("done", None, {"user_id": None, "job": "nightly"}))


@override_settings(**DB)
class WorkerTests(Base):
    def setUp(self):
        super().setUp()
        self.jobs = _Jobs(self)

    def test_higher_priority_runs_first(self):
        low = async_tasks.enqueue("t_echo", None, priority=AsyncTask.PRIORITY_LOW, n="low")
        normal = async_tasks.enqueue("t_echo", self.student_user, n="normal")
        high = async_tasks.enqueue("t_echo", self.other_user, priority=AsyncTask.PRIORITY_HIGH, n="high")
        for _ in range(3):
            async_tasks.run_next()
        self.assertEqual([kwargs["n"] for _uid, kwargs in self.jobs.calls], ["high", "normal", "low"])
        self.assertTrue(all(t.status == "done" for t in AsyncTask.objects.filter(pk__in=[low.pk, normal.pk, high.pk])))

    def test_retryable_errors_back_off_then_succeed(self):
        self.jobs.outcomes = ["retry", "network", "ok"]
        task = async_tasks.enqueue("t_flaky", self.student_user)
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            self.assertTrue(async_tasks.run_next())
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempts, task.error), ("queued", 1, "RetryTask: provider busy"))
        self.assertAlmostEqual((task.next_run_at - timezone.now()).total_seconds(), 5, delta=2)
        self.assertNotEqual(task.payload, {})  # kept for the next attempt
        self.assertFalse(async_tasks.run_next())  # not due yet
        AsyncTask.objects.filter(pk=task.pk).update(next_run_at=timezone.now())
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            async_tasks.run_next()
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempts), ("queued", 2))
        self.assertAlmostEqual((task.next_run_at - timezone.now()).total_seconds(), 10, delta=2)  # exponential
        AsyncTask.objects.filter(pk=task.pk).update(next_run_at=timezone.now())
        async_tasks.run_next()
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempts, task.result, task.payload), ("done", 3, {"ok": True}, {}))

    def test_dead_after_max_attempts(self):
        self.jobs.outcomes = ["retry", "retry"]
        task = async_tasks.enqueue("t_flaky", self.student_user, max_attempts=2)
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            async_tasks.run_next()
        AsyncTask.objects.filter(pk=task.pk).update(next_run_at=None)
        with self.assertLogs("internest_core.async_tasks", "ERROR"):
            async_tasks.run_next()
        task.refresh_from_db()
        self.assertEqual((task.status, task.http_status, task.attempts, task.payload), ("dead", 503, 2, {}))
        self.assertTrue(task.is_finished)
        self.assertIn("error", task.result)
        self.client.force_login(self.student_user)
        body = self.client.get(reverse("async_task_status", args=[task.pk])).json()
        self.assertEqual((body["status"], body["http_status"]), ("dead", 503))

    @override_settings(ASYNC_TASK_BACKEND="thread")
    def test_in_process_backends_cannot_retry(self):
        self.jobs.outcomes = ["retry"]
        task = AsyncTask.objects.create(user=self.student_user, kind="t_flaky")
        with self.assertLogs("internest_core.async_tasks", "ERROR"):
            async_tasks.run(task.pk, {})
        self.assertEqual(AsyncTask.objects.get(pk=task.pk).status, "dead")

    def test_a_task_stuck_in_running_is_reclaimed(self):
        task = async_tasks.enqueue("t_echo", self.student_user, n=1)
        AsyncTask.objects.filter(pk=task.pk).update(status="running", attempts=1, started_at=timezone.now() - timedelta(minutes=10),
                                                    lease_expires_at=timezone.now() - timedelta(minutes=5))  # its worker died
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            out = io.StringIO()
            call_command("run_async_tasks", "--once", stdout=out)
        self.assertIn("1 stale task(s) re-claimed", out.getvalue())
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempts), ("queued", 1))  # backing off before attempt 2
        AsyncTask.objects.filter(pk=task.pk).update(next_run_at=None)
        call_command("run_async_tasks", "--once", stdout=io.StringIO())
        task.refresh_from_db()
        self.assertEqual((task.status, task.attempts, task.result["n"]), ("done", 2, 1))

    def test_an_expired_lease_without_attempts_left_is_dead(self):
        task = async_tasks.enqueue("t_echo", self.student_user, max_attempts=1)
        AsyncTask.objects.filter(pk=task.pk).update(status="running", attempts=1, lease_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertLogs("internest_core.async_tasks", "ERROR"):
            self.assertEqual(async_tasks.reclaim_stale(), 1)
        self.assertEqual(AsyncTask.objects.get(pk=task.pk).status, "dead")

    def test_running_tasks_inside_their_lease_are_left_alone(self):
        task = async_tasks.enqueue("t_echo", self.student_user)
        async_tasks.run_next()
        AsyncTask.objects.filter(pk=task.pk).update(status="running", lease_expires_at=timezone.now() + timedelta(minutes=1))
        self.assertEqual(async_tasks.reclaim_stale(), 0)

    def test_polling_an_expired_lease_requeues_instead_of_failing(self):
        task = async_tasks.enqueue("t_echo", self.student_user)
        AsyncTask.objects.filter(pk=task.pk).update(status="running", attempts=1, started_at=timezone.now() - timedelta(minutes=10),
                                                    lease_expires_at=timezone.now() - timedelta(minutes=1))
        self.client.force_login(self.student_user)
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            body = self.client.get(reverse("async_task_status", args=[task.pk])).json()
        self.assertEqual(body["status"], "queued")

    def test_a_task_waiting_for_its_retry_is_not_timed_out(self):
        task = async_tasks.enqueue("t_echo", self.student_user)
        AsyncTask.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=4),
                                                    next_run_at=timezone.now() - timedelta(seconds=5))
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("async_task_status", args=[task.pk])).json()["status"], "queued")

    def test_the_lease_is_set_when_claimed_and_cleared_when_finished(self):
        seen = {}

        def look(user_id):
            seen["lease"] = AsyncTask.objects.get(kind="t_look").lease_expires_at
            return 200, {}
        async_tasks.REGISTRY["t_look"] = look
        self.addCleanup(async_tasks.REGISTRY.pop, "t_look", None)
        task = async_tasks.enqueue("t_look", self.student_user)
        async_tasks.run_next()
        self.assertAlmostEqual((seen["lease"] - timezone.now()).total_seconds(), 300, delta=5)
        self.assertIsNone(AsyncTask.objects.get(pk=task.pk).lease_expires_at)


class PollerScriptTests(Base):
    def test_the_poller_handles_429_and_503(self):
        from django.conf import settings

        js = (settings.BASE_DIR / "internest_core" / "static" / "js" / "async_task_poller.js").read_text(encoding="utf-8")
        self.assertIn("r.status === 429 || r.status === 503", js)
        self.assertIn("Retry-After", js)
        self.assertIn("throttled", js)


class CeleryRetryTests(Base):
    @override_settings(**dict(DB, ASYNC_TASK_BACKEND="celery"))
    def test_a_retry_is_handed_back_to_celery_with_a_countdown(self):
        jobs = _Jobs(self)
        jobs.outcomes = ["retry"]
        task = async_tasks.enqueue("t_flaky", self.student_user)
        with mock.patch.object(async_tasks, "celery_run") as celery, self.assertLogs("internest_core.async_tasks", "WARNING"):
            async_tasks.run(task.pk)
        celery.apply_async.assert_called_once_with((str(task.pk),), countdown=5)
