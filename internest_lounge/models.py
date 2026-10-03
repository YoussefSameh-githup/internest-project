from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _

from internest_core.models import PartnerProfile

FLAGS_TO_HIDE = 3
EDIT_WINDOW = timedelta(minutes=5)
DELETE_WINDOW = timedelta(minutes=15)


class Visibility(models.TextChoices):
    VISIBLE = "visible", _("Visible")
    HIDDEN = "hidden", _("Hidden pending review")
    REMOVED = "removed", _("Removed by admin")


class HiddenReason(models.TextChoices):
    NONE = "", "—"
    LINK_SPAM = "link_spam", _("Suspicious link")
    DUPLICATE = "duplicate", _("Duplicate text")
    FLAGS = "flags", _("Flagged by founders")


class LoungePost(models.Model):
    CATEGORY_CHOICES = [
        ("hiring", _("#Hiring")),
        ("advice", _("#Advice")),
        ("b2b", _("#B2B_Partnership")),
    ]

    author = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="lounge_posts")
    title = models.CharField(max_length=160, blank=True)  # optional: body-only posts like LinkedIn/X
    body = models.TextField(max_length=5000)
    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES, blank=True)
    body_fingerprint = models.CharField(max_length=64, db_index=True, editable=False)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.VISIBLE, db_index=True)
    hidden_reason = models.CharField(max_length=12, choices=HiddenReason.choices, blank=True)
    upvoters = models.ManyToManyField(PartnerProfile, blank=True, related_name="upvoted_lounge_posts")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.display_title

    @property
    def display_title(self):
        return self.title or Truncator(self.body).chars(60)

    @property
    def is_visible(self):
        return self.visibility == Visibility.VISIBLE

    @property
    def is_editable(self):
        """Authors may edit for 5 minutes after posting."""
        return timezone.now() - self.created_at <= EDIT_WINDOW

    @property
    def is_deletable(self):
        """Authors may delete for 15 minutes after posting."""
        return timezone.now() - self.created_at <= DELETE_WINDOW


class LoungeComment(models.Model):
    post = models.ForeignKey(LoungePost, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="lounge_comments")
    body = models.TextField(max_length=2000)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.VISIBLE)
    hidden_reason = models.CharField(max_length=12, choices=HiddenReason.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author} on {self.post}"


class LoungeFlag(models.Model):
    post = models.ForeignKey(LoungePost, on_delete=models.CASCADE, related_name="flags")
    reporter = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="lounge_flags")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("post", "reporter")
