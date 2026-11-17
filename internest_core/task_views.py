"""Status / cancel endpoints for async tasks (see internest_core/async_tasks.py). Owner-only, never cached."""
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import async_tasks
from .models import AsyncTask


def _own_task(request, task_id):
    task = AsyncTask.objects.filter(pk=task_id, user_id=request.user.pk).only(
        "id", "user_id", "kind", "status", "result", "http_status", "error", "created_at", "started_at").first()
    if task is None:
        raise Http404
    return task


def _json(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_GET
def task_status(request, task_id):
    task = async_tasks.expire_stale(_own_task(request, task_id))
    return _json(async_tasks.status_payload(task))


@login_required
@require_POST
def task_cancel(request, task_id):
    task = _own_task(request, task_id)
    AsyncTask.objects.filter(pk=task.pk, status=AsyncTask.STATUS_QUEUED).update(status=AsyncTask.STATUS_CANCELLED, payload={})
    task.refresh_from_db()
    return _json(async_tasks.status_payload(task))
