# C:\Internest\internest_core\models.py

from django.db import models
from django.contrib.auth.models import User 
from django.utils import timezone

# === 1. نموذج ملف الطالب الموحد (StudentProfile) ===
class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    university = models.CharField(max_length=100, blank=True, null=True, verbose_name="الجامعة")
    major = models.CharField(max_length=100, blank=True, null=True, verbose_name="التخصص")
    study_level = models.CharField(max_length=50, choices=[('1', 'سنة أولى'), ('2', 'سنة ثانية'), ('3', 'سنة ثالثة'), ('4', 'سنة رابعة/خريج')], blank=True, null=True, verbose_name="المستوى الدراسي")
    phone_number = models.CharField(max_length=20, blank=True, null=True, verbose_name="رقم الهاتف")
    cv_file = models.FileField(upload_to='cvs/', blank=True, null=True, verbose_name="ملف السيرة الذاتية (CV)")
    profile_picture = models.ImageField(upload_to='profile_pics/', blank=True, null=True, verbose_name="صورة الملف الشخصي")
    linkedin_url = models.URLField(max_length=200, blank=True, null=True, verbose_name="رابط LinkedIn")
    
    profile_completion_score = models.IntegerField(default=0, verbose_name="نسبة الإكمال (%)")
    gamification_score = models.IntegerField(default=0, verbose_name="نقاط الأداء")

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
    partner_code = models.CharField(max_length=50, unique=True, help_text="الكود السري للدخول المخصص.")
    logo = models.ImageField(upload_to='partner_logos/', null=True, blank=True, verbose_name="شعار الشركة/الجهة")
    
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
        required_fields = [self.official_phone, self.official_email, self.official_website, self.linkedin_url, self.logo]
        completed_fields = sum(1 for field in required_fields if field)
        self.profile_completion_score = int((completed_fields / 5) * 100)
        self.save()

    def __str__(self):
        return self.company_name

# === 2. نموذج فرص التدريب (Internship) ===
class Internship(models.Model):
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE, related_name="internships", verbose_name="الشريك (الشركة)")
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=100)
    required_majors = models.CharField(max_length=255) 
    deadline = models.DateField()
    is_premium = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    @property
    def company_name(self):
        return self.partner.company_name
        
    def __str__(self):
        return self.title

# === 3. نموذج طلبات التقديم (Application) ===
class Application(models.Model):
    internship = models.ForeignKey(Internship, on_delete=models.CASCADE)
    applicant = models.ForeignKey(User, on_delete=models.CASCADE)
    application_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default='قيد المراجعة', choices=[
        ('قيد المراجعة', 'قيد المراجعة'),
        ('محول للشريك', 'محول للشريك'), 
        ('مرفوض', 'مرفوض')
    ])
    
    class Meta:
        unique_together = ('internship', 'applicant')

    def __str__(self):
        return f"Application by {self.applicant.username} for {self.internship.title}"

# === 5. نماذج تقديم المحتوى من الشركاء (Submissions) ===
class PartnerInternshipSubmission(models.Model):
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=100)
    required_majors = models.CharField(max_length=255) 
    deadline = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES_SUBMISSION, default='Pending')
    submission_date = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"تقديم تدريب: {self.title} من {self.partner.company_name} ({self.status})"

class PartnerCourseSubmission(models.Model):
    partner = models.ForeignKey(PartnerProfile, on_delete=models.CASCADE)
    title = models.CharField(max_length=150)
    description = models.TextField(help_text="وصف تفصيلي للكورس.")
    price = models.DecimalField(max_digits=6, decimal_places=2, help_text="سعر الكورس (لأغراض الإيرادات).")
    instructor_name = models.CharField(max_length=100, help_text="اسم مقدم الكورس.")
    video_link = models.URLField(blank=True, null=True, help_text="رابط ترويجي/صورة/فيديو للكورس.")
    points_awarded = models.IntegerField(help_text="نقاط Gamification عند إكمال الطالب للكورس.")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES_SUBMISSION, default='Pending')
    submission_date = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"كورس {self.title} من {self.partner.company_name} ({self.status})"

# === 6. نموذج بيانات المتقدمين (صندوق وارد الشريك) ===
class PartnerApplicantData(models.Model):
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

# === 7. موديل الجولدن ليست ===
class GoldenListStudent(StudentProfile):
    class Meta:
        proxy = True 
        verbose_name = "طالب في الجولدن ليست"
        verbose_name_plural = "🏆 الجولدن ليست (أعلى الطلاب)"

# ---------------------------------
# --- (✨ 8. مـوديـلات الـتـاسـكـات (جـديـدة) ✨) ---
# ---------------------------------

class GamificationTask(models.Model):
    title = models.CharField(max_length=200, verbose_name="عنوان التاسك")
    description = models.TextField(verbose_name="وصف التاسك")
    points_awarded = models.IntegerField(default=10, verbose_name="النقاط الممنوحة")
    is_active = models.BooleanField(default=True, verbose_name="متاح للطلاب؟")
    
    class Meta:
        verbose_name = "مهمة (تاسك)"
        verbose_name_plural = "🎯 المهام (التاسكات)"
        
    def __str__(self):
        return self.title

class TaskQuestion(models.Model):
    task = models.ForeignKey(GamificationTask, on_delete=models.CASCADE, related_name="questions", verbose_name="التاسك التابع له")
    text = models.CharField(max_length=500, verbose_name="نص السؤال")
    
    class Meta:
        verbose_name = "سؤال"
        verbose_name_plural = "الأسئلة"
        
    def __str__(self):
        return self.text

class TaskAnswer(models.Model):
    question = models.ForeignKey(TaskQuestion, on_delete=models.CASCADE, related_name="answers", verbose_name="السؤال التابع له")
    text = models.CharField(max_length=255, verbose_name="نص الإجابة (الاختيار)")
    is_correct = models.BooleanField(default=False, verbose_name="هل هذه هي الإجابة الصحيحة؟")
    
    class Meta:
        verbose_name = "اختيار (إجابة)"
        verbose_name_plural = "الاختيارات (الإجابات)"
        
    def __str__(self):
        return f"{self.question.text} -> {self.text} ({self.is_correct})"

class StudentTaskRecord(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="task_records")
    task = models.ForeignKey(GamificationTask, on_delete=models.CASCADE, related_name="submissions")
    completed_on = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = "سجل إكمال مهمة"
        verbose_name_plural = "سجلات إكمال المهام"
        unique_together = ('student', 'task') # (✨ بيمنع الطالب يحل التاسك مرتين)
        
    def __str__(self):
        return f"{self.student.user.username} أكمل {self.task.title}"
    

# ---------------------------------
# --- (✨ 9. مـوديـلات نـظـام الـدفـع (جـديـدة) ✨) ---
# ---------------------------------

class DiscountCode(models.Model):
    code = models.CharField(max_length=50, unique=True, verbose_name="الكود")
    discount_percentage = models.PositiveIntegerField(verbose_name="نسبة الخصم (%)", help_text="رقم من 1 إلى 100")
    is_active = models.BooleanField(default=True, verbose_name="فعال؟")
    
    class Meta:
        verbose_name = "كود خصم"
        verbose_name_plural = "🏷️ أكواد الخصم"
        
    def __str__(self):
        return f"{self.code} ({self.discount_percentage}%)"

class StudentEnrollment(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrolled_courses", verbose_name="الطالب")
    course = models.ForeignKey(PartnerCourseSubmission, on_delete=models.CASCADE, related_name="enrollments", verbose_name="الكورس")
    enrolled_on = models.DateTimeField(auto_now_add=True, verbose_name="تاريخ التسجيل")
    final_price = models.DecimalField(max_digits=6, decimal_places=2, verbose_name="السعر المدفوع")
    
    class Meta:
        verbose_name = "تسجيل (كورس)"
        verbose_name_plural = "🧑‍🎓 تسجيلات الطلاب في الكورسات"
        unique_together = ('student', 'course') # (بيمنع الطالب يشتري نفس الكورس مرتين)
        
    def __str__(self):
        return f"{self.student.user.username} مسجل في {self.course.title}"