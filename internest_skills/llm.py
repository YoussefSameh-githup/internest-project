"""Legacy entry point of the AI layer: the original provider keys, the SDK client factory and `chat_json`.

`chat_json` keeps its signature for every existing caller but delegates to the AI engine (internest_ai.engine): circuit
breaker, quotas, usage log and the extra providers (Groq, OpenRouter, Cloudflare, a local model) apply to it too.
On any quota, rate-limit, timeout, HTTP or parsing error the call fails over to the next provider. If every provider
fails, LLMUnavailable is raised so callers fall back to local logic; nothing here may turn into an HTTP 500.
New feature code should call `internest_ai.engine.run` directly, with untrusted text in `data=`.
"""
import json
import logging
import re
from dataclasses import dataclass

import httpx
import openai
from django.conf import settings

from internest_ai.errors import AIUnavailable
from internest_core.api_monitor import current_feature, mask_secrets
from internest_core.api_registry import slugify_key

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
GEMINI_THINKING_HEADROOM = 2048  # extra max_tokens for Gemini's hidden reasoning

# Listed explicitly for readability; Exception also covers API status, timeout and parsing errors.
LLM_ERRORS = (httpx.ProxyError, openai.APIConnectionError, RuntimeError, Exception)


LLMUnavailable = AIUnavailable  # the LLM could not produce a usable answer; use the local fallback


def describe_error(exc) -> str:
    """Why a call failed, in one line: HTTP status + the provider's response body (secrets masked)."""
    if isinstance(exc, openai.APIStatusError):
        body = ""
        try:
            body = exc.response.text
        except Exception:  # streamed / unreadable body
            body = str(getattr(exc, "body", "") or "")
        body = mask_secrets(" ".join(body.split()))[:600]
        return f"HTTP {exc.status_code} {type(exc).__name__}: {body or exc.message}"
    if isinstance(exc, openai.APITimeoutError):
        return "timeout (no reply before SKILLS_LLM_TIMEOUT)"
    if isinstance(exc, openai.APIConnectionError):
        cause = exc.__cause__ or exc.__context__
        return f"connection failed: {type(cause).__name__ if cause else ''} {cause or exc}".strip()
    return f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class Provider:
    label: str
    api_key: str
    base_url: str
    model: str


_PROVIDER_KEYS = {"GEMINI_API_KEY": "gemini", "API_KEY_1": "agentrouter", "API_KEY_2": "backup_llm", "AZURE_OPENAI": "azure_openai",
                  "WHISPER_API_KEY": "whisper"}


def provider_name(provider: Provider) -> str:
    """The api_registry key of this provider (the monitor looks its name, role and billing page up there)."""
    return _PROVIDER_KEYS.get(provider.label, provider.label)


def provider_display_name(provider: Provider) -> str:
    from internest_core.api_registry import spec_for

    spec = spec_for(provider_name(provider))
    return spec.name if spec else provider.label


def providers() -> list[Provider]:
    """Configured keys in failover order (unset ones are skipped):

    1. GEMINI_API_KEY   direct Google Gemini (gemini-2.5-flash)
    2. API_KEY_1        AGENTROUTER_API_KEY / API_BASE_URL_1 (gpt-4o-mini)
    3. API_KEY_2        backup key (own endpoint/model, or the primary's)
    4. AZURE_OPENAI     Azure OpenAI / Copilot deployment
    The saved question bank is the last resort, handled by the callers.
    """
    primary_url = settings.AGENTROUTER_BASE_URL
    backup_url = settings.LLM_BACKUP_BASE_URL or primary_url
    google_url = getattr(settings, "GOOGLE_BASE_URL", "")
    azure_url = getattr(settings, "AZURE_OPENAI_ENDPOINT", "")
    candidates = [
        Provider("GEMINI_API_KEY", getattr(settings, "GEMINI_API_KEY", ""), google_url,
                 resolve_model(google_url, getattr(settings, "GEMINI_MODEL", ""))),
        Provider("API_KEY_1", settings.AGENTROUTER_API_KEY, primary_url, resolve_model(primary_url, settings.SKILLS_LLM_MODEL)),
        Provider("API_KEY_2", settings.LLM_BACKUP_API_KEY, backup_url, resolve_model(
            backup_url, settings.LLM_BACKUP_MODEL or (settings.SKILLS_LLM_MODEL if backup_url == primary_url else ""))),
        Provider("AZURE_OPENAI", getattr(settings, "AZURE_OPENAI_API_KEY", ""), azure_url,
                 getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "") or OPENAI_DEFAULT_MODEL),
    ]
    return [p for p in candidates if p.api_key and p.base_url]


def llm_enabled() -> bool:
    return bool(providers())


def resolve_model(base_url: str | None = None, explicit: str | None = None) -> str:
    """An explicit model wins; otherwise pick by endpoint."""
    if base_url is None:
        base_url, explicit = settings.AGENTROUTER_BASE_URL, settings.SKILLS_LLM_MODEL
    if explicit:
        return explicit
    if "googleapis.com" in (base_url or ""):
        return GEMINI_DEFAULT_MODEL
    if "groq.com" in (base_url or ""):
        return GROQ_DEFAULT_MODEL
    return OPENAI_DEFAULT_MODEL


def _client(timeout: float, provider: Provider | None = None, max_retries: int = 1):
    from openai import OpenAI

    from internest_core.http_client import get_client

    provider = provider or Provider("API_KEY_1", settings.AGENTROUTER_API_KEY, settings.AGENTROUTER_BASE_URL, "")
    # The SDK object is cheap; the pooled keep-alive connections (and the proxy choice, see ENABLE_PROXY_BYPASS) are shared.
    # An empty key would make the SDK raise at construction; the call itself then fails with a monitored 401 instead.
    return OpenAI(api_key=provider.api_key or "not-configured", base_url=provider.base_url, timeout=timeout, max_retries=max_retries, http_client=get_client())


_FENCE = re.compile(r"^\s*```[a-z]*\s*|\s*```\s*$", re.I)


def _close_truncated(text: str) -> str:
    """Make a JSON object that was cut off (max_tokens) parseable: drop the unfinished last element, close brackets."""
    stack, in_string, escaped, cut = [], False, False, None
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
            cut = (i + 1, list(stack))
        elif ch in "}]":
            if stack:
                stack.pop()
            cut = (i + 1, list(stack))
        elif ch == ",":
            cut = (i, list(stack))
    if cut is None:
        raise ValueError("no JSON object in LLM reply")
    end, still_open = cut
    return text[:end].rstrip().rstrip(",") + "".join(reversed(still_open))


def parse_json_object(text: str) -> dict:
    """LLM reply → dict. Tolerates ```json fences, text around the object and a reply truncated mid-string."""
    text = _FENCE.sub("", (text or "").strip())
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in LLM reply")
    text = text[start:]
    try:
        data, _end = json.JSONDecoder().raw_decode(text)  # ignores anything after the object
    except json.JSONDecodeError:
        data = json.loads(_close_truncated(text))
        logger.info("LLM reply was incomplete JSON; repaired it")
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data


def _ask(provider: Provider, system: str, user: str, max_tokens: int, timeout: float, max_retries: int) -> dict:
    """One direct call to one key, no failover / breaker / quota (diagnostics: `manage.py test_ai_connection`)."""
    from internest_ai.prompts import build_messages
    from internest_ai.providers import OpenAICompatibleProvider

    adapter = OpenAICompatibleProvider(key=provider_name(provider), label=provider.label, model=provider.model,
                                       base_url=provider.base_url, api_key=provider.api_key)
    result = adapter.chat(build_messages(system, user), max_tokens=max_tokens, timeout=timeout, max_retries=max_retries)
    if result.finish_reason == "length":
        logger.warning("LLM reply hit max_tokens (%s) via %s; repairing the JSON", max_tokens, provider.label)
    return parse_json_object(result.text)


def legacy_task() -> str:
    """AI-engine task name for a chat_json call: the api_feature label in effect ("CV Analysis" -> "cv_analysis")."""
    feature = current_feature()
    return slugify_key(feature) if feature else "chat_json"


def chat_json(system: str, user: str, max_tokens: int = 1000, timeout: float | None = None, validate=None) -> dict:
    """JSON-mode completion with provider failover. Raises LLMUnavailable only when every configured provider fails.

    `validate(data)` may raise to reject a 200 reply (e.g. too few usable questions): that provider then
    counts as failed and the next one in the chain is tried. Identical temperature-0 prompts are answered from the
    cache (internest_core.ai_cache); failures raise and are never cached. `user` is sent as written (trusted prompt);
    new code should pass untrusted text to internest_ai.engine.run(data=...) instead.
    """
    from internest_ai import engine

    return engine.run(legacy_task(), system=system, prompt=user, max_tokens=max_tokens, timeout=timeout, validate=validate,
                      log=logger)
