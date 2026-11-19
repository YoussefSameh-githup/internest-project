"""Database health (Phase 8): integrity, size, biggest tables, and the query plan of every hot query.

`manage.py db_health` prints it on the server; tests_backups asserts that no hot query does a full table scan, so a
new filter without an index fails CI instead of slowing the site down once the tables grow.
"""
import re
from datetime import timedelta

from django.db import connection
from django.utils import timezone


def _hot_queries():
    """(name, queryset) for the lookups that run on every busy page or job. Ids are placeholders: plans don't need data."""
    from internest_skills.models import ChallengeAttempt, StudentSkill
    from internest_skills.review import pending_queue
    from internest_startups.models import HiringPass, ProUpgradeRequest, ScreeningSubmission
    from internest_startups.talent import pool_queryset

    from .models import (
        Application, AsyncTask, EmailVerification, Internship, PartnerInternshipSubmission, SecurityEvent,
    )

    now = timezone.now()
    from .models import PartnerProfile

    return [
        ("opportunity feed", Internship.objects.open().order_by("-deadline", "-pk")),
        ("verified company names (feed badge)", PartnerProfile.objects.filter(is_fully_verified=True)
         .values_list("company_name", flat=True)),
        ("company's opportunities", Internship.objects.filter(partner_id=1).order_by("-created_at")),
        ("applicant inbox", Application.objects.filter(internship__partner_id=1, status="Submitted")),
        ("applicants of one opportunity", Application.objects.filter(internship_id=1, status="Shortlisted")),
        ("my applications", Application.objects.filter(applicant_id=1).order_by("-application_date")),
        ("a student's verified skills", StudentSkill.objects.filter(student_id=1, status=StudentSkill.STATUS_VERIFIED)),
        ("talent pool", pool_queryset({})),
        ("assessment review queue", pending_queue()),
        ("a skill's attempts", ChallengeAttempt.objects.filter(student_skill_id=1).order_by("-started_at")),
        ("company's open orders", ProUpgradeRequest.objects.filter(partner_id=1, status__in=ProUpgradeRequest.OPEN_STATUSES)),
        ("company's active passes", HiringPass.objects.filter(partner_id=1, expires_at__gt=now)),
        ("screening submission of an application", ScreeningSubmission.objects.filter(application_id=1)),
        ("posts this month", PartnerInternshipSubmission.objects.filter(partner_id=1, submission_date__gte=now - timedelta(days=30))),
        ("latest verification code", EmailVerification.objects.filter(
            user_id=1, email_type="personal", verified_at__isnull=True).order_by("-created_at")),
        ("background job queue", AsyncTask.objects.filter(status=AsyncTask.STATUS_QUEUED).order_by("created_at")),
        ("audit purge", SecurityEvent.objects.filter(created_at__lt=now)),
    ]


def full_scans(plan: str):
    """Tables read row by row with no index ("SCAN t"; "SCAN t USING INDEX" is an index walk and is fine), plus
    AUTOMATIC indexes (SQLite builds a temporary index on every run because a real one is missing).
    Subquery and CTE result scans ("SCAN (subquery-1)", "SCAN SUBQUERY 1") are not tables and are ignored."""
    scans = []
    for line in plan.splitlines():  # rows look like "3 0 216 SCAN internest_core_internship" (ids, then the detail)
        match = re.search(r"\bSCAN (\S+)(.*)$", line)
        if match and "USING" not in match.group(2) and not match.group(1).startswith(("(", "SUBQUERY", "CONSTANT")):
            scans.append(match.group(1))
        elif "AUTOMATIC" in line and "INDEX" in line:
            scans.append(line.split("SEARCH ", 1)[-1].split()[0] + " (automatic index)")
    return scans


def query_plans():
    results = []
    for name, qs in _hot_queries():
        plan = qs.explain()
        results.append({"name": name, "plan": plan, "full_scans": full_scans(plan)})
    return results


def database_report():
    with connection.cursor() as cur:
        cur.execute("PRAGMA quick_check")
        check = cur.fetchone()[0]
        cur.execute("PRAGMA page_count")
        pages = cur.fetchone()[0]
        cur.execute("PRAGMA page_size")
        page_size = cur.fetchone()[0]
        cur.execute("PRAGMA freelist_count")
        free = cur.fetchone()[0]
        cur.execute("PRAGMA journal_mode")
        journal = cur.fetchone()[0]
        tables = connection.introspection.table_names(cur)
        counts = []
        for table in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')  # names come from the schema, not from input
            counts.append((table, cur.fetchone()[0]))
    counts.sort(key=lambda kv: -kv[1])
    return {"quick_check": check, "size_mb": round(pages * page_size / 1024 / 1024, 2),
            "free_pct": round(100 * free / pages, 1) if pages else 0.0, "journal_mode": journal,
            "biggest_tables": counts[:12], "tables": len(tables)}
