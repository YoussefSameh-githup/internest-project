# C:\Internest\internest_core\forms.py

from django import forms
from django.contrib.auth.forms import UserCreationForm 


from .models import (
    StudentProfile, Internship, PartnerInternshipSubmission, 
    PartnerCourseSubmission, PartnerProfile, 
    TaskAnswer, TaskQuestion 
)

# === 1. فورم تعديل ملف الطالب (ProfileForm) ===
class ProfileForm(forms.ModelForm):
    # إضافة حقل الإيميل الشخصي كحقل منفصل (مطلوب)
    personal_email = forms.EmailField(
        label='البريد الإلكتروني الشخصي (مطلوب)',
        max_length=254,
        required=True,
        widget=forms.EmailInput(attrs={'class': 'form-control'})
    )

    class Meta:
        model = StudentProfile
        # إضافة حقلي الإيميل الشخصي والجامعي لقائمة الحقول
        fields = [
            'personal_email', 'university_email', 'university', 'major', 
            'study_level', 'phone_number', 'cv_file', 'profile_picture', 
            'linkedin_url'
        ]
        
        labels = {
            'university_email': 'البريد الإلكتروني الجامعي (اختياري)',
            'university': 'الجامعة', 'major': 'التخصص',
            'study_level': 'المستوى الدراسي', 'phone_number': 'رقم الهاتف',
            'cv_file': 'ملف السيرة الذاتية (CV)', 'profile_picture': 'صورة الملف الشخصي',
            'linkedin_url': 'رابط LinkedIn',
        }
        
        widgets = {
            'university_email': forms.EmailInput(attrs={'class': 'form-control'}),
            'university': forms.TextInput(attrs={'class': 'form-control'}),
            'major': forms.TextInput(attrs={'class': 'form-control'}),
            'study_level': forms.Select(attrs={'class': 'form-select'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'cv_file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'linkedin_url': forms.URLInput(attrs={'class': 'form-control'}),
        }
        
    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        if instance and not kwargs.get('initial'):
            kwargs['initial'] = {'personal_email': instance.personal_email}
        super().__init__(*args, **kwargs)
        
    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.personal_email = self.cleaned_data['personal_email']
        if commit:
            instance.save()
        return instance


# === 2. فورم تعديل ملف الشريك (PartnerProfileEditForm) ===
class PartnerProfileEditForm(forms.ModelForm):
    class Meta:
        model = PartnerProfile
        fields = [
            'company_name', 'partner_code', 'logo', 'is_academic', 'official_website',
            'official_email', 'official_phone', 'linkedin_url', 'facebook_url',
            'twitter_url', 'instagram_url'
        ]
        labels = {
            'company_name': 'اسم الشركة/الجهة', 'partner_code': 'الكود السري للشريك',
            'logo': 'شعار الشركة/الجهة (الرسمي)', 'is_academic': 'هل هي جهة أكاديمية؟',
            'official_website': 'الموقع الرسمي', 'official_email': 'البريد الإلكتروني الرسمي',
            'official_phone': 'رقم التواصل الرسمي', 'linkedin_url': 'رابط LinkedIn',
            'facebook_url': 'رابط Facebook', 'twitter_url': 'رابط Twitter', 
            'instagram_url': 'رابط Instagram',
        }

# === 3. فورم تقديم تدريب (من الشريك) (PartnerInternshipForm) ===
class PartnerInternshipForm(forms.ModelForm):
    class Meta:
        model = PartnerInternshipSubmission
        # 🛑 التعديل النهائي: حذف 'duration' ليتم حسابه تلقائياً
        fields = ['title', 'description', 'location', 'required_majors', 'deadline']
        
        # إضافة ويدجت التقويم لحقل الديدلاين
        widgets = {
            'deadline': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }


# === 4. فورم تقديم كورس (من الشريك) (PartnerCourseForm) ===
class PartnerCourseForm(forms.ModelForm):
    class Meta:
        model = PartnerCourseSubmission
        fields = ['title', 'description', 'price', 'instructor_name', 'video_link', 'points_awarded']

# ---------------------------------
# --- (✨ 5. فـورم الـتـاسـكـات الـجـديـدة (TaskQuizForm) ✨) ---
# ---------------------------------
class TaskQuizForm(forms.Form):
    
    def __init__(self, *args, **kwargs):
        task = kwargs.pop('task')
        super().__init__(*args, **kwargs)
        
        for question in task.questions.all():
            choices = []
            for answer in question.answers.all():
                choices.append((answer.id, answer.text))
            
            self.fields[f'question_{question.id}'] = forms.ChoiceField(
                label=question.text,
                choices=choices,
                widget=forms.RadioSelect(attrs={'class': 'form-check-input'})
            )

# ---------------------------------
# --- (✨ 6. فورم التسجيل المخصص (CustomStudentSignupForm) ✨) ---
# ---------------------------------
class CustomStudentSignupForm(UserCreationForm):
    personal_email = forms.EmailField(
        label='البريد الإلكتروني الشخصي',
        max_length=254,
        required=True,
        help_text='سيتم استخدام هذا الإيميل لإشعارات التدريب.'
    )

    class Meta(UserCreationForm.Meta):
        pass