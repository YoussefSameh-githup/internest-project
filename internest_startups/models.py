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
    submitted_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

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
