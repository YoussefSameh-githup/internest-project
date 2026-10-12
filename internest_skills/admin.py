from django.contrib import admin

from .models import (
    ChallengeAttempt, ChallengeItem, ChallengeResponse, EmployerSubscription,
    ExternalCourse, Skill, SkillProfile, StudentFreelanceGig, StudentSkill, SubSkill,
)
from .freelance_service import reject_gig, verify_gig


class SubSkillInline(admin.TabularInline):
    model = SubSkill
    extra = 1


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "discipline", "challenge_length", "pass_threshold", "is_active")
    list_filter = ("discipline", "is_active")
    search_fields = ("name", "aliases", "implicit_signals")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SubSkillInline]


@admin.register(SubSkill)
class SubSkillAdmin(admin.ModelAdmin):
    list_display = ("name", "skill")
    list_filter = ("skill__discipline", "skill")
    search_fields = ("name", "keywords")


@admin.register(ChallengeItem)
class ChallengeItemAdmin(admin.ModelAdmin):
    list_display = ("__str__", "kind", "time_limit_seconds", "weight", "is_active")
    list_filter = ("skill", "kind", "is_active")
    search_fields = ("prompt",)
    autocomplete_fields = ("sub_skill",)


@admin.register(ExternalCourse)
class ExternalCourseAdmin(admin.ModelAdmin):
    list_display = ("title", "provider", "sub_skill", "is_active")
    list_filter = ("provider", "is_active")
    search_fields = ("title",)
    autocomplete_fields = ("sub_skill",)


@admin.register(StudentSkill)
class StudentSkillAdmin(admin.ModelAdmin):
    list_display = ("student", "skill", "source", "status", "verified_via", "score", "percentile", "cooldown_until")
    list_filter = ("status", "verified_via", "source", "skill__discipline")
    search_fields = ("student__user__username", "skill__name")
    readonly_fields = ("created_at", "updated_at")


class ChallengeResponseInline(admin.TabularInline):
    model = ChallengeResponse
    extra = 0
    can_delete = False
    readonly_fields = ("item", "choice_index", "answer_text", "credit", "elapsed_ms", "timed_out", "integrity_flags", "typing_stats")


@admin.register(ChallengeAttempt)
class ChallengeAttemptAdmin(admin.ModelAdmin):
    list_display = ("student_skill", "state", "score_pct", "focus_warnings", "termination_reason", "started_at")
    list_filter = ("state", "termination_reason")
    search_fields = ("student_skill__student__user__username",)
    readonly_fields = ("token", "item_ids", "integrity_events", "sub_skill_breakdown", "started_at", "finished_at")
    inlines = [ChallengeResponseInline]


@admin.register(SkillProfile)
class SkillProfileAdmin(admin.ModelAdmin):
    list_display = ("student", "partner_university", "last_extracted_at")
    search_fields = ("student__user__username",)


@admin.register(EmployerSubscription)
class EmployerSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("partner", "plan", "valid_until")
    list_filter = ("plan",)
    search_fields = ("partner__company_name",)


from .models import SkillRelation  # noqa: E402


@admin.register(SkillRelation)
class SkillRelationAdmin(admin.ModelAdmin):
    list_display = ("skill", "related", "weight", "source")
    list_filter = ("source",)
    search_fields = ("skill__name", "related__name")
    autocomplete_fields = ("skill", "related")


@admin.register(StudentFreelanceGig)
class StudentFreelanceGigAdmin(admin.ModelAdmin):
    """Review queue: open the link and proof, then approve (skills become Verified) or reject."""

    list_display = ("project_title", "student", "platform", "platform_username", "owner_matched", "rating", "status", "created_at")
    list_filter = ("status", "owner_matched", "platform", "ai_parsed")
    search_fields = ("project_title", "link", "student__user__username")
    filter_horizontal = ("skills",)
    readonly_fields = ("student", "raw_text", "platform_username", "owner_matched", "ai_parsed", "reviewed_at", "created_at")
    actions = ["approve", "reject"]

    @admin.action(description="Approve: verify the selected gigs' skills")
    def approve(self, request, queryset):
        upgraded = sum(verify_gig(gig) for gig in queryset.exclude(status=StudentFreelanceGig.STATUS_VERIFIED))
        self.message_user(request, f"Approved. {upgraded} student skills are now verified.")

    @admin.action(description="Reject the selected gigs")
    def reject(self, request, queryset):
        for gig in queryset.filter(status__in=[StudentFreelanceGig.STATUS_PENDING, StudentFreelanceGig.STATUS_NEEDS_PROOF]):
            reject_gig(gig)  # the student sees a translated default reason
        self.message_user(request, "Rejected.")
