"""Admin-only API health and low-balance alerts. Every external API call (paid or free) goes through here.

    from internest_core.api_monitor import monitored_api_call, api_feature

    result = monitored_api_call("Gemini", "Question Generation", client.create, model=..., messages=...)

    @monitored("Fawaterak", "Payment invoice")      # or decorate a function that makes the call
    def create_invoice(...): ...

    with api_feature("CV Analysis"):                 # label for calls made deeper in the stack
        chat_json(...)

    check_api_balance("OpenRouter", "https://.../credits", {"Authorization": "Bearer ..."})   # early warning

Failures are classified (timeout, connection, auth 401/403, server 5xx, other HTTP, unexpected) and quota / billing
exhaustion (402, or 429 with credit/balance/quota words) is sent as a separate URGENT alert. One email per
(provider, alert type) per cooldown window. Alerts go to settings.ADMIN_ALERT_EMAIL only and sending never raises.
The original exception is always re-raised, so callers keep their own error handling.
"""
import contextlib
import contextvars
import functools
import logging
import re
import socket
from dataclasses import dataclass

import httpx
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)

# Alert types (also the cooldown key together with the provider).
QUOTA, BALANCE, AUTH, SERVER, TIMEOUT, CONNECTION, HTTP_ERROR, UNEXPECTED, RATE_LIMITED = (
    "quota", "balance", "auth", "server", "timeout", "connection", "http_error", "unexpected", "rate_limited")
URGENT_TYPES = (QUOTA, BALANCE)
TITLES = {
    QUOTA: "URGENT: Low Balance / Quota Exhausted", BALANCE: "URGENT: Low Balance", AUTH: "API key rejected (401/403)",
    SERVER: "Provider server error (5xx)", TIMEOUT: "Request timed out", CONNECTION: "Connection error",
    HTTP_ERROR: "Unexpected HTTP error", UNEXPECTED: "Unexpected response / failure",
}
ADVICE = {
    QUOTA: "Top up the account or raise the spending limit now. Until then this feature falls back or fails for users.",
    BALANCE: "Top up the account before the balance reaches zero.",
    AUTH: "The API key is wrong, revoked or blocked for this server. Replace it in .env and reload the app.",
    SERVER: "The provider is having problems. Usually temporary; check their status page.",
    TIMEOUT: "The provider did not answer in time. Check the provider status and the server's outbound access.",
    CONNECTION: "The server could not reach the provider. Check outbound network access / allow-lists.",
    HTTP_ERROR: "The provider rejected the request. Check the model name, endpoint URL and request format.",
    UNEXPECTED: "The call failed in an unexpected way. See the error details and the server log.",
}

# Billing / quota exhaustion words. A bare "rate limit" (requests per minute) is transient, not exhaustion.
_QUOTA_WORDS = re.compile(r"insufficient[_ ]quota|quota|credit|balance|billing|spending limit|usage limit|"
                          r"(?:monthly|daily|hard) limit|limit exceeded|exceeded (?:your )?(?:current )?limit", re.I)
_SECRET = re.compile(r"(sk-|AIza|gsk_)[A-Za-z0-9_\-]{8,}|(key=)[A-Za-z0-9_\-]{8,}")
_BALANCE_KEYS = ("balance", "available_balance", "remaining", "remaining_balance", "credits", "credit", "total_available",
                 "total_balance", "quota_remaining")

_feature = contextvars.ContextVar("api_feature", default="")


def mask_secrets(text: str) -> str:
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", text or "")


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


# ---------------------------------------------------------------- alerting
def _cooldown_ok(provider: str, alert_type: str) -> bool:
    """True for the first alert of this (provider, type) in the cooldown window; False while it is cooling down."""
    return cache.add(f"api-alert:{provider}:{alert_type}".lower(), 1, getattr(settings, "API_ALERT_COOLDOWN_SECONDS", 1800))


def _send_admin(subject: str, body: str) -> bool:
    """The only send path: ADMIN_ALERT_EMAIL, never anyone else, never raises."""
    admin = (getattr(settings, "ADMIN_ALERT_EMAIL", "") or "").strip()
    if not admin:
        logger.warning("API alert not sent: ADMIN_ALERT_EMAIL is not configured")
        return False
    try:
        return bool(send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [admin], fail_silently=True))
    except Exception:  # a mail problem must never break the user's request
        logger.exception("API alert email could not be sent")
        return False


def send_alert(provider: str, feature: str, alert_type: str, status: int | None = None, detail: str = "", *, force: bool = False) -> bool:
    """Email the admin about one API problem. Returns True when an email went out (False: cooling down or failed)."""
    if not force and not _cooldown_ok(provider, alert_type):
        return False
    title = TITLES.get(alert_type, "API problem")
    subject = f"[Internest] {title} — {provider}" + ("" if alert_type in URGENT_TYPES else f" · {feature}")
    body = "\n".join([
        title, "",
        f"API provider : {provider}",
        f"Feature      : {feature or 'n/a'}",
        f"HTTP status  : {status if status is not None else 'n/a (no response)'}",
        f"Error details: {mask_secrets(detail)[:800] or 'n/a'}",
        f"Timestamp    : {timezone.now():%Y-%m-%d %H:%M:%S} UTC",
        "", f"What to do: {ADVICE.get(alert_type, 'Check the provider dashboard and the server log.')}",
        "", f"Repeat alerts for this provider and type are suppressed for {getattr(settings, 'API_ALERT_COOLDOWN_SECONDS', 1800) // 60} minutes.",
    ])
    sent = _send_admin(subject, body)
    logger.warning("API alert (%s) for %s / %s: %s", alert_type, provider, feature, "emailed" if sent else "not emailed")
    return sent


def report_failure(provider: str, feature: str, exc: BaseException | None = None, *, failure: Failure | None = None) -> bool:
    """Classify `exc` (or use `failure`) and alert. Plain rate limits (429 without quota words) are not alerted."""
    try:
        failure = failure or classify(exc)
        if failure.alert_type == RATE_LIMITED:
            return False
        return send_alert(provider, feature or _feature.get() or "API call", failure.alert_type, failure.status, failure.detail)
    except Exception:  # monitoring must never break the call it watches
        logger.exception("API monitor failed while reporting")
        return False


# ---------------------------------------------------------------- the plug-in points
def monitored_api_call(provider: str, feature: str, fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)`; on an exception, alert the admin and re-raise it unchanged.

    A returned response with an error status (httpx-style `.status_code`) is alerted too and still returned."""
    feature = feature or _feature.get()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        report_failure(provider, feature, exc)
        raise
    failure = classify_response(result)
    if failure:
        report_failure(provider, feature, failure=failure)
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
    """Subclass for a future API: set `provider_name`, then make every request through `self.call(feature, fn, ...)`."""

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
                      *, parse=None, timeout: float = 10, feature: str = "Balance check") -> float | None:
    """Read a provider's balance endpoint; email an URGENT alert when it is at or below `threshold`.

    `parse(json) -> float` overrides the default lookup (balance / credits / remaining ... anywhere in the JSON).
    Returns the balance, or None when it could not be read (that is alerted as an API failure). Schedule it from
    cron / a management command for each paid API that has such an endpoint."""
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
        report_failure(provider_name, feature, failure=Failure(UNEXPECTED, None, "No balance value found in the provider's reply"))
        return None
    if balance <= threshold:
        send_alert(provider_name, feature, BALANCE, None, f"Remaining balance {balance:g} is at or below the threshold {threshold:g}.")
    return balance
