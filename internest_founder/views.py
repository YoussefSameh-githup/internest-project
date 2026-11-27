"""Founder Control Center views. Every view is wrapped by @founder_required (server-side, incl. JSON); every
state change is a POST (CSRF-protected), goes through the model's normal save (so IndexNow, events and existing
signals keep working) and is written to the audit trail."""
import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from internest_core import async_tasks
from internest_core.models import (
    Application, Internship, PartnerInternshipSubmission, PartnerProfile, SecurityEvent, StudentProfile, opportunity_today,
)
from internest_core.ratelimit import client_ip, hit
from internest_skills.models import StudentSkill
from internest_startups.models import ProUpgradeRequest

from . import events, health, metrics, perf
from .access import audit, founder_required
from .forms import FounderSettingsForm, LighthouseUploadForm
from .models import AutomationEvent, FounderNotification, FounderSettings, PerformanceMeasurement

User = get_user_model()
PER_PAGE = 25


def _page(request, qs):
    return Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))


def _q(request):
    return (request.GET.get("q") or "").strip()[:80]


def _ctx(request, section, **extra):
    unread = FounderNotification.objects.filter(read_at__isnull=True)
    return {"section": section, "unread_count": unread.count(),
            "critical_unread": unread.filter(severity=FounderNotification.CRITICAL).count(), **extra}


# ---------------------------------------------------------------- overview
@founder_required
@require_GET
def overview(request):
    days = metrics.period_days(request.GET.get("days"))
    data = metrics.overview(days)
    alerts = FounderNotification.objects.filter(read_at__isnull=True, severity__in=("critical", "warning"))[:5]
    return render(request, "founder/overview.html", _ctx(
        request, "overview", data=data, days=days, periods=list(metrics.PERIODS), alerts=alerts,
        activity=metrics.recent_activity(), pending_companies=PartnerProfile.objects.filter(is_academic=False, is_fully_verified=False)
        .select_related("user").order_by("-user__date_joined")[:5],
        pending_submissions=PartnerInternshipSubmission.objects.filter(status="Pending").select_related("partner")[:5]))


@founder_required
@require_GET
def health_api(request):
    snap = health.snapshot()
    return JsonResponse({"overall": snap["overall"], "at": snap["at"].isoformat(),
                         "checks": [{**c, "checked_at": c["checked_at"].isoformat()} for c in snap["checks"]]})


@founder_required
@require_GET
def search(request):
    q = _q(request)
    results = {}
    if len(q) >= 2:
        results = {
            "users": User.objects.filter(Q(username__icontains=q) | Q(email__icontains=q)).order_by("-date_joined")[:10],
            "companies": PartnerProfile.objects.filter(company_name__icontains=q).order_by("company_name")[:10],
            "internships": Internship.objects.filter(title__icontains=q).select_related("partner").order_by("-created_at")[:10],
        }
    return render(request, "founder/search.html", _ctx(request, "search", q=q, results=results))


# ---------------------------------------------------------------- users
@founder_required
@require_GET
def users(request):
    q, role, state = _q(request), request.GET.get("role", ""), request.GET.get("state", "")
    qs = User.objects.select_related("studentprofile", "partnerprofile").order_by("-date_joined")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    if role == "student":
        qs = qs.filter(studentprofile__isnull=False)
    elif role == "company":
        qs = qs.filter(partnerprofile__isnull=False)
    elif role == "staff":
        qs = qs.filter(is_staff=True)
    if state == "suspended":
        qs = qs.filter(is_active=False)
    elif state == "unverified":
        qs = qs.filter(studentprofile__personal_email_verified_at__isnull=True, studentprofile__isnull=False)
    return render(request, "founder/users.html", _ctx(request, "users", page=_page(request, qs), q=q, role=role, state=state))


@founder_required
@require_GET
def user_detail(request, pk):
    member = get_object_or_404(User.objects.select_related("studentprofile", "partnerprofile"), pk=pk)
    student = getattr(member, "studentprofile", None)
    skills = (StudentSkill.objects.filter(student=student).values("status").annotate(n=Count("pk")) if student else [])
    has_cv = bool(student and (student.cv_file or hasattr(student, "cv")))
    apps = Application.objects.filter(applicant=member).select_related("internship").order_by("-application_date")[:10]
    history = SecurityEvent.objects.filter(Q(actor=member) | Q(target=f"user:{member.pk}")).order_by("-created_at")[:10]
    return render(request, "founder/user_detail.html", _ctx(
        request, "users", member=member, student=student, skills=skills, has_cv=has_cv, apps=apps, history=history,
        can_suspend=not member.is_superuser and member.pk != request.user.pk))


@founder_required
@require_POST
def user_set_active(request, pk):
    member = get_object_or_404(User, pk=pk)
    active = request.POST.get("active") == "1"
    if member.is_superuser or member.pk == request.user.pk:
        audit(request, "user_set_active", f"user:{member.pk}", ok=False, active=active, reason="protected_account")
        messages.error(request, _("Superuser accounts and your own account cannot be suspended here."))
        return redirect("founder:user_detail", pk=pk)
    member.is_active = active
    member.save(update_fields=["is_active"])  # inactive users are signed out on their next request (ModelBackend)
    audit(request, "user_set_active", f"user:{member.pk}", active=active)
    messages.success(request, _("Account reactivated.") if active else _("Account suspended."))
    return redirect("founder:user_detail", pk=pk)


# ---------------------------------------------------------------- companies
@founder_required
@require_GET
def companies(request):
    q, status = _q(request), request.GET.get("status", "")
    today = opportunity_today()
    qs = (PartnerProfile.objects.filter(is_academic=False).select_related("user")
          .annotate(open_count=Count("internships", filter=Q(internships__is_active=True, internships__deadline__gte=today), distinct=True),
                    total_count=Count("internships", distinct=True))
          .order_by("-user__date_joined"))
    if q:
        qs = qs.filter(company_name__icontains=q)
    if status == "pending":
        qs = qs.filter(is_fully_verified=False)
    elif status == "verified":
        qs = qs.filter(is_fully_verified=True)
    submissions = PartnerInternshipSubmission.objects.values("status").annotate(n=Count("pk")).order_by()
    return render(request, "founder/companies.html", _ctx(request, "companies", page=_page(request, qs), q=q, status=status,
                                                          submissions={r["status"]: r["n"] for r in submissions}))


# ---------------------------------------------------------------- internships
def _problems(item, today):
    issues = []
    if len((item.description or "").strip()) < 120:
        issues.append(_("short description"))
    if item.is_active and today <= item.deadline <= today + timedelta(days=3):
        issues.append(_("deadline in 3 days"))
    if item.is_active and item.deadline >= today and item.app_count == 0 and item.created_at < timezone.now() - timedelta(days=7):
        issues.append(_("no applicants after a week"))
    return issues


@founder_required
@require_GET
def internships(request):
    q, state = _q(request), request.GET.get("state", "")
    today = opportunity_today()
    qs = Internship.objects.select_related("partner").annotate(app_count=Count("application")).order_by("-created_at")
    if q:
        qs = qs.filter(pk=int(q)) if q.isdigit() else qs.filter(Q(title__icontains=q) | Q(partner__company_name__icontains=q))
    if state == "open":
        qs = qs.filter(is_active=True, deadline__gte=today)
    elif state == "expired":
        qs = qs.filter(is_active=True, deadline__lt=today)
    elif state == "closed":
        qs = qs.filter(is_active=False)
    page = _page(request, qs)
    for item in page:
        item.issues = _problems(item, today)
    pending = PartnerInternshipSubmission.objects.filter(status="Pending").count()
    return render(request, "founder/internships.html", _ctx(request, "internships", page=page, q=q, state=state, pending=pending))


@founder_required
@require_POST
def internship_set_active(request, pk):
    item = get_object_or_404(Internship, pk=pk)
    active = request.POST.get("active") == "1"
    if item.is_active != active:
        item.is_active = active
        item.save(update_fields=["is_active"])  # signals: IndexNow recrawl + founder event, exactly like the admin
    audit(request, "internship_set_active", f"internship:{item.pk}", active=active)
    messages.success(request, _("Opportunity reopened.") if active else _("Opportunity closed."))
    return redirect(request.POST.get("next") if str(request.POST.get("next", "")).startswith("/founder/") else "founder:internships")


# ---------------------------------------------------------------- applications
@founder_required
@require_GET
def applications(request):
    status, q = request.GET.get("status", ""), _q(request)
    qs = Application.objects.select_related("internship", "internship__partner", "applicant").order_by("-application_date")
    if status in dict(Application.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(internship__title__icontains=q) | Q(applicant__username__icontains=q))
    counts = dict(Application.objects.values_list("status").annotate(n=Count("pk")).order_by())
    total = sum(counts.values())
    funnel = [{"code": c, "label": l, "n": counts.get(c, 0), "pct": round(counts.get(c, 0) * 100 / total) if total else 0}
              for c, l in Application.STATUS_CHOICES]
    return render(request, "founder/applications.html", _ctx(request, "applications", page=_page(request, qs), status=status,
                                                             q=q, funnel=funnel, total=total))


# ---------------------------------------------------------------- finance
@founder_required
@require_GET
def finance(request):
    status = request.GET.get("status", "")
    qs = ProUpgradeRequest.objects.select_related("partner").order_by("-created_at")
    if status in dict(ProUpgradeRequest.STATUS_CHOICES):
        qs = qs.filter(status=status)
    return render(request, "founder/finance.html", _ctx(request, "finance", page=_page(request, qs), status=status,
                                                        data=metrics.finance()))


# ---------------------------------------------------------------- analytics
@founder_required
@require_GET
def analytics(request):
    return render(request, "founder/analytics.html", _ctx(request, "analytics", data=metrics.analytics()))


# ---------------------------------------------------------------- system
@founder_required
@require_GET
def system(request):
    snap = health.snapshot(use_cache=request.GET.get("refresh") != "1")
    from internest_core.models import AsyncTask

    failing = (AsyncTask.objects.filter(status__in=(AsyncTask.STATUS_FAILED, AsyncTask.STATUS_DEAD),
                                        finished_at__gte=timezone.now() - timedelta(days=7))
               .values("kind", "status").annotate(n=Count("pk")).order_by("-n")[:10])
    return render(request, "founder/system.html", _ctx(request, "system", snap=snap, failing=failing,
                                                       backend=async_tasks.backend()))


# ---------------------------------------------------------------- performance
@founder_required
def performance(request):
    prefs = FounderSettings.load()
    form = LighthouseUploadForm()
    if request.method == "POST":
        if request.POST.get("op") == "probe":
            last = PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE).values_list("measured_at", flat=True).first()
            if last and timezone.now() - last < perf.PROBE_MIN_INTERVAL:
                messages.info(request, _("A probe ran less than 5 minutes ago; showing those results."))
            else:
                async_tasks.enqueue("founder_probe", None, idempotency_key="founder-probe", max_attempts=1)
                audit(request, "performance_probe", "probe")
                messages.success(request, _("Probe queued; results appear here within a minute."))
            return redirect("founder:performance")
        form = LighthouseUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                run = perf.import_lighthouse(form.cleaned_data["report"])
                audit(request, "lighthouse_import", f"lab:{run.pk}")
                messages.success(request, _("Lighthouse report imported."))
                return redirect("founder:performance")
            except ValueError as exc:
                form.add_error("report", str(exc))
    return render(request, "founder/performance.html", _ctx(
        request, "performance", probes=perf.latest_probes(), trend=perf.daily_ttfb(), labs=perf.lab_runs(),
        field=perf.field_summary(), recs=perf.recommendations(prefs), form=form, prefs=prefs,
        rum_rate=getattr(settings, "FOUNDER_RUM_SAMPLE_RATE", 0.1)))


@csrf_exempt  # anonymous beacons from public pages; no cookies are read, nothing but counters is written
@require_POST
def vitals_beacon(request):
    if not perf.vitals_script_enabled() or len(request.body) > 2048:
        return HttpResponse(status=204)
    if not hit(f"rum:{client_ip(request)}", 30, 60):
        return HttpResponse(status=204)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)
    perf.record_vitals(payload)
    return HttpResponse(status=204)


# ---------------------------------------------------------------- automations
@founder_required
def automations(request):
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "test":
            if events.is_configured():
                events.emit("system.test", {"requested_by": request.user.pk, "note": "connection test"})
                audit(request, "webhook_test", events.target_host())
                messages.success(request, _("Test event queued for delivery."))
            else:
                messages.error(request, _("n8n is not configured (N8N_WEBHOOK_URL and N8N_WEBHOOK_SECRET in .env)."))
        elif op == "retry":
            event = get_object_or_404(AutomationEvent, pk=request.POST.get("event"))
            ok = events.retry(event)
            audit(request, "webhook_retry", f"event:{event.pk}", ok=ok)
            messages.success(request, _("Event queued again.")) if ok else messages.error(request, _("Only failed events can be retried."))
        return redirect("founder:automations")
    status = request.GET.get("status", "")
    qs = AutomationEvent.objects.all()
    if status in dict(AutomationEvent.STATUSES):
        qs = qs.filter(status=status)
    counts = dict(AutomationEvent.objects.values_list("status").annotate(n=Count("pk")).order_by())
    last_ok = AutomationEvent.objects.filter(status=AutomationEvent.DELIVERED).order_by("-delivered_at").first()
    last_fail = AutomationEvent.objects.filter(status=AutomationEvent.FAILED).first()
    return render(request, "founder/automations.html", _ctx(
        request, "automations", page=_page(request, qs), status=status, configured=events.is_configured(),
        tabs=[(code, label, counts.get(code, 0)) for code, label in AutomationEvent.STATUSES],
        host=events.target_host(), last_ok=last_ok, last_fail=last_fail, secret_ok=len(events.webhook_secret()) >= 32,
        url_set=bool(getattr(settings, "N8N_WEBHOOK_URL", ""))))


# ---------------------------------------------------------------- notifications
@founder_required
def notifications(request):
    if request.method == "POST":
        if request.POST.get("op") == "read_all":
            FounderNotification.objects.filter(read_at__isnull=True).update(read_at=timezone.now())
        else:
            FounderNotification.objects.filter(pk=request.POST.get("id"), read_at__isnull=True).update(read_at=timezone.now())
        return redirect(request.get_full_path())
    severity, unread = request.GET.get("severity", ""), request.GET.get("unread") == "1"
    qs = FounderNotification.objects.all()
    if severity in dict(FounderNotification.SEVERITIES):
        qs = qs.filter(severity=severity)
    if unread:
        qs = qs.filter(read_at__isnull=True)
    return render(request, "founder/notifications.html", _ctx(request, "notifications", page=_page(request, qs),
                                                              severity=severity, unread=unread))


# ---------------------------------------------------------------- security
@founder_required
@require_GET
def security(request):
    action = request.GET.get("action", "")
    qs = SecurityEvent.objects.select_related("actor").order_by("-created_at")
    if action in dict(SecurityEvent.ACTION_CHOICES):
        qs = qs.filter(action=action)
    privileged = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).order_by("-is_superuser", "username")
    posture = [
        (_("Debug mode off"), not settings.DEBUG),
        (_("HTTPS redirect"), bool(getattr(settings, "SECURE_SSL_REDIRECT", False))),
        (_("HSTS"), getattr(settings, "SECURE_HSTS_SECONDS", 0) >= 31536000),
        (_("Secure session cookie"), bool(getattr(settings, "SESSION_COOKIE_SECURE", False))),
        (_("Secure CSRF cookie"), bool(getattr(settings, "CSRF_COOKIE_SECURE", False))),
        (_("Two-factor sign-in for staff"), "allauth.mfa" in settings.INSTALLED_APPS),
    ]
    return render(request, "founder/security.html", _ctx(request, "security", page=_page(request, qs), action=action,
                                                         actions=SecurityEvent.ACTION_CHOICES, privileged=privileged, posture=posture))


# ---------------------------------------------------------------- settings
@founder_required
def founder_settings(request):
    prefs = FounderSettings.load()
    form = FounderSettingsForm(request.POST or None, instance=prefs)
    if request.method == "POST" and form.is_valid():
        changed = sorted(form.changed_data)
        form.save()
        audit(request, "settings_changed", "founder_settings", fields=changed)
        messages.success(request, _("Settings saved."))
        return redirect("founder:settings")
    integrations = [
        (_("n8n webhooks"), events.is_configured()),
        (_("IndexNow"), bool(getattr(settings, "INDEXNOW_API_KEY", ""))),
        (_("Alert email recipient"), bool(prefs.recipient())),
        (_("Real-visitor Web Vitals"), perf.vitals_script_enabled()),
    ]
    return render(request, "founder/settings.html", _ctx(request, "settings", form=form, integrations=integrations))
