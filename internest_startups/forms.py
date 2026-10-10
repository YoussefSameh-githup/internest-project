import secrets

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


LOGO_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
LOGO_MAX_BYTES = 5 * 1024 * 1024


def clean_logo_upload(logo, partner):
    """Validate an uploaded logo and give it a short, ASCII-safe name.

    Phone/WhatsApp/Arabic filenames can be long or non-ASCII; renaming avoids storage path issues
    (and the 100-char FileField limit) on the server.
    """
    if not logo or not hasattr(logo, "content_type"):  # unchanged / cleared
        return logo
    ext = logo.name.rsplit(".", 1)[-1].lower() if "." in logo.name else ""
    if ext not in LOGO_EXTENSIONS:
        raise forms.ValidationError(_("Upload a PNG, JPG or WEBP image."))
    if logo.size > LOGO_MAX_BYTES:
        raise forms.ValidationError(_("The logo must be 5 MB or smaller."))
    logo.name = f"{partner.pk or 'new'}-{secrets.token_hex(4)}.{'jpg' if ext == 'jpeg' else ext}"
    return logo


class LogoForm(forms.ModelForm):
    """Self-service logo change: saves instantly and never touches verification or other fields."""

    class Meta:
        model = PartnerProfile
        fields = ["logo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logo"].required = True
        self.fields["logo"].widget = forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp"})

    def clean_logo(self):
        return clean_logo_upload(self.cleaned_data.get("logo"), self.instance)


class CompanyIdentityForm(forms.ModelForm):
    """Company name, website and social links (stored on PartnerProfile)."""

    class Meta:
        model = PartnerProfile
        fields = ["logo", "company_name", "official_email", "country_of_registration", "official_website", "linkedin_url",
                  "facebook_url", "twitter_url", "instagram_url"]
        labels = {
            "country_of_registration": _("Country of commercial registration"),
            "logo": _("Company logo"),
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
        widgets["logo"] = forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg,image/webp", "class": "logo-upload__input"})
        widgets["official_email"] = forms.EmailInput(attrs={**_input, "placeholder": "contact@company.com", "dir": "ltr"})

    def __init__(self, *args, **kwargs):
        from .pricing import COUNTRY_CHOICES

        super().__init__(*args, **kwargs)
        if self.instance.company_name.startswith(PLACEHOLDER_PREFIX):
            self.initial["company_name"] = ""
        self.fields["official_email"].required = True
        country = self.fields["country_of_registration"]
        country.widget = forms.Select(choices=[("", "—")] + COUNTRY_CHOICES, attrs={"class": "form-select"})
        country.required = True
        country.help_text = _("Sets your Pro pricing currency. It can only be changed by our team after verification.")
        if self.instance.is_fully_verified and self.instance.country_of_registration:
            country.disabled = True  # pricing region is locked once an admin verified the company

    def clean_logo(self):
        return clean_logo_upload(self.cleaned_data.get("logo"), self.instance)

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


