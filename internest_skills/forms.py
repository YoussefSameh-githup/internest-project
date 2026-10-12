from urllib.parse import urlparse

from django import forms
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _

from internest_core.models import PartnerProfile, validate_max_file_size

from .models import StudentFreelanceGig

_doc_validators = [FileExtensionValidator(["pdf", "docx"]), validate_max_file_size]


class SkillSourceForm(forms.Form):
    cv_file = forms.FileField(required=False, validators=_doc_validators, label=_("CV (PDF or DOCX)"))
    linkedin_pdf = forms.FileField(required=False, validators=_doc_validators, label=_("LinkedIn profile PDF export"))
    linkedin_url = forms.URLField(required=False, label=_("LinkedIn profile URL"))
    partner_university = forms.ModelChoiceField(
        queryset=PartnerProfile.objects.filter(is_academic=True, is_fully_verified=True).order_by("company_name"),
        required=False,
        label=_("Share my skill analytics with my university"),
        empty_label=_("— Do not share —"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if name == "partner_university" else "form-control"
        self.fields["cv_file"].widget.attrs["accept"] = ".pdf,.docx"
        self.fields["linkedin_pdf"].widget.attrs["accept"] = ".pdf"

    def clean_linkedin_url(self):
        url = self.cleaned_data.get("linkedin_url")
        if url:
            host = (urlparse(url).hostname or "").lower()
            if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
                raise forms.ValidationError(_("Enter a linkedin.com profile URL."))
        return url

    def clean(self):
        data = super().clean()
        if not (data.get("cv_file") or data.get("linkedin_pdf") or data.get("linkedin_url")):
            raise forms.ValidationError(_("Upload your CV, your LinkedIn PDF export, or add your LinkedIn URL."))
        return data


PLATFORM_HOSTS = {"mostaql": "mostaql.com", "khamsat": "khamsat.com", "upwork": "upwork.com", "fiverr": "fiverr.com"}


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
