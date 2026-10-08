"""Shared OpenAI-compatible client (AgentRouter / Gemini) for skill extraction and quiz generation.

Every failure (proxy 403s on PythonAnywhere, connection errors, timeouts, bad JSON) is raised as
LLMUnavailable so callers can fall back to local logic; nothing here may turn into an HTTP 500.
"""
import json
import logging

import httpx
import openai
from django.conf import settings

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

# Listed explicitly for readability; Exception also covers API status, timeout and parsing errors.
LLM_ERRORS = (httpx.ProxyError, openai.APIConnectionError, RuntimeError, Exception)


class LLMUnavailable(RuntimeError):
    """The LLM could not produce a usable answer; use the local fallback."""


def llm_enabled() -> bool:
    return bool(settings.AGENTROUTER_API_KEY and settings.AGENTROUTER_BASE_URL)


def resolve_model() -> str:
    """Explicit SKILLS_LLM_MODEL wins; otherwise pick by endpoint."""
    if settings.SKILLS_LLM_MODEL:
        return settings.SKILLS_LLM_MODEL
    if "googleapis.com" in (settings.AGENTROUTER_BASE_URL or ""):
        return GEMINI_DEFAULT_MODEL
    return OPENAI_DEFAULT_MODEL


def _client(timeout: float):
    from openai import DefaultHttpxClient, OpenAI

    kwargs = {}
    if settings.ENABLE_PROXY_BYPASS:
        # Ignore HTTP(S)_PROXY env vars. Leave off on PythonAnywhere free accounts (they need the platform proxy).
        kwargs["http_client"] = DefaultHttpxClient(trust_env=False)
    return OpenAI(
        api_key=settings.AGENTROUTER_API_KEY,
        base_url=settings.AGENTROUTER_BASE_URL,
        timeout=timeout,
        max_retries=1,
        **kwargs,
    )


def chat_json(system: str, user: str, max_tokens: int = 1000, timeout: float | None = None) -> dict:
    """Single JSON-mode completion. Raises LLMUnavailable on any transport, API or parsing error."""
    if not llm_enabled():
        raise LLMUnavailable("LLM not configured")
    try:
        resp = _client(timeout or settings.SKILLS_LLM_TIMEOUT).chat.completions.create(
            model=resolve_model(),
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        data = json.loads(resp.choices[0].message.content or "")
    except LLM_ERRORS as exc:
        logger.warning("LLM call failed (%s: %s)", type(exc).__name__, exc)
        raise LLMUnavailable(str(exc)) from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("LLM did not return a JSON object")
    return data
