from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from internest_core.models import PartnerProfile


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
        return self.max_uses is None or self.times_used < self.max_uses


class ProUpgradeRequest(models.Model):
    """A startup's Pro checkout. Pro is activated by an admin once payment is received."""

    STATUS_PENDING = "pending"
    STATUS_ACTIVE = "activated"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [(STATUS_PENDING, _("Awaiting payment")), (STATUS_ACTIVE, _("Activated")), (STATUS_CANCELLED, _("Cancelled"))]

    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="pro_requests")
    months = models.PositiveSmallIntegerField(default=1)
    list_price = models.DecimalField(max_digits=8, decimal_places=2)
    promo_code = models.ForeignKey(PromoCode, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests")
    discount_percent = models.PositiveSmallIntegerField(default=0)
    final_price = models.DecimalField(max_digits=8, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.partner} · Pro × {self.months} ({self.get_status_display()})"
