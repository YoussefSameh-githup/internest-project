"""Shared OpenAI-compatible client (AgentRouter / Gemini / OpenAI / Groq) for skill extraction and quizzes.

Two keys: API_KEY_1 is tried first; on any quota, rate-limit, timeout, HTTP or parsing error the call
fails over to API_KEY_2. If every key fails, LLMUnavailable is raised so callers fall back to local
logic; nothing here may turn into an HTTP 500.
"""
import json
import logging
import re
from dataclasses import dataclass

import httpx
import openai
from django.conf import settings

logger = logging.getLogger(__name__)

GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
GEMINI_THINKING_HEADROOM = 2048  # extra max_tokens for Gemini's hidden reasoning

# Listed explicitly for readability; Exception also covers API status, timeout and parsing errors.
LLM_ERRORS = (httpx.ProxyError, openai.APIConnectionError, RuntimeError, Exception)


class LLMUnavailable(RuntimeError):
    """The LLM could not produce a usable answer; use the local fallback."""


_SECRET = re.compile(r"(sk-|AIza|gsk_)[A-Za-z0-9_\-]{8,}|(key=)[A-Za-z0-9_\-]{8,}")


def describe_error(exc) -> str:
    """Why a call failed, in one line: HTTP status + the provider's response body (secrets masked)."""
    if isinstance(exc, openai.APIStatusError):
        body = ""
        try:
            body = exc.response.text
        except Exception:  # streamed / unreadable body
            body = str(getattr(exc, "body", "") or "")
        body = _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", " ".join(body.split()))[:600]
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


def providers() -> list[Provider]:
    """Configured keys in failover order: API_KEY_1, then API_KEY_2 (own endpoint/model, or the primary's)."""
    primary_url = settings.AGENTROUTER_BASE_URL
    backup_url = settings.LLM_BACKUP_BASE_URL or primary_url
    google_url = getattr(settings, "GOOGLE_BASE_URL", "")
    candidates = [
        Provider("API_KEY_1", settings.AGENTROUTER_API_KEY, primary_url, resolve_model(primary_url, settings.SKILLS_LLM_MODEL)),
        Provider("API_KEY_2", settings.LLM_BACKUP_API_KEY, backup_url, resolve_model(
            backup_url, settings.LLM_BACKUP_MODEL or (settings.SKILLS_LLM_MODEL if backup_url == primary_url else ""))),
        Provider("GEMINI_API_KEY", getattr(settings, "GEMINI_API_KEY", ""), google_url,
                 resolve_model(google_url, getattr(settings, "GEMINI_MODEL", ""))),
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
    if "googleapis.com" in provider.base_url:
        # Gemini 2.5 "thinking" tokens count against max_tokens and used to cut the JSON short; leave headroom.
        max_tokens += GEMINI_THINKING_HEADROOM
    resp = _client(timeout, provider, max_retries).chat.completions.create(
        model=provider.model,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "system", "content": system + " Reply with one JSON object only, no markdown."},
                  {"role": "user", "content": user}],
    )
    choice = resp.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        logger.warning("LLM reply hit max_tokens (%s) via %s; repairing the JSON", max_tokens, provider.label)
    return parse_json_object(choice.message.content or "")


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
            detail = describe_error(exc)
            errors.append(f"{provider.label}: {detail}")
            where = f"{provider.label} [{provider.base_url} · model {provider.model}]"
            if has_backup:
                logger.warning("LLM failover: %s failed: %s; switching to %s", where, detail, chain[i + 1].label)
            else:
                logger.warning("LLM call failed: %s: %s", where, detail)
    raise LLMUnavailable("; ".join(errors))
