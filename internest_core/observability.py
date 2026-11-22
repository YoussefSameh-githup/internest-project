"""Sentry error + performance monitoring, configured once from settings.py (pure SDK code: no models imported).

Privacy first: students' CVs, emails, codes and passwords must never leave the server. So:
  * send_default_pii=False          no IPs, cookies, usernames or emails attached automatically
  * include_local_variables=False   stack frames carry code and line numbers only, never the values in them
  * max_request_body_size="never"   POST bodies (CV text, forms, uploads) are never sent
  * scrub() walks every outgoing error AND transaction: auth headers, cookies and bodies are dropped; API keys,
    emails, one-time codes and capability links (UUID tokens in URLs) are masked wherever they appear.

PythonAnywhere runs web apps under uWSGI with threads disabled, and the SDK's default transport sends from a background
thread: events would queue and never leave. There (SENTRY_SYNC_TRANSPORT, default on PythonAnywhere) every envelope is
sent inline with a short timeout, the SDK's other background threads are switched off, and a failing Sentry is skipped
for a minute instead of slowing every request. Every delivery goes through the API monitor (api_registry key "sentry").

Enabled only when SENTRY_DSN is set (and never in the test runner).
"""
import logging
import re
import threading
import time
from pathlib import Path

SENSITIVE_HEADERS = {"authorization", "cookie", "x-csrftoken", "x-api-key", "proxy-authorization"}
_SECRET = re.compile(r"(sk-|AIza|gsk_|SG\.)[A-Za-z0-9_\-.]{8,}"
                     r"|((?:api_?key|token|password|secret|code|otp|email|uid|uidb64|signature|sig)=)[^&\s]+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_RESET_LINK = re.compile(r"(/accounts/reset/)[^/\s]+/[^/\s]+")
_KEEP = {"event_id", "trace_id", "span_id", "parent_span_id", "release", "environment", "platform", "sdk", "timestamp",
         "start_timestamp", "abs_path", "filename", "module", "function", "lineno", "colno", "in_app"}
_SKIP_TRACES = ("/healthz", "/static/", "/media/", "/favicon", "/robots.txt", "/api/tasks/")
_NOISY_LOGGERS = ("django.security.DisallowedHost",)  # bots probing with other Host headers: not our bug
DOWN_SECONDS = 60          # after a failed delivery, skip Sentry this long instead of slowing every request
SYNC_TIMEOUT_SECONDS = 3   # longest a request ever waits on Sentry


def _mask(text):
    if not isinstance(text, str):
        return text
    text = _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", text)
    text = _EMAIL.sub("[email]", text)
    text = _RESET_LINK.sub(r"\1[token]/[token]", text)
    return _UUID.sub("[uuid]", text)


def _scrub(value, key=""):
    if key in _KEEP:
        return value
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    return value


def scrub(event, hint=None):
    """Last line of defence before an error or a transaction leaves the server."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: ("[Filtered]" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}
    for frame_holder in (event.get("exception") or {}).get("values") or []:  # belt and braces with include_local_variables
        for frame in ((frame_holder.get("stacktrace") or {}).get("frames") or []):
            frame.pop("vars", None)
    return _scrub(event)


before_send = before_send_transaction = scrub


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


def detect_release(base_dir) -> str:
    """The deployed git commit (read from .git, no subprocess), so Sentry can say which deploy introduced an error."""
    git = Path(base_dir) / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        ref_file = git / ref
        if ref_file.exists():
            return ref_file.read_text().strip()[:12]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        pass
    return ""


# ------------------------------------------------------------------------------------------------ delivery
class DeliveryError(Exception):
    """Sentry answered with an error status (bad DSN, project removed, server error). Classified by the API monitor."""

    def __init__(self, status_code: int):
        super().__init__(f"Sentry answered HTTP {status_code}")
        self.status_code = status_code


def _transport_classes():
    from sentry_sdk.transport import HttpTransport

    class MonitoredTransport(HttpTransport):
        """Every delivery goes through the API monitor: a rejected DSN or an unreachable Sentry alerts the admin."""

        last_status = None  # last HTTP status (or "error: ...") seen; read by sentry_test and the ops check

        def _send_envelope(self, envelope):
            from . import api_monitor

            api_monitor.monitored_api_call("sentry", "Error reporting", super()._send_envelope, envelope)

        def _handle_response(self, response, envelope):
            type(self).last_status = response.status
            super()._handle_response(response=response, envelope=envelope)
            if response.status >= 400 and response.status != 429:  # 429 is Sentry's own quota: the SDK backs off
                raise DeliveryError(response.status)

    class SyncMonitoredTransport(MonitoredTransport):
        """Sends inline: for uWSGI without threads (PythonAnywhere). Never starts a thread, never waits > 3 s."""

        TIMEOUT = SYNC_TIMEOUT_SECONDS
        _down_until = 0.0
        _lock = threading.Lock()  # one delivery at a time per process; no thread is ever started

        def capture_envelope(self, envelope):
            cls = type(self)
            if time.monotonic() < cls._down_until or not cls._lock.acquire(blocking=False):
                for item in envelope.items:  # Sentry is down, or this is a nested event raised while reporting one
                    self.record_lost_event("network_error", item=item)
                return
            try:
                self._send_envelope(envelope)
            except Exception as exc:  # reporting must never break the request it describes
                cls.last_status = f"error: {type(exc).__name__}"
                # Unreachable, or the DSN was rejected: either way the next requests shouldn't pay for retrying it.
                cls._down_until = time.monotonic() + DOWN_SECONDS
            finally:
                cls._lock.release()

        def _get_pool_options(self):
            options = super()._get_pool_options()
            options["retries"] = False  # urllib3 would retry 3 times with back-off: >10 s on a request for a dead Sentry
            return options

        def flush(self, timeout, callback=None):
            return None  # nothing is ever queued

    return MonitoredTransport, SyncMonitoredTransport


def init_sentry(dsn: str, *, environment: str, release: str = "", traces_sample_rate: float = 0.1,
                profiles_sample_rate: float = 0.0, sync_transport: bool = False) -> bool:
    """Start the SDK. Returns False (and does nothing) without a DSN."""
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger

    threaded, sync = _transport_classes()
    for name in _NOISY_LOGGERS:
        ignore_logger(name)
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        transport=sync if sync_transport else threaded,
        integrations=[
            DjangoIntegration(transaction_style="url", middleware_spans=False),
            # logger.error/exception (e.g. "Async task failed", API monitor failures) become Sentry events.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sampler=make_traces_sampler(traces_sample_rate),
        # Profiling, release-health sessions and the back-pressure monitor each run a background thread.
        profiles_sample_rate=0.0 if sync_transport else profiles_sample_rate,
        auto_session_tracking=not sync_transport,
        enable_backpressure_handling=not sync_transport,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=before_send,
        before_send_transaction=before_send_transaction,
        attach_stacktrace=True,
    )
    return True


def transport_name() -> str:
    """"synchronous" / "background thread" / "off": shown by sentry_test and the ops check."""
    import sentry_sdk

    client = sentry_sdk.get_client()
    if not client.is_active():
        return "off"
    return "synchronous" if type(client.transport).__name__ == "SyncMonitoredTransport" else "background thread"


def last_delivery_status():
    """The HTTP status of the last delivery (200 = Sentry stored it), an "error: ..." string, or None."""
    import sentry_sdk

    return getattr(getattr(sentry_sdk.get_client(), "transport", None), "last_status", None)


def send_check(source: str) -> dict:
    """Capture one test error and report what happened to it (used by `manage.py sentry_test` and /ops/sentry-check/)."""
    import sentry_sdk

    if not sentry_sdk.get_client().is_active():
        return {"active": False, "transport": "off", "event_id": None, "delivery": None}
    try:
        raise SentryCheckError(f"Internest Sentry check from {source} (safe to resolve)")
    except SentryCheckError as exc:
        event_id = sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(timeout=10)  # instant with the synchronous transport
    return {"active": True, "transport": transport_name(), "event_id": event_id, "delivery": last_delivery_status()}


class SentryCheckError(Exception):
    """Raised on purpose by send_check(): shows up in Sentry as a separate, clearly named issue."""
