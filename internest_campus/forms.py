import re

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import CampusDemandVote, normalize

STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{4,20}$")


class CampusVoteForm(forms.ModelForm):
    class Meta:
        model = CampusDemandVote
        fields = ["university_name", "faculty_name", "department", "student_number"]
        labels = {
            "university_name": _("University name"),
            "faculty_name": _("Faculty / College"),
            "department": _("Department"),
            "student_number": _("Student ID"),
        }
        widgets = {
            "university_name": forms.TextInput(attrs={"class": "form-control", "autocomplete": "organization"}),
            "faculty_name": forms.TextInput(attrs={"class": "form-control"}),
            "department": forms.TextInput(attrs={"class": "form-control"}),
            "student_number": forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric", "autocomplete": "off"}),
        }

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.student = student
        self.fields["department"].required = True

    def _clean_name(self, field):
        value = re.sub(r"\s+", " ", self.cleaned_data.get(field, "")).strip()
        if len(value) < 2:
            raise forms.ValidationError(_("Please enter a valid name."))
        return value

    def clean_university_name(self):
        return self._clean_name("university_name")

    def clean_faculty_name(self):
        return self._clean_name("faculty_name")

    def clean_department(self):
        return self._clean_name("department")

    def clean_student_number(self):
        value = self.cleaned_data.get("student_number", "").strip().upper()
        if not STUDENT_ID_RE.match(value):
            raise forms.ValidationError(_("Student ID must be 4–20 letters or digits."))
        return value

    def clean(self):
        data = super().clean()
        university = data.get("university_name")
        if not university:
            return data
        key = normalize(university)
        votes = CampusDemandVote.objects.filter(university_key=key)
        if self.student is not None and votes.filter(student=self.student).exists():
            raise forms.ValidationError(_("You have already voted for this university."))
        if data.get("student_number") and votes.filter(student_number=data["student_number"]).exists():
            raise forms.ValidationError(_("This student ID has already voted for this university."))
        return data
