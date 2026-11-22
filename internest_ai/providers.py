"""Provider adapters: one contract, one OpenAI-compatible implementation, one deterministic mock.

    provider.chat(messages, *, json_mode=True, max_tokens=1000, timeout=30.0, max_retries=1, task="") -> ChatResult

Every adapter is flagged with its privacy class (local / external), whether it is free, and its capabilities (json mode,
long context). Configured providers come from two places:

  * the existing keys of internest_skills/llm.py (`llm.providers()`: Gemini, AgentRouter API_KEY_1, backup API_KEY_2,
    Azure OpenAI), so their settings and tests stay exactly as they were;
  * env-only providers: Groq (GROQ_API_KEY), OpenRouter (OPENROUTER_API_KEY, free models), Cloudflare Workers AI
    (CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN) and a local Ollama / llama.cpp server (AI_LOCAL_BASE_URL, no key).

Real calls use the pooled HTTP client (through `llm._client`) and go through api_monitor.monitored_api_call under their
api_registry key. The mock provider (AI_MOCK_PROVIDER=1) replaces the whole chain; it is refused when DEBUG is False
unless AI_MOCK_ALLOW_PRODUCTION=1.
"""
import json
import logging
import time
from dataclasses import dataclass

from django.conf import settings

from internest_core.api_monitor import current_feature, monitored_api_call
from internest_core.api_registry import spec_for

logger = logging.getLogger(__name__)

LOCAL, EXTERNAL = "local", "external"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
CLOUDFLARE_DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
LOCAL_DEFAULT_MODEL = "qwen2.5:7b-instruct"


def _legacy():
    from internest_skills import llm  # the legacy module owns the original key settings and the SDK client factory

    return llm


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    finish_reason: str = ""


@dataclass(frozen=True)
class BaseProvider:
    key: str                    # api_registry key (also the circuit-breaker / usage key)
    label: str                  # the env-style label shown in logs ("API_KEY_1", "GROQ_API_KEY", ...)
    model: str
    base_url: str = ""
    privacy: str = EXTERNAL
    free: bool = False
    json: bool = True           # supports response_format={"type": "json_object"}
    long_context: bool = True   # comfortably takes a full CV / job description (>= ~32k tokens)

    @property
    def external(self) -> bool:
        return self.privacy == EXTERNAL

    @property
    def display_name(self) -> str:
        spec = spec_for(self.key)
        return spec.name if spec else self.label

    def describe(self) -> str:
        return f"{self.label} [{self.base_url} · model {self.model}]"

    def chat(self, messages, *, json_mode=True, max_tokens=1000, timeout=30.0, max_retries=1, task="") -> ChatResult:
        raise NotImplementedError


@dataclass(frozen=True)
class OpenAICompatibleProvider(BaseProvider):
    api_key: str = ""

    def __repr__(self):  # never print the key
        return f"OpenAICompatibleProvider({self.key}, {self.base_url}, {self.model})"

    def credentials(self):
        llm = _legacy()
        return llm.Provider(self.label, self.api_key, self.base_url, self.model)

    def chat(self, messages, *, json_mode=True, max_tokens=1000, timeout=30.0, max_retries=1, task="") -> ChatResult:
        llm = _legacy()
        if "googleapis.com" in self.base_url:
            # Gemini 2.5 "thinking" tokens count against max_tokens and used to cut the JSON short; leave headroom.
            max_tokens += llm.GEMINI_THINKING_HEADROOM
        kwargs = {"model": self.model, "max_tokens": max_tokens, "temperature": 0, "messages": messages}
        if json_mode and self.json:
            kwargs["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        client = llm._client(timeout, self.credentials(), max_retries)
        resp = monitored_api_call(self.key, current_feature() or "LLM request", client.chat.completions.create, **kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        prompt_tokens, completion_tokens = _int(getattr(usage, "prompt_tokens", 0)), _int(getattr(usage, "completion_tokens", 0))
        total = _int(getattr(usage, "total_tokens", 0)) or prompt_tokens + completion_tokens
        finish = getattr(choice, "finish_reason", "")
        return ChatResult(text=choice.message.content or "", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                          total_tokens=total, provider=self.key, model=self.model, latency_ms=latency_ms,
                          finish_reason=finish if isinstance(finish, str) else "")


@dataclass(frozen=True)
class MockProvider(BaseProvider):
    """Deterministic answers for tests and load benchmarks: the task's registered mock, else an example of its schema."""

    def chat(self, messages, *, json_mode=True, max_tokens=1000, timeout=30.0, max_retries=1, task="") -> ChatResult:
        from .tasks import mock_response

        latency = int(getattr(settings, "AI_MOCK_LATENCY_MS", 0) or 0)
        if latency > 0:
            time.sleep(latency / 1000)
        text = json.dumps(mock_response(task, messages), ensure_ascii=False, sort_keys=True)
        prompt_tokens = max(1, sum(len(m.get("content") or "") for m in messages) // 4)
        completion_tokens = max(1, len(text) // 4)
        return ChatResult(text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                          total_tokens=prompt_tokens + completion_tokens, provider=self.key, model=self.model,
                          latency_ms=latency, finish_reason="stop")


MOCK = MockProvider(key="mock", label="AI_MOCK_PROVIDER", model="mock-1", base_url="mock://", privacy=LOCAL, free=True)


def mock_enabled() -> bool:
    if not getattr(settings, "AI_MOCK_PROVIDER", False):
        return False
    if not settings.DEBUG and not getattr(settings, "AI_MOCK_ALLOW_PRODUCTION", False):
        logger.error("AI_MOCK_PROVIDER=1 is ignored because DEBUG is False (set AI_MOCK_ALLOW_PRODUCTION=1 for a load test)")
        return False
    return True


# ---------------------------------------------------------------- configured providers
def legacy_providers() -> list[OpenAICompatibleProvider]:
    """The original four keys of internest_skills/llm.py, in their original order."""
    llm = _legacy()
    out = []
    for p in llm.providers():
        key = llm.provider_name(p)
        spec = spec_for(key)
        out.append(OpenAICompatibleProvider(key=key, label=p.label, model=p.model, base_url=p.base_url, api_key=p.api_key,
                                            free=not (spec.is_paid if spec else True)))
    return out


def env_providers() -> list[OpenAICompatibleProvider]:
    s, out = settings, []
    if getattr(s, "GROQ_API_KEY", ""):
        out.append(OpenAICompatibleProvider(
            key="groq", label="GROQ_API_KEY", base_url=s.GROQ_BASE_URL, api_key=s.GROQ_API_KEY,
            model=getattr(s, "GROQ_MODEL", "") or GROQ_DEFAULT_MODEL, free=True))
    if getattr(s, "OPENROUTER_API_KEY", ""):
        model = getattr(s, "OPENROUTER_MODEL", "") or OPENROUTER_DEFAULT_MODEL
        out.append(OpenAICompatibleProvider(
            key="openrouter", label="OPENROUTER_API_KEY", base_url=s.OPENROUTER_BASE_URL, api_key=s.OPENROUTER_API_KEY,
            model=model, free=model.endswith(":free"), json=False))  # JSON mode depends on the routed model
    if getattr(s, "CLOUDFLARE_ACCOUNT_ID", "") and getattr(s, "CLOUDFLARE_API_TOKEN", ""):
        out.append(OpenAICompatibleProvider(
            key="cloudflare_ai", label="CLOUDFLARE_API_TOKEN", api_key=s.CLOUDFLARE_API_TOKEN,
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{s.CLOUDFLARE_ACCOUNT_ID}/ai/v1",
            model=getattr(s, "CLOUDFLARE_AI_MODEL", "") or CLOUDFLARE_DEFAULT_MODEL, free=True, json=False, long_context=False))
    if getattr(s, "AI_LOCAL_BASE_URL", ""):
        out.append(OpenAICompatibleProvider(
            key="local_llm", label="AI_LOCAL", base_url=s.AI_LOCAL_BASE_URL, api_key=getattr(s, "AI_LOCAL_API_KEY", "") or "local",
            model=getattr(s, "AI_LOCAL_MODEL", "") or LOCAL_DEFAULT_MODEL, privacy=LOCAL, free=True,
            long_context=bool(getattr(s, "AI_LOCAL_LONG_CONTEXT", False))))
    return out


def configured() -> list[BaseProvider]:
    """Every usable provider (unordered policy-wise; see router.ordered). The mock replaces them all when enabled."""
    if mock_enabled():
        return [MOCK]
    return [*legacy_providers(), *env_providers()]
