import secrets
import uuid
from decimal import Decimal
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .upload_security import UPLOAD_VALIDATORS, RandomName


MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
OTP_EXPIRY_MINUTES = 30


def validate_max_file_size(file):
    if file and file.size > MAX_UPLOAD_SIZE:
        mb = MAX_UPLOAD_SIZE // (1024 * 1024)
        raise ValidationError(f"File size must not exceed {mb} MB.")


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"



# === 1. نموذج ملف الطالب الموحد (StudentProfile) ===
class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    
    # 🚀 التعديل 1: إضافة حقل الإيميل الشخصي (مطلوب)
    personal_email = models.EmailField(
        unique=True, 
        verbose_name="البريد الإلكتروني الشخصي",
        null=True, 
        blank=True 
    )
    
    university_email = models.EmailField(
        unique=True,
        blank=True,
        null=True,
        verbose_name="البريد الإلكتروني الجامعي"
    )

    personal_email_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ توثيق البريد الشخصي")
    university_email_verified_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ توثيق البريد الجامعي")

    @property
    def is_personal_email_verified(self) -> bool:
        return self.personal_email_verified_at is not None

    @property
    def is_university_email_verified(self) -> bool:
        return self.university_email_verified_at is not None

    @property
    def has_any_pending_email_verification(self) -> bool:
        if self.personal_email and not self.is_personal_email_verified:
            return True
        if self.university_email and not self.is_university_email_verified:
            return True
        return False
    # ... (بقية حقول StudentProfile) ...
    university = models.CharField(max_length=100, blank=True, null=True, verbose_name="الجامعة")
    major = models.CharField(max_length=100, blank=True, null=True, verbose_name="التخصص")
    study_level = models.CharField(max_length=50, choices=[('1', 'سنة أولى'), ('2', 'سنة ثانية'), ('3', 'سنة ثالثة'), ('4', 'سنة رابعة/خريج')], blank=True, null=True, verbose_name="المستوى الدراسي")
    phone_number = models.CharField(max_length=20, blank=True, null=True, verbose_name="رقم الهاتف")
    # Shown to startups before the CV/contacts unlock (skills-first review).
    bio = models.TextField(max_length=600, blank=True, default="", verbose_name=_("About me & projects"))
    portfolio_url = models.URLField(blank=True, default="", verbose_name=_("Portfolio link"))
    # Explicit opt-in to appear in the Pro Talent Pool (name, studies and verified skills only — never contacts).
    talent_pool_visible = models.BooleanField(default=False, db_index=True, verbose_name=_("Show me in the Talent Pool"))
    # Bulk "new opportunity" emails only; account emails (verification, shortlist, invitations) are always sent.
    email_new_opportunities = models.BooleanField(default=True, verbose_name=_("Email me about new opportunities"))
    # In-app notices still to show (keys from internest_core/notices.py). Lives on the profile every student page already
    # loads, so showing a notice costs no query.
    pending_notices = models.JSONField(default=list, blank=True, editable=False)

    class Meta:
        indexes = [
            # Talent Pool filters; partial indexes only cover opted-in students (small and exact).
            models.Index(fields=["university"], condition=Q(talent_pool_visible=True), name="student_pool_university_idx"),
            models.Index(fields=["major"], condition=Q(talent_pool_visible=True), name="student_pool_major_idx"),
        ]
    cv_file = models.FileField(
        upload_to=RandomName('cvs'), blank=True, null=True,
        validators=[
            FileExtensionValidator(['pdf', 'doc', 'docx']),
            validate_max_file_size,
            *UPLOAD_VALIDATORS,
        ],
        verbose_name="ملف السيرة الذاتية (CV)",
    )
    profile_picture = models.ImageField(
        upload_to=RandomName('profile_pics'), blank=True, null=True,
        validators=[
            FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp']),
            validate_max_file_size,
            *UPLOAD_VALIDATORS,
        ],
        verbose_name="صورة الملف الشخصي",
    )
    linkedin_url = models.URLField(max_length=200, blank=True, null=True, verbose_name="رابط LinkedIn")

    profile_completion_score = models.IntegerField(default=0, verbose_name="نسبة الإكمال (%)")

    def calculate_completion(self):
        score = 0
        if (self.university and self.university.strip()) or (self.major and self.major.strip()): score += 20
        if self.study_level: score += 20
        if self.cv_file: score += 20
        if self.profile_picture: score += 20
        if self.linkedin_url and self.linkedin_url.startswith('http'): score += 20
        self.profile_completion_score = score
        self.save()
    
    def __str__(self):
        return self.user.username

# === 4. نموذج الشريك الموحد (PartnerProfile) ===
STATUS_CHOICES_SUBMISSION = [
    ('Pending', 'قيد المراجعة'),
    ('Approved', 'تمت الموافقة'),
    ('Rejected', 'تم الرفض'),
]

class PartnerProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE) 
    company_name = models.CharField(max_length=200, unique=True, verbose_name="اسم الشركة/الجهة")
    # ISO country of the commercial register (startup onboarding). Sets the Pro pricing currency; locked once verified.
    country_of_registration = models.CharField(max_length=2, blank=True, default="", verbose_name=_("Country of registration"))
    # "Other" (XX) country: the name the company typed, and whether it is in Europe (EUR pricing) or not (USD).
    custom_country = models.CharField(max_length=80, blank=True, default="", verbose_name=_("Country name"))
    custom_country_in_europe = models.BooleanField(default=False, verbose_name=_("This country is in Europe"))

    @property
    def pricing_currency(self):
        """EGP for Egypt, EUR for Europe (listed or a European "Other"), USD everywhere else."""
        from internest_startups.pricing import currency_for

        return currency_for(self.country_of_registration, in_europe=self.custom_country_in_europe)
    partner_code = models.CharField(max_length=50, unique=True, help_text="الكود السري للدخول المخصص.")
    logo = models.ImageField(
        upload_to='partner_logos/', null=True, blank=True,
        validators=[
            FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp']),  # no SVG: it can carry script
            validate_max_file_size,
            *UPLOAD_VALIDATORS,
        ],
        verbose_name="شعار الشركة/الجهة",
    )
    
    is_academic = models.BooleanField(default=False, verbose_name="هل هي جهة أكاديمية؟")
    official_website = models.URLField(max_length=200, blank=True, null=True, verbose_name="الموقع الرسمي")
    official_email = models.EmailField(blank=True, null=True, verbose_name="البريد الإلكتروني الرسمي")
    official_phone = models.CharField(max_length=20, blank=True, null=True, verbose_name="رقم التواصل الرسمي")
    linkedin_url = models.URLField(max_length=200, blank=True, null=True)
    facebook_url = models.URLField(max_length=200, blank=True, null=True)
    twitter_url = models.URLField(max_length=200, blank=True, null=True)
    instagram_url = models.URLField(max_length=200, blank=True, null=True)
    contract_expiry_date = models.DateField(blank=True, null=True, verbose_name="تاريخ انتهاء التعاقد (يحددها الأدمن)")
    is_fully_verified = models.BooleanField(default=False, verbose_name="حساب موثوق (علامة الصح الزرقاء)")
    profile_completion_score = models.IntegerField(default=0, verbose_name="نسبة الإكمال (%)")

    def calculate_completion(self):
        if not self.is_academic:
            # Startups: self-service completion from their own fields, independent of admin verification.
            from internest_startups.completion import startup_completion  # startups app depends on core
            self.profile_completion_score = startup_completion(self)
            self.save(update_fields=["profile_completion_score"])
            return

        # 1. حساب النسبة بناءً على البيانات التي يملأها الشريك
        required_fields = [self.official_phone, self.official_email, self.official_website, self.linkedin_url, self.logo]
        completed_fields = sum(1 for field in required_fields if field)
        calculated_score = int((completed_fields / 5) * 100)
        
        # 🚀 التعديل: تطبيق المنطق الجديد
        if self.is_fully_verified:
            # 2. إذا تم توثيقه من المدير، تصبح النسبة 100% بغض النظر عن الحقول
            self.profile_completion_score = 100
        else:
            # 3. إذا لم يوثق، نستخدم النسبة المحسوبة من البيانات
            self.profile_completion_score = calculated_score
            
        self.save()

    def __str__(self):
        return self.company_name

# === 2. نموذج فرص التدريب (Internship) ===
ONLINE_LOCATION_KEYWORDS = ("online", "remote", "عن بعد", "عن بُعد", "أونلاين", "اونلاين")


def opportunity_today():
    """Deadlines are calendar dates (Cairo time): an opportunity is open through its deadline day."""
    return timezone.localdate()


class InternshipQuerySet(models.QuerySet):
    """One definition of "open" for every student-facing list, so an expired post can never leak back in."""

    def open(self):
        return self.filter(is_active=True, deadline__gte=opportunity_today())

    def expired(self):
        return self.filter(is_active=True, deadline__lt=opportunity_today())

    def closed(self):
        return self.filter(is_active=False)


class Internship(models.Model):
    STATUS_OPEN, STATUS_EXPIRED, STATUS_CLOSED = "open", "expired", "closed"

    objects = InternshipQuerySet.as_manager()
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="internships", verbose_name="الشريك (الشركة)")
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=100)
    required_majors = models.CharField(max_length=255) 
    deadline = models.DateField()
    is_premium = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)  # False: closed by the company (position filled) or by an admin
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    @property
    def is_open(self):
        """Accepting applicants: not closed and the deadline hasn't passed."""
        return self.is_active and not self.is_expired
    
    @property
    def is_online(self):
        """Remote/online opportunity (no physical address → no map)."""
        loc = (self.location or "").strip().lower()
        return not loc or any(k in loc for k in ONLINE_LOCATION_KEYWORDS)

    @property
    def is_expired(self):
        """The deadline day is over (computed live: nothing to run for a post to expire)."""
        return self.deadline < opportunity_today()

    @property
    def status(self):
        """open / expired (deadline passed) / closed (by the company or an admin) for dashboards and admin."""
        if not self.is_active:
            return self.STATUS_CLOSED
        return self.STATUS_EXPIRED if self.is_expired else self.STATUS_OPEN
        
    @property
    def applicants_count(self):
        """حساب عدد المتقدمين للفرصة."""
        return self.application_set.count()

    class Meta:
        indexes = [
            # Internship.objects.open() (is_active + deadline >= today) feeds every student list and the dashboards.
            # Partial index: on SQLite Django writes is_active=True as a bare "is_active" term, which a composite
            # (is_active, deadline) index cannot use (the feed was a full table scan, Phase 8 query-plan check).
            models.Index(fields=["deadline"], condition=models.Q(is_active=True), name="internship_open_deadline_idx"),
            models.Index(fields=["partner", "-created_at"], name="internship_partner_idx"),
        ]

    def __str__(self):
        return self.title

# === 3. نموذج طلبات التقديم (Application) ===
class Application(models.Model):
# ... (بقية Application Model) ...
    internship = models.ForeignKey(Internship, on_delete=models.CASCADE)
    applicant = models.ForeignKey(User, on_delete=models.CASCADE)
    application_date = models.DateTimeField(auto_now_add=True)
    # Empathy-driven lifecycle: students never see "rejected" — closed/declined shows as "fulfilled".
    STATUS_SUBMITTED = "submitted"
    STATUS_UNDER_REVIEW = "under_review"
    STATUS_SHORTLISTED = "shortlisted"
    STATUS_ACCEPTED = "accepted"
    STATUS_FULFILLED = "fulfilled"
    STATUS_COMPLETED = "completed"  # the gig was delivered and rated by the startup
    STATUS_CHOICES = [
        (STATUS_SUBMITTED, _("Submitted")),
        (STATUS_UNDER_REVIEW, _("Under review")),
        (STATUS_SHORTLISTED, _("Shortlisted for interview")),
        (STATUS_ACCEPTED, _("Accepted")),
        (STATUS_FULFILLED, _("Position filled")),
        (STATUS_COMPLETED, _("Gig completed")),
    ]
    status = models.CharField(max_length=50, default=STATUS_SUBMITTED, choices=STATUS_CHOICES)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    shortlisted_at = models.DateTimeField(null=True, blank=True)
    SOURCE_DIRECT = "direct"
    SOURCE_INVITED = "invited"  # applied through a Talent Pool invitation link (skill match waived)
    SOURCE_CHOICES = [(SOURCE_DIRECT, _("Direct")), (SOURCE_INVITED, _("Invited"))]
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_DIRECT)
    # When the student agreed that their CV and contact details are shared once shortlisted.
    contact_consent_at = models.DateTimeField(null=True, blank=True)

    # Still being decided: counted on the dashboard's active-applicants badge.
    ACTIVE_STATUSES = (STATUS_SUBMITTED, STATUS_UNDER_REVIEW, STATUS_SHORTLISTED)

    # Skills-first funnel: the CV and contact details stay locked until the startup shortlists.
    CONTACT_UNLOCKED_STATUSES = (STATUS_SHORTLISTED, STATUS_ACCEPTED, STATUS_COMPLETED)

    class Meta:
        unique_together = ('internship', 'applicant')
        indexes = [
            models.Index(fields=["applicant", "-application_date"], name="application_student_idx"),  # My applications
            models.Index(fields=["internship", "status"], name="application_intern_status_idx"),  # applicant inbox
        ]

    @property
    def contact_unlocked(self):
        return self.status in self.CONTACT_UNLOCKED_STATUSES

    @property
    def progress_step(self):
        """1–4 on the student's stepper (submitted → under review → shortlisted → accepted); 0 when filled."""
        if self.status == self.STATUS_COMPLETED:
            return 4
        order = [self.STATUS_SUBMITTED, self.STATUS_UNDER_REVIEW, self.STATUS_SHORTLISTED, self.STATUS_ACCEPTED]
        return order.index(self.status) + 1 if self.status in order else 0

    @property
    def student_status_label(self):
        """Status wording on the student's side."""
        if self.status == self.STATUS_SHORTLISTED:
            return _("You're shortlisted for an interview 🎉")
        return self.get_status_display()

    def mark_viewed(self):
        """The startup opened the applicant's file: Submitted → Under review (never moves backwards)."""
        if self.status == self.STATUS_SUBMITTED:
            self.status, self.reviewed_at = self.STATUS_UNDER_REVIEW, timezone.now()
            self.save(update_fields=["status", "reviewed_at"])

    def __str__(self):
        return f"Application by {self.applicant.username} for {self.internship.title}"

# === 5. نماذج تقديم المحتوى من الشركاء (Submissions) ===
class PartnerInternshipSubmission(models.Model):
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField()
    
    duration = models.CharField(
        max_length=50, 
        verbose_name="مدة التدريب",
        null=True, 
        blank=True 
    ) 
    
    location = models.CharField(max_length=100)
    required_majors = models.CharField(max_length=255) 
    deadline = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES_SUBMISSION, default='Pending')
    submission_date = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [  # dashboard: pending count + this month's posts (free-plan quota)
            models.Index(fields=["partner", "status"], name="internsub_partner_status_idx"),
            models.Index(fields=["partner", "submission_date"], name="internsub_partner_date_idx"),
        ]

    def __str__(self):
        return f"تقديم تدريب: {self.title} من {self.partner.company_name} ({self.status})"

class PartnerCourseSubmission(models.Model):
# ... (بقية PartnerCourseSubmission Model) ...
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE)
    title = models.CharField(max_length=150)
    description = models.TextField(help_text="وصف تفصيلي للكورس.")
    price = models.DecimalField(max_digits=6, decimal_places=2, help_text="سعر الكورس (لأغراض الإيرادات).")
    instructor_name = models.CharField(max_length=100, help_text="اسم مقدم الكورس.")
    video_link = models.URLField(blank=True, null=True, help_text="رابط ترويجي/صورة/فيديو للكورس.")
    # The skill this course teaches: completing it (confirmed by the partner or an admin) verifies the skill
    # automatically when the partner is accredited (fully verified).
    skill = models.ForeignKey("internest_skills.Skill", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="partner_courses", verbose_name=_("Skill taught"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES_SUBMISSION, default='Pending')
    submission_date = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["partner", "status"], name="coursesub_partner_status_idx")]

    def __str__(self):
        return f"كورس {self.title} من {self.partner.company_name} ({self.status})"

# === 6. نموذج بيانات المتقدمين (صندوق وارد الشريك) ===
class PartnerApplicantData(models.Model):
# ... (بقية PartnerApplicantData Model) ...
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="received_applicants", verbose_name="الشريك المستقبل")
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, verbose_name="ملف الطالب")
    internship = models.ForeignKey(Internship, on_delete=models.CASCADE, verbose_name="للتدريب")
    application = models.OneToOneField(Application, on_delete=models.CASCADE, verbose_name="الطلب الأصلي")
    forwarded_on = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ الإرسال")
    
    class Meta:
        unique_together = ('partner', 'student', 'internship')
        verbose_name = "ملف متقدم مُرسل للشريك"
        verbose_name_plural = "ملفات المتقدمين المُرسلة"

    def __str__(self):
        return f"ملف {self.student.user.username} أُرسل إلى {self.partner.company_name}"

# ---------------------------------
# --- (✨ 8. مـوديـلات الـتـاسـكـات (جـديـدة) ✨) ---
# ---------------------------------

class GamificationTask(models.Model):
# ... (بقية GamificationTask Model) ...
    title = models.CharField(max_length=200, verbose_name="عنوان التاسك")
    description = models.TextField(verbose_name="وصف التاسك")
    is_active = models.BooleanField(default=True, verbose_name="متاح للطلاب؟")
    
    class Meta:
        verbose_name = "مهمة (تاسك)"
        verbose_name_plural = "🎯 المهام (التاسكات)"
        
    def __str__(self):
        return self.title

class TaskQuestion(models.Model):
# ... (بقية TaskQuestion Model) ...
    task = models.ForeignKey(GamificationTask, on_delete=models.CASCADE, related_name="questions", verbose_name="التاسك التابع له")
    text = models.CharField(max_length=500, verbose_name="نص السؤال")
    
    class Meta:
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"
        
    def __str__(self):
        return self.text

class TaskAnswer(models.Model):
# ... (بقية TaskAnswer Model) ...
    question = models.ForeignKey(TaskQuestion, on_delete=models.CASCADE, related_name="answers", verbose_name="السؤال التابع له")
    text = models.CharField(max_length=255, verbose_name="نص الإجابة (الاختيار)")
    is_correct = models.BooleanField(default=False, verbose_name="هل هذه هي الإجابة الصحيحة؟")
    
    class Meta:
        verbose_name = "اختيار (إجابة)"
        verbose_name_plural = "الاختيارات (الإجابات)"
        
    def __str__(self):
        return f"{self.question.text} -> {self.text} ({self.is_correct})"

class StudentTaskRecord(models.Model):
# ... (بقية StudentTaskRecord Model) ...
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="task_records")
    task = models.ForeignKey(GamificationTask, on_delete=models.CASCADE, related_name="submissions")
    completed_on = models.DateTimeField(auto_now_add=True)
    correct = models.PositiveSmallIntegerField(default=0)
    total = models.PositiveSmallIntegerField(default=0)

    @property
    def mastery(self) -> int:
        """Share of correct answers, 0–100."""
        return round(self.correct * 100 / self.total) if self.total else 0
    
    class Meta:
        verbose_name = "سجل إكمال مهمة"
        verbose_name_plural = "سجلات إكمال المهام"
        unique_together = ('student', 'task') # (يمنع الطالب من حل التاسك مرتين)
        
    def __str__(self):
        return f"{self.student.user.username} أكمل {self.task.title}"
    

# ---------------------------------
# --- (✨ 9. مـوديـلات نـظـام الـدفـع (جـديـدة) ✨) ---
# ---------------------------------

class DiscountCode(models.Model):
    code = models.CharField(max_length=50, unique=True, verbose_name="الكود")
    discount_percentage = models.PositiveIntegerField(
        verbose_name="نسبة الخصم (%)",
        help_text="رقم من 1 إلى 100",
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    is_active = models.BooleanField(default=True, verbose_name="فعال؟")
    valid_until = models.DateField(null=True, blank=True, verbose_name="ينتهي في")
    max_uses = models.PositiveIntegerField(null=True, blank=True, verbose_name="أقصى عدد استخدامات")
    times_used = models.PositiveIntegerField(default=0, verbose_name="عدد مرات الاستخدام")

    def is_valid(self):
        if not self.is_active:
            return False
        if self.valid_until and self.valid_until < timezone.now().date():
            return False
        if self.max_uses is not None and self.times_used >= self.max_uses:
            return False
        return True
    
    class Meta:
        verbose_name = "كود خصم"
        verbose_name_plural = "🏷️ أكواد الخصم"
        
    def __str__(self):
        return f"{self.code} ({self.discount_percentage}%)"

# ---------------------------------
# --- (✨ Email verification OTP model ✨) ---
# ---------------------------------

class EmailVerification(models.Model):
    EMAIL_TYPE_PERSONAL = 'personal'
    EMAIL_TYPE_UNIVERSITY = 'university'
    EMAIL_TYPE_CHOICES = [
        (EMAIL_TYPE_PERSONAL, 'Personal email'),
        (EMAIL_TYPE_UNIVERSITY, 'University email'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='email_verifications')
    email = models.EmailField()
    email_type = models.CharField(max_length=20, choices=EMAIL_TYPE_CHOICES)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name = "Email verification"
        verbose_name_plural = "Email verifications"
        indexes = [
            models.Index(fields=['user', 'email_type', 'verified_at']),
            models.Index(fields=['code']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.email_type} OTP for {self.user.username} → {self.email}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.verified_at is not None

    def is_usable(self) -> bool:
        return not self.is_consumed and not self.is_expired and self.attempts < 5

    @classmethod
    def issue(cls, user, email: str, email_type: str, expiry_minutes: int = OTP_EXPIRY_MINUTES):
        """Invalidate previous open codes for this slot, then issue a fresh one."""
        cls.objects.filter(
            user=user,
            email_type=email_type,
            verified_at__isnull=True,
        ).delete()
        return cls.objects.create(
            user=user,
            email=email,
            email_type=email_type,
            code=generate_otp_code(),
            expires_at=timezone.now() + timedelta(minutes=expiry_minutes),
        )

    def mark_verified(self):
        self.verified_at = timezone.now()
        self.save(update_fields=['verified_at'])


class StudentEnrollment(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrolled_courses", verbose_name="الطالب")
    course = models.ForeignKey(PartnerCourseSubmission, on_delete=models.CASCADE, related_name="enrollments", verbose_name="الكورس")
    enrolled_on = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ التسجيل")
    final_price = models.DecimalField(max_digits=6, decimal_places=2, verbose_name="السعر المدفوع")
    # Completion is confirmed by the course's partner (or an admin), never by the student.
    STATUS_ENROLLED, STATUS_COMPLETED = "enrolled", "completed"
    STATUS_CHOICES = [(STATUS_ENROLLED, _("Enrolled")), (STATUS_COMPLETED, _("Completed"))]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ENROLLED, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    certificate_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)  # public verification link

    @property
    def is_completed(self):
        return self.status == self.STATUS_COMPLETED

    class Meta:
        verbose_name = "تسجيل (كورس)"
        verbose_name_plural = "🧑‍🎓 تسجيلات الطلاب في الكورسات"
        unique_together = ('student', 'course') # (يمنع الطالب من شراء نفس الكورس مرتين)
        
    def __str__(self):
        return f"{self.student.user.username} مسجل في {self.course.title}"

# === Verified work experience (proof of work) ===
TOP_VERIFIED_MIN_GIGS = 3  # "Top Verified Candidate" badge + Talent Pool priority
VERIFIED_MIN_RATING = Decimal("3.5")  # below this: no letter, not counted as verified experience
RATING_CHOICES = [Decimal(n) / 2 for n in range(10, 1, -1)]  # 5.0, 4.5 … 1.0
# Letter tier by rating: (minimum, key, English title, Arabic title)
LETTER_TIERS = [
    (Decimal("5.0"), "excellence", "Letter of Recommendation & Excellence", "خطاب توصية وتقدير أداء متميز"),
    (Decimal("4.0"), "recommendation", "Letter of Completion & Recommendation", "شهادة إتمام وتوصية عمل"),
    (VERIFIED_MIN_RATING, "completion", "Certificate of Internship Completion", "شهادة إتمام مهمة تدريبية"),
]


class GigExperienceQuerySet(models.QuerySet):
    def verified(self):
        """Gigs that earn a letter and count as verified experience (rating ≥ 3.5)."""
        return self.filter(star_rating__gte=VERIFIED_MIN_RATING)


class GigExperience(models.Model):
    """A gig the startup marked as completed and rated. Rated ≥ 3.5 → PDF letter with a public QR check."""

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="gig_experiences")
    company = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="issued_experiences")
    application = models.OneToOneField(Application, on_delete=models.SET_NULL, null=True, blank=True, related_name="experience")
    gig_title = models.CharField(max_length=200)
    # Snapshot of the opportunity description when the gig was completed (what the student actually worked on).
    gig_description = models.TextField(blank=True, default="")
    completion_date = models.DateField()
    star_rating = models.DecimalField(max_digits=2, decimal_places=1,
                                      validators=[MinValueValidator(Decimal("1")), MaxValueValidator(Decimal("5"))])
    review_text = models.TextField(max_length=1000)
    verification_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pdf_file = models.FileField(upload_to="letters/", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = GigExperienceQuerySet.as_manager()

    class Meta:
        ordering = ["-completion_date", "-pk"]

    def __str__(self):
        return f"{self.student} · {self.gig_title} @ {self.company} ({self.rating_label}★)"

    @property
    def verification_code(self):
        return str(self.verification_uuid).split("-")[0].upper()

    @property
    def is_verified(self):
        return Decimal(self.star_rating) >= VERIFIED_MIN_RATING

    @property
    def tier(self):
        """(key, English title, Arabic title) for the letter, or None below 3.5."""
        rating = Decimal(self.star_rating)
        return next(((key, en, ar) for minimum, key, en, ar in LETTER_TIERS if rating >= minimum), None)

    @property
    def rating_label(self):
        """'4.5', '5' (no trailing .0)."""
        return f"{Decimal(self.star_rating):.1f}".removesuffix(".0")

    @property
    def star_icons(self):
        """['full', 'full', 'half', 'empty', 'empty'] for 2.5 — used by templates and the PDF."""
        rating = Decimal(self.star_rating)
        return ["full" if n <= rating else "half" if n - Decimal("0.5") <= rating else "empty" for n in range(1, 6)]


def proof_of_work(student):
    """{"count", "avg", "top"} for a student's verified gigs (rated ≥ 3.5)."""
    stats = student.gig_experiences.verified().aggregate(count=models.Count("id"), avg=models.Avg("star_rating"))
    count = stats["count"] or 0
    return {"count": count, "avg": round(float(stats["avg"] or 0), 1), "top": count >= TOP_VERIFIED_MIN_GIGS}


class UniversityInquiry(models.Model):
    """Demo / free-pilot request from the /universities/ page. Universities never buy online: we contact them."""

    STATUS_NEW, STATUS_CONTACTED, STATUS_PILOT, STATUS_CLOSED = "new", "contacted", "pilot", "closed"
    STATUS_CHOICES = [(STATUS_NEW, _("New")), (STATUS_CONTACTED, _("Contacted")), (STATUS_PILOT, _("Pilot agreed")),
                      (STATUS_CLOSED, _("Closed"))]
    CHANNEL_CHOICES = [("wa", "WhatsApp"), ("mail", _("Email"))]

    name = models.CharField(_("Name"), max_length=120)
    position = models.CharField(_("Position"), max_length=120)
    university = models.CharField(_("University"), max_length=160)
    faculty = models.CharField(_("Faculty"), max_length=160, blank=True)
    email = models.EmailField(_("Email"), blank=True)
    phone = models.CharField(_("Mobile number"), max_length=30, blank=True)
    message = models.TextField(_("Message"), max_length=2000, blank=True)
    channel = models.CharField(max_length=5, choices=CHANNEL_CHOICES, default="wa")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    notes = models.TextField(blank=True, help_text="Internal notes (not shown to the university).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "University inquiries"

    def __str__(self):
        return f"{self.university} · {self.name}"


# === External API health & billing (internest_core/api_registry.py is the source of truth for the descriptive fields) ===
class APIProvider(models.Model):
    """One external API. Descriptive fields are synced from the code registry; the budget numbers are edited in Django admin."""

    TRACKING_CHOICES = [("none", _("No tracking")), ("live", _("Live (provider balance endpoint)")), ("ledger", _("Local ledger (estimated spend)"))]

    key = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, default="other")
    role = models.TextField(blank=True, help_text=_("What this API does on Internest"))
    impact = models.TextField(blank=True, help_text=_("Which features break or degrade when it is down"))
    degraded_behavior = models.TextField(blank=True)
    env_var = models.CharField(max_length=80, blank=True, help_text=_(".env key that holds its secret"))
    code_location = models.CharField(max_length=255, blank=True)
    dashboard_url = models.URLField(blank=True, help_text=_("Console / billing page"))
    registered = models.BooleanField(default=True, help_text=_("False = seen in a call but never added to api_registry.py"))
    is_paid = models.BooleanField(default=False)
    tracking_mode = models.CharField(max_length=10, choices=TRACKING_CHOICES, default="none")
    currency = models.CharField(max_length=10, default="USD")
    # Ledger mode: remaining = initial_budget - estimated_spent. Edit initial_budget after every top-up.
    initial_budget = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"), help_text=_("Money you loaded; 0 = tracking off"))
    estimated_spent = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"))
    low_balance_threshold = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal("0"), help_text=_("Alert when remaining <= this"))
    cost_per_call = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"), help_text=_("Estimated cost of one request"))
    cost_per_1k_tokens = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"), help_text=_("Estimated cost per 1,000 tokens"))
    # Live mode: the last balance read from the provider.
    last_balance = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_check_result = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]
        verbose_name = _("API provider")
        verbose_name_plural = _("API providers")

    def __str__(self):
        return self.name

    @property
    def remaining_balance(self):
        """Money left, or None when it cannot be known (live mode not read yet, or no budget entered in ledger mode)."""
        if self.tracking_mode == "live":
            return self.last_balance
        if self.tracking_mode == "ledger" and self.initial_budget > 0:
            return self.initial_budget - self.estimated_spent
        return None

    @property
    def is_low(self):
        remaining = self.remaining_balance
        return remaining is not None and remaining <= self.low_balance_threshold


class APIIncident(models.Model):
    """Every API problem seen, whether or not it produced an email (the 30-minute cooldown suppresses repeats)."""

    provider = models.ForeignKey(APIProvider, on_delete=models.CASCADE, related_name="incidents")
    feature = models.CharField(max_length=160, blank=True)
    alert_type = models.CharField(max_length=20, db_index=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    detail = models.TextField(blank=True)
    failover_status = models.TextField(blank=True)
    emailed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("API incident")
        verbose_name_plural = _("API incidents")

    def __str__(self):
        return f"{self.provider} · {self.alert_type} · {self.created_at:%Y-%m-%d %H:%M}"


class AsyncTask(models.Model):
    """A heavy job (AI polish, voice transcription, quiz preparation) run off the request thread.

    The web request creates the row and answers 202 right away; the browser polls /api/tasks/<id>/status/.
    See internest_core/async_tasks.py for the backends (sync, thread, db worker, celery)."""

    STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED = "queued", "running", "done", "failed", "cancelled"
    STATUS_CHOICES = [(STATUS_QUEUED, "Queued"), (STATUS_RUNNING, "Running"), (STATUS_DONE, "Done"),
                      (STATUS_FAILED, "Failed"), (STATUS_CANCELLED, "Cancelled")]
    FINISHED = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="async_tasks")
    kind = models.CharField(max_length=60)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    payload = models.JSONField(default=dict, blank=True)    # arguments, only kept for out-of-process backends
    result = models.JSONField(null=True, blank=True)        # the JSON body the synchronous endpoint would have returned
    http_status = models.PositiveSmallIntegerField(default=200)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="asynctask_status_created_idx")]

    @property
    def is_finished(self):
        return self.status in self.FINISHED

    def __str__(self):
        return f"{self.kind} · {self.status} · {self.pk}"


class TeamMember(models.Model):
    """A real person shown on the public trust page (Phase 3). Added by staff; nothing is shown until published."""

    name = models.CharField(max_length=120)
    role = models.CharField(max_length=120)
    bio = models.TextField(max_length=600, blank=True)
    linkedin_url = models.URLField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    is_published = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return f"{self.name} · {self.role}"


class TrustStory(models.Model):
    """A real testimonial or case study, published only with the person's recorded consent (Phase 3).

    Never invented: staff fill in where the words came from (`source_note`) and when consent was given. The page shows
    nothing until a story is published, and a published story must have consent.
    """

    KIND_TESTIMONIAL, KIND_CASE_STUDY = "testimonial", "case_study"
    KIND_CHOICES = [(KIND_TESTIMONIAL, "Testimonial"), (KIND_CASE_STUDY, "Case study")]

    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_TESTIMONIAL)
    person_name = models.CharField(max_length=120)
    person_role = models.CharField(max_length=160, help_text="e.g. 'Founder, NileCode' or 'Student, Cairo University'")
    title = models.CharField(max_length=160, blank=True, help_text="Case studies only")
    body = models.TextField(max_length=2000, help_text="Their own words (testimonial) or what happened (case study), unedited in meaning.")
    source_note = models.CharField(max_length=300, help_text="Where this came from: email date, interview, form… (internal, not shown)")
    consent_given_at = models.DateTimeField(null=True, blank=True, help_text="When the person agreed to be published.")
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=models.Q(is_published=False) | models.Q(consent_given_at__isnull=False),
                                   name="truststory_published_needs_consent"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.person_name}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.is_published and not self.consent_given_at:
            raise ValidationError("A story can only be published with the person's recorded consent.")


class SecurityEvent(models.Model):
    """Append-only audit trail of sensitive actions (internest_core.audit). Ids and paths only, never contents or secrets.
    Purged after SECURITY_EVENT_RETENTION_DAYS by `manage.py purge_security_events`."""

    PRIVATE_FILE_VIEW = "private_file_view"
    ORDER_ACTIVATED = "order_activated"
    ASSESSMENT_REVIEWED = "assessment_reviewed"
    SCREENING_SCORED = "screening_scored"
    DATA_EXPORT = "data_export"
    ACCOUNT_DELETED = "account_deleted"
    ACTION_CHOICES = [
        (PRIVATE_FILE_VIEW, _("Private file opened")),
        (ORDER_ACTIVATED, _("Order activated")),
        (ASSESSMENT_REVIEWED, _("Assessment reviewed")),
        (SCREENING_SCORED, _("Screening task scored")),
        (DATA_EXPORT, _("Data exported")),
        (ACCOUNT_DELETED, _("Account deleted")),
    ]

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=40, choices=ACTION_CHOICES, db_index=True)
    target = models.CharField(max_length=200, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Security event")
        verbose_name_plural = _("Security events")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.target}"
