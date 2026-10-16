import uuid

from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from internest_core.models import Internship, PartnerProfile, StudentProfile, validate_max_file_size


def current_year():
    return timezone.now().year


class CompanyProfile(models.Model):
    """Startup details submitted after self-registration. Name, website and social links live on PartnerProfile."""

    INDUSTRY_CHOICES = [
        ("software", _("Software & SaaS")),
        ("fintech", _("Fintech")),
        ("ecommerce", _("E-commerce & Retail")),
        ("edtech", _("Education")),
        ("healthtech", _("Health")),
        ("marketing", _("Marketing & Media")),
        ("logistics", _("Logistics & Mobility")),
        ("other", _("Other")),
    ]

    partner = models.OneToOneField(PartnerProfile, on_delete=models.CASCADE, related_name="company_profile")
    industry = models.CharField(max_length=20, choices=INDUSTRY_CHOICES)
    founded_year = models.PositiveSmallIntegerField(validators=[MinValueValidator(1950), MaxValueValidator(2100)])
    description = models.TextField(max_length=1000)
    founder_email = models.EmailField(blank=True, verbose_name="Founder official email")
    submitted_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    # Set when a verified startup renames itself: verification stays, an admin can re-check identity.
    identity_changed_at = models.DateTimeField(null=True, blank=True)
    previous_company_name = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Company profile"
        verbose_name_plural = "Company profiles"

    def __str__(self):
        return self.partner.company_name

    @property
    def is_verified(self):
        return self.partner.is_fully_verified


class PendingCompanyProfile(CompanyProfile):
    """Admin shortcut: only startups waiting for verification."""

    class Meta:
        proxy = True
        verbose_name = "Pending verification"
        verbose_name_plural = "⏳ Pending verification"


class PromoCode(models.Model):
    code = models.CharField(max_length=30, unique=True)
    discount_percent = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(100)])
    is_active = models.BooleanField(default=True)
    valid_until = models.DateField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    times_used = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.code} (-{self.discount_percent}%)"

    def is_valid(self):
        if not self.is_active:
            return False
        if self.valid_until and self.valid_until < timezone.now().date():
            return False
        if self.max_uses is None:
            return True
        # Unpaid / under-review orders already hold a use, so a limited code can't be spent twice in parallel.
        reserved = self.requests.filter(status__in=("pending", "verifying")).count()
        return self.times_used + reserved < self.max_uses


class ProUpgradeRequest(models.Model):
    """A startup's Pro checkout. Pro is activated by an admin once payment is received."""

    STATUS_PENDING = "pending"
    STATUS_VERIFYING = "verifying"  # manual transfer done, receipt uploaded, waiting for an admin to check it
    STATUS_ACTIVE = "activated"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [(STATUS_PENDING, _("Awaiting payment")), (STATUS_VERIFYING, _("Pending verification")),
                      (STATUS_ACTIVE, _("Activated")), (STATUS_CANCELLED, _("Cancelled"))]
    OPEN_STATUSES = (STATUS_PENDING, STATUS_VERIFYING)  # an order the company can't replace with a new one silently

    # Manual transfer methods (the receipt is checked by our team before Pro is activated).
    METHOD_INSTAPAY = "instapay"
    METHOD_REDOTPAY = "redotpay"

    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="pro_requests")
    months = models.PositiveSmallIntegerField(default=1)
    list_price = models.DecimalField(max_digits=8, decimal_places=2)
    promo_code = models.ForeignKey(PromoCode, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests")
    discount_percent = models.PositiveSmallIntegerField(default=0)
    final_price = models.DecimalField(max_digits=8, decimal_places=2)
    currency = models.CharField(max_length=3, default="EGP")
    country = models.CharField(max_length=2, blank=True, default="")
    gateway = models.CharField(max_length=20, default="manual")  # e.g. fawaterak / stripe
    # Checkout details (sent to the payment gateway) and the buyer's consent to the terms and refund policy.
    customer_name = models.CharField(max_length=120, blank=True)
    customer_email = models.EmailField(blank=True)
    customer_phone = models.CharField(max_length=20, blank=True)
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    gateway_reference = models.CharField(max_length=64, blank=True)  # Fawaterak invoice id
    payment_url = models.URLField(max_length=500, blank=True)
    payment_proof = models.FileField(_("Payment receipt"), upload_to="payment_proofs/", blank=True, validators=[
        FileExtensionValidator(["png", "jpg", "jpeg", "webp", "pdf"]), validate_max_file_size])
    payment_reference = models.CharField(_("Transfer reference"), max_length=64, blank=True)
    proof_submitted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.partner} · Pro × {self.months} · {self.final_price} {self.currency} ({self.get_status_display()})"

    @property
    def price_label(self):
        from .pricing import format_price

        return format_price(self.final_price, self.currency)


class TalentInvitation(models.Model):
    """A Pro startup invites a Talent Pool student to apply. Contacts stay hidden until the usual shortlist step."""

    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="talent_invitations")
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="talent_invitations")
    internship = models.ForeignKey(Internship, on_delete=models.CASCADE, related_name="talent_invitations")
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_CHOICES = [(STATUS_PENDING, _("Pending")), (STATUS_ACCEPTED, _("Applied"))]

    message = models.TextField(max_length=500, blank=True)
    # Secret link token: lets this student apply without meeting the skill match gate.
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["internship", "student"], name="unique_talent_invitation")]

    def __str__(self):
        return f"{self.partner} → {self.student} ({self.internship})"

    @property
    def apply_url(self):
        from django.urls import reverse

        return f"{reverse('internship_detail', args=[self.internship_id])}?invite_token={self.token}"
