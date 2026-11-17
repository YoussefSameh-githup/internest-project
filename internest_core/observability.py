"""Sentry error + performance monitoring, configured once from settings.py (pure SDK code: no models imported).

Privacy first: students' CVs, emails and passwords must never leave the server. So:
  * send_default_pii=False   no IPs, cookies, usernames or emails attached automatically
  * max_request_body_size="never"   POST bodies (CV text, forms, uploads) are never sent
  * before_send scrubs auth/session headers and secrets that slip into messages
Enabled only when SENTRY_DSN is set (and never in the test runner).
"""
import logging
import re

SENSITIVE_HEADERS = {"authorization", "cookie", "x-csrftoken", "x-api-key", "proxy-authorization"}
_SECRET = re.compile(r"(sk-|AIza|gsk_|SG\.)[A-Za-z0-9_\-.]{8,}|((?:api_?key|token|password|secret)=)[^&\s]+", re.I)
_SKIP_TRACES = ("/healthz", "/static/", "/media/", "/favicon", "/robots.txt", "/api/tasks/")


def _mask(text):
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", text) if isinstance(text, str) else text


def before_send(event, hint):
    """Last line of defence before an event leaves the server."""
    request = event.get("request") or {}
    request.pop("data", None)
    request.pop("cookies", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {k: ("[Filtered]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
    if "query_string" in request:
        request["query_string"] = _mask(request["query_string"])
    for exc in (event.get("exception") or {}).get("values") or []:
        exc["value"] = _mask(exc.get("value"))
    if isinstance(event.get("logentry"), dict):
        event["logentry"]["message"] = _mask(event["logentry"].get("message"))
        event["logentry"]["formatted"] = _mask(event["logentry"].get("formatted"))
    if "message" in event:
        event["message"] = _mask(event["message"])
    return event


def make_traces_sampler(rate: float):
    """Health checks, static files and task polling are noise: never traced. Everything else at `rate`."""
    def sampler(context):
        path = ((context.get("wsgi_environ") or {}).get("PATH_INFO")
                or (context.get("asgi_scope") or {}).get("path") or "")
        if path.startswith(_SKIP_TRACES):
            return 0.0
        parent = context.get("parent_sampled")
        return float(parent) if parent is not None else rate
    return sampler


def init_sentry(dsn: str, *, environment: str, release: str = "", traces_sample_rate: float = 0.1,
                profiles_sample_rate: float = 0.0) -> bool:
    """Start the SDK. Returns False (and does nothing) without a DSN."""
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        integrations=[
            DjangoIntegration(transaction_style="url", middleware_spans=False),
            # logger.error/exception (e.g. "Async task failed", API monitor failures) become Sentry events.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sampler=make_traces_sampler(traces_sample_rate),
        profiles_sample_rate=profiles_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        before_send=before_send,
        attach_stacktrace=True,
    )
    return True
