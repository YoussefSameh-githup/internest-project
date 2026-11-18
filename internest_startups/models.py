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
    custom_industry = models.CharField(max_length=80, blank=True, default="", verbose_name=_("Industry name"))
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
    """A company's paid order. Activated by an admin once payment is received (never by a client callback).

    Since 2026-11-18 new orders are Hiring Passes (`product = hiring_pass`). Rows with `product = pro` are historical
    Pro subscription orders: they are kept exactly as they were and never repriced.
    """

    PRODUCT_PRO = "pro"  # historical monthly/annual subscription (no longer sold)
    PRODUCT_HIRING_PASS = "hiring_pass"
    PRODUCT_CHOICES = [(PRODUCT_PRO, _("Internest Pro (legacy)")), (PRODUCT_HIRING_PASS, _("Hiring Pass"))]

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
    # Existing rows were back-filled as "pro" by migration 0011; anything created from now on is a Hiring Pass by default.
    product = models.CharField(max_length=12, choices=PRODUCT_CHOICES, default=PRODUCT_HIRING_PASS)
    # Hiring Pass: the one opportunity it is bought for (chosen at checkout, server-checked to belong to the company).
    internship = models.ForeignKey(Internship, null=True, blank=True, on_delete=models.SET_NULL, related_name="pass_orders")
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
        constraints = [
            # One open Hiring Pass order per company, enforced by the database (two simultaneous checkouts cannot both pass).
            models.UniqueConstraint(fields=["partner"], condition=models.Q(product="hiring_pass", status__in=["pending", "verifying"]),
                                    name="one_open_pass_order_per_partner"),
        ]

    def __str__(self):
        what = "Hiring Pass" if self.product == self.PRODUCT_HIRING_PASS else f"Pro × {self.months}"
        return f"{self.partner} · {what} · {self.final_price} {self.currency} ({self.get_status_display()})"

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


class HiringPass(models.Model):
    """30 days of paid hiring access for ONE opportunity. One-time: it never renews and nothing is charged again.

    Created only by the server: an admin activating a paid order, or a Founding 20 claim. `available_at_start` records how
    many verified candidates matched the opportunity's skills when the pass started (evidence for the availability policy).
    """

    SOURCE_PURCHASE = "purchase"
    SOURCE_FOUNDING = "founding"
    SOURCE_ADMIN = "admin"
    SOURCE_CHOICES = [(SOURCE_PURCHASE, _("Purchase")), (SOURCE_FOUNDING, _("Founding 20")), (SOURCE_ADMIN, _("Granted by admin"))]

    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="hiring_passes")
    internship = models.ForeignKey(Internship, null=True, on_delete=models.SET_NULL, related_name="hiring_passes")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    # One pass per order: activating the same order twice cannot create a second pass.
    # RESTRICT: the order or pass cannot be deleted on its own, but deleting the company (account erasure) still cascades.
    order = models.OneToOneField(ProUpgradeRequest, null=True, blank=True, on_delete=models.RESTRICT, related_name="hiring_pass")
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    available_at_start = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-expires_at"]
        indexes = [models.Index(fields=["partner", "expires_at"], name="hiringpass_partner_exp_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(expires_at__gt=models.F("starts_at")), name="hiringpass_expires_after_start")]

    def __str__(self):
        return f"{self.partner} · {self.internship or '—'} · until {self.expires_at:%Y-%m-%d}"

    @property
    def is_active(self):
        return self.starts_at <= timezone.now() < self.expires_at


class FoundingSeat(models.Model):
    """Founding 20: seats 1–20, one per company, each with one free Hiring Pass.

    The unique seat number and the 1–20 check are enforced by the database, so two companies claiming at the same moment
    cannot both get seat 20, and nobody gets seat 21. The company must claim it (no automatic enrolment). Feedback is
    optional and has no required positive answer.
    """

    LIMIT = 20
    YES, PARTLY, NO = "yes", "partly", "no"
    ANSWER_CHOICES = [(YES, _("Yes")), (PARTLY, _("Partly")), (NO, _("No"))]

    number = models.PositiveSmallIntegerField(unique=True)
    partner = models.OneToOneField(PartnerProfile, on_delete=models.CASCADE, related_name="founding_seat")
    hiring_pass = models.OneToOneField(HiringPass, on_delete=models.RESTRICT, related_name="founding_seat")
    claimed_at = models.DateTimeField(auto_now_add=True)
    # Structured product feedback (all optional).
    found_candidates = models.CharField(_("Did you find suitable candidates?"), max_length=6, choices=ANSWER_CHOICES, blank=True)
    candidates_contacted = models.PositiveSmallIntegerField(_("How many candidates did you contact?"), null=True, blank=True,
                                                            validators=[MaxValueValidator(500)])
    trust_in_verification = models.PositiveSmallIntegerField(_("How much do you trust the skill verification? (1–5)"), null=True,
                                                             blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    would_pay = models.CharField(_("Would you pay for your next Hiring Pass?"), max_length=6, choices=ANSWER_CHOICES, blank=True)
    missing = models.TextField(_("What was missing or should change?"), max_length=1000, blank=True)
    feedback_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [models.CheckConstraint(condition=models.Q(number__gte=1, number__lte=20), name="founding_seat_1_to_20")]

    def __str__(self):
        return f"Founding seat {self.number} · {self.partner}"
