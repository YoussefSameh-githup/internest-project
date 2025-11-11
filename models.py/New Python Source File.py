
# models.py - داخل تطبيق Internship_App

from django.db import models
from django.contrib.auth.models import User

# --- 1. نموذج ملف الطالب الموحد ---
class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    university = models.CharField(max_length=100)
    major = models.CharField(max_length=100) # التخصص
    study_level = models.CharField(max_length=50, choices=[('1', 'سنة أولى'), ('2', 'سنة ثانية'), ('3', 'سنة ثالثة'), ('4', 'سنة رابعة/خريج')])
    cv_file = models.FileField(upload_to='cvs/', blank=True, null=True)

    # حقل التلعيب (Gamification MVP)
    profile_completion_score = models.IntegerField(default=0) # 0-100%

    def calculate_completion(self):
        # منطق حساب نسبة الإكمال للملف (مثال مبسط)
        score = 0
        if self.university: score += 25
        if self.major: score += 25
        if self.cv_file: score += 50
        self.profile_completion_score = score
        self.save()

    def __str__(self):
        return self.user.username

# --- 2. نموذج فرص التدريب ---
class Internship(models.Model):
    title = models.CharField(max_length=200)
    company_name = models.CharField(max_length=100)
    location = models.CharField(max_length=100)
    description = models.TextField()
    required_majors = models.CharField(max_length=255) # التخصصات المطلوبة (Tags مفصولة بفواصل)
    deadline = models.DateField()

    def __str__(self):
        return self.title

# --- 3. نموذج طلبات التقديم ---
class Application(models.Model):
    internship = models.ForeignKey(Internship, on_delete=models.CASCADE)
    applicant = models.ForeignKey(User, on_delete=models.CASCADE)
    application_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default='قيد المراجعة', choices=[
        ('قيد المراجعة', 'قيد المراجعة'),
        ('مقبول', 'مقبول'),
        ('مرفوض', 'مرفوض')
    ])

    class Meta:
        # منع الطالب من التقديم على نفس الفرصة مرتين
        unique_together = ('internship', 'applicant')