"""Admin home KPIs and sidebar badge counts (wired in settings.UNFOLD)."""
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def _counters():
    """Name → zero-argument query; the sidebar badges run only the one they need."""
    from internest_skills.models import StudentFreelanceGig, StudentSkill
    from internest_startups.models import ProUpgradeRequest

    from .models import Application, Internship, PartnerInternshipSubmission, PartnerProfile, StudentProfile, UniversityInquiry

    def open_gigs():
        return Internship.objects.filter(is_active=True, deadline__gte=timezone.now().date())

    gigs = StudentFreelanceGig.objects
    return {
        "students": StudentProfile.objects.count,
        "verified_students": StudentProfile.objects.filter(personal_email_verified_at__isnull=False).count,
        "verified_skills": StudentSkill.objects.filter(status=StudentSkill.STATUS_VERIFIED).count,
        "companies": PartnerProfile.objects.filter(is_academic=False).count,
        "open_gigs": lambda: open_gigs().count(),
        "active_applicants": lambda: Application.objects.filter(internship__in=open_gigs(), status__in=Application.ACTIVE_STATUSES).count(),
        "needs_proof": gigs.filter(status=StudentFreelanceGig.STATUS_NEEDS_PROOF).count,
        "pending_gigs": gigs.filter(status=StudentFreelanceGig.STATUS_PENDING).count,
        "pending_companies": PartnerProfile.objects.filter(is_academic=False, is_fully_verified=False).count,
        "pending_submissions": PartnerInternshipSubmission.objects.filter(status="Pending").count,
        "pending_payments": ProUpgradeRequest.objects.filter(status=ProUpgradeRequest.STATUS_PENDING).count,
        "university_inquiries": UniversityInquiry.objects.filter(status=UniversityInquiry.STATUS_NEW).count,
    }


def _counts():
    return {name: query() for name, query in _counters().items()}


def dashboard_callback(request, context):
    c = _counts()
    changelist = lambda model: reverse(f"admin:{model}_changelist")  # noqa: E731
    context.update({
        "kpis": [
            {"title": _("Students"), "value": c["students"], "note": _("%(n)s with a verified email") % {"n": c["verified_students"]},
             "icon": "school", "link": changelist("internest_core_studentprofile")},
            {"title": _("Verified skills"), "value": c["verified_skills"], "note": _("Challenge or reviewed freelance work"),
             "icon": "verified", "link": changelist("internest_skills_studentskill") + "?status__exact=verified"},
            {"title": _("Open opportunities"), "value": c["open_gigs"], "note": _("%(n)s active applicants") % {"n": c["active_applicants"]},
             "icon": "work", "link": changelist("internest_core_internship") + "?is_active__exact=1"},
            {"title": _("Companies"), "value": c["companies"], "note": _("%(n)s awaiting verification") % {"n": c["pending_companies"]},
             "icon": "apartment", "link": changelist("internest_core_partnerprofile")},
        ],
        "queues": [
            {"title": _("Freelance work needing proof"), "value": c["needs_proof"], "icon": "upload_file",
             "link": changelist("internest_skills_studentfreelancegig") + "?status__exact=needs_proof"},
            {"title": _("Freelance work to review"), "value": c["pending_gigs"], "icon": "fact_check",
             "link": changelist("internest_skills_studentfreelancegig") + "?status__exact=pending"},
            {"title": _("Startups awaiting verification"), "value": c["pending_companies"], "icon": "domain_verification",
             "link": reverse("admin:internest_startups_pendingcompanyprofile_changelist")},
            {"title": _("Opportunity submissions"), "value": c["pending_submissions"], "icon": "pending_actions",
             "link": changelist("internest_core_partnerinternshipsubmission") + "?status__exact=Pending"},
            {"title": _("New university demo requests"), "value": c["university_inquiries"], "icon": "school",
             "link": changelist("internest_core_universityinquiry") + "?status__exact=new"},
            {"title": _("Pro payments to confirm"), "value": c["pending_payments"], "icon": "payments",
             "link": changelist("internest_startups_proupgraderequest") + "?status__exact=pending"},
        ],
    })
    return context


def _badge(key):
    def callback(request):
        return _counters()[key]() or None
    return callback


needs_proof_badge = _badge("needs_proof")
pending_gigs_badge = _badge("pending_gigs")
pending_companies_badge = _badge("pending_companies")
pending_submissions_badge = _badge("pending_submissions")
pending_payments_badge = _badge("pending_payments")
university_inquiries_badge = _badge("university_inquiries")
