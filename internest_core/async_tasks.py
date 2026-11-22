"""Lightweight async task runner: heavy AI jobs never hold a web worker while the LLM thinks.

An endpoint that supports it answers `Prefer: respond-async` requests with HTTP 202 {task_id, status, status_url}
in a few milliseconds; the browser polls the status URL (static/js/async_task_poller.js). Requests without the
header keep the original synchronous behaviour, so old clients and tests are unaffected.

    @register("cv_polish")
    def cv_polish_job(user_id, cv):           # -> (http_status, json_body), exactly what the sync view returns
        ...

    return enqueue_response("cv_polish", request.user, cv=cv)     # 202, or 429 / 503 + Retry-After when refused
    task = enqueue("cv_polish", request.user, cv=cv)              # lower level: raises TaskThrottled when refused

Backends (settings.ASYNC_TASK_BACKEND, env ASYNC_TASK_BACKEND):
  sync    run inline in the request (PythonAnywhere: web apps there cannot start threads). Same API, no speedup.
  thread  in-process thread pool (VPS / Gunicorn / Uvicorn). No broker needed.
  db      persisted queue drained by `python manage.py run_async_tasks` (an always-on task / systemd service).
  celery  hands the task id to Celery when it is installed (`internest_core.async_tasks.celery_run`); else falls back to db.
Job arguments must be JSON-serialisable; bytes are allowed (base64 in the queue for db/celery).

Reliability and admission control:
  * idempotency: the same user + kind + idempotency_key while queued/running returns the existing task (a partial
    unique index makes this hold under concurrent double-submits). enqueue_response derives the key from the arguments.
  * per-user cap: ASYNC_TASK_USER_MAX_ACTIVE active tasks per user -> TaskThrottled 429 + Retry-After.
  * backpressure: ASYNC_TASK_MAX_QUEUED queued tasks site-wide -> TaskThrottled 503 + Retry-After.
  * priority: higher `priority` is claimed first by the db worker (system jobs may use PRIORITY_LOW).
  * retries: a job raising RetryTask (or a transient database / network error) is retried with exponential backoff
    (next_run_at) up to max_attempts, then becomes "dead". Only persisted backends (db / celery) can retry.
  * leases: a running task whose lease expired (worker killed mid-job) is re-claimed by the db worker.
  * system jobs: `user=None`.
"""
import base64
import hashlib
import json
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, OperationalError, close_old_connections, transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

logger = logging.getLogger(__name__)

REGISTRY: dict = {}
BACKENDS = ("sync", "thread", "db", "celery")
_executor = None
_executor_lock = threading.Lock()


class TaskRejected(Exception):
    """Raised by a job to finish with a handled error (http_status + body), e.g. 'AI is busy'."""


class RetryTask(Exception):
    """Raised by a job for a transient failure: retried with backoff (persisted backends) until max_attempts."""

    def __init__(self, message="", delay: int | None = None):
        super().__init__(message)
        self.delay = delay


class TaskThrottled(Exception):
    """enqueue refused the task: 429 (this user has too many active tasks) or 503 (the queue is full)."""

    def __init__(self, status: int, code: str, retry_after: int, message: str):
        super().__init__(message)
        self.status, self.code, self.retry_after, self.message = status, code, max(1, int(retry_after)), message


RETRYABLE = (RetryTask, OperationalError, httpx.TransportError, TimeoutError, ConnectionError)


def register(kind: str):
    def decorator(fn):
        REGISTRY[kind] = fn
        return fn
    return decorator


def autodiscover():
    """Import `<app>.jobs` for every installed app so the registry is filled in web and worker processes alike."""
    from django.utils.module_loading import autodiscover_modules

    autodiscover_modules("jobs")
    return sorted(REGISTRY)


def backend() -> str:
    value = getattr(settings, "ASYNC_TASK_BACKEND", "sync")
    return value if value in BACKENDS else "sync"


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def wants_async(request) -> bool:
    return "respond-async" in request.headers.get("Prefer", "").lower()


# ---------------------------------------------------------------- argument (de)serialisation for persisted queues
def _encode(value):
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"__bytes__"}:
            return base64.b64decode(value["__bytes__"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def idempotency_key_for(kind: str, kwargs: dict) -> str:
    """A stable key for "the same job with the same arguments" (SHA-256 of the canonical JSON)."""
    canonical = json.dumps(_encode(kwargs), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{kind}\n{canonical}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- admission control
def _active_window() -> timedelta:
    """Older queued/running rows are stale (worker down, crashed job): they no longer count or deduplicate."""
    return timedelta(seconds=_setting("ASYNC_TASK_QUEUE_TIMEOUT", 120) + _setting("ASYNC_TASK_RUN_TIMEOUT", 300)
                     + _setting("ASYNC_TASK_RETRY_MAX_SECONDS", 300))


def _live_active(qs):
    from .models import AsyncTask

    return qs.filter(status__in=AsyncTask.ACTIVE, created_at__gte=timezone.now() - _active_window())


def _existing(user_id, kind, key):
    from .models import AsyncTask

    return _live_active(AsyncTask.objects.filter(user_id=user_id, kind=kind, idempotency_key=key)).first()


def _retire_stale_duplicates(user_id, kind, key):
    """An old active row with the same key would block the unique index: fail it (it was never going to finish)."""
    from .models import AsyncTask

    AsyncTask.objects.filter(user_id=user_id, kind=kind, idempotency_key=key, status__in=AsyncTask.ACTIVE,
                             created_at__lt=timezone.now() - _active_window()).update(
        status=AsyncTask.STATUS_FAILED, error="timeout", http_status=504, payload={}, finished_at=timezone.now())


def _admit(user_id, mode):
    from .models import AsyncTask

    cap = _setting("ASYNC_TASK_USER_MAX_ACTIVE", 2)
    if user_id is not None and cap > 0 and _live_active(AsyncTask.objects.filter(user_id=user_id)).count() >= cap:
        raise TaskThrottled(429, "too_many_tasks", _setting("ASYNC_TASK_USER_RETRY_AFTER", 10),
                            gettext("You already have tasks in progress. Please wait for them to finish, then try again."))
    limit = _setting("ASYNC_TASK_MAX_QUEUED", 500)
    if mode != "sync" and limit > 0 and AsyncTask.objects.filter(status=AsyncTask.STATUS_QUEUED).count() >= limit:
        raise TaskThrottled(503, "queue_full", _setting("ASYNC_TASK_BACKPRESSURE_RETRY_AFTER", 30),
                            gettext("We are very busy right now. Please try again in a moment."))


# ---------------------------------------------------------------- enqueue / run
def enqueue(kind: str, user, *, idempotency_key: str = "", priority: int = 0, max_attempts: int | None = None, **kwargs):
    """Create the task and hand it to the configured backend. Returns the AsyncTask (already done for `sync`).

    `user` may be None (system job). Raises TaskThrottled when the user or the queue is at its cap. With an
    `idempotency_key`, an active task of the same user + kind + key is returned instead of a new one
    (`task.deduplicated` is True then)."""
    from .models import AsyncTask

    if kind not in REGISTRY:
        raise KeyError(f"Unknown async task kind: {kind}")
    from django.utils import translation

    kwargs = {**kwargs, "_lang": translation.get_language()}  # the job answers in the user's language
    mode = backend()
    persisted = mode in ("db", "celery")
    user_id = getattr(user, "pk", user)
    key = idempotency_key if len(idempotency_key) <= 64 else hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    attempts = max(1, max_attempts if max_attempts is not None else _setting("ASYNC_TASK_MAX_ATTEMPTS", 3))
    with transaction.atomic():
        if user_id is not None:
            from django.contrib.auth import get_user_model

            list(get_user_model().objects.select_for_update().filter(pk=user_id).values_list("pk"))  # serialise per user
        if key:
            existing = _existing(user_id, kind, key)
            if existing is not None:
                existing.deduplicated = True
                return existing
            _retire_stale_duplicates(user_id, kind, key)
        _admit(user_id, mode)
        try:
            with transaction.atomic():
                task = AsyncTask.objects.create(user_id=user_id, kind=kind, payload=_encode(kwargs) if persisted else {},
                                                idempotency_key=key, priority=priority, max_attempts=attempts if persisted else 1)
        except IntegrityError:  # a concurrent double-submit created it first
            existing = _existing(user_id, kind, key) if key else None
            if existing is None:
                raise
            existing.deduplicated = True
            return existing
    task.deduplicated = False
    if random.random() < 0.02:
        purge_finished()
    if mode == "sync":
        run(task.pk, kwargs)
        task.refresh_from_db()
    elif mode == "thread":
        transaction.on_commit(lambda: _pool().submit(_run_in_thread, task.pk, kwargs))
    elif mode == "celery":
        transaction.on_commit(lambda: _send_to_celery(task.pk))
    return task


def _backoff(attempt: int, requested: int | None = None) -> int:
    if requested:
        return max(1, int(requested))
    base, cap = _setting("ASYNC_TASK_RETRY_BASE_SECONDS", 5), _setting("ASYNC_TASK_RETRY_MAX_SECONDS", 300)
    return min(cap, base * 2 ** max(0, attempt - 1))


def _dead_body() -> dict:
    return {"error": gettext("This is taking longer than expected. Please try again later.")}


def _retry_or_bury(task, *, persisted: bool, reason: str, delay: int | None = None) -> str:
    """After a retryable failure: back to the queue with backoff, or "dead" when no attempts are left."""
    from .models import AsyncTask

    now = timezone.now()
    if persisted and task.attempts < task.max_attempts:
        wait = _backoff(task.attempts, delay)
        AsyncTask.objects.filter(pk=task.pk, status=AsyncTask.STATUS_RUNNING).update(
            status=AsyncTask.STATUS_QUEUED, next_run_at=now + timedelta(seconds=wait), started_at=None, lease_expires_at=None,
            error=reason[:300])
        logger.warning("Async task %s (%s) attempt %s/%s failed (%s); retry in %ss", task.pk, task.kind, task.attempts,
                       task.max_attempts, reason, wait)
        if backend() == "celery":
            _send_to_celery(task.pk, countdown=wait)
        return AsyncTask.STATUS_QUEUED
    AsyncTask.objects.filter(pk=task.pk, status=AsyncTask.STATUS_RUNNING).update(
        status=AsyncTask.STATUS_DEAD, http_status=503, result=_dead_body(), error=reason[:300], payload={},
        lease_expires_at=None, finished_at=now)
    logger.error("Async task %s (%s) is dead after %s attempt(s): %s", task.pk, task.kind, task.attempts, reason)
    return AsyncTask.STATUS_DEAD


def run(task_id, kwargs=None) -> bool:
    """Execute one task if it is still queued (an atomic claim, so two workers never run the same job)."""
    from .models import AsyncTask

    now = timezone.now()
    claimed = AsyncTask.objects.filter(pk=task_id, status=AsyncTask.STATUS_QUEUED).update(
        status=AsyncTask.STATUS_RUNNING, started_at=now, attempts=F("attempts") + 1,
        lease_expires_at=now + timedelta(seconds=_setting("ASYNC_TASK_LEASE_SECONDS", 300)))
    if not claimed:
        return False
    task = AsyncTask.objects.get(pk=task_id)
    from django.utils import translation

    persisted = kwargs is None  # arguments come from the stored payload, so the task can run again later
    args = dict(kwargs if kwargs is not None else _decode(task.payload))
    lang = args.pop("_lang", None)
    try:
        with translation.override(lang):
            http_status, body = REGISTRY[task.kind](task.user_id, **args)
        status, error = AsyncTask.STATUS_DONE, ""
    except TaskRejected as exc:
        http_status, body = exc.args if len(exc.args) == 2 else (400, {"error": str(exc)})
        status, error = AsyncTask.STATUS_DONE, ""
    except RETRYABLE as exc:
        with translation.override(lang):
            _retry_or_bury(task, persisted=persisted, reason=f"{type(exc).__name__}: {exc}"[:300],
                           delay=getattr(exc, "delay", None))
        return True
    except Exception:
        logger.exception("Async task %s (%s) failed", task_id, task.kind)
        http_status, body, status, error = 500, None, AsyncTask.STATUS_FAILED, "internal_error"
    AsyncTask.objects.filter(pk=task_id).update(status=status, http_status=http_status, result=body, error=error,
                                                payload={}, lease_expires_at=None, finished_at=timezone.now())
    return True


def _run_in_thread(task_id, kwargs):
    close_old_connections()
    try:
        run(task_id, kwargs)
    finally:
        close_old_connections()


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=getattr(settings, "ASYNC_TASK_WORKERS", 4), thread_name_prefix="internest-task")
    return _executor


def _send_to_celery(task_id, countdown: int | None = None):
    try:
        if countdown:
            celery_run.apply_async((str(task_id),), countdown=countdown)
        else:
            celery_run.delay(str(task_id))
    except Exception:  # Celery missing or the broker down: the db worker still drains the queue
        logger.exception("Could not hand task %s to Celery; leaving it for the db worker", task_id)


try:  # optional Celery integration (VPS); never required
    from celery import shared_task

    @shared_task(name="internest_core.async_tasks.celery_run")
    def celery_run(task_id):
        close_old_connections()
        return run(task_id)
except ImportError:  # pragma: no cover - Celery not installed
    class _NoCelery:
        @staticmethod
        def delay(task_id):
            raise RuntimeError("Celery is not installed")

        apply_async = delay

    celery_run = _NoCelery()


def run_next() -> bool:
    """Claim and run the next due task: highest priority first, then oldest (db worker). False when nothing is due."""
    from .models import AsyncTask

    due = (AsyncTask.objects.filter(status=AsyncTask.STATUS_QUEUED)
           .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=timezone.now()))
           .order_by("-priority", "created_at").values_list("pk", flat=True)[:5])
    return any(run(task_id) for task_id in due)  # a task another worker claimed first is skipped


# ---------------------------------------------------------------- housekeeping
def _recover_expired_lease(task) -> str | None:
    """A running task past its lease: re-queue it (persisted, attempts left), else dead. None when another process won."""
    from .models import AsyncTask

    now = timezone.now()
    retryable = (AsyncTask.objects.filter(pk=task.pk, status=AsyncTask.STATUS_RUNNING, lease_expires_at__lt=now,
                                          attempts__lt=F("max_attempts")).exclude(payload={}))
    if retryable.update(status=AsyncTask.STATUS_QUEUED, started_at=None, lease_expires_at=None, error="lease_expired",
                        next_run_at=now + timedelta(seconds=_backoff(task.attempts))):
        logger.warning("Async task %s (%s) re-claimed: its worker stopped mid-job (lease expired)", task.pk, task.kind)
        return AsyncTask.STATUS_QUEUED
    if AsyncTask.objects.filter(pk=task.pk, status=AsyncTask.STATUS_RUNNING, lease_expires_at__lt=now).update(
            status=AsyncTask.STATUS_DEAD, http_status=503, result=_dead_body(), error="lease_expired", payload={},
            lease_expires_at=None, finished_at=now):
        logger.error("Async task %s (%s) is dead: its lease expired with no attempts left", task.pk, task.kind)
        return AsyncTask.STATUS_DEAD
    return None


def reclaim_stale() -> int:
    """db worker: recover every running task whose lease expired. Returns how many were handled."""
    from .models import AsyncTask

    stuck = AsyncTask.objects.filter(status=AsyncTask.STATUS_RUNNING, lease_expires_at__lt=timezone.now()).only(
        "id", "kind", "attempts")[:100]
    return sum(1 for task in stuck if _recover_expired_lease(task))


def expire_stale(task):
    """A task nobody picked up (worker down) or that hung is failed, so the browser stops waiting.

    A persisted task whose lease expired is re-queued instead while it has attempts left (see reclaim_stale)."""
    from .models import AsyncTask

    now = timezone.now()
    queued_limit = timedelta(seconds=_setting("ASYNC_TASK_QUEUE_TIMEOUT", 120))
    running_limit = timedelta(seconds=_setting("ASYNC_TASK_RUN_TIMEOUT", 300))
    if task.status == AsyncTask.STATUS_RUNNING and task.lease_expires_at and task.lease_expires_at < now \
            and backend() in ("db", "celery") and _recover_expired_lease(task) is not None:
        task.refresh_from_db()
        return task
    available_since = max(task.created_at, task.next_run_at or task.created_at)
    stale = ((task.status == AsyncTask.STATUS_QUEUED and now - available_since > queued_limit)
             or (task.status == AsyncTask.STATUS_RUNNING and task.started_at and now - task.started_at > running_limit))
    if stale and AsyncTask.objects.filter(pk=task.pk, status=task.status).update(
            status=AsyncTask.STATUS_FAILED, error="timeout", http_status=504, payload={}, finished_at=now):
        task.refresh_from_db()
    return task


def purge_finished(older_than=timedelta(days=1)) -> int:
    from .models import AsyncTask

    deleted, _detail = AsyncTask.objects.filter(status__in=AsyncTask.FINISHED, created_at__lt=timezone.now() - older_than).delete()
    return deleted


# ---------------------------------------------------------------- HTTP helpers
def status_payload(task) -> dict:
    data = {"task_id": str(task.pk), "kind": task.kind, "status": task.status,
            "status_url": reverse("async_task_status", args=[task.pk])}
    if task.is_finished:
        data.update({"http_status": task.http_status, "result": task.result, "error": task.error or None})
    else:
        data["poll_after_ms"] = 600
    return data


def accepted(task) -> JsonResponse:
    """The 202 answer for a just-enqueued task (already finished when the backend is `sync`)."""
    response = JsonResponse(status_payload(task), status=202)
    response["Location"] = reverse("async_task_status", args=[task.pk])
    response["Cache-Control"] = "no-store"
    return response


def throttled_response(exc: TaskThrottled) -> JsonResponse:
    """429 / 503 with Retry-After and a message the poller shows to the user."""
    response = JsonResponse({"error": exc.message, "code": exc.code, "retry_after": exc.retry_after}, status=exc.status)
    response["Retry-After"] = str(exc.retry_after)
    response["Cache-Control"] = "no-store"
    return response


def enqueue_response(kind: str, user, *, idempotency_key: str | None = None, **kwargs) -> JsonResponse:
    """The view helper: enqueue (deduplicated on the arguments by default) and answer 202, or 429 / 503 when refused."""
    key = idempotency_key if idempotency_key is not None else idempotency_key_for(kind, kwargs)
    try:
        return accepted(enqueue(kind, user, idempotency_key=key, **kwargs))
    except TaskThrottled as exc:
        logger.info("Async task %s refused for user %s: %s", kind, getattr(user, "pk", user), exc.code)
        return throttled_response(exc)
