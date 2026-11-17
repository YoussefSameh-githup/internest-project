"""Phase 1: pooled HTTP client with safe retries, deterministic AI cache, non-blocking async AI tasks."""
import io
import json
from datetime import timedelta
from unittest import mock

import httpx
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core import ai_cache, async_tasks, http_client
from internest_core.models import AsyncTask
from internest_skills.tests import Base

LLM_KEYS = dict(GEMINI_API_KEY="g-key", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/", GEMINI_MODEL="",
                AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="", AZURE_OPENAI_API_KEY="", AZURE_OPENAI_ENDPOINT="")
ASYNC = {"HTTP_PREFER": "respond-async"}


def _reply(content):
    return mock.Mock(choices=[mock.Mock(message=mock.Mock(content=json.dumps(content)), finish_reason="stop")],
                     usage=mock.Mock(total_tokens=10, prompt_tokens=5, completion_tokens=5))


def _resp(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://api.example.test/v1"))


# ---------------------------------------------------------------- pooled client
class HttpClientTests(SimpleTestCase):
    def tearDown(self):
        http_client.close_clients()

    def test_one_pooled_client_per_process_with_limits(self):
        first = http_client.get_client()
        self.assertIs(first, http_client.get_client())
        pool = first._transport._pool
        self.assertEqual((pool._max_connections, pool._max_keepalive_connections), (100, 30))
        self.assertIsInstance(first._transport, http_client.RetryTransport)

    def test_a_forked_worker_gets_its_own_client(self):
        first = http_client.get_client()
        with mock.patch.object(http_client.os, "getpid", return_value=-1):
            self.assertIsNot(http_client.get_client(), first)

    def test_settings_change_rebuilds_the_pool(self):
        first = http_client.get_client()
        with override_settings(HTTP_CLIENT_RETRIES=0):
            self.assertIsNot(http_client.get_client(), first)
            self.assertEqual(http_client.get_client()._transport.retries, 0)

    def _send(self, outcomes, method="POST"):
        transport = http_client.RetryTransport(retries=2)
        with mock.patch.object(httpx.HTTPTransport, "handle_request", side_effect=outcomes) as inner, \
                mock.patch.object(http_client.time, "sleep") as sleep:
            try:
                result = transport.handle_request(httpx.Request(method, "https://api.example.test/v1"))
            except Exception as exc:
                result = exc
        return result, inner.call_count, sleep.call_count

    def test_retries_connect_errors_and_502_503_with_backoff(self):
        result, calls, sleeps = self._send([httpx.ConnectError("refused"), _resp(503), _resp(200)])
        self.assertEqual((result.status_code, calls, sleeps), (200, 3, 2))
        result, calls, _ = self._send([_resp(502), _resp(200)])
        self.assertEqual((result.status_code, calls), (200, 2))

    def test_never_retries_auth_quota_rate_limit_or_read_timeouts(self):
        for status in (400, 401, 402, 403, 429, 500, 504):
            result, calls, _ = self._send([_resp(status), _resp(200)])
            self.assertEqual((result.status_code, calls), (status, 1), status)
        result, calls, _ = self._send([httpx.ReadTimeout("slow"), _resp(200)])
        self.assertIsInstance(result, httpx.ReadTimeout)  # the request may have been processed: never resend it
        self.assertEqual(calls, 1)

    def test_gives_up_after_the_retry_budget(self):
        result, calls, _ = self._send([httpx.ConnectError("x")] * 3)
        self.assertIsInstance(result, httpx.ConnectError)
        self.assertEqual(calls, 3)
        result, calls, _ = self._send([_resp(503)] * 3)
        self.assertEqual((result.status_code, calls), (503, 3))

    def test_backoff_is_jittered_and_capped(self):
        for attempt in range(8):
            self.assertTrue(0 <= http_client.backoff_delay(attempt) <= 2.0)

    def test_llm_sdk_uses_the_shared_pool(self):
        from internest_skills.llm import _client

        self.assertIs(_client(5)._client, http_client.get_client())

    def test_async_client_is_pooled_per_event_loop(self):
        import asyncio

        async def grab():
            return http_client.get_async_client(), http_client.get_async_client()

        a, b = asyncio.run(grab())
        self.assertIs(a, b)
        self.assertIsInstance(a._transport, http_client.AsyncRetryTransport)


# ---------------------------------------------------------------- AI cache
@override_settings(AI_CACHE_TTL=3600, CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "ai-tests"}})
class AICacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_key_is_a_stable_sha256_of_canonical_json(self):
        a = ai_cache.cache_key("ns", {"b": 1, "a": [1, 2]})
        self.assertEqual(a, ai_cache.cache_key("ns", {"a": [1, 2], "b": 1}))
        self.assertNotEqual(a, ai_cache.cache_key("ns", {"a": [2, 1], "b": 1}))
        self.assertRegex(a, r"^ai:ns:v1:[0-9a-f]{64}$")

    def test_hit_skips_the_call(self):
        compute = mock.Mock(return_value={"x": 1})
        self.assertEqual(ai_cache.cached("ns", {"p": 1}, compute), {"x": 1})
        self.assertEqual(ai_cache.cached("ns", {"p": 1}, compute), {"x": 1})
        self.assertEqual(compute.call_count, 1)

    def test_failures_are_not_cached(self):
        compute = mock.Mock(side_effect=[RuntimeError("down"), {"ok": 1}])
        with self.assertRaises(RuntimeError):
            ai_cache.cached("ns", {"p": 2}, compute)
        self.assertEqual(ai_cache.cached("ns", {"p": 2}, compute), {"ok": 1})

    def test_disabled_by_ttl_zero_and_by_context(self):
        compute = mock.Mock(return_value=1)
        with override_settings(AI_CACHE_TTL=0):
            ai_cache.cached("ns", {"p": 3}, compute)
            ai_cache.cached("ns", {"p": 3}, compute)
        with ai_cache.no_ai_cache():
            ai_cache.cached("ns", {"p": 3}, compute)
        self.assertEqual(compute.call_count, 3)

    def test_a_broken_cache_never_breaks_the_call(self):
        with mock.patch.object(ai_cache.cache, "get", side_effect=ConnectionError("redis down")), \
                mock.patch.object(ai_cache.cache, "set", side_effect=ConnectionError("redis down")), self.assertLogs("internest_core.ai_cache"):
            self.assertEqual(ai_cache.cached("ns", {"p": 4}, lambda: 7), 7)

    @override_settings(**LLM_KEYS)
    def test_identical_llm_prompt_hits_the_api_once(self):
        from internest_skills import llm

        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=_reply({"ok": 1})) as create:
            self.assertEqual(llm.chat_json("sys", "same prompt"), {"ok": 1})
            started = timezone.now()
            self.assertEqual(llm.chat_json("sys", "same prompt"), {"ok": 1})
            self.assertLess((timezone.now() - started).total_seconds(), 0.05)
            llm.chat_json("sys", "another prompt")
        self.assertEqual(create.call_count, 2)

    @override_settings(**LLM_KEYS)
    def test_quiz_generation_is_never_cached(self):
        from internest_skills import quizgen

        skill = mock.Mock()
        with mock.patch.object(quizgen, "build_system_prompt", return_value="s"), mock.patch.object(quizgen, "build_user_prompt", return_value="u"), \
                mock.patch.object(quizgen, "validate_quiz_reply"), \
                mock.patch("openai.resources.chat.completions.Completions.create", return_value=_reply({"q": []})) as create:
            quizgen.request_raw_items(skill)
            quizgen.request_raw_items(skill)
        self.assertEqual(create.call_count, 2)


# ---------------------------------------------------------------- async tasks
@override_settings(**LLM_KEYS)
class AsyncTaskEndpointTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)

    def _polish(self, **extra):
        with mock.patch("internest_students.cv_builder_service.polish_cv", return_value={"name": "Polished"}) as polish:
            resp = self.client.post(reverse("cv_builder_polish"), data=json.dumps({"cv": {"name": "Raw"}}),
                                    content_type="application/json", **extra)
        return resp, polish

    def test_without_the_header_the_endpoint_stays_synchronous(self):
        resp, _ = self._polish()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cv"]["name"], "Polished")
        self.assertFalse(AsyncTask.objects.exists())

    @override_settings(ASYNC_TASK_BACKEND="sync")
    def test_async_request_answers_202_with_a_pollable_status(self):
        resp, polish = self._polish(**ASYNC)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["result"]["cv"]["name"], "Polished")
        self.assertEqual(resp["Location"], body["status_url"])
        status = self.client.get(body["status_url"])
        self.assertEqual(status.json()["result"]["cv"]["name"], "Polished")
        self.assertEqual(status["Cache-Control"], "no-store")
        self.assertEqual(polish.call_count, 1)

    @override_settings(ASYNC_TASK_BACKEND="thread")
    def test_thread_backend_returns_before_the_ai_runs(self):
        submitted = []
        fake_pool = mock.Mock(submit=lambda fn, *args: submitted.append((fn, args)))
        with mock.patch.object(async_tasks, "_pool", return_value=fake_pool), self.captureOnCommitCallbacks(execute=True):
            resp, polish = self._polish(**ASYNC)
            self.assertEqual(resp.status_code, 202)
            self.assertEqual(resp.json()["status"], "queued")
            self.assertEqual(resp.json()["poll_after_ms"], 600)
            self.assertEqual(polish.call_count, 0)  # the web worker did not wait for the LLM
        self.assertEqual(len(submitted), 1)
        task_id = resp.json()["task_id"]
        self.assertEqual(self.client.get(reverse("async_task_status", args=[task_id])).json()["status"], "queued")
        with mock.patch("internest_students.cv_builder_service.polish_cv", return_value={"name": "Later"}):
            async_tasks.run(*submitted[0][1])
        done = self.client.get(reverse("async_task_status", args=[task_id])).json()
        self.assertEqual((done["status"], done["http_status"], done["result"]["cv"]["name"]), ("done", 200, "Later"))

    @override_settings(ASYNC_TASK_BACKEND="db")
    def test_db_backend_persists_arguments_and_the_worker_drains_the_queue(self):
        audio = SimpleUploadedFile("v.webm", b"\x1aE\xdf\xa3voice-bytes", content_type="audio/webm")
        with mock.patch("internest_students.cv_builder_service.voice_enabled", return_value=True):
            resp = self.client.post(reverse("cv_builder_voice"), {"cv": json.dumps({"name": "A"}), "audio": audio}, **ASYNC)
        self.assertEqual(resp.status_code, 202)
        task = AsyncTask.objects.get()
        self.assertEqual(task.status, "queued")
        self.assertIn("__bytes__", json.dumps(task.payload))
        with mock.patch("internest_students.cv_builder_service.transcribe", return_value="I studied finance at Cairo University") as tr, \
                mock.patch("internest_students.cv_builder_service.polish_cv", return_value={"name": "Voice"}):
            call_command("run_async_tasks", "--once", stdout=io.StringIO())
        self.assertEqual(tr.call_args.args[0], b"\x1aE\xdf\xa3voice-bytes")
        task.refresh_from_db()
        self.assertEqual((task.status, task.result["cv"]["name"], task.payload), ("done", "Voice", {}))

    @override_settings(ASYNC_TASK_BACKEND="sync")
    def test_handled_errors_keep_their_status_and_message(self):
        from internest_skills.llm import LLMUnavailable

        with mock.patch("internest_students.cv_builder_service.polish_cv", side_effect=LLMUnavailable("busy")):
            body = self.client.post(reverse("cv_builder_polish"), data=json.dumps({"cv": {"name": "Raw"}}),
                                    content_type="application/json", **ASYNC).json()
        self.assertEqual((body["status"], body["http_status"]), ("done", 503))
        self.assertIn("busy right now", body["result"]["error"])

    @override_settings(ASYNC_TASK_BACKEND="sync")
    def test_job_answers_in_the_users_language(self):
        from internest_skills.llm import LLMUnavailable

        self.client.cookies["internest_lang"] = "ar"
        with mock.patch("internest_students.cv_builder_service.polish_cv", side_effect=LLMUnavailable("busy")):
            body = self.client.post(reverse("cv_builder_polish"), data=json.dumps({"cv": {}}), content_type="application/json", **ASYNC).json()
        self.assertNotIn("busy right now", body["result"]["error"])

    @override_settings(ASYNC_TASK_BACKEND="sync")
    def test_crash_inside_a_job_becomes_a_clean_failure(self):
        with mock.patch("internest_students.cv_builder_service.polish_cv", side_effect=ZeroDivisionError), self.assertLogs("internest_core.async_tasks", "ERROR"):
            body = self.client.post(reverse("cv_builder_polish"), data=json.dumps({"cv": {}}), content_type="application/json", **ASYNC).json()
        self.assertEqual((body["status"], body["http_status"], body["error"]), ("failed", 500, "internal_error"))

    def test_status_is_private_to_the_owner(self):
        task = AsyncTask.objects.create(user=self.other_user, kind="cv_polish")
        self.assertEqual(self.client.get(reverse("async_task_status", args=[task.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("async_task_status", args=[task.pk])).status_code, 302)

    def test_a_task_nobody_picks_up_times_out_instead_of_spinning_forever(self):
        task = AsyncTask.objects.create(user=self.student_user, kind="cv_polish")
        AsyncTask.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        body = self.client.get(reverse("async_task_status", args=[task.pk])).json()
        self.assertEqual((body["status"], body["http_status"], body["error"]), ("failed", 504, "timeout"))

    def test_cancel_only_stops_queued_tasks(self):
        queued = AsyncTask.objects.create(user=self.student_user, kind="cv_polish")
        self.assertEqual(self.client.post(reverse("async_task_cancel", args=[queued.pk])).json()["status"], "cancelled")
        self.assertFalse(async_tasks.run(queued.pk, {}))  # a cancelled task is never executed

    def test_a_task_runs_at_most_once(self):
        task = AsyncTask.objects.create(user=self.student_user, kind="cv_polish")
        with mock.patch("internest_students.cv_builder_service.polish_cv", return_value={"name": "x"}) as polish:
            self.assertTrue(async_tasks.run(task.pk, {"cv": {}}))
            self.assertFalse(async_tasks.run(task.pk, {"cv": {}}))
        self.assertEqual(polish.call_count, 1)

    def test_purge_removes_old_finished_tasks_only(self):
        old = AsyncTask.objects.create(user=self.student_user, kind="cv_polish", status="done")
        AsyncTask.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        fresh = AsyncTask.objects.create(user=self.student_user, kind="cv_polish", status="done")
        stuck = AsyncTask.objects.create(user=self.student_user, kind="cv_polish")
        AsyncTask.objects.filter(pk=stuck.pk).update(created_at=timezone.now() - timedelta(days=3))
        self.assertEqual(async_tasks.purge_finished(), 1)
        self.assertEqual(set(AsyncTask.objects.values_list("pk", flat=True)), {fresh.pk, stuck.pk})

    def test_every_job_kind_is_registered(self):
        self.assertTrue({"cv_polish", "cv_voice", "skill_challenge_start"} <= set(async_tasks.REGISTRY))


@override_settings(ASYNC_TASK_BACKEND="sync")
class AsyncChallengeStartTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.student_user)
        self.ss = self._claim()

    def test_async_start_returns_the_challenge_url(self):
        from internest_skills import views as skill_views

        attempt = mock.Mock(token="11111111-1111-1111-1111-111111111111")
        with mock.patch.object(skill_views, "start_challenge", return_value=(skill_views.CHALLENGE_STARTED, attempt)):
            body = self.client.post(reverse("skills_challenge_start", args=[self.ss.pk]), **ASYNC).json()
        self.assertEqual(body["result"]["redirect"], reverse("skills_challenge", kwargs={"token": attempt.token}))

    def test_async_errors_carry_a_message(self):
        from internest_skills import views as skill_views

        with mock.patch.object(skill_views, "start_challenge", return_value=(skill_views.CHALLENGE_REFUSED, "Cooling down")):
            body = self.client.post(reverse("skills_challenge_start", args=[self.ss.pk]), **ASYNC).json()
        self.assertEqual((body["http_status"], body["result"]["error"], body["result"]["redirect"]), (409, "Cooling down", reverse("skills_hub")))
        with mock.patch.object(skill_views, "start_challenge", return_value=(skill_views.CHALLENGE_UNAVAILABLE, self.ss)):
            body = self.client.post(reverse("skills_challenge_start", args=[self.ss.pk]), **ASYNC).json()
        self.assertEqual(body["http_status"], 503)

    def test_someone_elses_skill_is_refused_before_anything_is_queued(self):
        other = self._claim(student=self.other_student)
        self.assertEqual(self.client.post(reverse("skills_challenge_start", args=[other.pk]), **ASYNC).status_code, 404)
        self.assertFalse(AsyncTask.objects.exists())

    def test_sync_form_post_is_unchanged(self):
        from internest_skills import views as skill_views

        with mock.patch.object(skill_views, "start_challenge", return_value=(skill_views.CHALLENGE_REFUSED, "Cooling down")):
            resp = self.client.post(reverse("skills_challenge_start", args=[self.ss.pk]))
        self.assertRedirects(resp, reverse("skills_hub"), fetch_redirect_response=False)

    def test_start_forms_and_base_template_load_the_poller(self):
        page = self.client.get(reverse("skills_hub")).content.decode()
        self.assertIn("data-async-task", page)
        self.assertIn("js/async_task_poller.js", page)
        self.assertIn('bootstrap.bundle.min.js" defer', page)
