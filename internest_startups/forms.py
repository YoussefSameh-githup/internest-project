from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from internest_core.models import PartnerProfile

from .completion import PLACEHOLDER_PREFIX
from .models import CompanyProfile

_input = {"class": "form-control"}


class StartupSignupForm(forms.Form):
    full_name = forms.CharField(label=_("Full name"), max_length=150, widget=forms.TextInput(attrs={**_input, "autocomplete": "name"}))
    email = forms.EmailField(label=_("Work email"), widget=forms.EmailInput(attrs={**_input, "autocomplete": "email"}))
    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput(attrs={**_input, "autocomplete": "new-password"}))
    password2 = forms.CharField(label=_("Confirm password"), widget=forms.PasswordInput(attrs={**_input, "autocomplete": "new-password"}))

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("An account with this email already exists."))
        return email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", _("The two passwords do not match."))
        elif p1:
            try:
                password_validation.validate_password(p1, User(username=data.get("email", ""), email=data.get("email", "")))
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data


class CompanyIdentityForm(forms.ModelForm):
    """Company name, website and social links (stored on PartnerProfile)."""

    class Meta:
        model = PartnerProfile
        fields = ["company_name", "official_email", "official_website", "linkedin_url", "facebook_url", "twitter_url", "instagram_url"]
        labels = {
            "company_name": _("Company name"),
            "official_email": _("Company official email"),
            "official_website": _("Website"),
            "linkedin_url": _("LinkedIn URL"),
            "facebook_url": _("Facebook URL"),
            "twitter_url": _("Twitter URL"),
            "instagram_url": _("Instagram URL"),
        }
        widgets = {f: forms.URLInput(attrs={**_input, "placeholder": "https://", "dir": "ltr"})
                   for f in ["official_website", "linkedin_url", "facebook_url", "twitter_url", "instagram_url"]}
        widgets["company_name"] = forms.TextInput(attrs=_input)
        widgets["official_email"] = forms.EmailInput(attrs={**_input, "placeholder": "contact@company.com", "dir": "ltr"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.company_name.startswith(PLACEHOLDER_PREFIX):
            self.initial["company_name"] = ""
        self.fields["official_email"].required = True

    def clean(self):
        data = super().clean()
        if not any(data.get(f) for f in ("linkedin_url", "facebook_url", "twitter_url", "instagram_url")):
            raise forms.ValidationError(_("Add at least one social media link (LinkedIn, Facebook, Twitter or Instagram)."))
        return data


class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ["industry", "founded_year", "description", "founder_email"]
        labels = {
            "founder_email": _("Founder official email"),
            "industry": _("Industry / field"),
            "founded_year": _("Founded year"),
            "description": _("Brief company description"),
        }
        widgets = {
            "industry": forms.Select(attrs={"class": "form-select"}),
            "founded_year": forms.NumberInput(attrs={**_input, "min": 1950}),
            "description": forms.Textarea(attrs={**_input, "rows": 4, "maxlength": 1000}),
            "founder_email": forms.EmailInput(attrs={**_input, "placeholder": "founder@company.com", "dir": "ltr"}),
        }

    def __init__(self, *args, company_email=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["founder_email"].required = True
        self.company_email = (company_email or "").lower()

    def clean_founder_email(self):
        email = self.cleaned_data["founder_email"].strip().lower()
        if self.company_email and email == self.company_email:
            raise forms.ValidationError(_("The founder email must be different from the company email."))
        return email

    def clean_founded_year(self):
        year = self.cleaned_data["founded_year"]
        if year > timezone.now().year:
            raise forms.ValidationError(_("The founded year cannot be in the future."))
        return year


