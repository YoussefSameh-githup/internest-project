"""Thresholds used in UI copy, so texts always match the enforced rules."""


def skill_gate(request):
    from internest_skills.matching import MATCH_THRESHOLD

    return {"MATCH_THRESHOLD": MATCH_THRESHOLD}
