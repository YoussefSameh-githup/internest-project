"""Download my data / Delete my account (students). Logic lives in account_privacy; this is the HTTP layer."""
import json

from django import forms
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from . import account_privacy, audit


def _student_or_404(request):
    if not account_privacy.can_self_serve(request.user):
        raise Http404
    return request.user


@login_required
@require_POST
def account_export(request):
    user = _student_or_404(request)
    data = account_privacy.export_student_data(user)
    audit.record(request, "data_export", target=f"user:{user.pk}")
    response = HttpResponse(json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2),
                            content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="internest-data-{timezone.localdate():%Y-%m-%d}.json"'
    response["Cache-Control"] = "private, no-store"
    return response


class DeleteAccountForm(forms.Form):
    """Password when the account has one; accounts created through Google etc. type their email instead."""
    confirm = forms.BooleanField(required=True, error_messages={"required": _("Tick the box to confirm.")})
    password = forms.CharField(required=False, strip=False, widget=forms.PasswordInput)
    email = forms.CharField(required=False)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        if self.user.has_usable_password():
            if not self.user.check_password(cleaned.get("password") or ""):
                self.add_error("password", _("That password isn't right."))
        else:
            expected = (self.user.email or self.user.username or "").strip().lower()
            if not expected or (cleaned.get("email") or "").strip().lower() != expected:
                self.add_error("email", _("Type the email address of this account exactly."))
        return cleaned


@login_required
def account_delete(request):
    user = _student_or_404(request)
    form = DeleteAccountForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        pk = user.pk
        account_privacy.delete_student_account(user)
        logout(request)
        audit.record(request, "account_deleted", target=f"user:{pk}")
        messages.success(request, _("Your account and its data were deleted."))
        return redirect("landing")
    return render(request, "registration/delete_account.html", {"form": form, "uses_password": user.has_usable_password()})
