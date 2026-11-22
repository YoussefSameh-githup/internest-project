"""Router order and policy, circuit breaker (open / half-open / Retry-After) and the engine skipping open circuits."""
from unittest import mock

import httpx
import openai
from django.core.cache import cache
from django.test import TestCase, override_settings

from . import breaker, engine, router
from .tasks import TaskSpec
from .testing import CREATE, NO_PROVIDERS, reply

FOUR = dict(NO_PROVIDERS, GEMINI_API_KEY="g", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
            AGENTROUTER_API_KEY="a", AGENTROUTER_BASE_URL="https://agentrouter.example/v1", GROQ_API_KEY="q",
            CLOUDFLARE_ACCOUNT_ID="acc", CLOUDFLARE_API_TOKEN="t", AI_LOCAL_BASE_URL="http://127.0.0.1:11434/v1")
BREAKER = dict(AI_BREAKER_ENABLED=True, AI_BREAKER_FAILURES=2, AI_BREAKER_WINDOW_SECONDS=60, AI_BREAKER_OPEN_SECONDS=30,
               AI_BREAKER_MAX_RETRY_AFTER=600)


def _status_error(code, headers=None):
    response = httpx.Response(code, headers=headers or {}, request=httpx.Request("POST", "https://x.test/v1/chat/completions"))
    cls = openai.RateLimitError if code == 429 else openai.InternalServerError
    return cls(f"HTTP {code}", response=response, body=None)


@override_settings(**FOUR)
class RouterTests(TestCase):
    def test_default_order_is_local_then_free_then_paid(self):
        self.assertEqual([p.key for p in router.ordered()], ["local_llm", "gemini", "groq", "cloudflare_ai", "agentrouter"])

    @override_settings(AI_PROVIDER_ORDER=["groq", "API_KEY_1", "nope"])
    def test_explicit_order_uses_only_the_listed_configured_providers(self):
        self.assertEqual([p.key for p in router.ordered()], ["groq", "agentrouter"])

    @override_settings(AI_LOCAL_BASE_URL="", GROQ_API_KEY="")
    def test_unconfigured_providers_are_skipped(self):
        self.assertEqual([p.key for p in router.ordered()], ["gemini", "cloudflare_ai", "agentrouter"])

    def test_private_tasks_redact_only_for_external_providers(self):
        routes = router.chain(TaskSpec("t"), private=True)
        self.assertEqual({r.provider.key: r.redact for r in routes},
                         {"local_llm": False, "gemini": True, "groq": True, "cloudflare_ai": True, "agentrouter": True})
        self.assertFalse(any(r.redact for r in router.chain(TaskSpec("t"), private=False)))

    def test_local_only_and_long_context_policies(self):
        self.assertEqual([p.key for p in router.eligible(TaskSpec("t", local_only=True))], ["local_llm"])
        self.assertEqual([p.key for p in router.eligible(TaskSpec("t", long_context=True))], ["gemini", "groq", "agentrouter"])

    @override_settings(**BREAKER)
    def test_open_circuits_are_left_out_of_the_chain(self):
        cache.clear()
        breaker.record_failure("gemini")
        breaker.record_failure("gemini")
        self.assertNotIn("gemini", [r.provider.key for r in router.chain(TaskSpec("t"), private=False)])
        self.assertIn("gemini", [p.key for p in router.eligible(TaskSpec("t"))])  # still part of the cache key
        cache.clear()


@override_settings(**BREAKER)
class BreakerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.clock = [1_000_000.0]
        patcher = mock.patch.object(breaker, "_now", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opens_after_consecutive_failures_then_half_opens_with_a_single_probe(self):
        breaker.record_failure("groq")
        self.assertEqual(breaker.state("groq"), breaker.CLOSED)
        with self.assertLogs("internest_ai.breaker", "WARNING"):
            breaker.record_failure("groq")
        self.assertEqual(breaker.state("groq"), breaker.OPEN)
        self.assertFalse(breaker.allow("groq"))
        self.clock[0] += 31
        self.assertEqual(breaker.state("groq"), breaker.HALF_OPEN)
        self.assertTrue(breaker.available("groq"))
        self.assertTrue(breaker.allow("groq"))       # the one probe
        self.assertFalse(breaker.allow("groq"))      # everyone else waits for it
        self.assertFalse(breaker.available("groq"))
        breaker.record_success("groq")
        self.assertEqual(breaker.state("groq"), breaker.CLOSED)
        self.assertTrue(breaker.allow("groq"))

    def test_a_failed_probe_reopens_the_circuit(self):
        breaker.record_failure("groq")
        breaker.record_failure("groq")
        self.clock[0] += 31
        self.assertTrue(breaker.allow("groq"))
        breaker.record_failure("groq")
        self.assertEqual(breaker.state("groq"), breaker.OPEN)
        self.clock[0] += 29
        self.assertEqual(breaker.state("groq"), breaker.OPEN)
        self.clock[0] += 2
        self.assertEqual(breaker.state("groq"), breaker.HALF_OPEN)

    def test_failures_outside_the_window_do_not_add_up_and_success_resets(self):
        breaker.record_failure("groq")
        self.clock[0] += 61
        breaker.record_failure("groq")
        self.assertEqual(breaker.state("groq"), breaker.CLOSED)
        breaker.record_success("groq")
        breaker.record_failure("groq")
        self.assertEqual(breaker.state("groq"), breaker.CLOSED)

    def test_429_retry_after_opens_for_that_long_capped(self):
        breaker.record_failure("groq", _status_error(429, {"Retry-After": "120"}))
        self.assertEqual(breaker.state("groq"), breaker.OPEN)
        self.clock[0] += 119
        self.assertEqual(breaker.state("groq"), breaker.OPEN)
        self.clock[0] += 2
        self.assertEqual(breaker.state("groq"), breaker.HALF_OPEN)
        breaker.record_failure("gemini", _status_error(429, {"Retry-After": "86400"}))
        self.assertEqual(breaker.retry_after_of(_status_error(429, {"Retry-After": "86400"})), 600)
        self.assertIsNone(breaker.retry_after_of(_status_error(429)))
        self.assertIsNone(breaker.retry_after_of(_status_error(500, {"Retry-After": "5"})))

    @override_settings(AI_BREAKER_ENABLED=False)
    def test_disabled_breaker_never_opens(self):
        for _ in range(5):
            breaker.record_failure("groq")
        self.assertTrue(breaker.allow("groq"))

    def test_a_broken_cache_never_breaks_the_call(self):
        with mock.patch.object(breaker.cache, "get", side_effect=ConnectionError("redis down")), \
                self.assertLogs("internest_ai.breaker", "ERROR"):
            self.assertTrue(breaker.allow("groq"))


@override_settings(**dict(NO_PROVIDERS, GEMINI_API_KEY="g", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
                          AGENTROUTER_API_KEY="a", AGENTROUTER_BASE_URL="https://agentrouter.example/v1"), **BREAKER)
class EngineBreakerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_tripped_provider_is_not_called_at_all(self):
        with mock.patch(CREATE, side_effect=[_status_error(500), reply({"ok": 1}), _status_error(500), reply({"ok": 2})]), \
                self.assertLogs("internest_ai", "WARNING"):
            engine.run("t1", system="s", prompt="a")
            engine.run("t1", system="s", prompt="b")  # second consecutive Gemini failure: the circuit opens
        self.assertEqual(breaker.state("gemini"), breaker.OPEN)
        with mock.patch(CREATE, return_value=reply({"ok": 3})) as create:
            self.assertEqual(engine.run("t1", system="s", prompt="c"), {"ok": 3})
        self.assertEqual([c.kwargs["model"] for c in create.call_args_list], ["gpt-4o-mini"])  # Gemini skipped, no timeout paid

    def test_every_circuit_open_fails_fast(self):
        for key in ("gemini", "agentrouter"):
            breaker.record_failure(key, _status_error(429, {"Retry-After": "60"}))
        with mock.patch(CREATE) as create, self.assertRaisesMessage(engine.AIUnavailable, "circuit open"):
            engine.run("t1", system="s", prompt="d")
        create.assert_not_called()
