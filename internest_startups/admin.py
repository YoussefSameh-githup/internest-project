from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from internest_core.admin_base import BAD, INFO, OK, WAIT, ModelAdmin, flag_badge, status_badge

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


class _Base(ModelAdmin):
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

    @flag_badge(_("Verified"), _("Verified"), _("Pending"))
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



from django.db import IntegrityError  # noqa: E402

from .entitlements import activate_order as activate_paid_order  # noqa: E402
from .models import FoundingSeat, HiringPass, PromoCode, ProUpgradeRequest  # noqa: E402


@admin.register(PromoCode)
class PromoCodeAdmin(ModelAdmin):
    list_display = ("code", "discount_percent", "is_active", "valid_until", "times_used", "max_uses")
    list_filter = ("is_active", "valid_until")
    search_fields = ("code",)


def activate_order(req):
    """Admin entry point kept for existing callers: see entitlements.activate_order (idempotent, amount-checked)."""
    return activate_paid_order(req.pk)


@admin.action(description="✅ Receipt verified → activate (Hiring Pass, or a legacy Pro order)")
def activate_requests(modeladmin, request, queryset):
    count = 0
    for req in queryset.only("pk"):
        try:
            count += activate_paid_order(req.pk)
        except (ValueError, IntegrityError) as exc:  # wrong amount/opportunity or a pass already exists: nothing changed
            modeladmin.message_user(request, f"Order {req.pk} not activated: {exc}", messages.ERROR)
    modeladmin.message_user(request, f"{count} order(s) activated.", messages.SUCCESS)


@admin.action(description="Cancel selected requests")
def cancel_requests(modeladmin, request, queryset):
    queryset.filter(status__in=ProUpgradeRequest.OPEN_STATUSES).update(status=ProUpgradeRequest.STATUS_CANCELLED)


@admin.register(ProUpgradeRequest)
class ProUpgradeRequestAdmin(ModelAdmin):
    """Manual payments: open the receipt, check the transfer arrived, then run "Receipt verified → activate Pro"."""

    list_display = ("partner", "product", "internship", "status_col", "final_price", "currency", "gateway", "receipt", "payment_reference",
                    "proof_submitted_at", "access_until", "created_at", "activated_at")
    list_filter = ("product", "status", "gateway", "promo_code", "created_at")
    date_hierarchy = "created_at"
    status_col = status_badge("status", {ProUpgradeRequest.STATUS_PENDING: INFO, ProUpgradeRequest.STATUS_VERIFYING: WAIT,
                                         ProUpgradeRequest.STATUS_ACTIVE: OK, ProUpgradeRequest.STATUS_CANCELLED: BAD})
    search_fields = ("partner__company_name", "partner__user__email", "customer_email", "customer_phone", "gateway_reference", "payment_reference")
    # Amounts and what was bought are financial records: never edited after the order exists (only status moves).
    # Status moves only through the actions, so "activated" always means access was granted exactly once.
    readonly_fields = ("status", "partner", "product", "internship", "months", "list_price", "promo_code", "discount_percent", "final_price",
                       "currency", "country", "created_at", "activated_at", "terms_accepted_at", "gateway_reference", "payment_url",
                       "proof_submitted_at", "receipt")
    actions = [activate_requests, cancel_requests]

    @admin.display(description="Receipt")
    def receipt(self, obj):
        if not obj.payment_proof:
            return "—"
        return format_html('<a href="{}" target="_blank" rel="noopener" style="font-weight:700">🧾 Open receipt</a>', obj.payment_proof.url)

    @admin.display(description="Access until")
    def access_until(self, obj):
        hiring_pass = getattr(obj, "hiring_pass", None)
        if hiring_pass is not None:
            return f"{hiring_pass.expires_at:%Y-%m-%d}"
        sub = getattr(obj.partner, "subscription", None)
        return sub.valid_until if obj.product == "pro" and sub and sub.plan == "pro" else "—"


@admin.register(HiringPass)
class HiringPassAdmin(ModelAdmin):
    """Read-only record of every pass. Passes are created by order activation or a Founding 20 claim, never by hand."""

    list_display = ("partner", "internship", "source", "starts_at", "expires_at", "available_at_start", "order")
    list_filter = ("source", "expires_at")
    search_fields = ("partner__company_name", "internship__title")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FoundingSeat)
class FoundingSeatAdmin(ModelAdmin):
    list_display = ("number", "partner", "claimed_at", "found_candidates", "would_pay", "trust_in_verification", "feedback_at")
    search_fields = ("partner__company_name",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


from .models import TalentInvitation  # noqa: E402


@admin.register(TalentInvitation)
class TalentInvitationAdmin(ModelAdmin):
    list_display = ("partner", "student", "internship", "status_col", "created_at")
    list_filter = ("status", "created_at")
    status_col = status_badge("status", {TalentInvitation.STATUS_ACCEPTED: OK, TalentInvitation.STATUS_PENDING: INFO})
    search_fields = ("partner__company_name", "student__user__username", "internship__title")
    raw_id_fields = ("partner", "student", "internship")
