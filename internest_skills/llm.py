"""Shared OpenAI-compatible client (AgentRouter / Gemini) for skill extraction and quiz generation."""
import json

from django.conf import settings

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


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
    """Single JSON-mode completion. Raises on any transport, API or parsing error."""
    resp = _client(timeout or settings.SKILLS_LLM_TIMEOUT).chat.completions.create(
        model=resolve_model(),
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "")
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data
