"""Founder KPIs and series from the real tables (aggregate queries only; cached briefly per period).

Nothing here estimates or fills gaps: a metric the app does not record (refunds, page views) is simply absent,
and the templates say so.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncWeek
from django.utils import timezone

from internest_core.models import (
    Application, Internship, PartnerInternshipSubmission, PartnerProfile, StudentEnrollment, StudentProfile, opportunity_today,
)
from internest_startups.models import ProUpgradeRequest

User = get_user_model()
PERIODS = {"7": 7, "30": 30, "90": 90}
CACHE_SECONDS = 60
PLACED = (Application.STATUS_ACCEPTED, Application.STATUS_COMPLETED)


def period_days(value) -> int:
    return PERIODS.get(str(value), 30)


def _window(days):
    now = timezone.now()
    return now - timedelta(days=days), now - timedelta(days=2 * days)


def _growth(current, previous):
    if previous == 0:
        return None  # no honest percentage from zero
    return round((current - previous) * 100 / previous)


def _pair(qs, field, start, prev_start):
    row = qs.aggregate(cur=Count("pk", filter=Q(**{f"{field}__gte": start})),
                       prev=Count("pk", filter=Q(**{f"{field}__gte": prev_start, f"{field}__lt": start})))
    return {"current": row["cur"], "previous": row["prev"], "growth": _growth(row["cur"], row["prev"])}


def revenue_by_currency(qs):
    return {r["currency"]: r["total"] for r in qs.values("currency").annotate(total=Sum("final_price")).order_by("currency")}


def overview(days: int) -> dict:
    key = f"founder:overview:{days}"
    cached = cache.get(key)
    if cached:
        return cached
    start, prev_start = _window(days)
    companies = PartnerProfile.objects.filter(is_academic=False)
    today = opportunity_today()  # same rule as Internship.objects.open() / expired()
    internships = Internship.objects.aggregate(
        open=Count("pk", filter=Q(is_active=True, deadline__gte=today)),
        expired=Count("pk", filter=Q(is_active=True, deadline__lt=today)),
        closed=Count("pk", filter=Q(is_active=False)))
    apps = Application.objects.aggregate(
        total=Count("pk"), pending=Count("pk", filter=Q(status__in=Application.ACTIVE_STATUSES)),
        placed=Count("pk", filter=Q(status__in=PLACED)))
    students = StudentProfile.objects.aggregate(total=Count("pk"), verified=Count("pk", filter=Q(personal_email_verified_at__isnull=False)),
                                                active=Count("pk", filter=Q(user__last_login__gte=start)))
    paid = ProUpgradeRequest.objects.filter(status=ProUpgradeRequest.STATUS_ACTIVE)
    data = {
        "days": days,
        "totals": {
            "users": User.objects.count(), "students": students["total"], "verified_students": students["verified"],
            "active_students": students["active"], "companies": companies.count(),
            "pending_companies": companies.filter(is_fully_verified=False).count(),
            "pending_submissions": PartnerInternshipSubmission.objects.filter(status="Pending").count(),
            "open_internships": internships["open"], "expired_internships": internships["expired"],
            "closed_internships": internships["closed"], "applications": apps["total"], "pending_applications": apps["pending"],
            "placements": apps["placed"],
            "orders_to_verify": ProUpgradeRequest.objects.filter(status=ProUpgradeRequest.STATUS_VERIFYING).count(),
        },
        "growth": {
            "users": _pair(User.objects.all(), "date_joined", start, prev_start),
            "companies": _pair(companies, "user__date_joined", start, prev_start),
            "internships": _pair(Internship.objects.all(), "created_at", start, prev_start),
            "applications": _pair(Application.objects.all(), "application_date", start, prev_start),
            "payments": _pair(paid, "activated_at", start, prev_start),
        },
        "revenue": {"period": revenue_by_currency(paid.filter(activated_at__gte=start)), "all_time": revenue_by_currency(paid)},
    }
    cache.set(key, data, CACHE_SECONDS)
    return data


def recent_activity(limit=12) -> list:
    """Newest real events across the main tables (one small query each), merged by time."""
    items = []
    for u in User.objects.order_by("-date_joined").values("pk", "username", "date_joined")[:limit]:
        items.append({"at": u["date_joined"], "kind": "user", "label": u["username"], "link": f"/founder/users/{u['pk']}/"})
    for i in Internship.objects.order_by("-created_at").values("pk", "title", "created_at")[:limit]:
        items.append({"at": i["created_at"], "kind": "internship", "label": i["title"], "link": f"/founder/internships/?q={i['pk']}"})
    for a in Application.objects.order_by("-application_date").values("pk", "internship__title", "application_date")[:limit]:
        items.append({"at": a["application_date"], "kind": "application", "label": a["internship__title"], "link": "/founder/applications/"})
    for p in ProUpgradeRequest.objects.order_by("-created_at").values("pk", "product", "status", "created_at")[:limit]:
        items.append({"at": p["created_at"], "kind": "payment", "label": f"#{p['pk']} {p['product']} ({p['status']})", "link": "/founder/finance/"})
    return sorted(items, key=lambda x: x["at"], reverse=True)[:limit]


def weekly(qs, field, weeks=12) -> list:
    since = timezone.now() - timedelta(weeks=weeks)
    rows = (qs.filter(**{f"{field}__gte": since}).annotate(week=TruncWeek(field)).values("week")
            .annotate(n=Count("pk")).order_by("week"))
    counts = {r["week"].date() if hasattr(r["week"], "date") else r["week"]: r["n"] for r in rows}
    start = timezone.localdate() - timedelta(days=timezone.localdate().weekday()) - timedelta(weeks=weeks - 1)
    series = [{"week": start + timedelta(weeks=i), "n": counts.get(start + timedelta(weeks=i), 0)} for i in range(weeks)]
    peak = max((s["n"] for s in series), default=0) or 1
    for s in series:
        s["pct"] = round(s["n"] * 100 / peak)
    return series


def analytics() -> dict:
    cached = cache.get("founder:analytics")
    if cached:
        return cached
    completion = StudentProfile.objects.aggregate(
        c0=Count("pk", filter=Q(profile_completion_score__lt=40)),
        c40=Count("pk", filter=Q(profile_completion_score__gte=40, profile_completion_score__lt=80)),
        c80=Count("pk", filter=Q(profile_completion_score__gte=80)))
    funnel = dict(Application.objects.values_list("status").annotate(n=Count("pk")).order_by())
    total_apps = sum(funnel.values())
    majors = list(StudentProfile.objects.exclude(major__isnull=True).exclude(major="").values("major")
                  .annotate(n=Count("pk")).order_by("-n")[:8])
    locations = list(Internship.objects.values("location").annotate(n=Count("pk")).order_by("-n")[:8])
    data = {
        "registrations": weekly(User.objects.all(), "date_joined"),
        "companies": weekly(PartnerProfile.objects.filter(is_academic=False), "user__date_joined"),
        "internships": weekly(Internship.objects.all(), "created_at"),
        "applications": weekly(Application.objects.all(), "application_date"),
        "completion": completion,
        "funnel": [{"status": label, "n": funnel.get(code, 0), "pct": round(funnel.get(code, 0) * 100 / total_apps) if total_apps else 0}
                   for code, label in Application.STATUS_CHOICES],
        "applications_total": total_apps,
        "apps_per_open": round(total_apps / max(1, Internship.objects.count()), 1),
        "majors": majors,
        "locations": locations,
    }
    cache.set("founder:analytics", data, CACHE_SECONDS * 5)
    return data


def finance(status=None, page_qs=None) -> dict:
    orders = ProUpgradeRequest.objects.all()
    by_status = dict(orders.values_list("status").annotate(n=Count("pk")).order_by())
    paid = orders.filter(status=ProUpgradeRequest.STATUS_ACTIVE)
    months = (paid.filter(activated_at__isnull=False).annotate(week=TruncWeek("activated_at")).values("week", "currency")
              .annotate(total=Sum("final_price")).order_by("-week")[:24])
    courses = StudentEnrollment.objects.aggregate(n=Count("pk"), total=Sum("final_price"))
    return {"by_status": [{"code": c, "label": l, "n": by_status.get(c, 0)} for c, l in ProUpgradeRequest.STATUS_CHOICES],
            "revenue": revenue_by_currency(paid), "weekly": list(months),
            "by_gateway": list(paid.values("gateway").annotate(n=Count("pk"), total=Sum("final_price")).order_by("-n")),
            "courses": {"n": courses["n"], "total": courses["total"] or Decimal("0")}}
