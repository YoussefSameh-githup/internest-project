import json
import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from internest_core.views import get_user_context
from internest_skills.llm import LLMUnavailable, llm_enabled
from internest_skills.models import Skill
from internest_skills.views import student_gate

from . import cv_builder_service as svc
from .cv_pdf import render_cv_pdf
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


def _limit_reached():
    return JsonResponse({"error": _("You've reached today's AI limit for the CV builder. Your data is saved in the form — try again tomorrow.")}, status=429)


@student_gate()
def cv_builder(request):
    student = request.student
    record = StudentCV.objects.filter(student=student).first()
    context = get_user_context(request)
    context.update({
        "cv": record.data if record and record.data else svc.initial_cv(student),
        "has_saved_cv": bool(record and record.data),
        "taxonomy": list(Skill.objects.filter(is_active=True).order_by("name").values_list("name", flat=True)),
        "ai_enabled": llm_enabled(),
        "voice_enabled": svc.voice_enabled(),
        "max_seconds": 180,
    })
    return render(request, "students/student_cv_builder.html", context)


@student_gate(api=True)
@require_POST
def cv_polish(request):
    cv = _payload(request)
    if not llm_enabled():
        return JsonResponse({"cv": cv, "error": _("AI polishing isn't available right now. You can still finish and download your CV.")}, status=503)
    if not _use_ai(request.student):
        return _limit_reached()
    try:
        return JsonResponse({"cv": svc.polish_cv(cv)})
    except LLMUnavailable:
        return JsonResponse({"cv": cv, "error": _("Our AI is busy right now, so your text was kept as is. Try polishing again in a few minutes.")}, status=503)


@student_gate(api=True)
@require_POST
def process_voice_cv(request):
    cv, audio = _payload(request), request.FILES.get("audio")
    content_type = (getattr(audio, "content_type", "") or "").split(";")[0].strip().lower()
    if audio is None or content_type not in AUDIO_TYPES:
        return JsonResponse({"error": _("Please record a voice note first (WebM, OGG, MP3, M4A or WAV).")}, status=400)
    if audio.size > svc.AUDIO_MAX_BYTES:
        return JsonResponse({"error": _("The recording is too long. Keep it under 3 minutes.")}, status=400)
    if not svc.voice_enabled():
        return JsonResponse({"error": _("Voice input isn't available right now. Please fill in the form instead.")}, status=503)
    if not _use_ai(request.student):
        return _limit_reached()
    try:
        transcript = svc.transcribe(audio.read(), f"voice.{AUDIO_TYPES[content_type]}", content_type)
    except svc.VoiceUnavailable:
        return JsonResponse({"error": _("We couldn't process your voice note right now. Please try again in a few minutes or fill in the form.")}, status=503)
    if len(transcript) < 15:
        return JsonResponse({"error": _("We couldn't hear enough in this recording. Speak a little closer to the microphone and try again.")}, status=400)
    try:
        return JsonResponse({"cv": svc.polish_cv(cv, transcript), "transcript": transcript})
    except LLMUnavailable:
        return JsonResponse({"cv": cv, "transcript": transcript,
                             "error": _("We transcribed your voice note, but the AI couldn't organize it right now. Copy the details from the transcript below into the form.")}, status=503)


@student_gate(api=True)
@require_POST
def cv_save(request):
    cv = _payload(request)
    if not cv["name"]:
        return JsonResponse({"error": _("Add your full name in Personal info."), "step": 0}, status=400)
    pending = svc.placeholders(cv)
    if pending:
        return JsonResponse({"error": _("Replace the placeholders with real numbers before saving: %(items)s") % {"items": ", ".join(pending)}}, status=400)
    synced = svc.sync_profile(request.student, cv)
    record = _cv_record(request.student)
    record.data = cv
    record.save(update_fields=["data", "updated_at"])
    return JsonResponse({"cv": cv, "synced": synced, "pdf_url": reverse("cv_builder_pdf"),
                         "message": _("CV saved. %(skills)s skills and %(projects)s projects were added to your profile.") % synced})


def _saved_cv(request):
    record = StudentCV.objects.filter(student=request.student).first()
    return svc.clean_cv(record.data) if record and record.data else None


@student_gate()
@require_GET
def cv_pdf(request):
    cv = _saved_cv(request)
    if cv is None:
        messages.info(request, _("Save your CV first, then download it."))
        return redirect("cv_builder")
    response = HttpResponse(render_cv_pdf(cv), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{slugify(cv["name"]) or "cv"}-cv.pdf"'
    return response


@student_gate()
@require_GET
def cv_preview(request):
    cv = _saved_cv(request)
    if cv is None:
        return redirect("cv_builder")
    for project in cv["projects"]:
        project["link"], project["link_display"] = svc.clean_url(project["link"])
    return render(request, "students/cv_ats.html", {"cv": cv, "contacts": svc.contact_items(cv)})
