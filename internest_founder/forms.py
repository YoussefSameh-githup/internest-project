from django import forms
from django.utils.translation import gettext_lazy as _

from . import perf
from .models import FounderSettings


class FounderSettingsForm(forms.ModelForm):
    class Meta:
        model = FounderSettings
        fields = ["alert_email", "email_critical", "daily_summary", "users_milestone_step", "applications_milestone_step",
                  "queue_backlog_alert", "disk_alert_percent", "slow_page_ms"]
        labels = {
            "alert_email": _("Alert email"), "email_critical": _("Email critical alerts"), "daily_summary": _("Daily summary email"),
            "users_milestone_step": _("User milestone every"), "applications_milestone_step": _("Application milestone every"),
            "queue_backlog_alert": _("Queue backlog alert at"), "disk_alert_percent": _("Disk alert at (%)"),
            "slow_page_ms": _("Slow page threshold (ms)"),
        }

    def clean_disk_alert_percent(self):
        value = self.cleaned_data["disk_alert_percent"]
        if not 50 <= value <= 99:
            raise forms.ValidationError(_("Choose a value between 50 and 99."))
        return value

    def clean(self):
        data = super().clean()
        for name in ("users_milestone_step", "applications_milestone_step", "queue_backlog_alert", "slow_page_ms"):
            if data.get(name) is not None and data[name] < 1:
                self.add_error(name, _("Must be at least 1."))
        return data


class LighthouseUploadForm(forms.Form):
    report = forms.FileField(label=_("Lighthouse JSON report"))

    def clean_report(self):
        upload = self.cleaned_data["report"]
        if upload.size > 8 * 1024 * 1024:
            raise forms.ValidationError(_("The report is larger than 8 MB."))
        try:
            return perf.loads_report(upload.read())
        except (ValueError, UnicodeDecodeError):
            raise forms.ValidationError(_("This is not a JSON file."))
