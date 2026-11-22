"""Employer screening tasks: the company sets the task, applicants submit, the company scores (see screening.py)."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from internest_core import audit, identity
from internest_core.models import Application, Internship
from internest_core.views import get_user_context

from . import screening
from .gate import startup_partner
from .models import ScreeningTask


class ScreeningTaskForm(forms.ModelForm):
    criteria_text = forms.CharField(
        label=gettext_lazy("Scoring criteria"), widget=forms.Textarea(attrs={"rows": 5, "dir": "auto"}),
        help_text=gettext_lazy('One per line as "criterion | points" (1–10 points, up to 6 lines), e.g. "Correct numbers | 4".'))

    class Meta:
        model = ScreeningTask
        fields = ["title", "instructions", "deliverable", "pass_mark", "due_days", "required"]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 8, "dir": "auto"}), "title": forms.TextInput(attrs={"dir": "auto"})}

    MODE_FULL, MODE_LOCKED, MODE_RELAX = "full", "locked", "relax"

    def __init__(self, *args, mode="full", **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.old_due, self.old_required = self.instance.due_days, self.instance.required
        self.fields["criteria_text"].initial = screening.criteria_text(self.instance.criteria) if self.instance.pk else ""
        if mode != self.MODE_FULL:  # answers exist: what is scored and how must not change under the applicants
            for name in ("criteria_text", "pass_mark", "deliverable"):
                self.fields[name].disabled = True
        if mode == self.MODE_RELAX:  # no paid access any more: the company can only make the task easier to meet
            for name in ("title", "instructions"):
                self.fields[name].disabled = True

    def clean_due_days(self):
        due = self.cleaned_data["due_days"]
        if self.mode != self.MODE_FULL and self.old_due and due < self.old_due:
            raise forms.ValidationError(_("Applicants may already be working on it: the time to submit can only be extended."))
        return due

    def clean_required(self):
        required = self.cleaned_data["required"]
        if self.mode != self.MODE_FULL and required and not self.old_required:
            raise forms.ValidationError(_("An optional task can't become required once applicants have it."))
        return required

    def clean_criteria_text(self):
        if self.mode != self.MODE_FULL:
            return self.instance.criteria
        try:
            return screening.parse_criteria(self.cleaned_data["criteria_text"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc))

    def save(self, commit=True):
        self.instance.criteria = self.cleaned_data["criteria_text"]
        return super().save(commit)


def _safe_link(link) -> bool:
    """http(s) URLs only: the company opens this link, so javascript:, data: and the like are refused."""
    from django.core.validators import URLValidator

    try:
        URLValidator(schemes=["http", "https"])(link)
    except forms.ValidationError:
        return False
    return len(link) <= 500


def _own_internship(request, internship_id):
    partner = startup_partner(request.user)
    if partner is None:
        raise Http404
    return partner, get_object_or_404(Internship, pk=internship_id, partner=partner)


@login_required
def screening_task_edit(request, internship_id):
    partner, internship = _own_internship(request, internship_id)
    task = screening.task_for(internship)
    paid = screening.can_manage(partner, internship)
    if not paid and task is None:
        messages.warning(request, _("Screening tasks come with a Hiring Pass for this opportunity."))
        return redirect("startup_upgrade")
    locked = bool(task and task.submissions.exists())
    # Without paid access an existing task can still be relaxed (made optional, more time), so nobody is stuck behind it.
    mode = ScreeningTaskForm.MODE_RELAX if not paid else (ScreeningTaskForm.MODE_LOCKED if locked else ScreeningTaskForm.MODE_FULL)
    if request.method == "POST" and request.POST.get("delete"):
        if task and mode == ScreeningTaskForm.MODE_FULL:
            task.delete()
            messages.info(request, _("Screening task removed."))
        return redirect("partner_dashboard_section", section="applicants")
    form = ScreeningTaskForm(request.POST or None, instance=task or ScreeningTask(internship=internship), mode=mode)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Screening task saved. Every applicant to this opportunity now sees it in My applications."))
        return redirect("partner_dashboard_section", section="applicants")
    context = get_user_context(request)
    context.update({"form": form, "task": task, "internship": internship, "locked": locked,
                    "relax_only": mode == ScreeningTaskForm.MODE_RELAX})
    return render(request, "startups/screening_task_form.html", context)


@login_required
def screening_submit(request, app_id):
    """The applicant's own page for the task of the opportunity they applied to."""
    app = get_object_or_404(Application.objects.select_related("internship__partner"), pk=app_id, applicant=request.user)
    if identity.student_profile(request.user) is None:
        raise Http404
    task = screening.task_for(app.internship)
    if task is None:
        raise Http404
    submission = screening.submission_for(app)
    if submission is None and app.status not in Application.ACTIVE_STATUSES:
        raise Http404  # the application is closed: nothing left to screen
    error, answer_text, link = None, "", ""
    if request.method == "POST" and submission is None:
        answer_text, link = request.POST.get("answer_text", ""), request.POST.get("link", "").strip()
        try:
            if link and not _safe_link(link):
                raise ValueError(_("Enter a valid link starting with https://"))
            screening.submit(task, app, answer_text, link)
        except ValueError as exc:
            error = str(exc)
        else:
            messages.success(request, _("Task submitted. The company will review it."))
            return redirect("screening_submit", app_id=app.pk)
    context = get_user_context(request)
    context.update({"app": app, "task": task, "submission": submission, "due": screening.due_at(task, app),
                    "is_open": screening.is_open(task, app), "error": error, "answer_text": answer_text, "link": link})
    return render(request, "startups/screening_submit.html", context)


@login_required
@require_POST
def screening_score(request, app_id):
    partner = identity.partner_profile(request.user)
    if partner is None:
        raise Http404
    app = get_object_or_404(Application.objects.select_related("internship"), pk=app_id, internship__partner=partner)
    submission = screening.submission_for(app)
    if submission is None:
        raise Http404
    if not screening.can_manage(partner, app.internship):  # audit L5: scoring is a paid feature of THIS opportunity
        messages.error(request, _("Scoring screening answers comes with a Hiring Pass for this opportunity. "
                                  "You can still make the task optional."))
        return redirect("partner_applicant", app_id=app.pk)
    points = {k.removeprefix("points_"): v for k, v in request.POST.items() if k.startswith("points_")}
    screening.score(submission, points, request.POST.get("note", ""))
    audit.record(request, "screening_scored", target=f"application:{app.pk}")
    messages.success(request, _("Screening score saved."))
    return redirect("partner_applicant", app_id=app.pk)


def panel_context(partner, app):
    """Data for startups/_screening_panel.html on the applicant page (None when nothing should be shown)."""
    task = screening.task_for(app.internship)
    can_manage = screening.can_manage(partner, app.internship)
    submission = screening.submission_for(app) if task else None
    rows = []
    if task:
        given = (submission.scores if submission else {}) or {}
        rows = [{**c, "given": given.get(str(i), "")} for i, c in enumerate(task.criteria or [])]
    return {
        "task": task, "submission": submission, "rows": rows, "can_manage": can_manage,
        "due": screening.due_at(task, app) if task else None,
        "block": screening.shortlist_block_reason(app) if task else None,
        "edit_url": reverse("screening_task_edit", args=[app.internship_id]),
        "score_url": reverse("screening_score", args=[app.pk]),
    }
