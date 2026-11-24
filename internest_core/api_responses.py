"""The one JSON error shape of every Internest API: {"error": <message for people>, "code": <machine code>, ...}.

Used by the AI APIs (internest_skills.api, internest_careers.api, internest_students.api), the task queue's 429/503
answers (async_tasks.enqueue_response) and the JSON error middleware; async_task_poller.js shows data.error.
"""
from django.http import JsonResponse


def json_error(message, code: str, status: int, *, retry_after: int | None = None, **extra) -> JsonResponse:
    response = JsonResponse({"error": str(message), "code": code, **extra}, status=status)
    response["Cache-Control"] = "no-store"
    if retry_after:
        response["Retry-After"] = str(int(retry_after))
    return response


def csrf_failure(request, reason=""):
    """CSRF_FAILURE_VIEW: /api/ callers get the JSON shape (same message as the 403_csrf.html page); pages keep the page."""
    if request.path_info.startswith("/api/"):
        from django.utils.translation import gettext as _

        return json_error(_("Your form session expired. Go back, refresh the page and try again."), "csrf_failed", 403)
    from django.views.csrf import csrf_failure as page

    return page(request, reason=reason)
