"""Which providers handle a request, in which order, and what each one may see.

Order: AI_PROVIDER_ORDER (comma-separated registry keys, e.g. "local_llm,gemini,groq,agentrouter") is explicit: only the
listed providers are used, in that order. Empty (default): local first, then free external, then paid; ties keep the
configured order (Gemini, AgentRouter, backup, Azure, then Groq, OpenRouter, Cloudflare).
Skipped: unconfigured providers, providers whose circuit is open, and providers the task policy excludes
(local_only tasks never leave the server; long_context tasks need a long-context model).
Privacy: for a private task an external provider gets `redact=True` (only redacted data); a local provider never does.
"""
import logging
from dataclasses import dataclass

from django.conf import settings

from . import breaker, providers
from .providers import BaseProvider

logger = logging.getLogger(__name__)

ALIASES = {"local": "local_llm", "ollama": "local_llm", "cloudflare": "cloudflare_ai", "azure": "azure_openai",
           "backup": "backup_llm", "api_key_1": "agentrouter", "api_key_2": "backup_llm"}


@dataclass(frozen=True)
class Route:
    provider: BaseProvider
    redact: bool = False


def _order_setting() -> list[str]:
    raw = getattr(settings, "AI_PROVIDER_ORDER", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    keys = [ALIASES.get(k.strip().lower(), k.strip().lower()) for k in raw if k and k.strip()]
    return list(dict.fromkeys(keys))


def ordered(configured: list[BaseProvider] | None = None) -> list[BaseProvider]:
    configured = providers.configured() if configured is None else configured
    explicit = _order_setting()
    if explicit and configured != [providers.MOCK]:
        rank = {key: i for i, key in enumerate(explicit)}
        return sorted((p for p in configured if p.key in rank), key=lambda p: rank[p.key])
    return sorted(configured, key=lambda p: (p.privacy != providers.LOCAL, not p.free))


def eligible(task_spec, configured: list[BaseProvider] | None = None) -> list[BaseProvider]:
    """Providers the policy allows, in order, ignoring circuit state (used for the cache key and diagnostics)."""
    out = []
    for p in ordered(configured):
        if task_spec.local_only and p.external:
            continue
        if task_spec.long_context and not p.long_context:
            continue
        out.append(p)
    return out


def chain(task_spec, *, private: bool, candidates: list[BaseProvider] | None = None) -> list[Route]:
    routes = []
    for p in eligible(task_spec) if candidates is None else candidates:
        if not breaker.available(p.key):
            logger.info("AI router: skipping %s for %s (circuit %s)", p.key, task_spec.name, breaker.state(p.key))
            continue
        routes.append(Route(p, redact=private and p.external))
    return routes
