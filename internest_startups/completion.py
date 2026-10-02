"""Startup profile completion (Condition B of the publish gate). Needs no admin action."""

PLACEHOLDER_PREFIX = "__pending__"
SOCIAL_FIELDS = ("linkedin_url", "facebook_url", "twitter_url", "instagram_url")


def completion_checks(partner) -> dict:
    profile = getattr(partner, "company_profile", None)
    return {
        "company_name": bool(partner.company_name) and not partner.company_name.startswith(PLACEHOLDER_PREFIX),
        "company_email": bool(partner.official_email),
        "founder_email": bool(profile and profile.founder_email),
        "industry": bool(profile and profile.industry),
        "founded_year": bool(profile and profile.founded_year),
        "description": bool(profile and profile.description and profile.description.strip()),
        "website": bool(partner.official_website),
        "social": any(getattr(partner, f) for f in SOCIAL_FIELDS),
    }


def startup_completion(partner) -> int:
    checks = completion_checks(partner)
    return sum(checks.values()) * 100 // len(checks)
