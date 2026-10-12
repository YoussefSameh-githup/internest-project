"""Shared OpenAI-compatible client (AgentRouter / Gemini / OpenAI / Groq) for skill extraction and quizzes.

Two keys: API_KEY_1 is tried first; on any quota, rate-limit, timeout, HTTP or parsing error the call
fails over to API_KEY_2. If every key fails, LLMUnavailable is raised so callers fall back to local
logic; nothing here may turn into an HTTP 500.
"""
import json
import logging
from dataclasses import dataclass

import httpx
import openai
from django.conf import settings

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Listed explicitly for readability; Exception also covers API status, timeout and parsing errors.
LLM_ERRORS = (httpx.ProxyError, openai.APIConnectionError, RuntimeError, Exception)


class LLMUnavailable(RuntimeError):
    """The LLM could not produce a usable answer; use the local fallback."""


@dataclass(frozen=True)
class Provider:
    label: str
    api_key: str
    base_url: str
    model: str


def providers() -> list[Provider]:
    """Configured keys in failover order: API_KEY_1, then API_KEY_2 (own endpoint/model, or the primary's)."""
    primary_url = settings.AGENTROUTER_BASE_URL
    backup_url = settings.LLM_BACKUP_BASE_URL or primary_url
    candidates = [
        Provider("API_KEY_1", settings.AGENTROUTER_API_KEY, primary_url, resolve_model(primary_url, settings.SKILLS_LLM_MODEL)),
        Provider("API_KEY_2", settings.LLM_BACKUP_API_KEY, backup_url, resolve_model(
            backup_url, settings.LLM_BACKUP_MODEL or (settings.SKILLS_LLM_MODEL if backup_url == primary_url else ""))),
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
    from openai import DefaultHttpxClient, OpenAI

    provider = provider or Provider("API_KEY_1", settings.AGENTROUTER_API_KEY, settings.AGENTROUTER_BASE_URL, "")
    kwargs = {}
    if settings.ENABLE_PROXY_BYPASS:
        # Ignore HTTP(S)_PROXY env vars. Leave off on PythonAnywhere free accounts (they need the platform proxy).
        kwargs["http_client"] = DefaultHttpxClient(trust_env=False)
    return OpenAI(api_key=provider.api_key, base_url=provider.base_url, timeout=timeout, max_retries=max_retries, **kwargs)


def _ask(provider: Provider, system: str, user: str, max_tokens: int, timeout: float, max_retries: int) -> dict:
    resp = _client(timeout, provider, max_retries).chat.completions.create(
        model=provider.model,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "")
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data


def chat_json(system: str, user: str, max_tokens: int = 1000, timeout: float | None = None) -> dict:
    """JSON-mode completion with key failover. Raises LLMUnavailable only when every configured key fails."""
    chain = providers()
    if not chain:
        raise LLMUnavailable("LLM not configured")
    errors = []
    for i, provider in enumerate(chain):
        has_backup = i + 1 < len(chain)
        try:
            # No SDK retries when a backup key exists: failing over is faster than retrying a rate-limited key.
            return _ask(provider, system, user, max_tokens, timeout or settings.SKILLS_LLM_TIMEOUT, 0 if has_backup else 1)
        except LLM_ERRORS as exc:
            errors.append(f"{provider.label}: {type(exc).__name__}")
            if has_backup:
                logger.warning("LLM failover: %s failed (%s: %s); switching to %s",
                               provider.label, type(exc).__name__, exc, chain[i + 1].label)
            else:
                logger.warning("LLM call failed (%s: %s)", type(exc).__name__, exc)
    raise LLMUnavailable("; ".join(errors))
