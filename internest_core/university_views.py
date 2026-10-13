"""Universities landing page (/universities/): interactive demo + free-pilot / demo booking. No online checkout."""
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse
from django.shortcuts import render
from django.utils.translation import get_language
from django.views.decorators.http import require_POST

from .models import UniversityInquiry

INQUIRIES_PER_HOUR = 10  # per IP; the form also opens WhatsApp / email, so this only limits database spam
LIMITS = {"name": 120, "role": 120, "uni": 160, "fac": 160, "email": 254, "phone": 30, "msg": 2000}


def university_landing(request):
    phone = settings.SUPPORT_PHONE
    digits = re.sub(r"\D", "", phone)
    whatsapp = "20" + digits[1:] if digits.startswith("0") else digits
    display = f"{digits[:4]} {digits[4:7]} {digits[7:]}" if len(digits) == 11 else phone
    return render(request, "landing/universities.html", {
        "page_lang": "en" if (get_language() or "ar").startswith("en") else "ar",
        "whatsapp": whatsapp, "phone_display": display,
    })


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else "") or request.META.get("REMOTE_ADDR", "")


@require_POST
def university_inquiry(request):
    """Log a demo / pilot request (the page also opens WhatsApp or email with the same details)."""
    if request.POST.get("website"):  # honeypot: bots fill every field
        return JsonResponse({"ok": True})
    key = f"uni-inquiry:{_client_ip(request)}"
    if cache.get(key, 0) >= INQUIRIES_PER_HOUR:
        return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)

    data = {k: (request.POST.get(k) or "").strip()[:n] for k, n in LIMITS.items()}
    if data["email"]:
        try:
            validate_email(data["email"])
        except ValidationError:
            data["email"] = ""
    if not (data["name"] and data["role"] and data["uni"] and (data["email"] or data["phone"])):
        return JsonResponse({"ok": False, "error": "missing_fields"}, status=400)

    UniversityInquiry.objects.create(
        name=data["name"], position=data["role"], university=data["uni"], faculty=data["fac"], email=data["email"],
        phone=data["phone"], message=data["msg"], channel="mail" if request.POST.get("channel") == "mail" else "wa",
    )
    cache.set(key, cache.get(key, 0) + 1, 3600)
    return JsonResponse({"ok": True})
