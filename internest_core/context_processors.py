"""Thresholds used in UI copy, so texts always match the enforced rules."""


def skill_gate(request):
    from internest_skills.matching import MATCH_THRESHOLD

    return {"MATCH_THRESHOLD": MATCH_THRESHOLD}


def site_contact(request):
    from django.conf import settings

    phone = settings.SUPPORT_PHONE
    return {"SUPPORT_PHONE": phone, "SUPPORT_PHONE_INTL": "+2" + phone if phone.startswith("0") else phone,
            "SUPPORT_EMAIL": settings.SUPPORT_EMAIL}
