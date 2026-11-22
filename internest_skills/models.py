import uuid
from datetime import timedelta

from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy as _

from internest_core.models import (
    Internship, PartnerInternshipSubmission, PartnerProfile, StudentProfile, validate_max_file_size,
)
from internest_core.upload_security import UPLOAD_VALIDATORS, RandomName

RETEST_COOLDOWN = timedelta(hours=48)  # after a failed or terminated challenge


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
    # Market Readiness Gate: skills an opportunity requires (Internship.required_skills).
    opportunities = models.ManyToManyField(Internship, blank=True, related_name="required_skills")
    opportunity_submissions = models.ManyToManyField(PartnerInternshipSubmission, blank=True, related_name="required_skills")
    relations_refreshed_at = models.DateTimeField(null=True, blank=True, editable=False)

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
    KIND_PRACTICAL = "practical"  # a real task graded by rubric; a human reviewer sets the final credit
    KIND_CHOICES = [(KIND_MCQ, "Multiple choice"), (KIND_CASE, "Short case / written answer"),
                    (KIND_PRACTICAL, "Practical scenario (human-reviewed)")]
    QUICK_SECONDS = (30, 60)          # multiple choice and short case
    PRACTICAL_SECONDS = (300, 1800)   # practical scenario

    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="items")
    sub_skill = models.ForeignKey(SubSkill, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_MCQ)
    prompt = models.TextField()
    choices = models.JSONField(default=list, blank=True, help_text='MCQ only: ["option A", "option B", ...]')
    correct_index = models.PositiveSmallIntegerField(null=True, blank=True)
    rubric = models.JSONField(
        default=list, blank=True,
        help_text='Case: list of concept groups, e.g. [["npv","net present value"],["discount"]]. Practical: list of '
                  '{"criterion": "...", "points": 2, "terms": ["indicative", "words"]}.',
    )
    DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD = "easy", "medium", "hard"
    DIFFICULTY_CHOICES = [(DIFFICULTY_EASY, _("Easy")), (DIFFICULTY_MEDIUM, _("Medium")), (DIFFICULTY_HARD, _("Hard"))]
    DIFFICULTY_ORDER = {DIFFICULTY_EASY: 0, DIFFICULTY_MEDIUM: 1, DIFFICULTY_HARD: 2}

    difficulty = models.CharField(max_length=6, choices=DIFFICULTY_CHOICES, default=DIFFICULTY_MEDIUM)
    code_snippet = models.TextField(blank=True, help_text="Optional code shown under the question (technical skills).")
    topic = models.CharField(max_length=160, blank=True, help_text="Specific concept tested; used for improvement feedback.")
    time_limit_seconds = models.PositiveSmallIntegerField(default=45, validators=[MinValueValidator(30), MaxValueValidator(1800)],
                                                          help_text="30–60 s for multiple choice and case; 300–1800 s for practical.")
    weight = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    # AI-generated questions enter the bank unreviewed; a second model re-solves them (quizgen.verify_items):
    # key_verified None = not checked yet, True = the verifier agreed with the key, False = disputed (deactivated).
    reviewed = models.BooleanField(default=True, help_text="Checked by staff. AI-generated questions start unreviewed.")
    key_verified = models.BooleanField(null=True, blank=True, help_text="Answer key re-solved by a second AI pass.")

    def __str__(self):
        return f"[{self.skill.name}/{self.sub_skill.name}] {self.prompt[:60]}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.sub_skill_id and self.skill_id and self.sub_skill.skill_id != self.skill_id:
            raise ValidationError("Sub-skill must belong to the selected skill.")
        low, high = self.PRACTICAL_SECONDS if self.kind == self.KIND_PRACTICAL else self.QUICK_SECONDS
        if not low <= (self.time_limit_seconds or 0) <= high:
            raise ValidationError(f"Time limit for this kind must be {low}–{high} seconds.")
        if self.kind == self.KIND_MCQ:
            if len(self.choices or []) < 2 or self.correct_index is None or self.correct_index >= len(self.choices):
                raise ValidationError("MCQ needs ≥2 choices and a valid correct_index.")
        elif self.kind == self.KIND_PRACTICAL:
            from .review import criteria

            rows = self.rubric or []
            if not rows or len(criteria(self)) != len(rows) or not all(isinstance(r.get("terms", []), list) for r in rows):
                raise ValidationError('Practical items need rubric criteria: [{"criterion": "...", "points": 2, "terms": [...]}] '
                                      "with whole-number points above 0.")
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
    cv_summary = models.CharField(max_length=400, blank=True)
    top_domains = models.JSONField(default=list, blank=True)
    last_analysis = models.ForeignKey("CVAnalysis", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return f"Skill profile: {self.student}"


class StudentSkill(models.Model):
    SOURCE_EXPLICIT = "explicit"
    SOURCE_IMPLICIT = "implicit"
    SOURCE_CHOICES = [(SOURCE_EXPLICIT, _("Explicit")), (SOURCE_IMPLICIT, _("Implicit (inferred)"))]

    STATUS_CLAIMED = "claimed"
    STATUS_VERIFIED = "verified"
    STATUS_LAG = "lag"
    STATUS_REVIEW = "review"  # passed the automatic checks; a reviewer confirms before it counts as verified
    STATUS_CHOICES = [(STATUS_CLAIMED, _("Claimed")), (STATUS_VERIFIED, _("Verified")), (STATUS_LAG, _("Skill lag detected")),
                      (STATUS_REVIEW, _("Under review"))]

    VIA_CHALLENGE = "challenge"
    VIA_FREELANCE = "freelance"
    VIA_CERTIFICATE = "certificate"
    VIA_COURSE = "course"
    VIA_CHOICES = [(VIA_CHALLENGE, _("Skill challenge")), (VIA_FREELANCE, _("Reviewed freelance work")),
                   (VIA_CERTIFICATE, _("Reviewed certificate")), (VIA_COURSE, _("Completed accredited course"))]

    # Where a claimed skill comes from. "Verified" is never a provenance: it is the status (+ verified_via) set by a
    # challenge, reviewed work, certificate or course. Inferred skills are AI suggestions the student confirmed.
    PROVENANCE_DECLARED = "declared"    # the CV names the skill (or one of its aliases)
    PROVENANCE_EVIDENCED = "evidenced"  # the CV describes work that shows it (curated taxonomy signals)
    PROVENANCE_INFERRED = "inferred"    # an AI suggestion with a located evidence span, confirmed by the student
    PROVENANCE_CHOICES = [(PROVENANCE_DECLARED, _("Declared in your CV")), (PROVENANCE_EVIDENCED, _("Shown by your experience")),
                          (PROVENANCE_INFERRED, _("Suggested by AI"))]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="skills")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="student_skills")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    evidence = models.CharField(max_length=300, blank=True)
    provenance = models.CharField(max_length=10, choices=PROVENANCE_CHOICES, default=PROVENANCE_DECLARED)
    confidence = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)],
                                                  help_text="0-100: how sure the extraction is (not a verification).")
    years_experience = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_CLAIMED)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    percentile = models.PositiveSmallIntegerField(null=True, blank=True)
    lag_sub_skills = models.JSONField(default=list, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_via = models.CharField(max_length=12, choices=VIA_CHOICES, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("student", "skill")
        ordering = ["-status", "skill__name"]
        indexes = [
            # Talent Pool: verified skills → student ids (internest_skills.verification), and the min-score filter.
            models.Index(fields=["status", "student"], name="studentskill_status_idx"),
            models.Index(fields=["score", "student"], name="studentskill_score_idx"),
            # Leaderboard rank / percentile: COUNTs of peers with a higher score on the same skill.
            models.Index(fields=["skill", "score"], name="studentskill_skill_score_idx"),
        ]

    def __str__(self):
        return f"{self.student} · {self.skill} ({self.status})"

    @property
    def is_verified(self):
        return self.status == self.STATUS_VERIFIED

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

    @property
    def cooldown_hours_remaining(self):
        """Whole hours left, rounded up (shown as "retake in X hours")."""
        return -(-self.cooldown_seconds_remaining // 3600)

    def start_cooldown(self):
        self.cooldown_until = timezone.now() + RETEST_COOLDOWN


class ChallengeAttempt(models.Model):
    STATE_ACTIVE = "active"
    STATE_COMPLETED = "completed"
    STATE_TERMINATED = "terminated"
    STATE_CHOICES = [(STATE_ACTIVE, "Active"), (STATE_COMPLETED, "Completed"), (STATE_TERMINATED, "Terminated (integrity)")]

    MAX_FOCUS_WARNINGS = 2
    MAX_FLAGGED_RESPONSES = 2

    REVIEW_NONE, REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED = "", "pending", "approved", "rejected"
    REVIEW_CHOICES = [(REVIEW_NONE, "Not needed"), (REVIEW_PENDING, "Waiting for review"), (REVIEW_APPROVED, "Approved"),
                      (REVIEW_REJECTED, "Rejected")]

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
    # Human review (internest_skills/review.py): signals that sent the attempt to a reviewer, and the decision.
    review_status = models.CharField(max_length=10, choices=REVIEW_CHOICES, default=REVIEW_NONE, blank=True, db_index=True)
    review_reasons = models.JSONField(default=list, blank=True)
    reviewed_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=500, blank=True)

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
    review_credit = models.FloatField(null=True, blank=True)  # set by a reviewer; replaces `credit` in the final score
    ai_suggestion = models.JSONField(default=dict, blank=True)  # advisory only (review.ai_suggestion)

    class Meta:
        unique_together = ("attempt", "item")

    @property
    def final_credit(self):
        return self.credit if self.review_credit is None else self.review_credit


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


class SkillRelation(models.Model):
    """Semantic relatedness between two skills (0–1), used by the Market Readiness Gate.

    Stored once per pair (lookups check both directions). Sources: curated seed data, AI, or name heuristics.
    """

    SOURCE_CHOICES = [("seed", "Seed"), ("ai", "AI"), ("admin", "Admin")]

    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="relations_out")
    related = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="relations_in")
    weight = models.FloatField(validators=[MinValueValidator(0.0), MaxValueValidator(1.0)])
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="admin")

    class Meta:
        unique_together = ("skill", "related")

    def __str__(self):
        return f"{self.skill} ↔ {self.related} ({self.weight:.2f})"


class StudentFreelanceGig(models.Model):
    """Freelance work a student imported (Mostaql, Khamsat, Upwork, Fiverr, portfolio) as proof of skills.

    The AI reads the pasted description/review; the skills become Verified once the gig is approved
    (by an admin, or immediately when settings.FREELANCE_AUTO_VERIFY is on).
    """

    PLATFORM_CHOICES = [("mostaql", "Mostaql | مستقل"), ("khamsat", "Khamsat | خمسات"), ("upwork", "Upwork"),
                        ("fiverr", "Fiverr"), ("github", "GitHub"), ("portfolio", _("Portfolio link"))]
    STATUS_PENDING, STATUS_NEEDS_PROOF = "pending", "needs_proof"
    STATUS_VERIFIED, STATUS_REJECTED = "verified", "rejected"
    STATUS_CHOICES = [(STATUS_PENDING, _("Under review")), (STATUS_NEEDS_PROOF, _("Needs proof review")),
                      (STATUS_VERIFIED, _("Verified")), (STATUS_REJECTED, _("Not accepted"))]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="freelance_gigs")
    platform = models.CharField(max_length=12, choices=PLATFORM_CHOICES)
    link = models.URLField(max_length=300)
    # Handle parsed from the link and whether it matches the student's name / email / LinkedIn / GitHub.
    platform_username = models.CharField(max_length=100, blank=True)
    owner_matched = models.BooleanField(default=False)
    raw_text = models.TextField(max_length=6000)
    proof = models.FileField(upload_to=RandomName("freelance_proofs"), blank=True, validators=[
        FileExtensionValidator(["png", "jpg", "jpeg", "webp", "pdf"]), validate_max_file_size, *UPLOAD_VALIDATORS])
    project_title = models.CharField(max_length=160, blank=True)
    client_feedback = models.TextField(max_length=1000, blank=True)
    rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    deliverables = models.JSONField(default=list, blank=True)
    skills = models.ManyToManyField(Skill, blank=True, related_name="freelance_gigs")
    ai_parsed = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    review_note = models.CharField(max_length=300, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_platform_display()} · {self.project_title or self.link}"


class Course(models.Model):
    """Course recommended for a skill gap: partner courses first, open (internet) courses as the fallback."""

    title = models.CharField(_("Title"), max_length=200)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="courses", verbose_name=_("Skill"))
    provider_name = models.CharField(_("Provider"), max_length=120)
    url = models.URLField(_("Link"), max_length=500)
    is_partner = models.BooleanField(_("Certified partner"), default=False, db_index=True,
                                     help_text="Partner courses are recommended first and show the “Certified partner” badge.")
    priority = models.IntegerField(_("Priority"), default=0, help_text="Higher numbers are shown first.")
    discount_note = models.CharField(_("Discount note"), max_length=160, blank=True,
                                     help_text="Shown on partner course cards, e.g. “20% off with code INTERNEST20”.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_partner", "-priority", "title"]
        indexes = [models.Index(fields=["skill", "is_partner", "-priority"], name="course_skill_partner_idx")]

    def __str__(self):
        return f"{self.title} ({self.provider_name})"


class SkillCertificate(models.Model):
    """Certificate a student adds to prove a skill. Under review until an admin checks it; then the skill is Verified."""

    STATUS_UNDER_REVIEW, STATUS_VERIFIED, STATUS_REJECTED = "under_review", "verified", "rejected"
    STATUS_CHOICES = [(STATUS_UNDER_REVIEW, _("Under review")), (STATUS_VERIFIED, _("Verified")), (STATUS_REJECTED, _("Not accepted"))]

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="skill_certificates")
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="certificates")
    title = models.CharField(_("Certificate title"), max_length=160)
    issuer = models.CharField(_("Issuer"), max_length=120, blank=True)
    url = models.URLField(_("Verification link"), max_length=500, blank=True)
    file = models.FileField(_("Certificate file"), upload_to=RandomName("skill_certificates"), blank=True, validators=[
        FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "webp"]), validate_max_file_size, *UPLOAD_VALIDATORS])
    trusted_issuer = models.BooleanField(default=False, help_text="The link is on a known certificate-verification site.")
    status = models.CharField(max_length=14, choices=STATUS_CHOICES, default=STATUS_UNDER_REVIEW, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} · {self.skill} ({self.get_status_display()})"


class SkillSuggestion(models.Model):
    """A skill name an AI reply used that is not in the taxonomy. AI output never creates Skill rows (audit M1):
    staff approve a suggestion (it becomes an active Skill), merge it into an existing skill (its name becomes an
    alias) or reject it, in the Django admin."""

    STATUS_PENDING, STATUS_APPROVED, STATUS_MERGED, STATUS_REJECTED = "pending", "approved", "merged", "rejected"
    STATUS_CHOICES = [(STATUS_PENDING, _("Waiting for review")), (STATUS_APPROVED, _("Approved")), (STATUS_MERGED, _("Merged")),
                      (STATUS_REJECTED, _("Rejected"))]

    key = models.CharField(max_length=100, unique=True, editable=False)  # taxonomy.normalize_key(name)
    name = models.CharField(max_length=100)
    discipline = models.CharField(max_length=20, choices=Discipline.choices, default=Discipline.GENERAL)
    seen_count = models.PositiveIntegerField(default=1)
    example = models.CharField(max_length=300, blank=True, help_text="An evidence snippet from the text it was found in.")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    merge_into = models.ForeignKey(Skill, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                   help_text="Pick the existing skill this name means, then run the Merge action.")
    skill = models.ForeignKey(Skill, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-seen_count", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_status_display()}, x{self.seen_count})"


class CVAnalysis(models.Model):
    """One CV / LinkedIn / pasted-text analysis (internest_skills.cv_analysis). `facts` come from the deterministic
    parser (cv_facts), `ai` holds the AI suggestions kept apart from them; the raw text is never stored here."""

    STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED = "queued", "running", "done", "failed"
    STATUS_CHOICES = [(STATUS_QUEUED, _("Queued")), (STATUS_RUNNING, _("Analyzing")), (STATUS_DONE, _("Done")),
                      (STATUS_FAILED, _("Failed"))]
    ACTIVE = (STATUS_QUEUED, STATUS_RUNNING)
    SOURCE_CHOICES = [("cv", _("CV file")), ("linkedin", _("LinkedIn PDF")), ("text", _("Pasted text")),
                      ("mixed", _("Several sources"))]

    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="cv_analyses")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="cv")
    file_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    facts = models.JSONField(default=dict, blank=True)
    ai = models.JSONField(default=dict, blank=True)
    quality_score = models.PositiveSmallIntegerField(null=True, blank=True)
    counted = models.BooleanField(default=False, editable=False)  # took one of the student's daily analyses
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"], name="cvanalysis_user_created_idx"),
                   models.Index(fields=["user", "file_sha256"], name="cvanalysis_user_sha_idx")]
        verbose_name_plural = "CV analyses"

    def __str__(self):
        return f"CV analysis {self.pk} · {self.user} ({self.status})"

    @property
    def is_active(self):
        return self.status in self.ACTIVE
