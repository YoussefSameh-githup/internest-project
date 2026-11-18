"""In-app notices for students: a one-time card on every page until the student acts on it.

Each notice is a key here plus a template `components/notices/<key>.html`. Pending keys are stored on
`StudentProfile.pending_notices`, which every student page already loads, so showing them costs no query.

    notices.add(StudentProfile.objects.filter(...), notices.TALENT_POOL_SUMMARY)   # e.g. from a data migration
    notices.dismiss(profile, key)                                                    # the student acted on it
"""
TALENT_POOL_SUMMARY = "talent_pool_summary"  # 2026-11-18: verified free companies see an anonymous summary

KNOWN = {TALENT_POOL_SUMMARY}


def pending(profile):
    """Known keys still to show, oldest first (unknown keys from a removed notice are ignored)."""
    return [key for key in (getattr(profile, "pending_notices", None) or []) if key in KNOWN]


def add(profiles, key):
    """Queue `key` for every profile in the queryset that does not have it yet. Returns how many were updated."""
    assert key in KNOWN, key
    changed = []
    for profile in profiles.only("pk", "pending_notices"):
        if key not in (profile.pending_notices or []):
            profile.pending_notices = [*(profile.pending_notices or []), key]
            changed.append(profile)
    profiles.model.objects.bulk_update(changed, ["pending_notices"], batch_size=500)
    return len(changed)


def dismiss(profile, key):
    if key in (profile.pending_notices or []):
        profile.pending_notices = [k for k in profile.pending_notices if k != key]
        profile.save(update_fields=["pending_notices"])
