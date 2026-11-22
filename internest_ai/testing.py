"""Test helpers for code that uses the AI engine.

    @override_settings(**NO_PROVIDERS, **MOCK_ONLY)          # deterministic answers, no network
    def test_x(self):
        with override_mock("cv_polish", {"name": "Mona"}):
            ...

    with mock.patch(CREATE, side_effect=[reply({"ok": 1})]): ...   # or fake the OpenAI SDK call itself
"""
import json
from types import SimpleNamespace

CREATE = "openai.resources.chat.completions.Completions.create"

NO_PROVIDERS = dict(
    GEMINI_API_KEY="", AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="", LLM_BACKUP_BASE_URL="", AZURE_OPENAI_API_KEY="",
    AZURE_OPENAI_ENDPOINT="", GROQ_API_KEY="", OPENROUTER_API_KEY="", CLOUDFLARE_ACCOUNT_ID="", CLOUDFLARE_API_TOKEN="",
    AI_LOCAL_BASE_URL="", AI_PROVIDER_ORDER=[], AI_MOCK_PROVIDER=False, AI_CACHE_TTL=0,
)
MOCK_ONLY = dict(AI_MOCK_PROVIDER=True, AI_MOCK_ALLOW_PRODUCTION=True, AI_MOCK_LATENCY_MS=0)


def reply(content, finish_reason="stop", prompt_tokens=11, completion_tokens=7):
    """A fake OpenAI SDK chat completion (content may be a dict, serialised as JSON)."""
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                              total_tokens=prompt_tokens + completion_tokens))
