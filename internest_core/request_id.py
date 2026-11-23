"""One id per request: response header X-Request-ID, every log line, and the Sentry event, so a user's report, the
log and the error can be matched. An incoming X-Request-ID (from nginx or a client) is reused only when it is a
short, safe token; otherwise a fresh one is generated."""
import contextvars
import logging
import re
import uuid

_current = contextvars.ContextVar("request_id", default="-")
_SAFE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
HEADER = "X-Request-ID"


def current() -> str:
    return _current.get()


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = incoming if _SAFE.match(incoming) else uuid.uuid4().hex[:16]
        request.request_id = request_id
        token = _current.set(request_id)
        try:
            try:
                import sentry_sdk

                sentry_sdk.set_tag("request_id", request_id)
            except ImportError:  # Sentry is optional
                pass
            response = self.get_response(request)
        finally:
            _current.reset(token)
        response[HEADER] = request_id
        return response


class RequestIDLogFilter(logging.Filter):
    """Adds %(request_id)s to every record ("-" outside a request: workers, management commands)."""

    def filter(self, record):
        record.request_id = _current.get()
        return True
