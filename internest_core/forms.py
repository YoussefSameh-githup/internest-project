# C:\Internest\internest_core\forms.py

from django import forms
from .models import (
    StudentProfile, Internship, PartnerInternshipSubmission, 
    PartnerCourseSubmission, PartnerProfile
)

# === 1. فورم تعديل ملف الطالب ===
class ProfileForm(forms.ModelForm):
    class Meta:
        model = StudentProfile
        fields = ['university', 'major', 'study_level', 'phone_number', 'cv_file', 'profile_picture', 'linkedin_url']
        labels = {
            'university': 'الجامعة',
            'major': 'التخصص',
            'study_level': 'المستوى الدراسي',
            'phone_number': 'رقم الهاتف',
            'cv_file': 'ملف السيرة الذاتية (CV)',
            'profile_picture': 'صورة الملف الشخصي',
            'linkedin_url': 'رابط LinkedIn',
        }
        widgets = {
            'university': forms.TextInput(attrs={'class': 'form-control'}),
            'major': forms.TextInput(attrs={'class': 'form-control'}),
            'study_level': forms.Select(attrs={'class': 'form-select'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'cv_file': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'profile_picture': forms.ClearableFileInput(attrs={'class': 'form-control'}),
            'linkedin_url': forms.URLInput(attrs={'class': 'form-control'}),
        }

# === 2. فورم تعديل ملف الشريك ===
class PartnerProfileEditForm(forms.ModelForm):
    class Meta:
        model = PartnerProfile
        fields = [
            'company_name',
            'partner_code',
            'logo', 
            'is_academic',
            'official_website',
            'official_email',
            'official_phone',
            'linkedin_url',
            'facebook_url',
            'twitter_url',
            'instagram_url'
        ]
        labels = {
            'company_name': 'اسم الشركة/الجهة',
            'partner_code': 'الكود السري للشريك',
            'logo': 'شعار الشركة/الجهة (الرسمي)',
            'is_academic': 'هل هي جهة أكاديمية؟',
            'official_website': 'الموقع الرسمي',
            'official_email': 'البريد الإلكتروني الرسمي',
            'official_phone': 'رقم التواصل الرسمي',
            'linkedin_url': 'رابط LinkedIn',
            'facebook_url': 'رابط Facebook',
            'twitter_url': 'رابط Twitter',
            'instagram_url': 'رابط Instagram',
        }
        # (شيلنا الـ widgets علشان نستخدم الطريقة الأبسط)

# === 3. فورم تقديم تدريب (من الشريك) ===
class PartnerInternshipForm(forms.ModelForm):
    class Meta:
        model = PartnerInternshipSubmission
        fields = ['title', 'description', 'location', 'required_majors', 'deadline']

# === 4. فورم تقديم كورس (من الشريك) ===
class PartnerCourseForm(forms.ModelForm):
    class Meta:
        model = PartnerCourseSubmission
        fields = ['title', 'description', 'price', 'instructor_name', 'video_link', 'points_awarded']

# ---------------------------------
# --- (✨ 5. فـورم الـتـاسـكـات الـجـديـدة ✨) ---
# ---------------------------------
class TaskQuizForm(forms.Form):
    
    def __init__(self, *args, **kwargs):
        # (✨) بنستقبل "التاسك" من الـ view
        task = kwargs.pop('task')
        super().__init__(*args, **kwargs)
        
        # (✨) بنلف على كل سؤال جوه التاسك
        for question in task.questions.all():
            # (✨) بنحضر قايمة الاختيارات
            choices = []
            for answer in question.answers.all():
                choices.append((answer.id, answer.text))
            
            # (✨) بنعمل "خانة سؤال" جديدة (ديناميك)
            self.fields[f'question_{question.id}'] = forms.ChoiceField(
                label=question.text,
                choices=choices,
                widget=forms.RadioSelect(attrs={'class': 'form-check-input'}) # (علشان يظهروا كـ radio buttons)
            )