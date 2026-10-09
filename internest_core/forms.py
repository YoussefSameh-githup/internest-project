from django import forms
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Q

from .models import (
    StudentProfile, Internship, PartnerInternshipSubmission,
    PartnerCourseSubmission, PartnerProfile,
    TaskAnswer, TaskQuestion,
)


def _email_taken_by_another_profile(email: str, exclude_profile_pk=None) -> bool:
    """A given email may only belong to one student account across BOTH email slots."""
    if not email:
        return False
    qs = StudentProfile.objects.filter(
        Q(personal_email__iexact=email) | Q(university_email__iexact=email)
    )
    if exclude_profile_pk:
        qs = qs.exclude(pk=exclude_profile_pk)
    return qs.exists()

# === 1. فورم تعديل ملف الطالب (ProfileForm) ===
class ProfileForm(forms.ModelForm):
    # إضافة حقل الإيميل الشخصي كحقل منفصل (مطلوب)
    personal_email = forms.EmailField(
        label=_("Personal email (required)"),
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
            'linkedin_url', 'bio', 'portfolio_url', 'talent_pool_visible',
        ]
        
        labels = {
            'university_email': _("University email (optional)"),
            'university': _("University"), 'major': _("Major"),
            'study_level': _("Study level"), 'phone_number': _("Phone number"),
            'cv_file': _("CV file"), 'profile_picture': _("Profile picture"),
            'linkedin_url': _("LinkedIn URL"),
            'bio': _("About me & projects"),
            'portfolio_url': _("Portfolio link (GitHub, Behance, website)"),
            'talent_pool_visible': _("Show me in the Talent Pool"),
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
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'maxlength': 600}),
            'portfolio_url': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://'}),
            'talent_pool_visible': forms.CheckboxInput(),
        }
        
    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        if instance and not kwargs.get('initial'):
            kwargs['initial'] = {'personal_email': instance.personal_email}
        super().__init__(*args, **kwargs)

    def clean_personal_email(self):
        email = self.cleaned_data.get('personal_email')
        if email and _email_taken_by_another_profile(email, exclude_profile_pk=self.instance.pk):
            raise forms.ValidationError(_("This email is already in use by another account."))
        return email

    def clean_university_email(self):
        email = self.cleaned_data.get('university_email')
        if email and _email_taken_by_another_profile(email, exclude_profile_pk=self.instance.pk):
            raise forms.ValidationError(_("This email is already in use by another account."))
        return email

    def clean(self):
        cleaned = super().clean()
        personal = (cleaned.get('personal_email') or '').strip().lower()
        university = (cleaned.get('university_email') or '').strip().lower()
        if personal and university and personal == university:
            raise forms.ValidationError(_("Personal and university emails must be different."))
        return cleaned

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
        # partner_code, is_academic and official_phone are admin-managed / retired from the partner UI.
        fields = [
            'company_name', 'logo', 'official_website', 'official_email',
            'linkedin_url', 'facebook_url', 'twitter_url', 'instagram_url',
        ]
        labels = {
            'company_name': _("Company / organization name"), 'partner_code': _("Partner code"),
            'logo': _("Official logo"), 'is_academic': _("Academic institution?"),
            'official_website': _("Official website"), 'official_email': _("Official email"),
            'official_phone': _("Official phone"), 'linkedin_url': _("LinkedIn URL"),
            'facebook_url': _("Facebook URL"), 'twitter_url': _("Twitter URL"), 
            'instagram_url': _("Instagram URL"),
        }

# === 3. فورم تقديم تدريب (من الشريك) (PartnerInternshipForm) ===
ONLINE_LOCATION = "Online"
MAX_CUSTOM_SKILLS = 10


class PartnerInternshipForm(forms.ModelForm):
    LOCATION_ONLINE, LOCATION_ONSITE = "online", "onsite"

    location_mode = forms.ChoiceField(
        choices=[(LOCATION_ONLINE, _("Online / remote")), (LOCATION_ONSITE, _("On-site / offline"))],
        initial=LOCATION_ONLINE, required=False, widget=forms.RadioSelect, label=_("Work location"),
    )
    address = forms.CharField(
        required=False, max_length=100, label=_("Office address"),
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": _("e.g. Smart Village, Giza"), "autocomplete": "street-address"}),
    )
    # Market Readiness Gate: students need ≥80% coverage (exact or semantically related verified skills).
    required_skills = forms.ModelMultipleChoiceField(
        queryset=None, required=False, label=_("Required skills"),
        widget=forms.SelectMultiple(attrs={"class": "form-control", "size": 6}),
    )
    custom_skills = forms.CharField(
        required=False, label=_("Other skills"),
        help_text=_("Type a skill that is not in the list and press Enter. Our AI links it to related skills students have verified."),
        widget=forms.HiddenInput,
    )

    class Meta:
        model = PartnerInternshipSubmission
        fields = ['title', 'description', 'required_majors', 'deadline']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 6}),
            'required_majors': forms.TextInput(attrs={'class': 'form-control'}),
            'deadline': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from internest_skills.models import Skill  # skills app depends on core; import lazily
        self.fields["required_skills"].queryset = Skill.objects.filter(is_active=True).order_by("name")

    def clean_custom_skills(self):
        names, seen = [], set()
        for raw in (self.cleaned_data.get("custom_skills") or "").split(","):
            name = " ".join(raw.split())
            if not name or name.lower() in seen:
                continue
            if not 2 <= len(name) <= 60:
                raise forms.ValidationError(_("Each skill must be 2–60 characters."))
            seen.add(name.lower())
            names.append(name)
        if len(names) > MAX_CUSTOM_SKILLS:
            raise forms.ValidationError(_("Add at most %(n)s custom skills.") % {"n": MAX_CUSTOM_SKILLS})
        return names

    def clean(self):
        data = super().clean()
        if (data.get("location_mode") or self.LOCATION_ONLINE) == self.LOCATION_ONSITE:
            if not (data.get("address") or "").strip():
                self.add_error("address", _("Enter the office address for on-site opportunities."))
            else:
                self.instance.location = data["address"].strip()
        else:
            self.instance.location = ONLINE_LOCATION
        return data

    def all_required_skills(self):
        """Selected skills + custom ones (created on the fly). Returns (skills, newly_created)."""
        from django.utils.text import slugify
        from internest_skills.models import Skill

        skills, created = list(self.cleaned_data.get("required_skills") or []), []
        for name in self.cleaned_data.get("custom_skills") or []:
            skill = Skill.objects.filter(name__iexact=name).first()
            if skill is None:
                base = slugify(name, allow_unicode=True)[:90] or "skill"
                slug, n = base, 2
                while Skill.objects.filter(slug=slug).exists():
                    slug, n = f"{base}-{n}", n + 1
                skill = Skill.objects.create(name=name, slug=slug, discipline="general", aliases=name)
                created.append(skill)
            if skill not in skills:
                skills.append(skill)
        return skills, created


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
        label=_("Personal email"),
        max_length=254,
        required=True,
        help_text=_("We'll send opportunity notifications to this email.")
    )

    class Meta(UserCreationForm.Meta):
        pass

    def clean_personal_email(self):
        email = self.cleaned_data.get('personal_email')
        if email and _email_taken_by_another_profile(email):
            raise forms.ValidationError(_("This email is already registered."))
        return email


class OTPVerificationForm(forms.Form):
    """Six-digit OTP entered by the student during email verification."""
    code = forms.CharField(
        label='OTP Code',
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'inputmode': 'numeric',
            'autocomplete': 'one-time-code',
            'pattern': r'\d{6}',
            'placeholder': '••••••',
        }),
    )
    email_type = forms.ChoiceField(
        choices=[('personal', 'Personal'), ('university', 'University')],
        widget=forms.HiddenInput(),
    )

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip()
        if not code.isdigit() or len(code) != 6:
            raise forms.ValidationError(_("The verification code must be 6 digits."))
        return code