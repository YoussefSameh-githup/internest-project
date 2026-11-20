"""Forgot password: email → 6-digit code → new password. Security rules live in password_reset.py.

The flow is tied to the browser session (the email, then the verified code id), so a code entered in one browser
cannot be used to set a password from another, and nothing in the URL identifies the account.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.password_validation import password_validators_help_texts
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from . import identity
from . import password_reset as service

SESSION_EMAIL = "pwreset_email"
SESSION_CODE = "pwreset_code"


class ResetEmailForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("Email"), max_length=254)


class ResetCodeForm(forms.Form):
    code = forms.CharField(label=gettext_lazy("6-digit code"), min_length=6, max_length=6,
                           error_messages={"min_length": gettext_lazy("Enter the 6 digits from the email."),
                                           "max_length": gettext_lazy("Enter the 6 digits from the email.")})

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code.isdigit():
            raise forms.ValidationError(_("Enter the 6 digits from the email."))
        return code


def _status_message(request, status):
    if status == service.COOLDOWN:
        messages.info(request, _("Please wait a minute before asking for another code."))
    elif status == service.LIMITED:
        messages.warning(request, _("Too many codes were requested for this email. Try again in an hour."))
    # SENT: no flash message; the code page itself states the uniform answer (identical for every address).


def password_reset_request(request):
    form = ResetEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = service.normalise(form.cleaned_data["email"])
        status = service.request_code(email)
        request.session[SESSION_EMAIL] = email
        request.session.pop(SESSION_CODE, None)
        _status_message(request, status)
        return redirect("password_reset_verify")
    return render(request, "registration/password_reset_request.html", {"form": form})


def password_reset_verify(request):
    email = request.session.get(SESSION_EMAIL)
    if not email:
        return redirect("password_reset")
    form = ResetCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        row = service.verify_code(email, form.cleaned_data["code"])
        if row is not None:
            request.session.cycle_key()  # new session id once the code is proven
            request.session[SESSION_CODE] = row.pk
            return redirect("password_reset_new")
        form.add_error("code", _("That code is incorrect or has expired. Check your latest email or ask for a new code."))
    return render(request, "registration/password_reset_verify.html", {
        "form": form, "email": email, "cooldown": service.cooldown_remaining(email)})


@require_POST
def password_reset_resend(request):
    email = request.session.get(SESSION_EMAIL)
    if not email:
        return redirect("password_reset")
    _status_message(request, service.request_code(email))
    return redirect("password_reset_verify")


def _login_url_for(user):
    home = "partner_dashboard" if identity.partner_profile(user) is not None else "list"
    return f"{reverse('login')}?{urlencode({'next': reverse(home)})}"


def password_reset_new(request):
    row = service.verified_code(request.session.get(SESSION_CODE))
    if row is None or row.email != request.session.get(SESSION_EMAIL):
        request.session.pop(SESSION_CODE, None)
        messages.error(request, _("This reset step has expired. Ask for a new code."))
        return redirect("password_reset")
    form = SetPasswordForm(row.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = service.set_new_password(row, form.cleaned_data["new_password1"], request)
        if user is None:  # spent by another request a moment ago
            messages.error(request, _("This reset step has expired. Ask for a new code."))
            return redirect("password_reset")
        logout(request)  # whoever was signed in here (and every other session of this user) is signed out
        messages.success(request, _("Your password was changed. Sign in with your username %(username)s and the new password.")
                         % {"username": user.get_username()})
        return redirect(_login_url_for(user))
    return render(request, "registration/password_reset_new.html", {
        "form": form, "had_password": row.user.has_usable_password(), "rules": password_validators_help_texts()})


def legacy_reset_link(request, *args, **kwargs):
    """Django's old emailed reset links and their 'done' pages now lead into the code-based flow."""
    return redirect("password_reset")
