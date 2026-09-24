"""Shared OpenAI-compatible client (AgentRouter) for skill extraction and quiz generation."""
import json

from django.conf import settings


def llm_enabled() -> bool:
    return bool(settings.AGENTROUTER_API_KEY and settings.AGENTROUTER_BASE_URL)


def chat_json(system: str, user: str, max_tokens: int, timeout: float | None = None) -> dict:
    """Single JSON-mode completion. Raises on any transport, API or parsing error."""
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.AGENTROUTER_API_KEY,
        base_url=settings.AGENTROUTER_BASE_URL,
        timeout=timeout or settings.SKILLS_LLM_TIMEOUT,
        max_retries=1,
    )
    resp = client.chat.completions.create(
        model=settings.SKILLS_LLM_MODEL,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    data = json.loads(resp.choices[0].message.content or "")
    if not isinstance(data, dict):
        raise ValueError("LLM did not return a JSON object")
    return data
