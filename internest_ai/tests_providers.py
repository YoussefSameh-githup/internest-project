"""Provider adapters with mocked HTTP (httpx.MockTransport behind the pooled client), registry entries, mock provider."""
import json
from unittest import mock

import httpx
import openai
from django.core.cache import cache
from django.test import TestCase, override_settings

from internest_core import api_registry
from internest_core.models import APIIncident

from . import engine, providers, tasks
from .prompts import build_messages
from .testing import MOCK_ONLY, NO_PROVIDERS


def _completion(content='{"ok": true}', prompt_tokens=12, completion_tokens=5):
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


class _FakeHTTP:
    """Every outbound request is answered by `handler`; the requests are kept for assertions."""

    def __init__(self, testcase, handler):
        self.requests = []

        def record(request):
            self.requests.append(request)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(record))
        testcase.addCleanup(client.close)
        patcher = mock.patch("internest_core.http_client.get_client", return_value=client)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    @property
    def body(self):
        return json.loads(self.requests[-1].content)


def _only(key):
    [p] = [p for p in providers.configured() if p.key == key]
    return p


@override_settings(**NO_PROVIDERS)
class AdapterTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(GROQ_API_KEY="gsk-test-key", GROQ_MODEL="")
    def test_groq_adapter_posts_an_openai_request_and_normalises_the_result(self):
        http = _FakeHTTP(self, lambda r: httpx.Response(200, json=_completion('{"a": 1}', 30, 10)))
        groq = _only("groq")
        self.assertEqual((groq.privacy, groq.free, groq.json, groq.model), (providers.EXTERNAL, True, True, "llama-3.3-70b-versatile"))
        result = groq.chat(build_messages("sys", "hello"), max_tokens=300, timeout=5, max_retries=0)
        request = http.requests[0]
        self.assertEqual(str(request.url), "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer gsk-test-key")
        self.assertEqual((http.body["model"], http.body["max_tokens"], http.body["temperature"]), ("llama-3.3-70b-versatile", 300, 0))
        self.assertEqual(http.body["response_format"], {"type": "json_object"})
        self.assertEqual((result.text, result.prompt_tokens, result.completion_tokens, result.total_tokens), ('{"a": 1}', 30, 10, 40))
        self.assertEqual((result.provider, result.model, result.finish_reason), ("groq", "llama-3.3-70b-versatile", "stop"))
        self.assertGreaterEqual(result.latency_ms, 0)

    @override_settings(CLOUDFLARE_ACCOUNT_ID="acc123", CLOUDFLARE_API_TOKEN="cf-token")
    def test_cloudflare_uses_the_account_endpoint_without_json_mode(self):
        http = _FakeHTTP(self, lambda r: httpx.Response(200, json=_completion()))
        cf = _only("cloudflare_ai")
        self.assertFalse(cf.long_context)
        cf.chat(build_messages("sys", "hi"), timeout=5)
        self.assertEqual(str(http.requests[0].url), "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1/chat/completions")
        self.assertNotIn("response_format", http.body)
        self.assertIn("no markdown", http.body["messages"][0]["content"])  # JSON is still asked for in the prompt

    @override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="")
    def test_openrouter_defaults_to_a_free_model(self):
        _FakeHTTP(self, lambda r: httpx.Response(200, json=_completion()))
        router_provider = _only("openrouter")
        self.assertTrue(router_provider.model.endswith(":free"))
        self.assertTrue(router_provider.free)

    @override_settings(OPENROUTER_API_KEY="or-key", OPENROUTER_MODEL="openai/gpt-4o")
    def test_openrouter_paid_model_is_flagged_paid(self):
        self.assertFalse(_only("openrouter").free)

    @override_settings(AI_LOCAL_BASE_URL="http://127.0.0.1:11434/v1", AI_LOCAL_MODEL="qwen2.5:3b")
    def test_local_server_needs_no_key_and_is_private(self):
        http = _FakeHTTP(self, lambda r: httpx.Response(200, json=_completion()))
        local = _only("local_llm")
        self.assertEqual((local.privacy, local.free, local.model), (providers.LOCAL, True, "qwen2.5:3b"))
        local.chat(build_messages("s", "u"), timeout=5)
        self.assertEqual(str(http.requests[0].url), "http://127.0.0.1:11434/v1/chat/completions")

    @override_settings(GROQ_API_KEY="gsk-test-key", ADMIN_ALERT_EMAIL="", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_failures_go_through_the_api_monitor_under_the_registry_key(self):
        _FakeHTTP(self, lambda r: httpx.Response(500, json={"error": {"message": "groq down"}}))
        with self.assertRaises(openai.InternalServerError):
            _only("groq").chat(build_messages("s", "u"), timeout=5, max_retries=0)
        incident = APIIncident.objects.get()
        self.assertEqual((incident.provider.key, incident.provider.registered, incident.http_status), ("groq", True, 500))

    def test_secrets_never_appear_in_the_repr(self):
        p = providers.OpenAICompatibleProvider(key="groq", label="GROQ_API_KEY", model="m", base_url="https://x", api_key="gsk-secret-123")
        self.assertNotIn("gsk-secret-123", repr(p))

    def test_every_new_provider_is_registered_as_ai(self):
        for key in ("groq", "openrouter", "cloudflare_ai", "local_llm"):
            spec = api_registry.REGISTRY[key]
            self.assertEqual(spec.category, "ai")
            self.assertTrue(spec.env_var and spec.code_location and spec.dashboard_url.startswith("https://"))

    @override_settings(GEMINI_API_KEY="g", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/",
                       AGENTROUTER_API_KEY="a", AGENTROUTER_BASE_URL="https://agentrouter.example/v1", GROQ_API_KEY="q")
    def test_legacy_keys_keep_their_flags(self):
        flags = {p.key: (p.free, p.privacy) for p in providers.configured()}
        self.assertEqual(flags, {"gemini": (True, "external"), "agentrouter": (False, "external"), "groq": (True, "external")})


@override_settings(**NO_PROVIDERS)
class MockProviderTests(TestCase):
    @override_settings(AI_MOCK_PROVIDER=True, AI_MOCK_ALLOW_PRODUCTION=False)
    def test_refused_when_debug_is_off_without_the_production_override(self):
        with self.assertLogs("internest_ai.providers", "ERROR"):
            self.assertFalse(providers.mock_enabled())
        with self.assertRaises(engine.AIUnavailable):
            engine.run("anything", system="s", prompt="p")

    @override_settings(AI_MOCK_PROVIDER=True, DEBUG=True)
    def test_allowed_in_debug(self):
        self.assertTrue(providers.mock_enabled())

    @override_settings(**MOCK_ONLY, GEMINI_API_KEY="g-key")
    def test_mock_replaces_the_whole_chain_and_answers_the_registered_response(self):
        self.assertEqual(providers.configured(), [providers.MOCK])
        with tasks.override_mock("mock_demo", {"summary": "ok", "skills": ["SQL"]}), mock.patch(
                "openai.resources.chat.completions.Completions.create") as create:
            self.assertEqual(engine.run("mock_demo", system="s", data="cv"), {"summary": "ok", "skills": ["SQL"]})
        create.assert_not_called()

    @override_settings(**MOCK_ONLY)
    def test_schema_example_when_no_mock_is_registered_and_callable_mocks(self):
        with mock.patch.dict(tasks._TASKS):
            tasks.register_task("mock_schema", schema={"name": str, "years": (int, float), "skills": [str], "note?": str})
            self.assertEqual(engine.run("mock_schema", system="s"), {"name": "mock", "years": 1, "skills": ["mock"], "note": "mock"})
            tasks.register_mock_response("mock_schema", lambda messages: {"name": messages[1]["content"][:2], "years": 2, "skills": []})
            self.assertEqual(engine.run("mock_schema", system="s", prompt="Hi there")["name"], "Hi")

    def test_a_mock_that_breaks_its_schema_is_rejected_at_registration(self):
        with mock.patch.dict(tasks._TASKS), self.assertRaises(ValueError):
            tasks.register_task("mock_bad", schema={"name": str}, mock={"name": 3})

    @override_settings(**dict(MOCK_ONLY, AI_MOCK_LATENCY_MS=40))
    def test_configurable_latency(self):
        with mock.patch("internest_ai.providers.time.sleep") as sleep:
            result = engine.execute("mock_latency", system="s", prompt="p")
        sleep.assert_called_once_with(0.04)
        self.assertEqual((result.provider, result.latency_ms), ("mock", 40))
