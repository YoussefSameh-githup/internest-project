"""Lightweight async task runner: heavy AI jobs never hold a web worker while the LLM thinks.

An endpoint that supports it answers `Prefer: respond-async` requests with HTTP 202 {task_id, status, status_url}
in a few milliseconds; the browser polls the status URL (static/js/async_task_poller.js). Requests without the
header keep the original synchronous behaviour, so old clients and tests are unaffected.

    @register("cv_polish")
    def cv_polish_job(user_id, cv):           # -> (http_status, json_body), exactly what the sync view returns
        ...

    task = enqueue("cv_polish", request.user, cv=cv)
    return accepted(task)

Backends (settings.ASYNC_TASK_BACKEND, env ASYNC_TASK_BACKEND):
  sync    run inline in the request (PythonAnywhere: web apps there cannot start threads). Same API, no speedup.
  thread  in-process thread pool (VPS / Gunicorn / Uvicorn). No broker needed.
  db      persisted queue drained by `python manage.py run_async_tasks` (an always-on task / systemd service).
  celery  hands the task id to Celery when it is installed (`internest_core.async_tasks.celery_run`); else falls back to db.
Job arguments must be JSON-serialisable; bytes are allowed (base64 in the queue for db/celery).
"""
import base64
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections, transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

REGISTRY: dict = {}
BACKENDS = ("sync", "thread", "db", "celery")
_executor = None
_executor_lock = threading.Lock()


class TaskRejected(Exception):
    """Raised by a job to finish with a handled error (http_status + body), e.g. 'AI is busy'."""


def register(kind: str):
    def decorator(fn):
        REGISTRY[kind] = fn
        return fn
    return decorator


def autodiscover():
    """Import `<app>.jobs` for every installed app so the registry is filled in web and worker processes alike."""
    from django.apps import apps
    from django.utils.module_loading import autodiscover_modules

    autodiscover_modules("jobs")
    return sorted(REGISTRY)


def backend() -> str:
    value = getattr(settings, "ASYNC_TASK_BACKEND", "sync")
    return value if value in BACKENDS else "sync"


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


# ---------------------------------------------------------------- enqueue / run
def enqueue(kind: str, user, **kwargs):
    """Create the task and hand it to the configured backend. Returns the AsyncTask (already done for `sync`)."""
    from .models import AsyncTask

    if kind not in REGISTRY:
        raise KeyError(f"Unknown async task kind: {kind}")
    from django.utils import translation

    kwargs = {**kwargs, "_lang": translation.get_language()}  # the job answers in the user's language
    mode = backend()
    persisted = mode in ("db", "celery")
    task = AsyncTask.objects.create(user=user, kind=kind, payload=_encode(kwargs) if persisted else {})
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


def run(task_id, kwargs=None) -> bool:
    """Execute one task if it is still queued (an atomic claim, so two workers never run the same job)."""
    from .models import AsyncTask

    claimed = AsyncTask.objects.filter(pk=task_id, status=AsyncTask.STATUS_QUEUED).update(
        status=AsyncTask.STATUS_RUNNING, started_at=timezone.now())
    if not claimed:
        return False
    task = AsyncTask.objects.get(pk=task_id)
    from django.utils import translation

    args = dict(kwargs if kwargs is not None else _decode(task.payload))
    lang = args.pop("_lang", None)
    try:
        with translation.override(lang):
            http_status, body = REGISTRY[task.kind](task.user_id, **args)
        status, error = AsyncTask.STATUS_DONE, ""
    except TaskRejected as exc:
        http_status, body = exc.args if len(exc.args) == 2 else (400, {"error": str(exc)})
        status, error = AsyncTask.STATUS_DONE, ""
    except Exception:
        logger.exception("Async task %s (%s) failed", task_id, task.kind)
        http_status, body, status, error = 500, None, AsyncTask.STATUS_FAILED, "internal_error"
    AsyncTask.objects.filter(pk=task_id).update(status=status, http_status=http_status, result=body, error=error,
                                                payload={}, finished_at=timezone.now())
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


def _send_to_celery(task_id):
    try:
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

    celery_run = _NoCelery()


def run_next() -> bool:
    """Claim and run the oldest queued task (db worker). False when the queue is empty."""
    from .models import AsyncTask

    task_id = AsyncTask.objects.filter(status=AsyncTask.STATUS_QUEUED).order_by("created_at").values_list("pk", flat=True).first()
    return run(task_id) if task_id else False


# ---------------------------------------------------------------- housekeeping
def expire_stale(task):
    """A task nobody picked up (worker down) or that hung is failed, so the browser stops waiting."""
    from .models import AsyncTask

    now = timezone.now()
    queued_limit = timedelta(seconds=getattr(settings, "ASYNC_TASK_QUEUE_TIMEOUT", 120))
    running_limit = timedelta(seconds=getattr(settings, "ASYNC_TASK_RUN_TIMEOUT", 300))
    stale = ((task.status == AsyncTask.STATUS_QUEUED and now - task.created_at > queued_limit)
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
