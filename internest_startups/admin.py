from django.contrib import admin, messages
from django.utils import timezone

from .models import CompanyProfile, PendingCompanyProfile


@admin.action(description="✅ Approve & verify selected startups")
def approve_startups(modeladmin, request, queryset):
    count = 0
    for profile in queryset.select_related("partner"):
        partner = profile.partner
        if not partner.is_fully_verified:
            partner.is_fully_verified = True
            partner.save(update_fields=["is_fully_verified"])
            partner.calculate_completion()  # verified partners are 100% → can post opportunities
            profile.verified_at = timezone.now()
            profile.save(update_fields=["verified_at"])
            count += 1
    modeladmin.message_user(request, f"{count} startup(s) verified. They can now post opportunities and use the Founders Network.", messages.SUCCESS)


@admin.action(description="Revoke verification")
def revoke_verification(modeladmin, request, queryset):
    for profile in queryset.select_related("partner"):
        profile.partner.is_fully_verified = False
        profile.partner.save(update_fields=["is_fully_verified"])
        profile.partner.calculate_completion()
    queryset.update(verified_at=None)


@admin.action(description="Mark identity change as reviewed")
def clear_identity_flag(modeladmin, request, queryset):
    queryset.update(identity_changed_at=None, previous_company_name="")


class _Base(admin.ModelAdmin):
    list_display = ("company", "industry", "founded_year", "website", "owner_email", "verified", "identity_changed_at", "submitted_at")
    list_filter = ("partner__is_fully_verified", "industry", ("identity_changed_at", admin.EmptyFieldListFilter))
    search_fields = ("partner__company_name", "partner__user__email", "description")
    readonly_fields = ("submitted_at", "verified_at", "identity_changed_at", "previous_company_name")
    actions = [approve_startups, revoke_verification, clear_identity_flag]

    @admin.display(ordering="partner__company_name", description="Company")
    def company(self, obj):
        return obj.partner.company_name

    @admin.display(description="Website")
    def website(self, obj):
        return obj.partner.official_website or "—"

    @admin.display(description="Owner email")
    def owner_email(self, obj):
        return obj.partner.user.email

    @admin.display(boolean=True, ordering="partner__is_fully_verified", description="Verified")
    def verified(self, obj):
        return obj.partner.is_fully_verified


@admin.register(CompanyProfile)
class CompanyProfileAdmin(_Base):
    pass


@admin.register(PendingCompanyProfile)
class PendingCompanyProfileAdmin(_Base):
    actions = [approve_startups]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(partner__is_fully_verified=False).order_by("submitted_at")


from django.db.models import F  # noqa: E402

from .models import PromoCode, ProUpgradeRequest  # noqa: E402
from .tiers import activate_pro  # noqa: E402


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "discount_percent", "is_active", "valid_until", "times_used", "max_uses")
    list_filter = ("is_active",)
    search_fields = ("code",)


@admin.action(description="💳 Payment received → activate Pro")
def activate_requests(modeladmin, request, queryset):
    count = 0
    for req in queryset.filter(status=ProUpgradeRequest.STATUS_PENDING).select_related("partner", "promo_code"):
        activate_pro(req.partner, req.months)
        req.status, req.activated_at = ProUpgradeRequest.STATUS_ACTIVE, timezone.now()
        req.save(update_fields=["status", "activated_at"])
        if req.promo_code_id:
            PromoCode.objects.filter(pk=req.promo_code_id).update(times_used=F("times_used") + 1)
        count += 1
    modeladmin.message_user(request, f"{count} Pro subscription(s) activated.", messages.SUCCESS)


@admin.action(description="Cancel selected requests")
def cancel_requests(modeladmin, request, queryset):
    queryset.filter(status=ProUpgradeRequest.STATUS_PENDING).update(status=ProUpgradeRequest.STATUS_CANCELLED)


@admin.register(ProUpgradeRequest)
class ProUpgradeRequestAdmin(admin.ModelAdmin):
    list_display = ("partner", "status", "list_price", "promo_code", "discount_percent", "final_price", "pro_until", "created_at", "activated_at")
    list_filter = ("status", "promo_code")
    search_fields = ("partner__company_name", "partner__user__email")
    readonly_fields = ("created_at", "activated_at")
    actions = [activate_requests, cancel_requests]

    @admin.display(description="Pro valid until")
    def pro_until(self, obj):
        sub = getattr(obj.partner, "subscription", None)
        return sub.valid_until if sub and sub.plan == "pro" else "—"


from .models import TalentInvitation  # noqa: E402


@admin.register(TalentInvitation)
class TalentInvitationAdmin(admin.ModelAdmin):
    list_display = ("partner", "student", "internship", "created_at")
    list_filter = ("created_at",)
    search_fields = ("partner__company_name", "student__user__username", "internship__title")
    raw_id_fields = ("partner", "student", "internship")
