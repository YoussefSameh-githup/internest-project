from django.db import models
from django.utils.translation import gettext_lazy as _

from internest_core.models import StudentProfile


class StudentCV(models.Model):
    """The student's ATS resume as cleaned JSON (see cv_builder_service.EMPTY_CV for the schema)."""

    student = models.OneToOneField(StudentProfile, on_delete=models.CASCADE, related_name="cv")
    data = models.JSONField(default=dict)
    ai_day = models.DateField(null=True, blank=True)  # AI polish / voice calls cost money: capped per day
    ai_uses = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CV: {self.student}"


class StudentProject(models.Model):
    """Portfolio project on the student's profile; the CV builder replaces its own rows on every save."""

    SOURCE_CV_BUILDER = "cv_builder"

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(_("Title"), max_length=120)
    description = models.TextField(_("Description"), max_length=600, blank=True)
    tech_stack = models.CharField(_("Tech stack"), max_length=200, blank=True)
    link = models.URLField(_("Link"), blank=True)
    source = models.CharField(max_length=20, default=SOURCE_CV_BUILDER)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return self.title
