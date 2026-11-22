"""The AI engine: every language-model request of Internest goes through `run` (or `execute` for the metadata).

    data = engine.run("cv_polish", system=POLISH_RULES, prompt="Polish this CV.", data={"cv": cv_text},
                      user=request.user, partner=None, max_tokens=2500, timeout=45, private=True)

Steps: task policy (internest_ai.tasks) -> cache lookup (internest_core.ai_cache; the key includes the user id for
private / user-scoped requests, so one user's answer is never served to another) -> quota reservation (cache hits are
free) -> provider chain from the router (order, circuit breaker, privacy) -> one monitored call per provider until one
answers with a JSON object that passes the task schema and `validate` -> usage rows (AIUsage) for every attempt.
Private requests send external providers only redacted data (placeholders are restored in the reply); local providers
see the original. `prompt` is trusted instruction text; anything a user typed or uploaded belongs in `data`.
Raises AIUnavailable (== internest_skills.llm.LLMUnavailable) when nothing usable came back, QuotaExceeded (a subclass)
when a cap is reached. Plain calculations (scores, counts, matching arithmetic) never belong here.
"""
import logging
import time
from dataclasses import dataclass

from django.conf import settings

from internest_core import ai_cache
from internest_core.api_monitor import failover_scope

from . import breaker, prompts, quotas, redact, router, tasks
from .errors import AIUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AIResult:
    data: dict
    provider: str = ""
    model: str = ""
    cached: bool = False
    total_tokens: int = 0
    latency_ms: int = 0


def _pk(obj):
    if obj is None:
        return None
    return getattr(obj, "pk", obj)


def _legacy():
    from internest_skills import llm

    return llm


def run(task: str, **kwargs) -> dict:
    """The parsed JSON object (see `execute` for the arguments)."""
    return execute(task, **kwargs).data


def execute(task: str, *, system: str, data=None, prompt: str = "", user=None, partner=None, validate=None,
            max_tokens: int = 1000, timeout: float | None = None, cache: bool = True, private: bool | None = None,
            known_names=(), json_mode: bool = True, log: logging.Logger | None = None) -> AIResult:
    spec = tasks.get_task(task)
    private = spec.private if private is None else private
    log = log or logger
    user_id, partner_id = _pk(user), _pk(partner)
    candidates = router.eligible(spec)
    if not candidates:
        raise AIUnavailable("LLM not configured")
    scoped_user = user_id if (private or (user_id is not None and spec.user_scoped_cache)) else None
    payload = {"task": task, "system": system, "prompt": prompt, "data": data, "max_tokens": max_tokens, "json": json_mode,
               "private": bool(private), "user": scoped_user, "names": sorted(map(str, known_names or ())),
               "validate": getattr(validate, "__qualname__", None),
               "models": [(p.key, p.base_url, p.model) for p in candidates]}
    misses = []

    def compute():
        misses.append(1)
        return _run_chain(spec, candidates, system=system, data=data, prompt=prompt, user_id=user_id, partner_id=partner_id,
                          validate=validate, max_tokens=max_tokens, timeout=timeout, private=private,
                          known_names=known_names, json_mode=json_mode, log=log)

    if not cache:
        return compute()
    result = ai_cache.cached(f"engine:{task}", payload, compute)
    if not misses:  # answered from the cache: free, but logged so usage numbers stay honest
        quotas.record_usage(task, user_id=user_id, partner_id=partner_id, provider="cache", model=result.model, cached=True)
        return AIResult(data=result.data, provider=result.provider, model=result.model, cached=True)
    return result


def _attempt_failed(log, errors, route, routes, i, exc, describe_error):
    provider = route.provider
    detail = describe_error(exc)
    errors.append(f"{provider.label}: {detail}")
    following = routes[i + 1].provider.label if i + 1 < len(routes) else None
    if following:
        log.warning("LLM failover: %s failed: %s; switching to %s", provider.describe(), detail, following)
    else:
        log.warning("LLM call failed: %s: %s", provider.describe(), detail)
    return detail


def _run_chain(spec, candidates, *, system, data, prompt, user_id, partner_id, validate, max_tokens, timeout, private,
               known_names, json_mode, log) -> AIResult:
    llm = _legacy()
    routes = router.chain(spec, private=private, candidates=candidates)
    if not routes:
        raise AIUnavailable("every AI provider is cooling down (circuit open); try again shortly")
    reservation = quotas.reserve(spec.name, user_id=user_id, partner_id=partner_id)
    timeout = timeout or settings.SKILLS_LLM_TIMEOUT
    usage = {"user_id": user_id, "partner_id": partner_id}
    errors = []
    with failover_scope() as scope:
        for i, route in enumerate(routes):
            provider = route.provider
            if not breaker.allow(provider.key):  # another request is already probing this half-open provider
                errors.append(f"{provider.label}: circuit open")
                continue
            mapping = {}
            sent = data
            if route.redact and data is not None:
                sent, mapping = redact.redact_data(data, known_names)
            messages = prompts.build_messages(system, prompt, sent, json_mode=json_mode)
            started = time.perf_counter()
            try:
                # No SDK retries when a backup exists: failing over is faster than retrying a rate-limited key.
                result = provider.chat(messages, json_mode=json_mode, max_tokens=max_tokens, timeout=timeout,
                                       max_retries=0 if i + 1 < len(routes) else 1, task=spec.name)
            except Exception as exc:
                breaker.record_failure(provider.key, exc)
                detail = _attempt_failed(log, errors, route, routes, i, exc, llm.describe_error)
                quotas.record_usage(spec.name, provider=provider.key, model=provider.model, ok=False, error=detail,
                                    latency_ms=int((time.perf_counter() - started) * 1000), **usage)
                continue
            breaker.record_success(provider.key)
            try:
                if result.finish_reason == "length":
                    log.warning("LLM reply hit max_tokens (%s) via %s; repairing the JSON", max_tokens, provider.label)
                parsed = redact.restore(llm.parse_json_object(result.text), mapping)
                if spec.schema is not None:
                    prompts.validate_shape(parsed, spec.schema)
                if validate is not None:
                    validate(parsed)
            except Exception as exc:  # a 200 reply we cannot use: the next provider gets a chance
                detail = _attempt_failed(log, errors, route, routes, i, exc, llm.describe_error)
                quotas.record_usage(spec.name, provider=provider.key, model=result.model, ok=False, error=detail,
                                    prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                                    total_tokens=result.total_tokens, latency_ms=result.latency_ms, **usage)
                continue
            if i:
                log.info("LLM answered via %s after %s failed", provider.label, ", ".join(r.provider.label for r in routes[:i]))
            scope.served_by = provider.display_name
            quotas.record_usage(spec.name, provider=provider.key, model=result.model, prompt_tokens=result.prompt_tokens,
                                completion_tokens=result.completion_tokens, total_tokens=result.total_tokens,
                                latency_ms=result.latency_ms, **usage)
            return AIResult(data=parsed, provider=provider.key, model=result.model, total_tokens=result.total_tokens,
                            latency_ms=result.latency_ms)
    quotas.release(reservation)
    raise AIUnavailable("; ".join(errors))
