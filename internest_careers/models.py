from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from internest_core.models import Application, Internship, PartnerProfile


class InterviewKit(models.Model):
    """A structured interview: questions with purpose, expected evidence, scoring criteria and follow-ups.

    Owned by the user who generated it: a partner (for an applicant or an opportunity) or a student (practice).
    `questions` items: {question, purpose, expected_evidence, criteria: [str], follow_ups: [str], difficulty, skill,
    kind, source} (see interview_service.build_questions)."""

    SOURCE_DETERMINISTIC, SOURCE_AI = "deterministic", "ai"
    SOURCE_CHOICES = [(SOURCE_DETERMINISTIC, _("Question bank")), (SOURCE_AI, _("Question bank + AI"))]
    SENIORITY_INTERN, SENIORITY_JUNIOR, SENIORITY_MID = "intern", "junior", "mid"
    SENIORITY_CHOICES = [(SENIORITY_INTERN, _("Intern")), (SENIORITY_JUNIOR, _("Junior / fresh graduate")),
                         (SENIORITY_MID, _("Experienced (1-2 years)"))]
    AI_NONE, AI_DONE, AI_UNAVAILABLE, AI_QUOTA = "none", "done", "unavailable", "quota"
    AI_STATUS_CHOICES = [(AI_NONE, "Not requested"), (AI_DONE, "Added"), (AI_UNAVAILABLE, "AI unavailable"),
                         (AI_QUOTA, "Daily AI limit reached")]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="interview_kits")
    partner = models.ForeignKey(PartnerProfile, null=True, blank=True, on_delete=models.CASCADE, related_name="interview_kits")
    internship = models.ForeignKey(Internship, null=True, blank=True, on_delete=models.SET_NULL, related_name="interview_kits")
    application = models.ForeignKey(Application, null=True, blank=True, on_delete=models.SET_NULL, related_name="interview_kits")
    role = models.CharField(_("Role"), max_length=120)
    seniority = models.CharField(_("Seniority"), max_length=10, choices=SENIORITY_CHOICES, default=SENIORITY_INTERN)
    questions = models.JSONField(default=list)
    source = models.CharField(max_length=15, choices=SOURCE_CHOICES, default=SOURCE_DETERMINISTIC)
    ai_status = models.CharField(max_length=12, choices=AI_STATUS_CHOICES, default=AI_NONE)
    includes_assessments = models.BooleanField(default=False)  # built with scores / flagged areas (own data or paid access)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"], name="interviewkit_owner_idx")]

    def __str__(self):
        return f"{self.role} ({self.get_seniority_display()})"

    @property
    def is_practice(self) -> bool:
        return self.partner_id is None

    @property
    def can_enrich(self) -> bool:
        return self.ai_status in (self.AI_NONE, self.AI_UNAVAILABLE)
