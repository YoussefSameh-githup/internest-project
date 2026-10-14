from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from internest_core.admin_base import BAD, INFO, OK, WAIT, ModelAdmin, TabularInline, flag_badge, status_badge

from .models import (
    ChallengeAttempt, ChallengeItem, ChallengeResponse, Course, EmployerSubscription,
    ExternalCourse, Skill, SkillCertificate, SkillProfile, StudentFreelanceGig, StudentSkill, SubSkill,
)
from .certificates import reject_certificate, verify_certificate
from .freelance_service import reject_gig, verify_gig


class SubSkillInline(TabularInline):
    model = SubSkill
    extra = 1


@admin.register(Skill)
class SkillAdmin(ModelAdmin):
    list_display = ("name", "discipline", "challenge_length", "pass_threshold", "is_active")
    list_filter = ("discipline", "is_active")
    search_fields = ("name", "aliases", "implicit_signals")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SubSkillInline]


@admin.register(SubSkill)
class SubSkillAdmin(ModelAdmin):
    list_display = ("name", "skill")
    list_filter = ("skill__discipline", "skill")
    search_fields = ("name", "keywords")


@admin.register(ChallengeItem)
class ChallengeItemAdmin(ModelAdmin):
    list_display = ("__str__", "kind", "time_limit_seconds", "weight", "is_active")
    list_filter = ("skill", "kind", "is_active")
    search_fields = ("prompt",)
    autocomplete_fields = ("sub_skill",)


@admin.register(ExternalCourse)
class ExternalCourseAdmin(ModelAdmin):
    list_display = ("title", "provider", "sub_skill", "is_active")
    list_filter = ("provider", "is_active")
    search_fields = ("title",)
    autocomplete_fields = ("sub_skill",)


@admin.register(StudentSkill)
class StudentSkillAdmin(ModelAdmin):
    list_display = ("student", "skill", "source_col", "status_col", "verified_via", "score", "percentile", "years_experience", "updated_at")
    list_filter = ("status", "verified_via", "source", "skill__discipline", "updated_at")
    search_fields = ("student__user__username", "student__user__email", "skill__name")
    list_select_related = ("student__user", "skill")
    readonly_fields = ("created_at", "updated_at", "lag_sub_skills", "verified_at")
    status_col = status_badge("status", {StudentSkill.STATUS_VERIFIED: OK, StudentSkill.STATUS_CLAIMED: INFO, StudentSkill.STATUS_LAG: WAIT})
    source_col = status_badge("source", {StudentSkill.SOURCE_EXPLICIT: "primary", StudentSkill.SOURCE_IMPLICIT: INFO}, _("Source"))


class ChallengeResponseInline(TabularInline):
    model = ChallengeResponse
    extra = 0
    can_delete = False
    readonly_fields = ("item", "choice_index", "answer_text", "credit", "elapsed_ms", "timed_out", "integrity_flags", "typing_stats")


@admin.register(ChallengeAttempt)
class ChallengeAttemptAdmin(ModelAdmin):
    list_display = ("student_skill", "state", "score_pct", "focus_warnings", "termination_reason", "started_at")
    list_filter = ("state", "termination_reason", "started_at")
    date_hierarchy = "started_at"
    search_fields = ("student_skill__student__user__username",)
    readonly_fields = ("token", "item_ids", "integrity_events", "sub_skill_breakdown", "started_at", "finished_at")
    inlines = [ChallengeResponseInline]


@admin.register(SkillProfile)
class SkillProfileAdmin(ModelAdmin):
    list_display = ("student", "partner_university", "last_extracted_at", "extraction_count_today")
    list_filter = ("partner_university", "last_extracted_at")
    search_fields = ("student__user__username", "linkedin_url")
    readonly_fields = ("last_extracted_at", "extraction_count_today", "extraction_day", "cv_summary", "top_domains")


@admin.register(EmployerSubscription)
class EmployerSubscriptionAdmin(ModelAdmin):
    list_display = ("partner", "plan_col", "valid_until")
    list_filter = ("plan", "valid_until")
    plan_col = status_badge("plan", {"pro": OK, "free": None}, _("Plan"))
    search_fields = ("partner__company_name",)


from .models import SkillRelation  # noqa: E402


@admin.register(SkillRelation)
class SkillRelationAdmin(ModelAdmin):
    list_display = ("skill", "related", "weight", "source")
    list_filter = ("source",)
    search_fields = ("skill__name", "related__name")
    autocomplete_fields = ("skill", "related")


@admin.register(StudentFreelanceGig)
class StudentFreelanceGigAdmin(ModelAdmin):
    """Review queue: open the link and proof, then approve (skills become Verified) or reject."""

    list_display = ("project_title", "student", "platform", "handle", "owner", "rating", "status_col", "proof_col", "created_at")
    list_filter = ("status", "owner_matched", "platform", "ai_parsed", "created_at")
    search_fields = ("project_title", "link", "platform_username", "student__user__username", "student__user__email")
    filter_horizontal = ("skills",)
    readonly_fields = ("student", "raw_text", "platform_username", "owner_matched", "ai_parsed", "deliverables", "reviewed_at", "created_at")
    date_hierarchy = "created_at"
    actions = ["approve", "reject"]
    status_col = status_badge("status", {StudentFreelanceGig.STATUS_VERIFIED: OK, StudentFreelanceGig.STATUS_PENDING: INFO,
                                         StudentFreelanceGig.STATUS_NEEDS_PROOF: WAIT, StudentFreelanceGig.STATUS_REJECTED: BAD})

    @display(description=_("Handle"))
    def handle(self, obj):
        return f"@{obj.platform_username}" if obj.platform_username else "—"

    @flag_badge(_("Owner match"), _("Matched"), _("Check proof"))
    def owner(self, obj):
        return obj.owner_matched

    @flag_badge(_("Proof"), _("Uploaded"), _("None"), false_color=None)
    def proof_col(self, obj):
        return bool(obj.proof)

    @admin.action(description="Approve: verify the selected gigs' skills")
    def approve(self, request, queryset):
        upgraded = sum(verify_gig(gig) for gig in queryset.exclude(status=StudentFreelanceGig.STATUS_VERIFIED))
        self.message_user(request, f"Approved. {upgraded} student skills are now verified.")

    @admin.action(description="Reject the selected gigs")
    def reject(self, request, queryset):
        for gig in queryset.filter(status__in=[StudentFreelanceGig.STATUS_PENDING, StudentFreelanceGig.STATUS_NEEDS_PROOF]):
            reject_gig(gig)  # the student sees a translated default reason
        self.message_user(request, "Rejected.")


@admin.register(SkillCertificate)
class SkillCertificateAdmin(ModelAdmin):
    """Review queue: open the link or file, then approve (the skill becomes Verified) or reject."""

    list_display = ("title", "student", "skill", "issuer", "trusted", "evidence", "status_col", "created_at")
    list_filter = ("status", "trusted_issuer", "created_at")
    search_fields = ("title", "issuer", "url", "skill__name", "student__user__username", "student__user__email")
    readonly_fields = ("student", "trusted_issuer", "reviewed_at", "created_at")
    date_hierarchy = "created_at"
    actions = ["approve", "reject"]
    status_col = status_badge("status", {SkillCertificate.STATUS_VERIFIED: OK, SkillCertificate.STATUS_UNDER_REVIEW: WAIT,
                                         SkillCertificate.STATUS_REJECTED: BAD})

    @flag_badge(_("Known issuer"), _("Known site"), _("Check"), false_color=None)
    def trusted(self, obj):
        return obj.trusted_issuer

    @display(description=_("Evidence"))
    def evidence(self, obj):
        return " + ".join(str(x) for x, ok in ((_("Link"), obj.url), (_("File"), obj.file)) if ok) or "—"

    @admin.action(description=_("Approve: verify the selected certificates' skills"))
    def approve(self, request, queryset):
        upgraded = sum(verify_certificate(c) for c in queryset.exclude(status=SkillCertificate.STATUS_VERIFIED))
        self.message_user(request, _("Approved. %(n)s student skills are now verified.") % {"n": upgraded})

    @admin.action(description=_("Reject the selected certificates"))
    def reject(self, request, queryset):
        for cert in queryset.filter(status=SkillCertificate.STATUS_UNDER_REVIEW):
            reject_certificate(cert)
        self.message_user(request, _("Rejected."))


@admin.register(Course)
class CourseAdmin(ModelAdmin):
    """Skill-gap course catalogue: partner courses are recommended first (higher priority first)."""

    list_display = ("title", "skill", "provider_name", "partner", "priority", "discount_note", "is_active")
    list_editable = ("priority", "is_active")
    list_filter = ("is_partner", "is_active", "skill__discipline", "skill")
    search_fields = ("title", "provider_name", "skill__name", "url")
    autocomplete_fields = ("skill",)
    list_select_related = ("skill",)
    actions = ["mark_partner", "mark_open"]
    fieldsets = (
        (None, {"fields": ("title", "skill", "provider_name", "url")}),
        (_("Recommendation"), {"fields": ("is_partner", "priority", "discount_note", "is_active")}),
    )

    @flag_badge(_("Certified partner"), _("Partner"), _("Open course"), false_color=INFO)
    def partner(self, obj):
        return obj.is_partner

    @admin.action(description=_("Mark as certified partner courses"))
    def mark_partner(self, request, queryset):
        queryset.update(is_partner=True)

    @admin.action(description=_("Mark as open (non-partner) courses"))
    def mark_open(self, request, queryset):
        queryset.update(is_partner=False, discount_note="")
