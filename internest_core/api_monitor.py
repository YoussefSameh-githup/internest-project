"""Admin-only API health and billing alerts. Every external API call (paid or free) goes through here.

    from internest_core.api_monitor import monitored_api_call, api_feature, failover_scope

    result = monitored_api_call("gemini", "Question Generation", client.create, model=..., messages=...)

    @monitored("fawaterak", "Payment invoice")      # or decorate a function that makes the call
    def create_invoice(...): ...

    with api_feature("CV Analysis"):                 # label for calls made deeper in the stack
        chat_json(...)

    with failover_scope() as scope:                  # a chain of providers: the email reports whether a backup saved the day
        ...; scope.served_by = "Azure OpenAI"

The first argument is the registry key from `api_registry.py` (a display name works too). Describe a new API there once and it
gets alert emails with full context, an incident log, a Django-admin row and balance tracking with no per-service alert code.

What is detected: timeouts, connection errors, 401/403 (key), 429 (rate limit; quota/billing words make it URGENT), 5xx and any
other failure. Quota / billing exhaustion (402, 429 or 403 with credit/balance/quota words) and a low balance send
"URGENT: Low Balance" emails. One email per (provider, alert type) per 30 minutes, every incident is stored in the database,
and alerts go to settings.ADMIN_ALERT_EMAIL only. Sending never raises; the original exception is always re-raised unchanged.
"""
import contextlib
import contextvars
import functools
import logging
import re
import socket
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import httpx
from django.conf import settings
from django.core.cache import cache
from django.core.mail import get_connection, send_mail
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

from . import api_registry
from .api_registry import CATEGORIES, REGISTRY, slugify_key, spec_for

logger = logging.getLogger(__name__)

# Alert types (also the cooldown key together with the provider).
QUOTA, BALANCE, AUTH, SERVER, TIMEOUT, CONNECTION, HTTP_ERROR, UNEXPECTED, RATE_LIMITED = (
    "quota", "balance", "auth", "server", "timeout", "connection", "http_error", "unexpected", "rate_limited")
URGENT_TYPES = (QUOTA, BALANCE)
TITLES = {
    QUOTA: "URGENT: Low Balance / Quota Exhausted", BALANCE: "URGENT: Low Balance", AUTH: "API key rejected (401/403)",
    SERVER: "Provider server error (5xx)", TIMEOUT: "Request timed out", CONNECTION: "Connection error",
    HTTP_ERROR: "Unexpected HTTP error", UNEXPECTED: "Unexpected response / failure", RATE_LIMITED: "Rate limit hit (429)",
}
ACTIONS = {
    QUOTA: ["Open the billing page: {url} and top up the account (or raise the spending limit).",
            "If you track this balance in Django admin, add what you loaded to \"Initial budget\" (Admin → API providers → {name}).",
            "Run: python manage.py check_api_health (confirms the key and the balance)."],
    BALANCE: ["Open the billing page: {url} and top up before the balance reaches zero.",
              "If you track this balance in Django admin, add what you loaded to \"Initial budget\" (Admin → API providers → {name}).",
              "Run: python manage.py check_api_health."],
    AUTH: ["Open {url} and create or copy a valid API key.",
           "Edit .env on the server (PythonAnywhere → Files → internest-project/.env): set {env} to the new key.",
           "Web tab → Reload, then run: python manage.py check_api_health."],
    SERVER: ["Check the provider's status page / console: {url}. 5xx errors are usually temporary.",
             "Nothing to change on your side unless this lasts more than an hour; the cooldown stops repeat emails."],
    TIMEOUT: ["Check the provider's status page / console: {url}.",
              "If it keeps happening, check the server's outbound access (PythonAnywhere free accounts only reach allow-listed sites)."],
    CONNECTION: ["Check the provider's status page / console: {url}.",
                 "Check the server's outbound access (PythonAnywhere free accounts only reach allow-listed sites)."],
    RATE_LIMITED: ["Open {url} and check the rate / quota limits of your plan.",
                   "Wait a few minutes, or upgrade the plan / spread the traffic if this repeats."],
    HTTP_ERROR: ["The provider rejected the request: check the model name, endpoint URL and {env} in .env.",
                 "Console: {url}. Details are also in logs/ai.log."],
    UNEXPECTED: ["See the error details above and the server log (logs/ai.log).", "Console: {url}."],
}

# Billing / quota exhaustion words. A bare "rate limit" (requests per minute) is transient, not exhaustion.
_QUOTA_WORDS = re.compile(r"insufficient[_ ]quota|quota|credit|balance|billing|spending limit|usage limit|"
                          r"(?:monthly|daily|hard) limit|limit exceeded|exceeded (?:your )?(?:current )?limit", re.I)
_SECRET = re.compile(r"(sk-|AIza|gsk_)[A-Za-z0-9_\-]{8,}|(key=)[A-Za-z0-9_\-]{8,}|(SG\.)[A-Za-z0-9_\-\.]{8,}")
_BALANCE_KEYS = ("balance", "available_balance", "remaining", "remaining_balance", "remain", "credits", "credit", "total_available",
                 "total_balance", "quota_remaining")
COOLDOWN_SECONDS = 1800

_feature = contextvars.ContextVar("api_feature", default="")
_scope = contextvars.ContextVar("api_scope", default=None)
_sending_alert = contextvars.ContextVar("api_sending_alert", default=False)


def mask_secrets(text: str) -> str:
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "***", text or "")


@contextlib.contextmanager
def api_feature(name: str):
    """Label every monitored call made inside the block (for call sites that don't know their feature)."""
    token = _feature.set(name)
    try:
        yield
    finally:
        _feature.reset(token)


def current_feature() -> str:
    return _feature.get()


def cooldown_seconds() -> int:
    return getattr(settings, "API_ALERT_COOLDOWN_SECONDS", COOLDOWN_SECONDS)


@dataclass(frozen=True)
class Failure:
    alert_type: str
    status: int | None
    detail: str


# ---------------------------------------------------------------- classification
def _status_of(exc) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        status = getattr(obj, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _body_of(exc) -> str:
    try:
        text = exc.response.text
    except Exception:  # no response, or a streamed body
        text = ""
    return " ".join((text or str(getattr(exc, "body", "") or "") or str(exc)).split())


def classify(exc) -> Failure:
    """Turn an exception from an API call into an alert type, HTTP status and a short masked detail line."""
    import openai

    status, body = _status_of(exc), _body_of(exc)
    detail = mask_secrets(f"{type(exc).__name__}: {body}")[:700]
    if status is not None:
        quota_text = bool(_QUOTA_WORDS.search(body))
        if status == 402 or (status in (429, 403) and quota_text):
            return Failure(QUOTA, status, detail)
        if status == 429:
            return Failure(RATE_LIMITED, status, detail)
        if status in (401, 403):
            return Failure(AUTH, status, detail)
        if status >= 500:
            return Failure(SERVER, status, detail)
        return Failure(HTTP_ERROR, status, detail)
    if isinstance(exc, (httpx.TimeoutException, openai.APITimeoutError, TimeoutError, socket.timeout)):
        return Failure(TIMEOUT, None, detail)
    if isinstance(exc, (httpx.TransportError, openai.APIConnectionError, ConnectionError, OSError)):
        return Failure(CONNECTION, None, detail)
    return Failure(UNEXPECTED, None, detail)


def classify_response(response) -> Failure | None:
    """A returned HTTP response (httpx-style) with an error status, else None. Used for calls that don't raise."""
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or status < 400:
        return None

    class _Err(Exception):
        pass

    err = _Err(getattr(response, "text", "") or "")
    err.response = response
    return classify(err)


# ---------------------------------------------------------------- registry rows
def _spec_fields(spec) -> dict:
    return {"name": spec.name, "category": spec.category, "role": spec.role, "impact": spec.impact, "degraded_behavior": spec.degraded_behavior,
            "env_var": spec.env_var, "code_location": spec.code_location, "dashboard_url": spec.dashboard_url, "registered": True,
            "is_paid": spec.is_paid, "tracking_mode": spec.tracking}


def _ledger_defaults(spec) -> dict:
    return {"currency": spec.currency, "cost_per_call": Decimal(str(spec.cost_per_call)), "cost_per_1k_tokens": Decimal(str(spec.cost_per_1k_tokens))}


def sync_provider(spec):
    """Create the admin row for a registered API, or refresh its descriptive fields (never the budget numbers)."""
    from .models import APIProvider

    row, created = APIProvider.objects.get_or_create(key=spec.key, defaults={**_spec_fields(spec), **_ledger_defaults(spec)})
    if not created:
        changed = {f: v for f, v in _spec_fields(spec).items() if getattr(row, f) != v}
        if changed:
            for field, value in changed.items():
                setattr(row, field, value)
            row.save(update_fields=list(changed))
    return row


def sync_registry():
    return [sync_provider(spec) for spec in REGISTRY.values()]


def _unregistered_fields(name: str) -> dict:
    return {"name": name, "category": "other", "registered": False, "env_var": "", "dashboard_url": "",
            "role": "Not described yet: add this API to internest_core/api_registry.py so alerts explain what it does.",
            "impact": "Unknown: the API is not in the registry."}


def provider_row(key_or_name: str):
    """The APIProvider row for a registry key / display name. An unknown API is auto-created as "unregistered"."""
    from .models import APIProvider

    spec = spec_for(key_or_name)
    if spec:
        return sync_provider(spec)
    key = slugify_key(key_or_name)
    row, _ = APIProvider.objects.get_or_create(key=key, defaults=_unregistered_fields(key_or_name))
    return row


def _ephemeral_row(key_or_name: str):
    """An unsaved row (for test alerts and when the database is unavailable)."""
    from .models import APIProvider

    spec = spec_for(key_or_name)
    return APIProvider(key=spec.key if spec else slugify_key(key_or_name), **(_spec_fields(spec) if spec else _unregistered_fields(key_or_name)))


# ---------------------------------------------------------------- email
def _action_steps(row, alert_type: str) -> list[str]:
    values = {"url": row.dashboard_url or "the provider's console", "env": row.env_var or "the API key variable", "name": row.name}
    return [f"{i}. {step.format(**values)}" for i, step in enumerate(ACTIONS.get(alert_type, ACTIONS[UNEXPECTED]), 1)]


def build_alert(row, alert_type: str, feature: str, status, detail: str, failover_status: str):
    """(subject, body) with the full context: what the API is, what is affected, diagnostics and what to do."""
    title = TITLES.get(alert_type, "API problem")
    subject = f"[Internest] {title} — {row.name}" + ("" if alert_type in URGENT_TYPES else f" · {feature}")
    remaining = row.remaining_balance
    balance_line = (f"Balance      : about {remaining:,.2f} {row.currency} left (alert threshold {row.low_balance_threshold:,.2f})"
                    if remaining is not None else None)
    lines = [
        title, "=" * len(title), "",
        "1) API PROVIDER & SERVICE PURPOSE",
        f"Provider     : {row.name} ({CATEGORIES.get(row.category, row.category)})",
        f"Role         : {row.role or 'n/a'}",
        "",
        "2) AFFECTED FEATURES & FAILOVER STATUS",
        f"Affected now : {row.impact or 'n/a'}",
        f"Failover     : {failover_status or 'No automatic backup for this service.'}",
        "",
        "3) TECHNICAL DIAGNOSTICS",
        f"Feature      : {feature or 'n/a'}",
        f"HTTP status  : {status if status is not None else 'n/a (no response)'}",
        f"Error        : {mask_secrets(detail)[:800] or 'n/a'}",
        f"Env variable : {row.env_var or 'n/a'}",
        f"Code location: {row.code_location or 'n/a'}",
        f"Console / billing: {row.dashboard_url or 'n/a'}",
        *([balance_line] if balance_line else []),
        f"Timestamp    : {timezone.now():%Y-%m-%d %H:%M:%S} UTC",
        "",
        "4) WHAT TO DO NOW",
        *_action_steps(row, alert_type),
        "",
        f"Repeat alerts for this provider and type are suppressed for {cooldown_seconds() // 60} minutes; every incident is still logged "
        "(Django admin → API incidents).",
    ]
    if not row.registered:
        lines += ["", f"Note: \"{row.name}\" is not in internest_core/api_registry.py yet. Add it there to get a full description in these emails."]
    return subject, "\n".join(lines)


def _send_admin(subject: str, body: str) -> bool:
    """The only send path: ADMIN_ALERT_EMAIL, never anyone else, never raises.

    If the normal backend fails (e.g. SendGrid is the API that is down) and Gmail SMTP credentials exist, retry through SMTP."""
    admin = (getattr(settings, "ADMIN_ALERT_EMAIL", "") or "").strip()
    if not admin:
        logger.warning("API alert not sent: ADMIN_ALERT_EMAIL is not configured")
        return False
    token = _sending_alert.set(True)  # the email backend must not report failures of the alert itself (no loops)
    try:
        sent = _try_send(subject, body, admin, None)
        real = getattr(settings, "MONITORED_EMAIL_BACKEND", "") or settings.EMAIL_BACKEND
        if not sent and "anymail" in real and getattr(settings, "EMAIL_HOST_PASSWORD", ""):
            sent = _try_send(subject, body, admin, get_connection("django.core.mail.backends.smtp.EmailBackend", fail_silently=True))
        return sent
    finally:
        _sending_alert.reset(token)


def _try_send(subject, body, admin, connection) -> bool:
    try:
        return bool(send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [admin], fail_silently=True, connection=connection))
    except Exception:  # a mail problem must never break the user's request
        logger.exception("API alert email could not be sent")
        return False


def sending_alert() -> bool:
    return _sending_alert.get()


# ---------------------------------------------------------------- alerting
def _cooldown_ok(row, alert_type: str) -> bool:
    """True for the first alert of this (provider, type) in the cooldown window; False while it is cooling down.

    The cache gives an atomic in-process guard; the incident table makes it hold across PythonAnywhere workers."""
    from .models import APIIncident

    if not cache.add(f"api-alert:{row.key}:{alert_type}", 1, cooldown_seconds()):
        return False
    if row.pk:
        try:
            since = timezone.now() - timedelta(seconds=cooldown_seconds())
            if APIIncident.objects.filter(provider=row, alert_type=alert_type, emailed=True, created_at__gte=since).exists():
                return False
        except DatabaseError:
            pass
    return True


def _log_incident(row, alert_type, feature, status, detail, failover_status, emailed):
    from .models import APIIncident

    try:
        with transaction.atomic():
            APIIncident.objects.create(provider=row, alert_type=alert_type, feature=(feature or "")[:160], http_status=status,
                                       detail=mask_secrets(detail)[:2000], failover_status=failover_status, emailed=emailed)
    except Exception:
        logger.exception("Could not store the API incident")


def default_failover_text(key_or_name: str) -> str:
    spec = spec_for(key_or_name)
    return "No automatic backup for this service. " + (spec.degraded_behavior if spec else "")


def send_alert(provider: str, feature: str, alert_type: str, status: int | None = None, detail: str = "", *,
               force: bool = False, record: bool = True, failover_status: str = "") -> bool:
    """Email the admin about one API problem and log it. Returns True when an email went out."""
    try:
        row = provider_row(provider) if record else _ephemeral_row(provider)
    except DatabaseError:
        row, record = _ephemeral_row(provider), False
    failover_status = failover_status or default_failover_text(provider)
    emailed = False
    if force or _cooldown_ok(row, alert_type):
        subject, body = build_alert(row, alert_type, feature, status, detail, failover_status)
        emailed = _send_admin(subject, body)
        if not emailed:
            cache.delete(f"api-alert:{row.key}:{alert_type}")  # let the next incident try again
    if record:
        _log_incident(row, alert_type, feature, status, detail, failover_status, emailed)
    logger.warning("API alert (%s) for %s / %s: %s", alert_type, row.name, feature, "emailed" if emailed else "not emailed")
    return emailed


class FailoverScope:
    """Collects the failures of one provider chain so each email can say whether a backup provider saved the request."""

    def __init__(self):
        self.pending: list[tuple[str, str, Failure]] = []
        self.served_by = ""  # display name of the provider that finally answered

    def flush(self):
        for provider, feature, failure in self.pending:
            text = (f"Fallback worked: {self.served_by} answered the request, so users saw no error." if self.served_by
                    else "No backup answered. " + (spec_for(provider).degraded_behavior if spec_for(provider) else "The feature is degraded."))
            try:
                send_alert(provider, feature, failure.alert_type, failure.status, failure.detail, failover_status=text)
            except Exception:
                logger.exception("API monitor failed while flushing a failover scope")
        self.pending.clear()


@contextlib.contextmanager
def failover_scope():
    scope = FailoverScope()
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
        scope.flush()


def report_failure(provider: str, feature: str, exc: BaseException | None = None, *, failure: Failure | None = None) -> bool:
    """Classify `exc` (or use `failure`) and alert (deferred to the end of the chain inside a failover scope)."""
    try:
        failure = failure or classify(exc)
        feature = feature or _feature.get() or "API call"
        scope = _scope.get()
        if scope is not None:
            scope.pending.append((provider, feature, failure))
            return False
        return send_alert(provider, feature, failure.alert_type, failure.status, failure.detail)
    except Exception:  # monitoring must never break the call it watches
        logger.exception("API monitor failed while reporting")
        return False


# ---------------------------------------------------------------- budget ledger
def record_usage(provider: str, result=None, *, tokens: int | None = None) -> None:
    """Ledger mode: deduct the estimated cost of one successful call and warn when the remaining balance gets low."""
    spec = spec_for(provider)
    if spec is None or spec.tracking != api_registry.TRACKING_LEDGER:
        return
    try:
        if tokens is None:
            usage = getattr(result, "usage", None)
            tokens = getattr(usage, "total_tokens", None)
            tokens = tokens if isinstance(tokens, int) else 0
        from .models import APIProvider

        row = provider_row(provider)
        cost = (row.cost_per_call + Decimal(tokens) / 1000 * row.cost_per_1k_tokens).quantize(Decimal("0.000001"))
        if cost > 0:
            APIProvider.objects.filter(pk=row.pk).update(estimated_spent=F("estimated_spent") + cost)
            row.refresh_from_db()
        if row.initial_budget > 0 and row.is_low:
            send_alert(row.key, "Balance tracker", BALANCE, None,
                       f"Estimated remaining balance {row.remaining_balance:,.2f} {row.currency} is at or below the alert threshold "
                       f"{row.low_balance_threshold:,.2f} {row.currency} (initial budget {row.initial_budget:,.2f}, estimated spent {row.estimated_spent:,.2f}).")
    except Exception:
        logger.exception("API usage ledger failed")


# ---------------------------------------------------------------- the plug-in points
def monitored_api_call(provider: str, feature: str, fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)`; on an exception, alert the admin and re-raise it unchanged.

    A returned response with an error status (httpx-style `.status_code`) is alerted too and still returned.
    A successful call is added to the provider's spend ledger when it has one."""
    feature = feature or _feature.get()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        report_failure(provider, feature, exc)
        raise
    failure = classify_response(result)
    if failure:
        report_failure(provider, feature, failure=failure)
    else:
        record_usage(provider, result)
    return result


def monitored(provider: str, feature: str = ""):
    """Decorator form of `monitored_api_call`."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return monitored_api_call(provider, feature, fn, *args, **kwargs)
        return wrapper
    return decorator


class BaseMonitoredAPIClient:
    """Subclass for a future API: set `provider_name` (its registry key), then make every request through `self.call(feature, fn, ...)`."""

    provider_name = "API"

    def call(self, feature: str, fn, *args, **kwargs):
        return monitored_api_call(self.provider_name, feature, fn, *args, **kwargs)


# ---------------------------------------------------------------- early low-balance warning
def _find_balance(data) -> float | None:
    queue = [data]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key in _BALANCE_KEYS:
                value = item.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return float(value)
                if isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        return float(value)
            queue.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            queue.extend(item)
    return None


def check_api_balance(provider_name: str, balance_url: str, headers: dict | None = None, threshold: float | None = None,
                      *, parse=None, timeout: float = 10, feature: str = "Balance check", strict: bool = True) -> float | None:
    """Read a provider's balance endpoint; email an URGENT alert when it is at or below `threshold`.

    `parse(json) -> float` overrides the default lookup (balance / credits / remaining ... anywhere in the JSON).
    Returns the balance, or None when it could not be read (alerted as an API failure unless `strict=False`)."""
    threshold = float(getattr(settings, "API_LOW_BALANCE_THRESHOLD", 2.0) if threshold is None else threshold)
    try:
        resp = monitored_api_call(provider_name, feature, httpx.get, balance_url, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        balance = float(parse(data)) if parse else _find_balance(data)
    except Exception:
        logger.warning("Balance check failed for %s", provider_name)
        return None
    if balance is None:
        if strict:
            report_failure(provider_name, feature, failure=Failure(UNEXPECTED, None, "No balance value found in the provider's reply"))
        return None
    if balance <= threshold:
        send_alert(provider_name, feature, BALANCE, None, f"Remaining balance {balance:g} is at or below the threshold {threshold:g}.")
    return balance
