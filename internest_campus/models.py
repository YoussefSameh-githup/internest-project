import hashlib
import re
import secrets

from django.db import models

from internest_core.models import StudentProfile

CAMPAIGN_GOAL = 500


def normalize(text: str) -> str:
    """Case/spacing/Arabic-letter-variant insensitive key, so 'Ain Shams' == 'ain  shams' and 'أ' == 'ا'."""
    text = re.sub(r"\s+", " ", (text or "").strip().casefold())
    text = re.sub("[أإآ]", "ا", text)
    return text.replace("ى", "ي").replace("ة", "ه")


def campus_key(university: str, faculty: str) -> str:
    return f"{normalize(university)}|{normalize(faculty)}"


def campus_code(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:6].upper()


def new_share_code() -> str:
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


class CampusDemandVote(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="campus_votes")
    university_name = models.CharField(max_length=150)
    faculty_name = models.CharField(max_length=150)
    department = models.CharField(max_length=150, blank=True)
    # Named student_number because the `student` FK already owns the `student_id` attribute.
    student_number = models.CharField(max_length=30, verbose_name="Student ID")
    university_key = models.CharField(max_length=150, db_index=True, editable=False)
    campus_key = models.CharField(max_length=310, db_index=True, editable=False)
    # This vote's own share code (used in ?ref=CAMPUS-CODE links); never exposes the student ID.
    referral_code = models.CharField(max_length=16, unique=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["student", "university_key"], name="one_vote_per_user_per_university"),
            models.UniqueConstraint(fields=["student_number", "university_key"], name="one_vote_per_student_id_per_university"),
        ]

    def __str__(self):
        return f"{self.university_name} / {self.faculty_name} ({self.masked_student_id})"

    def save(self, *args, **kwargs):
        self.university_key = normalize(self.university_name)
        self.campus_key = campus_key(self.university_name, self.faculty_name)
        if not self.referral_code:
            self.referral_code = new_share_code()
        super().save(*args, **kwargs)

    @property
    def share_ref(self) -> str:
        return f"{campus_code(self.campus_key)}-{self.referral_code}"

    @property
    def masked_student_id(self) -> str:
        sid = self.student_number
        if len(sid) <= 4:
            return "•" * len(sid)
        return f"{sid[:2]}{'•' * (len(sid) - 4)}{sid[-2:]}"

    @property
    def campus_votes(self) -> int:
        return CampusDemandVote.objects.filter(campus_key=self.campus_key).count()

    @property
    def voter_number(self) -> int:
        """Position of this vote within its campus campaign ("Student #143")."""
        return CampusDemandVote.objects.filter(campus_key=self.campus_key, pk__lte=self.pk).count()

    @property
    def progress_percent(self) -> int:
        return min(100, self.campus_votes * 100 // CAMPAIGN_GOAL)

    @classmethod
    def from_ref(cls, ref: str):
        code = (ref or "").rsplit("-", 1)[-1]
        if not code:
            return None
        return cls.objects.filter(referral_code=code).first()
