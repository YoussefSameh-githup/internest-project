"""One pooled HTTP client per process for every outbound API call (AI providers, payments, balance checks).

    from internest_core import http_client
    http_client.get_client().post(url, json=...)          # sync, keep-alive pool shared by all requests
    await http_client.get_async_client().post(url, ...)   # async (ASGI / async views), one pool per event loop

Why: building a new client per call pays a fresh TCP + TLS handshake (100-300 ms) every time. The pool keeps
connections to Gemini / OpenAI-compatible endpoints warm. HTTP/2 is used when the optional `h2` package is installed.

Retries: only for failures where the request certainly did not do anything on the provider side: connection
errors before the request was sent, and 502/503 replies. Never for 401/402/403/429 or timeouts after sending,
so a paid call is never billed twice and quota errors reach the API monitor unchanged.
"""
import asyncio
import logging
import os
import random
import threading
import time

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({502, 503})
_RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)


def _setting(name, default):
    return getattr(settings, name, default)


def _limits() -> httpx.Limits:
    return httpx.Limits(max_connections=_setting("HTTP_CLIENT_MAX_CONNECTIONS", 100),
                        max_keepalive_connections=_setting("HTTP_CLIENT_MAX_KEEPALIVE", 30), keepalive_expiry=30)


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(_setting("HTTP_CLIENT_READ_TIMEOUT", 60), connect=_setting("HTTP_CLIENT_CONNECT_TIMEOUT", 5),
                         pool=_setting("HTTP_CLIENT_POOL_TIMEOUT", 5))


def http2_available() -> bool:
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter: 0..min(2s, 0.25 * 2^attempt)."""
    return random.uniform(0, min(2.0, 0.25 * (2 ** attempt)))


class RetryTransport(httpx.HTTPTransport):
    """httpx transport that retries connect failures and 502/503 with jittered backoff."""

    def __init__(self, *args, retries: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.retries = retries

    def handle_request(self, request):
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = super().handle_request(request)
            except _RETRY_EXCEPTIONS:
                if last:
                    raise
                logger.info("HTTP connect to %s failed; retry %s", request.url.host, attempt + 1)
            else:
                if response.status_code not in RETRY_STATUSES or last:
                    return response
                response.close()
                logger.info("HTTP %s from %s; retry %s", response.status_code, request.url.host, attempt + 1)
            time.sleep(backoff_delay(attempt))
        raise RuntimeError("unreachable")  # pragma: no cover


class AsyncRetryTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *args, retries: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.retries = retries

    async def handle_async_request(self, request):
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = await super().handle_async_request(request)
            except _RETRY_EXCEPTIONS:
                if last:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or last:
                    return response
                await response.aclose()
            await asyncio.sleep(backoff_delay(attempt))
        raise RuntimeError("unreachable")  # pragma: no cover


def proxy_url() -> str | None:
    """The outbound proxy from the environment (PythonAnywhere free accounts need theirs); None with ENABLE_PROXY_BYPASS."""
    if _setting("ENABLE_PROXY_BYPASS", False):
        return None
    return (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None)


def _client_kwargs(transport_cls):
    http2 = http2_available()
    transport = transport_cls(retries=_setting("HTTP_CLIENT_RETRIES", 2), limits=_limits(), http2=http2, proxy=proxy_url())
    # trust_env=False on the client: env proxies would otherwise be mounted as plain transports and bypass the retries.
    return {"transport": transport, "timeout": _timeout(), "trust_env": False, "http2": http2,
            "headers": {"User-Agent": "Internest/1.0"}, "follow_redirects": False}


_lock = threading.Lock()
_sync_client: httpx.Client | None = None
_sync_pid: int | None = None
_async_clients: dict = {}


def get_client() -> httpx.Client:
    """The process-wide pooled client. Recreated after a fork (gunicorn --preload) so workers never share sockets."""
    global _sync_client, _sync_pid
    pid = os.getpid()
    if _sync_client is None or _sync_pid != pid or _sync_client.is_closed:
        with _lock:
            if _sync_client is None or _sync_pid != pid or _sync_client.is_closed:
                _sync_client, _sync_pid = httpx.Client(**_client_kwargs(RetryTransport)), pid
    return _sync_client


def get_async_client() -> httpx.AsyncClient:
    """One pooled AsyncClient per running event loop (an AsyncClient must not cross loops)."""
    loop = asyncio.get_running_loop()
    key = (os.getpid(), id(loop))
    client = _async_clients.get(key)
    if client is None or client.is_closed:
        for stale in [k for k, c in _async_clients.items() if c.is_closed or k[0] != key[0]]:
            _async_clients.pop(stale, None)
        client = _async_clients[key] = httpx.AsyncClient(**_client_kwargs(AsyncRetryTransport))
    return client


def request(method: str, url: str, **kwargs) -> httpx.Response:
    return get_client().request(method, url, **kwargs)


def get(url: str, **kwargs) -> httpx.Response:
    return get_client().get(url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    return get_client().post(url, **kwargs)


def _reset_on_setting_change(*, setting, **kwargs):
    if setting.startswith("HTTP_CLIENT_") or setting == "ENABLE_PROXY_BYPASS":
        close_clients()


def close_clients() -> None:
    """Close the pools (tests, shutdown)."""
    global _sync_client
    with _lock:
        if _sync_client is not None:
            _sync_client.close()
        _sync_client = None
    _async_clients.clear()


from django.core.signals import setting_changed  # noqa: E402  (tests use override_settings)

setting_changed.connect(_reset_on_setting_change, dispatch_uid="internest_http_client_reset")
