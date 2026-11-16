"""Thresholds used in UI copy, so texts always match the enforced rules."""


def skill_gate(request):
    from internest_skills.matching import MATCH_THRESHOLD

    return {"MATCH_THRESHOLD": MATCH_THRESHOLD}


def site_contact(request):
    from django.conf import settings

    phone = settings.SUPPORT_PHONE
    return {"SUPPORT_PHONE": phone, "SUPPORT_PHONE_INTL": "+2" + phone if phone.startswith("0") else phone,
            "SUPPORT_EMAIL": settings.SUPPORT_EMAIL}


SOCIAL_NAMES = {"google": "Google", "linkedin_oauth2": "LinkedIn", "github": "GitHub", "microsoft": "Microsoft"}


def configured_social_providers():
    """Providers that have credentials in .env, in display order. A missing key means the button is simply not shown."""
    from django.conf import settings

    configured = [p for p in settings.SOCIAL_LOGIN_PROVIDER_ORDER if settings.SOCIALACCOUNT_PROVIDERS.get(p, {}).get("APP")]
    return [{"id": p, "name": SOCIAL_NAMES[p], "primary": i == 0} for i, p in enumerate(configured)]


def social_providers(request):
    return {"SOCIAL_PROVIDERS": configured_social_providers()}
