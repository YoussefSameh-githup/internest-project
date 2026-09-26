import uuid
from datetime import timedelta

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _

from internest_core.models import PartnerProfile, StudentProfile

RETEST_COOLDOWN_DAYS = 14


def _split_csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Discipline(models.TextChoices):
    COMPUTING = "computing", _("Computer Science & IT")
    BUSINESS = "business", _("Business & Finance")
    MEDIA = "media", _("Media & Design")
    LAW = "law", _("Law")
    ENGINEERING = "engineering", _("Engineering")
    HEALTH = "health", _("Health Sciences")
    GENERAL = "general", _("Transferable / Soft Skills")


class Skill(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    discipline = models.CharField(max_length=20, choices=Discipline.choices)
    aliases = models.TextField(
        blank=True,
        help_text="Comma-separated terms (any language) that explicitly name this skill in a CV.",
    )
    implicit_signals = models.TextField(
        blank=True,
        help_text="Comma-separated phrases that imply this skill from projects / work history.",
    )
    challenge_length = models.PositiveSmallIntegerField(default=10, validators=[MinValueValidator(3), MaxValueValidator(30)])
    pass_threshold = models.PositiveSmallIntegerField(default=70, validators=[MinValueValidator(1), MaxValueValidator(100)])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["discipline", "name"]

    def __str__(self):
        return self.name

    @property
    def alias_list(self):
        return [self.name] + _split_csv(self.aliases)

    @property
    def implicit_signal_list(self):
        return _split_csv(self.implicit_signals)

    @property
    def has_challenge(self):
        return self.items.filter(is_active=True).count() >= 3


class SubSkill(models.Model):
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="sub_skills")
    name = models.CharField(max_length=120)
    keywords = models.TextField(blank=True, help_text="Comma-separated search terms used to match courses.")

    class Meta:
        unique_together = ("skill", "name")
        ordering = ["skill", "name"]

    def __str__(self):
        return f"{self.skill.name} › {self.name}"

    @property
    def search_terms(self):
        return [self.name] + _split_csv(self.keywords)


class ChallengeItem(models.Model):
    KIND_MCQ = "mcq"
    KIND_CASE = "case"
    KIND_CHOICES = [(KIND_MCQ, "Multiple choice"), (KIND_CASE, "Short case / written answer")]

    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="items")
    sub_skill = models.ForeignKey(SubSkill, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_MCQ)
    prompt = models.TextField()
    choices = models.JSONField(default=list, blank=True, help_text='MCQ only: ["option A", "option B", ...]')
    correct_index = models.PositiveSmallIntegerField(null=True, blank=True)
    rubric = models.JSONField(
        default=list, blank=True,
        help_text='Case only: list of concept groups; each group is a list of accepted terms, e.g. [["npv","net present value"],["discount"]].',
    )
    DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD = "easy", "medium", "hard"
    DIFFICULTY_CHOICES = [(DIFFICULTY_EASY, _("Easy")), (DIFFICULTY_MEDIUM, _("Medium")), (DIFFICULTY_HARD, _("Hard"))]
    DIFFICULTY_ORDER = {DIFFICULTY_EASY: 0, DIFFICULTY_MEDIUM: 1, DIFFICULTY_HARD: 2}

    difficulty = models.CharField(max_length=6, choices=DIFFICULTY_CHOICES, default=DIFFICULTY_MEDIUM)
    code_snippet = models.TextField(blank=True, help_text="Optional code shown under the question (technical skills).")
    topic = models.CharField(max_length=160, blank=True, help_text="Specific concept tested; used for improvement feedback.")
    time_limit_seconds = models.PositiveSmallIntegerField(default=45, validators=[MinValueValidator(30), MaxValueValidator(60)])
    weight = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"[{self.skill.name}/{self.sub_skill.name}] {self.prompt[:60]}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.sub_skill_id and self.skill_id and self.sub_skill.skill_id != self.skill_id:
            raise ValidationError("Sub-skill must belong to the selected skill.")
        if self.kind == self.KIND_MCQ:
            if len(self.choices or []) < 2 or self.correct_index is None or self.correct_index >= len(self.choices):
                raise ValidationError("MCQ needs ≥2 choices and a valid correct_index.")
        elif not self.rubric:
            raise ValidationError("Case items need a rubric.")


class ExternalCourse(models.Model):
    PROVIDERS = [("Coursera", "Coursera"), ("edX", "edX"), ("Udemy", "Udemy"), ("YouTube", "YouTube"), ("Other", "Other")]

    sub_skill = models.ForeignKey(SubSkill, on_delete=models.CASCADE, related_name="external_courses")
    title = models.CharField(max_length=200)
    provider = models.CharField(max_length=20, choices=PROVIDERS)
    url = models.URLField()
    expected_outcome = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.title} ({self.provider})"


class SkillProfile(models.Model):
    student = models.OneToOneField(StudentProfile, on_delete=models.CASCADE, related_name="skill_profile")
    partner_university = models.ForeignKey(
        PartnerProfile, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={"is_academic": True}, related_name="affiliated_skill_profiles",
        help_text="Academic partner allowed to view this student's skill analytics.",
    )
    linkedin_url = models.URLField(blank=True)
    last_extracted_at = models.DateTimeField(null=True, blank=True)
    extraction_count_today = models.PositiveSmallIntegerField(default=0)
    extraction_day = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Skill profile: {self.student}"


class StudentSkill(models.Model):
    SOURCE_EXPLICIT = "explicit"
    SOURCE_IMPLICIT = "implicit"
    SOURCE_CHOICES = [(SOURCE_EXPLICIT, _("Explicit")), (SOURCE_IMPLICIT, _("Implicit (inferred)"))]

    STATUS_CLAIMED = "claimed"
    STATUS_VERIFIED = "verified"
    STATUS_LAG = "lag"
    STATUS_CHOICES = [(STATUS_CLAIMED, _("Claimed")), (STATUS_VERIFIED, _("Verified")), (STATUS_LAG, _("Skill lag detected"))]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="skills")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="student_skills")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    evidence = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_CLAIMED)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    percentile = models.PositiveSmallIntegerField(null=True, blank=True)
    lag_sub_skills = models.JSONField(default=list, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("student", "skill")
        ordering = ["-status", "skill__name"]

    def __str__(self):
        return f"{self.student} · {self.skill} ({self.status})"

    @property
    def in_cooldown(self):
        return self.cooldown_until is not None and self.cooldown_until > timezone.now()

    @property
    def cooldown_seconds_remaining(self):
        if not self.in_cooldown:
            return 0
        return int((self.cooldown_until - timezone.now()).total_seconds())

    @property
    def status_label(self):
        if self.status == self.STATUS_LAG and self.lag_sub_skills:
            return gettext("Skill Lag Detected in %(areas)s") % {"areas": ", ".join(s["name"] for s in self.lag_sub_skills)}
        return self.get_status_display()

    def start_cooldown(self):
        self.cooldown_until = timezone.now() + timedelta(days=RETEST_COOLDOWN_DAYS)


class ChallengeAttempt(models.Model):
    STATE_ACTIVE = "active"
    STATE_COMPLETED = "completed"
    STATE_TERMINATED = "terminated"
    STATE_CHOICES = [(STATE_ACTIVE, "Active"), (STATE_COMPLETED, "Completed"), (STATE_TERMINATED, "Terminated (integrity)")]

    MAX_FOCUS_WARNINGS = 2
    MAX_FLAGGED_RESPONSES = 2

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    student_skill = models.ForeignKey(StudentSkill, on_delete=models.CASCADE, related_name="attempts")
    item_ids = models.JSONField(default=list)
    current_index = models.PositiveSmallIntegerField(default=0)
    current_served_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=STATE_ACTIVE)
    focus_warnings = models.PositiveSmallIntegerField(default=0)
    integrity_events = models.JSONField(default=list, blank=True)
    termination_reason = models.CharField(max_length=120, blank=True)
    score_pct = models.PositiveSmallIntegerField(null=True, blank=True)
    sub_skill_breakdown = models.JSONField(default=dict, blank=True)
    improvement_topics = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Attempt {self.token} · {self.student_skill} ({self.state})"

    @property
    def is_active(self):
        return self.state == self.STATE_ACTIVE

    @property
    def total_items(self):
        return len(self.item_ids)

    def log_event(self, kind, **data):
        self.integrity_events = (self.integrity_events or []) + [
            {"type": kind, "at": timezone.now().isoformat(), **data}
        ]


class ChallengeResponse(models.Model):
    attempt = models.ForeignKey(ChallengeAttempt, on_delete=models.CASCADE, related_name="responses")
    item = models.ForeignKey(ChallengeItem, on_delete=models.CASCADE)
    answer_text = models.TextField(blank=True)
    choice_index = models.SmallIntegerField(null=True, blank=True)
    credit = models.FloatField(default=0.0)
    elapsed_ms = models.PositiveIntegerField(default=0)
    timed_out = models.BooleanField(default=False)
    typing_stats = models.JSONField(default=dict, blank=True)
    integrity_flags = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("attempt", "item")


class EmployerSubscription(models.Model):
    PLAN_FREE = "free"
    PLAN_PRO = "pro"
    PLAN_CHOICES = [(PLAN_FREE, "Free"), (PLAN_PRO, "Internest Pro")]

    partner = models.OneToOneField(PartnerProfile, on_delete=models.CASCADE, related_name="subscription")
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES, default=PLAN_FREE)
    valid_until = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.partner} · {self.get_plan_display()}"

    @property
    def is_pro_active(self):
        if self.plan != self.PLAN_PRO:
            return False
        return self.valid_until is None or self.valid_until >= timezone.now().date()
