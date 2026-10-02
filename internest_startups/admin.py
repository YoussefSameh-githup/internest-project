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
    modeladmin.message_user(request, f"{count} startup(s) verified. They can now post opportunities and use the Founders Lounge.", messages.SUCCESS)


@admin.action(description="Revoke verification")
def revoke_verification(modeladmin, request, queryset):
    for profile in queryset.select_related("partner"):
        profile.partner.is_fully_verified = False
        profile.partner.save(update_fields=["is_fully_verified"])
        profile.partner.calculate_completion()
    queryset.update(verified_at=None)


class _Base(admin.ModelAdmin):
    list_display = ("company", "industry", "founded_year", "website", "owner_email", "verified", "submitted_at")
    list_filter = ("partner__is_fully_verified", "industry")
    search_fields = ("partner__company_name", "partner__user__email", "description")
    readonly_fields = ("submitted_at", "verified_at")
    actions = [approve_startups, revoke_verification]

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
