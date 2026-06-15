# C:\Internest\internest_core\admin.py

from django.contrib import admin, messages
from .models import (
    StudentProfile, Internship, Application, PartnerProfile, 
    PartnerInternshipSubmission, PartnerCourseSubmission,
    PartnerApplicantData,
    GoldenListStudent,
    GamificationTask, TaskQuestion, TaskAnswer, StudentTaskRecord,
    
    # (✨ استيراد موديلات الدفع الجديدة)
    DiscountCode, StudentEnrollment
)
from django.db import IntegrityError
from django.utils.html import format_html 

# --- تسجيل النماذج العادية ---
admin.site.register(StudentProfile)
admin.site.register(PartnerApplicantData)


@admin.register(PartnerProfile)
class PartnerProfileAdmin(admin.ModelAdmin):
    list_display = (
        'company_name', 
        'official_email', 
        'is_fully_verified', 
        'profile_completion_score',
    )
    list_filter = ('is_fully_verified', 'is_academic')
    search_fields = ('company_name', 'official_email', 'user__username')
    
    fieldsets = (
        (None, {
            'fields': (('user', 'company_name'), ('partner_code', 'is_academic'))
        }),
        ('معلومات التواصل والتوثيق', {
            'fields': (('official_email', 'official_phone'), ('official_website', 'linkedin_url'), ('facebook_url', 'twitter_url', 'instagram_url'))
        }),
        ('الشعارات والصور', {
            'fields': ('logo',) 
        }),
        ('حالة الحساب', {
            'fields': ('is_fully_verified', 'profile_completion_score', 'contract_expiry_date')
        }),
    )
    readonly_fields = ('profile_completion_score',)
    
    # 🚀 التعديل المطلوب: استدعاء calculate_completion بعد كل عملية حفظ في الأدمن
    def save_model(self, request, obj, form, change):
        # 1. حفظ النموذج أولاً
        super().save_model(request, obj, form, change)
        
        # 2. استدعاء دالة حساب النسبة، والتي ستضبطها على 100% إذا كانت is_fully_verified=True
        obj.calculate_completion()


# --- تسجيل النماذج المخصصة ---

@admin.register(Internship)
class InternshipAdmin(admin.ModelAdmin):
    list_display = ('title', 'partner', 'deadline', 'is_premium', 'is_active') 
    list_filter = ('partner__company_name', 'is_premium', 'is_active')
    search_fields = ('title', 'description', 'partner__company_name')
    actions = ['activate_internships']
    def activate_internships(self, request, queryset):
        queryset.update(is_active=True) 
        self.message_user(request, "تم تفعيل التدريبات المحددة بنجاح.")
    activate_internships.short_description = "تفعيل التدريبات المحددة"

@admin.register(PartnerInternshipSubmission)
class PartnerInternshipSubmissionAdmin(admin.ModelAdmin):
    list_display = ('title', 'partner', 'submission_date', 'status', 'location', 'deadline')
    list_filter = ('status', 'partner__company_name')
    actions = ['approve_submissions', 'reject_submissions']

    def approve_submissions(self, request, queryset):
        approved_count = 0
        email_errors = []
        
        for submission in queryset.filter(status='Pending'): 
            try:
                Internship.objects.create(
                    partner=submission.partner, 
                    title=submission.title,
                    description=submission.description,
                    location=submission.location,
                    required_majors=submission.required_majors,
                    deadline=submission.deadline,
                    is_premium=True,
                    is_active=True 
                )
                
                submission.status = 'Approved'
                submission.save()
                approved_count += 1
                
            except Exception as e:
                email_errors.append(f"الطلب {submission.title}: فشل النشر أو الإشعارات. الخطأ: {str(e)[:100]}...")
                continue
                
        if approved_count > 0:
            self.message_user(request, f"تمت الموافقة على {approved_count} طلبات وتم نشرها للطلاب.", messages.SUCCESS)
        
        if email_errors:
            for error in email_errors:
                self.message_user(request, f"⚠️ تنبيه: {error}", messages.WARNING)

        if approved_count == 0 and not email_errors:
            self.message_user(request, "لم يتم العثور على طلبات معلقة للموافقة.", messages.INFO)
            
    approve_submissions.short_description = "الموافقة على الطلبات ونشرها"
    
    def reject_submissions(self, request, queryset):
        queryset.filter(status='Pending').update(status='Rejected')
        self.message_user(request, "تم رفض الطلبات المحددة.")
    reject_submissions.short_description = "رفض الطلبات المحددة"

@admin.register(PartnerCourseSubmission)
class PartnerCourseSubmissionAdmin(admin.ModelAdmin):
    list_display = ('title', 'partner', 'price', 'points_awarded', 'status', 'submission_date')
    list_filter = ('status', 'partner__company_name')
    search_fields = ('title', 'instructor_name', 'partner__company_name')
    actions = ['approve_courses', 'reject_courses']
    def approve_courses(self, request, queryset):
        updated_count = queryset.filter(status='Pending').update(status='Approved')
        self.message_user(request, f"تمت الموافقة على {updated_count} كورس/كورسات.")
    approve_courses.short_description = "الموافقة على الكورسات ونشرها"
    def reject_courses(self, request, queryset):
        updated_count = queryset.filter(status='Pending').update(status='Rejected')
        self.message_user(request, f"تم رفض {updated_count} كورس/كورسات.")
    reject_courses.short_description = "رفض الكورسات المحددة"
    

@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('internship', 'applicant', 'application_date', 'status')
    list_filter = ('status', 'internship__partner__company_name')
    search_fields = ('internship__title', 'applicant__username')
    actions = ['approve_and_forward_applications']
    def approve_and_forward_applications(self, request, queryset):
        pending_apps = queryset.filter(status='قيد المراجعة')
        forwarded_count = 0
        for app in pending_apps:
            try:
                student_profile = app.applicant.studentprofile
                internship = app.internship
                partner = internship.partner
                PartnerApplicantData.objects.create(
                    partner=partner,
                    student=student_profile,
                    internship=internship,
                    application=app
                )
                app.status = 'محول للشريك'
                app.save()
                forwarded_count += 1
            except StudentProfile.DoesNotExist:
                self.message_user(request, f"خطأ: الطالب {app.applicant.username} ليس له ملف شخصي.", messages.ERROR)
            except IntegrityError:
                self.message_user(request, f"تنبيه: الطلب الخاص بـ {app.applicant.username} تم إرساله مسبقاً.", messages.WARNING)
                app.status = 'محول للشريك' 
                app.save()
            except Exception as e:
                self.message_user(request, f"حدث خطأ: {str(e)}", messages.ERROR)
        if forwarded_count > 0:
            self.message_user(request, f"تمت الموافقة وإرسال {forwarded_count} ملفات للشركاء بنجاح.")
    approve_and_forward_applications.short_description = "موافقة وإرسال البروفايلات للشركاء"

@admin.register(GoldenListStudent)
class GoldenListStudentAdmin(admin.ModelAdmin):
    ordering = ('-gamification_score',)
    list_display = ('user', 'gamification_score', 'university', 'major', 'study_level', 'profile_completion_score')
    list_filter = ('university', 'major', 'study_level')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'university', 'major')
    def has_add_permission(self, request): return False 
    def has_delete_permission(self, request, obj=None): return False 
    def has_change_permission(self, request, obj=None): return False

class TaskAnswerInline(admin.TabularInline):
    model = TaskAnswer
    extra = 1
    fields = ('text', 'is_correct')

@admin.register(TaskQuestion)
class TaskQuestionAdmin(admin.ModelAdmin):
    model = TaskQuestion
    inlines = [TaskAnswerInline]
    list_display = ('text', 'task')
    list_filter = ('task',)
    search_fields = ('text', 'task__title')

class TaskQuestionInline(admin.StackedInline):
    model = TaskQuestion
    extra = 0
    show_change_link = True

@admin.register(GamificationTask)
class GamificationTaskAdmin(admin.ModelAdmin):
    model = GamificationTask
    inlines = [TaskQuestionInline]
    list_display = ('title', 'points_awarded', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('title',)

@admin.register(StudentTaskRecord)
class StudentTaskRecordAdmin(admin.ModelAdmin):
    list_display = ('student', 'task', 'completed_on')
    list_filter = ('task', 'completed_on')
    search_fields = ('student__user__username', 'task__title')
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False
    def has_change_permission(self, request, obj=None): return False

# ---------------------------------
# --- (✨ تسجيل موديلات الدفع الجديدة ✨) ---
# ---------------------------------
@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = ('code', 'discount_percentage', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('code',)

@admin.register(StudentEnrollment)
class StudentEnrollmentAdmin(admin.ModelAdmin):
    list_display = ('student', 'course', 'enrolled_on', 'final_price')
    list_filter = ('course__title', 'enrolled_on')
    search_fields = ('student__user__username', 'course__title')
    
    # (بنقفل التعديل والإضافة من هنا لأنها بتتم تلقائي)
    def has_add_permission(self, request):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
    def has_change_permission(self, request, obj=None):
        return False