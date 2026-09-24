from django.contrib import admin

from .models import (
    ChallengeAttempt, ChallengeItem, ChallengeResponse, EmployerSubscription,
    ExternalCourse, Skill, SkillProfile, StudentSkill, SubSkill,
)


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
    list_display = ("student", "skill", "source", "status", "score", "percentile", "cooldown_until")
    list_filter = ("status", "source", "skill__discipline")
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
