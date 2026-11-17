"""Who is this user? One lookup per request for the student / partner profile, including the "not one" answer.

Django caches a found reverse one-to-one (`user.studentprofile`) but NOT a missing one, so every gate, context helper
and view that asks "is this a partner?" for a student re-ran the same query (2-3 times per request). These helpers
memoise both outcomes on the user object, which lives exactly one request. Saving or deleting a profile updates the
memo through signals (see internest_core/signals.py), so a profile created mid-request is seen immediately.
"""
from django.core.exceptions import ObjectDoesNotExist

STUDENT_ATTR = "_internest_student_profile"
PARTNER_ATTR = "_internest_partner_profile"
_MISSING = object()


def _memo(user, attr, accessor):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    value = getattr(user, attr, _MISSING)
    if value is _MISSING:
        try:
            value = getattr(user, accessor)
        except ObjectDoesNotExist:
            value = None
        setattr(user, attr, value)
    return value


def student_profile(user):
    """The user's StudentProfile, or None."""
    return _memo(user, STUDENT_ATTR, "studentprofile")


def partner_profile(user):
    """The user's PartnerProfile (startup, company or university), or None."""
    return _memo(user, PARTNER_ATTR, "partnerprofile")


def remember(profile, attr, deleted=False):
    """Keep the memo of an already-loaded user in step with a saved / deleted profile."""
    user = profile._state.fields_cache.get("user")
    if user is not None:
        setattr(user, attr, None if deleted else profile)
