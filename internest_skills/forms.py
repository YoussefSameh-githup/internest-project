from urllib.parse import urlparse

from django import forms
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _

from internest_core.models import PartnerProfile, validate_max_file_size

from .models import Skill, StudentFreelanceGig

_doc_validators = [FileExtensionValidator(["pdf", "docx"]), validate_max_file_size]
MIN_PASTED_CHARS = 80


class SkillSourceForm(forms.Form):
    cv_file = forms.FileField(required=False, validators=_doc_validators, label=_("CV (PDF or DOCX)"))
    linkedin_pdf = forms.FileField(required=False, validators=_doc_validators, label=_("LinkedIn profile PDF export"))
    linkedin_url = forms.URLField(required=False, label=_("LinkedIn profile URL"))
    # No PDF? Students can paste their LinkedIn "About", "Experience" and "Skills" sections instead.
    pasted_text = forms.CharField(required=False, max_length=20000, label=_("Paste your LinkedIn or CV text"),
                                  widget=forms.Textarea(attrs={"rows": 8, "dir": "auto"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-control"
        self.fields["cv_file"].widget.attrs["accept"] = ".pdf,.docx"
        self.fields["linkedin_pdf"].widget.attrs["accept"] = ".pdf"

    def clean_linkedin_url(self):
        url = self.cleaned_data.get("linkedin_url")
        if url:
            host = (urlparse(url).hostname or "").lower()
            if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
                raise forms.ValidationError(_("Enter a linkedin.com profile URL."))
        return url

    def clean_pasted_text(self):
        text = (self.cleaned_data.get("pasted_text") or "").strip()
        if text and len(text) < MIN_PASTED_CHARS:
            raise forms.ValidationError(_("Paste a few more lines (at least %(n)s characters) — for example your About, Experience and Skills sections.") % {"n": MIN_PASTED_CHARS})
        return text

    def clean(self):
        data = super().clean()
        if not (data.get("cv_file") or data.get("linkedin_pdf") or data.get("pasted_text") or data.get("linkedin_url")) and not self.errors:
            raise forms.ValidationError(_("Upload your CV or LinkedIn PDF, or paste your LinkedIn / CV text."))
        return data


PLATFORM_HOSTS = {"mostaql": "mostaql.com", "khamsat": "khamsat.com", "upwork": "upwork.com", "fiverr": "fiverr.com",
                  "github": "github.com"}


class FreelanceImportForm(forms.Form):
    platform = forms.ChoiceField(choices=StudentFreelanceGig.PLATFORM_CHOICES, widget=forms.RadioSelect, label=_("Platform"))
    link = forms.URLField(max_length=300, label=_("Portfolio / gig link"), assume_scheme="https")
    raw_text = forms.CharField(min_length=40, max_length=6000, widget=forms.Textarea(attrs={"rows": 7, "dir": "auto"}),
                               label=_("Work description / client review"))
    proof = forms.FileField(required=False, label=_("Proof (screenshot or PDF, optional)"),
                            validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp", "pdf"]), validate_max_file_size])

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.student = student
        self.fields["proof"].widget.attrs["accept"] = "image/png,image/jpeg,image/webp,application/pdf"

    def clean(self):
        data = super().clean()
        link, platform = data.get("link"), data.get("platform")
        if link and platform in PLATFORM_HOSTS:
            host = (urlparse(link).hostname or "").lower()
            expected = PLATFORM_HOSTS[platform]
            if not (host == expected or host.endswith("." + expected)):
                self.add_error("link", _("This link isn't on %(site)s. Choose the right platform or use “Portfolio link”.") % {"site": expected})
        if link and self.student and self.student.freelance_gigs.filter(link=link).exists():
            self.add_error("link", _("You already imported this link."))
        return data


class UniversityShareForm(forms.Form):
    partner_university = forms.ModelChoiceField(
        queryset=PartnerProfile.objects.filter(is_academic=True, is_fully_verified=True).order_by("company_name"),
        required=False, empty_label=_("— Do not share —"), label=_("Share my skill analytics with my university"))


class GigProofForm(forms.Form):
    proof = forms.FileField(label=_("Proof (screenshot or PDF, optional)"),
                            validators=[FileExtensionValidator(["png", "jpg", "jpeg", "webp", "pdf"]), validate_max_file_size])


class CertificateForm(forms.Form):
    skill = forms.ModelChoiceField(queryset=Skill.objects.filter(is_active=True), label=_("Skill"))
    title = forms.CharField(max_length=160, label=_("Certificate title"))
    issuer = forms.CharField(max_length=120, required=False, label=_("Issuer"))
    url = forms.URLField(max_length=500, required=False, label=_("Verification link"), assume_scheme="https")
    file = forms.FileField(required=False, label=_("Certificate file"),
                           validators=[FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "webp"]), validate_max_file_size])

    def clean(self):
        data = super().clean()
        if not (data.get("url") or data.get("file")) and not self.errors:
            raise forms.ValidationError(_("Add the certificate's verification link or upload the certificate."))
        return data
