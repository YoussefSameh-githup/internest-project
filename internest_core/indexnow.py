"""IndexNow: tell Bing / Yandex / Seznam / Naver that a public URL changed, so they recrawl it soon.

One entry point for every publication path: `notify(urls)` (or `notify_internships(pks)`). It keeps only public,
indexable URLs on SITE_URL, and enqueues ONE `indexnow_submit` job on the existing DB queue after the surrounding
transaction commits, so no request or transaction ever waits on the network. Inside `with batch():` every notify is
collected and sent as one request (admin bulk actions, submission approvals).

The worker job POSTs {host, key, keyLocation, urlList} to api.indexnow.org through `monitored_api_call`.
200/202 = accepted (NOT indexed). 429 / 5xx / network errors are retried with backoff; 400/403/422 are configuration
or validation problems and are not retried. A missing INDEXNOW_API_KEY or SITE_URL disables submissions (logged once
per process), nothing else. The key is public by design (served at keyLocation) but is still never logged.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.db import transaction
from django.urls import reverse

from . import http_client
from .api_monitor import monitored_api_call
from .async_tasks import RetryTask, enqueue, register
from .seo import PUBLIC_PAGES, is_noindex_path, site_url

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.indexnow.org/indexnow"
JOB = "indexnow_submit"
PROVIDER = "indexnow"
MAX_URLS = 10_000           # IndexNow limit per request
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER = 3600      # seconds; a longer Retry-After is capped
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
ACCEPTED = (200, 202)
RETRY_STATUSES = (429, 500, 502, 503, 504)

_local = threading.local()
_warned = False


# ---------------------------------------------------------------- configuration
def api_key() -> str:
    return (getattr(settings, "INDEXNOW_API_KEY", "") or "").strip()


def base_url() -> str:
    """The canonical https origin (SITE_URL). IndexNow needs a fixed host, so there is no request fallback."""
    base = site_url()
    return base if base.startswith("https://") else ""


def is_configured() -> bool:
    global _warned
    ok = bool(api_key() and base_url())
    if not ok and not _warned:
        _warned = True
        logger.warning("IndexNow disabled: set INDEXNOW_API_KEY and an https SITE_URL in .env to enable submissions.")
    return ok


# ---------------------------------------------------------------- URLs
def internship_url(pk) -> str:
    return f"{base_url()}{reverse('internship_detail', args=[pk])}"


def public_page_urls() -> list[str]:
    """The indexable site pages from seo.PUBLIC_PAGES plus the sitemap (never private or personal paths)."""
    base = base_url()
    return [f"{base}{reverse(name)}" for name in PUBLIC_PAGES] + [f"{base}{reverse('sitemap')}"]


def clean(urls) -> list[str]:
    """Only absolute URLs on our own origin, outside the private / noindex prefixes; deduplicated, order kept."""
    base = base_url()
    out, seen = [], set()
    for url in urls:
        url = (url or "").strip()
        if not base or not url.startswith(base + "/") or url in seen:
            continue
        path = url[len(base):]
        if is_noindex_path(path) or "?" in path or "#" in path:
            continue
        seen.add(url)
        out.append(url)
    return out


def payload(urls: list[str]) -> dict:
    base, key = base_url(), api_key()
    return {"host": urlsplit(base).hostname, "key": key, "keyLocation": f"{base}/{key}.txt", "urlList": list(urls)}


# ---------------------------------------------------------------- scheduling (web process)
def queue_urls(urls: list[str]) -> None:
    for start in range(0, len(urls), MAX_URLS):
        chunk = urls[start:start + MAX_URLS]
        key = hashlib.sha256("\n".join(sorted(chunk)).encode("utf-8")).hexdigest()
        try:
            enqueue(JOB, None, idempotency_key=key, max_attempts=MAX_ATTEMPTS, urls=chunk)
        except Exception:  # queue full / DB hiccup: never break the publishing request
            logger.exception("Could not enqueue an IndexNow submission (%s URLs)", len(chunk))


def notify(urls) -> int:
    """Queue a submission for these URLs once the current transaction commits. Returns how many URLs were kept."""
    try:
        if not is_configured():
            return 0
        kept = clean(urls)
        if not kept:
            return 0
        pending = getattr(_local, "urls", None)
        if pending is not None:  # inside batch(): sent together when it ends
            pending.extend(kept)
            return len(kept)
        transaction.on_commit(lambda: queue_urls(kept))
        return len(kept)
    except Exception:
        logger.exception("IndexNow notify failed")
        return 0


def notify_internships(pks) -> int:
    pks = list(pks)
    if not pks or not is_configured():
        return 0
    return notify([internship_url(pk) for pk in pks] + [f"{base_url()}{reverse('sitemap')}"])


@contextmanager
def batch():
    """Collect every notify() in this block into one submission (nested blocks join the outer one)."""
    if getattr(_local, "urls", None) is not None:
        yield
        return
    _local.urls = []
    try:
        yield
    finally:
        urls, _local.urls = _local.urls, None
        if urls:
            notify(urls)


# ---------------------------------------------------------------- submission (worker)
def _retry_after(response) -> int | None:
    try:
        return min(MAX_RETRY_AFTER, max(1, int(response.headers.get("Retry-After", ""))))
    except (TypeError, ValueError):
        return None


def submit(urls) -> tuple[int, dict]:
    """POST one batch now. Raises RetryTask for temporary failures; returns (status, body) otherwise."""
    if not is_configured():
        return 503, {"error": "IndexNow is not configured", "code": "indexnow_not_configured"}
    kept = clean(urls)[:MAX_URLS]
    if not kept:
        return 200, {"submitted": 0}
    response = monitored_api_call(PROVIDER, JOB, http_client.post, ENDPOINT, json=payload(kept),
                                  headers={"Content-Type": "application/json; charset=utf-8"}, timeout=TIMEOUT)
    status = response.status_code
    if status in ACCEPTED:
        logger.info("IndexNow accepted %s URL(s) (HTTP %s)", len(kept), status)
        return 200, {"submitted": len(kept), "indexnow_status": status}
    if status in RETRY_STATUSES:
        raise RetryTask(f"IndexNow HTTP {status}", delay=_retry_after(response))
    logger.error("IndexNow rejected %s URL(s): HTTP %s (key file, host or URL list; not retried)", len(kept), status)
    return 502, {"error": f"IndexNow HTTP {status}", "code": "indexnow_rejected", "indexnow_status": status}


@register(JOB)
def indexnow_submit_job(user_id, urls):
    return submit(urls)
