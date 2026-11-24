import json
import logging

from django.contrib import messages
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from internest_ai.errors import QuotaExceeded
from internest_core import async_tasks
from internest_core.views import get_user_context
from internest_skills.llm import LLMUnavailable
from internest_skills.models import Skill
from internest_skills.views import student_gate

from . import ai_tasks
from . import cv_builder_service as svc
from .cv_pdf import DEFAULT_TEMPLATE, TEMPLATES, render_cv_pdf
from .models import StudentCV

logger = logging.getLogger(__name__)

AI_USES_PER_DAY = 15
AUDIO_TYPES = {"audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
               "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}


def _cv_record(student):
    record, _created = StudentCV.objects.get_or_create(student=student)
    return record


def _payload(request):
    """CV dict from a JSON body ({"cv": {...}}) or a multipart "cv" field."""
    try:
        raw = request.POST.get("cv") if request.content_type.startswith("multipart/") else json.loads(request.body or b"{}").get("cv")
        return svc.clean_cv(json.loads(raw) if isinstance(raw, str) else raw)
    except (ValueError, AttributeError):
        return svc.clean_cv({})


def _use_ai(student):
    """Count one AI call against today's cap; False when the cap is reached."""
    with transaction.atomic():
        record = StudentCV.objects.select_for_update().get(pk=_cv_record(student).pk)
        today = timezone.localdate()
        if record.ai_day != today:
            record.ai_day, record.ai_uses = today, 0
        if record.ai_uses >= AI_USES_PER_DAY:
            return False
        record.ai_uses += 1
        record.save(update_fields=["ai_day", "ai_uses"])
        return True


def _refund_ai(user_id):
    """Give today's use back: the request was throttled or answered by an already-running task, nothing was spent."""
    StudentCV.objects.filter(student__user_id=user_id, ai_day=timezone.localdate(), ai_uses__gt=0).update(
        ai_uses=F("ai_uses") - 1)


def _queue_ai(request, kind, **kwargs):
    """Enqueue an AI job (202 + polling). A refusal (429 / 503) or a duplicate of a running task costs no daily use."""
    try:
        task = async_tasks.enqueue(kind, request.user, idempotency_key=async_tasks.idempotency_key_for(kind, kwargs), **kwargs)
    except async_tasks.TaskThrottled as exc:
        _refund_ai(request.user.pk)
        return async_tasks.throttled_response(exc)
    if task.deduplicated:
        _refund_ai(request.user.pk)
    return async_tasks.accepted(task)


def _quota_body(cv, exc, **extra) -> tuple[int, dict]:
    return 429, {"cv": cv, "error": exc.user_message, "code": "ai_quota", "retry_after": exc.retry_after, **extra}


def _limit_reached():
    return JsonResponse({"code": "daily_limit", "error": _("You've reached today's AI limit for the CV builder. Your data is saved in the form — try again tomorrow.")}, status=429)


@student_gate()
def cv_builder(request):
    student = request.student
    record = StudentCV.objects.filter(student=student).first()
    context = get_user_context(request)
    context.update({
        "cv": record.data if record and record.data else svc.initial_cv(student),
        "has_saved_cv": bool(record and record.data),
        "taxonomy": list(Skill.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
        "ai_enabled": ai_tasks.available(),
        "voice_enabled": svc.voice_enabled(),
        "max_seconds": 180,
    })
    return render(request, "students/student_cv_builder.html", context)


@student_gate(api=True)
@require_POST
def cv_polish(request):
    return polish_request(request, _payload(request), force_async=False)


def polish_request(request, cv, *, force_async):
    """The polish answer for the builder page (sync or 202) and the JSON API (always 202)."""
    if not ai_tasks.available():
        return JsonResponse({"cv": cv, "error": _("AI polishing isn't available right now. You can still finish and download your CV."),
                             "code": "ai_unavailable"}, status=503)
    if not _has_content(cv):
        return JsonResponse({"cv": cv, "code": "empty_cv", "error": _(
            "Add something to your CV first (a summary, an experience, a project or a skill), then polish it.")},
            status=400)
    if not _use_ai(request.student):
        return _limit_reached()
    if force_async or async_tasks.wants_async(request):  # 202 now, the browser polls /api/tasks/<id>/status/
        return _queue_ai(request, "cv_polish", cv=cv)
    return _respond(polish_outcome(cv, request.user.pk))


def _has_content(cv) -> bool:
    content = svc.polish_content(cv)
    return bool(str(content["summary"] or "").strip() or content["education"] or content["experiences"]
                or content["projects"] or content["skills"])


def _respond(outcome) -> JsonResponse:
    status, body = outcome
    return JsonResponse(body, status=status)


def polish_outcome(cv, user_id=None) -> tuple[int, dict]:
    """(HTTP status, JSON body) of an AI polish. Shared by the sync endpoint and the async job."""
    try:
        return 200, {"cv": svc.polish_cv(cv, user=user_id)}
    except QuotaExceeded as exc:  # the engine's cap: today's builder use is given back
        _refund_ai(user_id)
        return _quota_body(cv, exc)
    except LLMUnavailable:
        return 503, {"cv": cv, "code": "ai_busy", "error": _("Our AI is busy right now, so your text was kept as is. Try polishing again in a few minutes.")}


@student_gate(api=True)
@require_POST
def process_voice_cv(request):
    cv, audio = _payload(request), request.FILES.get("audio")
    content_type = (getattr(audio, "content_type", "") or "").split(";")[0].strip().lower()
    if audio is None or content_type not in AUDIO_TYPES:
        return JsonResponse({"code": "audio_missing", "error": _("Please record a voice note first (WebM, OGG, MP3, M4A or WAV).")}, status=400)
    if audio.size > svc.AUDIO_MAX_BYTES:
        return JsonResponse({"code": "audio_too_long", "error": _("The recording is too long. Keep it under 3 minutes.")}, status=400)
    if not svc.voice_enabled():
        return JsonResponse({"code": "voice_unavailable", "error": _("Voice input isn't available right now. Please fill in the form instead.")}, status=503)
    if not _use_ai(request.student):
        return _limit_reached()
    filename = f"voice.{AUDIO_TYPES[content_type]}"
    if async_tasks.wants_async(request):
        return _queue_ai(request, "cv_voice", cv=cv, audio=audio.read(), filename=filename, content_type=content_type)
    return _respond(voice_outcome(cv, audio.read(), filename, content_type, request.user.pk))


def voice_outcome(cv, audio: bytes, filename: str, content_type: str, user_id=None) -> tuple[int, dict]:
    """(HTTP status, JSON body) of transcription + AI organisation. Shared by the sync endpoint and the async job."""
    try:
        transcript = svc.transcribe(audio, filename, content_type)
    except svc.VoiceUnavailable:
        return 503, {"code": "transcription_failed", "error": _("We couldn't process your voice note right now. Please try again in a few minutes or fill in the form.")}
    if len(transcript) < 15:
        return 400, {"code": "audio_unclear", "error": _("We couldn't hear enough in this recording. Speak a little closer to the microphone and try again.")}
    try:
        return 200, {"cv": svc.polish_cv(cv, transcript, user=user_id), "transcript": transcript}
    except QuotaExceeded as exc:
        _refund_ai(user_id)
        return _quota_body(cv, exc, transcript=transcript)
    except LLMUnavailable:
        return 503, {"cv": cv, "transcript": transcript, "code": "ai_busy",
                     "error": _("We transcribed your voice note, but the AI couldn't organize it right now. Copy the details from the transcript below into the form.")}


@student_gate(api=True)
@require_POST
def cv_save(request):
    cv = _payload(request)
    if not cv["name"]:
        return JsonResponse({"code": "name_required", "error": _("Add your full name in Personal info."), "step": 0}, status=400)
    pending = svc.placeholders(cv)
    if pending:
        return JsonResponse({"code": "placeholders", "error": _("Replace the placeholders with real numbers before saving: %(items)s") % {"items": ", ".join(pending)}}, status=400)
    synced = svc.sync_profile(request.student, cv)
    record = _cv_record(request.student)
    record.data = cv
    record.save(update_fields=["data", "updated_at"])
    return JsonResponse({"cv": cv, "synced": synced, "pdf_url": reverse("cv_builder_pdf"), "ats": svc.ats_report(cv),
                         "message": _("CV saved. %(skills)s skills and %(projects)s projects were added to your profile.") % synced})


def saved_cv(student):
    record = StudentCV.objects.filter(student=student).first()
    return svc.clean_cv(record.data) if record and record.data else None


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def template_choice(request) -> str:
    value = request.GET.get("template", "")
    return value if value in TEMPLATES else DEFAULT_TEMPLATE


def export_response(cv, fmt: str, template: str = DEFAULT_TEMPLATE) -> HttpResponse:
    """The CV file download (PDF in either template, or DOCX). Shared by the builder pages and the JSON API."""
    if fmt == "docx":
        from .cv_docx import render_cv_docx

        content, content_type = render_cv_docx(cv), DOCX_TYPE
    else:
        content, content_type = render_cv_pdf(cv, template), "application/pdf"
    response = HttpResponse(content, content_type=content_type)
    suffix = "" if fmt == "docx" or template == DEFAULT_TEMPLATE else f"-{template}"
    response["Content-Disposition"] = f'attachment; filename="{slugify(cv["name"]) or "cv"}-cv{suffix}.{fmt}"'
    response["Cache-Control"] = "no-store"
    return response


def _download(request, fmt):
    cv = saved_cv(request.student)
    if cv is None:
        messages.info(request, _("Save your CV first, then download it."))
        return redirect("cv_builder")
    return export_response(cv, fmt, template_choice(request))


@student_gate()
@require_GET
def cv_pdf(request):
    return _download(request, "pdf")


@student_gate()
@require_GET
def cv_docx(request):
    return _download(request, "docx")


@student_gate()
@require_GET
def cv_preview(request):
    cv = saved_cv(request.student)
    if cv is None:
        return redirect("cv_builder")
    for e in cv["education"]:
        e["period"] = svc.format_period(e["start_date"], e["end_date"], e["is_current"])
    for project in cv["projects"]:
        project["link"], project["link_display"] = svc.clean_url(project["link"])
    return render(request, "students/cv_ats.html", {"cv": cv, "contacts": svc.contact_items(cv), "ats": svc.ats_report(cv),
                                                    "templates": TEMPLATES})
