from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from django.contrib.sites.admin import SiteAdmin as BaseSiteAdmin
from django.contrib.sites.models import Site
from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from internest_skills.models import Skill

from .admin_base import BAD, INFO, OK, WAIT, ModelAdmin, StackedInline, TabularInline, flag_badge, status_badge
from .models import (
    Application, DiscountCode, EmailVerification, GamificationTask, GigExperience, Internship,
    PartnerApplicantData, PartnerCourseSubmission, PartnerInternshipSubmission, PartnerProfile, StudentEnrollment,
    StudentProfile, StudentTaskRecord, TaskAnswer, TaskQuestion, UniversityInquiry,
)

SUBMISSION_COLORS = {"Pending": WAIT, "Approved": OK, "Rejected": BAD}
APPLICATION_COLORS = {
    Application.STATUS_SUBMITTED: INFO, Application.STATUS_UNDER_REVIEW: WAIT, Application.STATUS_SHORTLISTED: "primary",
    Application.STATUS_ACCEPTED: OK, Application.STATUS_FULFILLED: BAD, Application.STATUS_COMPLETED: OK,
}


class OpportunityStatusFilter(admin.SimpleListFilter):
    title = _("Status")
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [("open", _("Open for applications")), ("expired", _("Expired")), ("closed", _("Closed"))]

    def queryset(self, request, queryset):
        return getattr(queryset, self.value())() if self.value() in ("open", "expired", "closed") else queryset


# ---------------------------------------------------------------- accounts & audit
admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    list_display = ("username", "email", "full_name", "role", "active", "date_joined", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser", "date_joined", "last_login")
    list_select_related = ("studentprofile", "partnerprofile")
    date_hierarchy = "date_joined"
    readonly_fields = ("last_login", "date_joined")

    @display(description=_("Name"))
    def full_name(self, obj):
        return obj.get_full_name() or "—"

    @display(description=_("Role"), label={"student": INFO, "company": "primary", "staff": WAIT, "other": None})
    def role(self, obj):
        if obj.is_staff:
            return "staff", _("Staff")
        if hasattr(obj, "partnerprofile"):
            return "company", _("Company")
        if hasattr(obj, "studentprofile"):
            return "student", _("Student")
        return "other", "—"

    @flag_badge(_("Active"), _("Active"), _("Disabled"), false_color=BAD)
    def active(self, obj):
        return obj.is_active


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass


admin.site.unregister(Site)


@admin.register(Site)
class SiteAdmin(BaseSiteAdmin, ModelAdmin):
    pass


@admin.register(LogEntry)
class AuditLogAdmin(ModelAdmin):
    """Read-only audit trail of every change made in this admin."""

    list_display = ("action_time", "user", "content_type", "object_repr", "action", "change_message")
    list_filter = ("action_flag", "content_type", "action_time")
    search_fields = ("object_repr", "change_message", "user__username")
    date_hierarchy = "action_time"

    @display(description=_("Action"), label={1: OK, 2: INFO, 3: BAD})
    def action(self, obj):
        return obj.action_flag, obj.get_action_flag_display()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------- students
@admin.register(StudentProfile)
class StudentProfileAdmin(ModelAdmin):
    list_display = ("user", "university", "major", "study_level", "personal_verified", "university_verified",
                    "profile_completion_score", "talent_pool_visible", "email_new_opportunities")
    list_filter = ("study_level", "talent_pool_visible", "email_new_opportunities",
                   ("personal_email_verified_at", admin.EmptyFieldListFilter), "university")
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email", "personal_email",
                     "university_email", "university", "major")
    list_select_related = ("user",)
    readonly_fields = ("profile_completion_score", "personal_email_verified_at", "university_email_verified_at")
    actions = ["verify_emails"]

    @flag_badge(_("Personal email"), _("Verified"), _("Pending"))
    def personal_verified(self, obj):
        return obj.is_personal_email_verified

    @flag_badge(_("University email"), _("Verified"), _("Not verified"), false_color=None)
    def university_verified(self, obj):
        return obj.is_university_email_verified

    @admin.action(description=_("Verify selected students' emails"))
    def verify_emails(self, request, queryset):
        now = timezone.now()
        personal = queryset.filter(personal_email__isnull=False, personal_email_verified_at__isnull=True).exclude(personal_email="").update(personal_email_verified_at=now)
        university = queryset.filter(university_email__isnull=False, university_email_verified_at__isnull=True).exclude(university_email="").update(university_email_verified_at=now)
        self.message_user(request, _("Verified %(p)s personal and %(u)s university emails.") % {"p": personal, "u": university}, messages.SUCCESS)


@admin.register(EmailVerification)
class EmailVerificationAdmin(ModelAdmin):
    list_display = ("user", "email", "email_type", "verified", "created_at", "expires_at", "attempts")
    list_filter = ("email_type", ("verified_at", admin.EmptyFieldListFilter), "created_at")
    search_fields = ("user__username", "email")
    readonly_fields = ("code", "created_at", "expires_at", "verified_at", "attempts")

    @flag_badge(_("Verified"), _("Verified"), _("Pending"))
    def verified(self, obj):
        return obj.verified_at is not None

    def has_add_permission(self, request):
        return False


class TaskAnswerInline(TabularInline):
    model = TaskAnswer
    extra = 1
    fields = ("text", "is_correct")


@admin.register(TaskQuestion)
class TaskQuestionAdmin(ModelAdmin):
    inlines = [TaskAnswerInline]
    list_display = ("text", "task")
    list_filter = ("task",)
    search_fields = ("text", "task__title")


class TaskQuestionInline(StackedInline):
    model = TaskQuestion
    extra = 0
    show_change_link = True


@admin.register(GamificationTask)
class GamificationTaskAdmin(ModelAdmin):
    inlines = [TaskQuestionInline]
    list_display = ("title", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title",)


@admin.register(StudentTaskRecord)
class StudentTaskRecordAdmin(ModelAdmin):
    list_display = ("student", "task", "correct", "total", "completed_on")
    list_filter = ("task", "completed_on")
    search_fields = ("student__user__username", "task__title")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GigExperience)
class GigExperienceAdmin(ModelAdmin):
    list_display = ("gig_title", "student", "company", "rating", "completion_date", "verification_code")
    list_filter = ("star_rating", "completion_date")
    search_fields = ("student__user__username", "company__company_name", "gig_title")
    raw_id_fields = ("student", "company", "application")
    readonly_fields = ("verification_uuid", "created_at")
    date_hierarchy = "completion_date"

    @display(description=_("Rating"), label={True: OK, False: WAIT}, ordering="star_rating")
    def rating(self, obj):
        return obj.is_verified, f"★ {obj.rating_label}"


# ---------------------------------------------------------------- companies & opportunities
@admin.register(PartnerProfile)
class PartnerProfileAdmin(ModelAdmin):
    list_display = ("company_name", "official_email", "kind", "verified", "profile_completion_score", "country_of_registration")
    list_filter = ("is_fully_verified", "is_academic", "country_of_registration")
    search_fields = ("company_name", "official_email", "user__username", "user__email")
    readonly_fields = ("profile_completion_score",)
    actions = ["verify_partners"]
    fieldsets = (
        (None, {"fields": (("user", "company_name"), ("partner_code", "is_academic"))}),
        (_("Contact & verification"), {"fields": (("official_email", "official_phone"), ("official_website", "linkedin_url"),
                                                  ("facebook_url", "twitter_url", "instagram_url"))}),
        (_("Logo"), {"fields": ("logo",)}),
        (_("Account status"), {"fields": ("is_fully_verified", "profile_completion_score", "contract_expiry_date")}),
    )

    @display(description=_("Type"), label={True: INFO, False: "primary"})
    def kind(self, obj):
        return obj.is_academic, _("University") if obj.is_academic else _("Company")

    @flag_badge(_("Verified"), _("Verified"), _("Pending"))
    def verified(self, obj):
        return obj.is_fully_verified

    @admin.action(description=_("Verify selected partners"))
    def verify_partners(self, request, queryset):
        for partner in queryset.filter(is_fully_verified=False):
            partner.is_fully_verified = True
            partner.save(update_fields=["is_fully_verified"])
            partner.calculate_completion()
        self.message_user(request, _("Selected partners are verified."), messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        obj.calculate_completion()  # verified partners are 100% complete


class InternshipRequiredSkillInline(TabularInline):
    """Market Readiness Gate: skills students must have verified (≥70% match) to apply."""
    model = Skill.opportunities.through
    extra = 1
    verbose_name = "Required skill"
    verbose_name_plural = "Required skills (students need ≥70% match to apply)"


@admin.register(Internship)
class InternshipAdmin(ModelAdmin):
    inlines = [InternshipRequiredSkillInline]
    list_display = ("title", "partner", "state", "deadline", "applicants", "is_premium", "created_at")
    list_filter = (OpportunityStatusFilter, "is_active", "is_premium", "deadline", "partner__company_name")
    search_fields = ("title", "description", "partner__company_name")
    readonly_fields = ("created_at",)
    date_hierarchy = "deadline"
    actions = ["activate_internships", "close_internships"]

    @display(description=_("Status"), label={"open": OK, "expired": WAIT, "closed": BAD})
    def state(self, obj):
        labels = {"open": _("Open for applications"), "expired": _("Expired"), "closed": _("Closed")}
        return obj.status, labels[obj.status]

    @display(description=_("Applicants"))
    def applicants(self, obj):
        return obj.applicants_count

    @admin.action(description=_("Activate selected opportunities"))
    def activate_internships(self, request, queryset):
        queryset.update(is_active=True)
        self.message_user(request, _("Selected opportunities are active."))

    @admin.action(description=_("Close selected opportunities"))
    def close_internships(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, _("Selected opportunities are closed."))


@admin.register(PartnerInternshipSubmission)
class PartnerInternshipSubmissionAdmin(ModelAdmin):
    list_display = ("title", "partner", "status_col", "submission_date", "location", "deadline")
    list_filter = ("status", "submission_date", "partner__company_name")
    search_fields = ("title", "description", "partner__company_name")
    date_hierarchy = "submission_date"
    actions = ["approve_submissions", "reject_submissions"]
    status_col = status_badge("status", SUBMISSION_COLORS)

    @admin.action(description=_("Approve and publish selected submissions"))
    def approve_submissions(self, request, queryset):
        approved, errors = 0, []
        for submission in queryset.filter(status="Pending"):
            try:
                internship = Internship.objects.create(
                    partner=submission.partner, title=submission.title, description=submission.description,
                    location=submission.location, required_majors=submission.required_majors, deadline=submission.deadline,
                    is_premium=True, is_active=True,
                )
                internship.required_skills.set(submission.required_skills.all())
                submission.status = "Approved"
                submission.save()
                approved += 1
            except Exception as exc:
                errors.append(f"{submission.title}: {str(exc)[:100]}")
        if approved:
            self.message_user(request, _("%(n)s submissions approved and published to students.") % {"n": approved}, messages.SUCCESS)
        for error in errors:
            self.message_user(request, f"⚠️ {error}", messages.WARNING)
        if not approved and not errors:
            self.message_user(request, _("No pending submissions selected."), messages.INFO)

    @admin.action(description=_("Reject selected submissions"))
    def reject_submissions(self, request, queryset):
        queryset.filter(status="Pending").update(status="Rejected")
        self.message_user(request, _("Selected submissions were rejected."))


@admin.register(PartnerCourseSubmission)
class PartnerCourseSubmissionAdmin(ModelAdmin):
    list_display = ("title", "partner", "skill", "price", "status_col", "submission_date")
    autocomplete_fields = ("skill",)
    list_filter = ("status", "submission_date", "partner__company_name")
    search_fields = ("title", "instructor_name", "partner__company_name")
    actions = ["approve_courses", "reject_courses"]
    status_col = status_badge("status", SUBMISSION_COLORS)

    @admin.action(description=_("Approve and publish selected courses"))
    def approve_courses(self, request, queryset):
        n = queryset.filter(status="Pending").update(status="Approved")
        self.message_user(request, _("%(n)s courses approved.") % {"n": n})

    @admin.action(description=_("Reject selected courses"))
    def reject_courses(self, request, queryset):
        n = queryset.filter(status="Pending").update(status="Rejected")
        self.message_user(request, _("%(n)s courses rejected.") % {"n": n})


@admin.register(Application)
class ApplicationAdmin(ModelAdmin):
    list_display = ("internship", "applicant", "status_col", "source", "application_date", "reviewed_at", "decided_at")
    list_filter = ("status", "source", "application_date", "internship__partner__company_name")
    search_fields = ("internship__title", "applicant__username", "applicant__email", "applicant__first_name", "applicant__last_name")
    list_select_related = ("internship", "applicant")
    readonly_fields = ("application_date", "reviewed_at", "decided_at", "shortlisted_at", "contact_consent_at")
    date_hierarchy = "application_date"
    actions = ["approve_and_forward_applications"]
    status_col = status_badge("status", APPLICATION_COLORS)

    @admin.action(description=_("Approve and forward selected profiles to partners"))
    def approve_and_forward_applications(self, request, queryset):
        forwarded = 0
        for app in queryset.filter(status=Application.STATUS_SUBMITTED):
            try:
                PartnerApplicantData.objects.create(partner=app.internship.partner, student=app.applicant.studentprofile,
                                                    internship=app.internship, application=app)
                app.status = Application.STATUS_UNDER_REVIEW
                app.save()
                forwarded += 1
            except StudentProfile.DoesNotExist:
                self.message_user(request, f"{app.applicant.username}: no student profile.", messages.ERROR)
            except IntegrityError:
                app.status = Application.STATUS_UNDER_REVIEW
                app.save()
                self.message_user(request, f"{app.applicant.username}: already forwarded.", messages.WARNING)
        if forwarded:
            self.message_user(request, _("%(n)s profiles forwarded to partners.") % {"n": forwarded})


@admin.register(PartnerApplicantData)
class PartnerApplicantDataAdmin(ModelAdmin):
    list_display = ("partner", "student", "internship")
    search_fields = ("partner__company_name", "student__user__username", "internship__title")
    raw_id_fields = ("partner", "student", "internship", "application")


@admin.register(DiscountCode)
class DiscountCodeAdmin(ModelAdmin):
    list_display = ("code", "discount_percentage", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code",)


@admin.register(StudentEnrollment)
class StudentEnrollmentAdmin(ModelAdmin):
    """Enrollments; "Mark completed" issues the certificate and auto-verifies the course's skill (accredited partners)."""

    list_display = ("student", "course", "status_col", "enrolled_on", "completed_at", "final_price")
    list_filter = ("status", "enrolled_on", "course__title")
    search_fields = ("student__user__username", "course__title")
    status_col = status_badge("status", {StudentEnrollment.STATUS_COMPLETED: OK, StudentEnrollment.STATUS_ENROLLED: INFO})
    actions = ["mark_completed"]

    @admin.action(description=_("Mark completed: issue certificate and verify the skill"))
    def mark_completed(self, request, queryset):
        from .courses import complete_enrollment
        done = queryset.exclude(status=StudentEnrollment.STATUS_COMPLETED)
        n = done.count()
        verified = sum(complete_enrollment(e) for e in done)
        self.message_user(request, _("%(n)s completed, %(v)s skills verified.") % {"n": n, "v": verified}, messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(UniversityInquiry)
class UniversityInquiryAdmin(ModelAdmin):
    """Demo / free-pilot requests from /universities/."""

    list_display = ("university", "faculty", "name", "position", "email", "phone", "channel", "status_col", "created_at")
    list_filter = ("status", "channel", "created_at")
    search_fields = ("university", "faculty", "name", "email", "phone")
    readonly_fields = ("name", "position", "university", "faculty", "email", "phone", "message", "channel", "created_at")
    date_hierarchy = "created_at"
    status_col = status_badge("status", {UniversityInquiry.STATUS_NEW: WAIT, UniversityInquiry.STATUS_CONTACTED: INFO,
                                         UniversityInquiry.STATUS_PILOT: OK, UniversityInquiry.STATUS_CLOSED: None})

    def has_add_permission(self, request):
        return False
