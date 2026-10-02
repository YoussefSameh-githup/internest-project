from urllib.parse import quote, urlencode

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Max
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from internest_core.views import _get_partner_profile, _get_student_profile, get_user_context

from .forms import CampusVoteForm
from .models import CAMPAIGN_GOAL, CampusDemandVote


def _share_payload(request, vote):
    link = request.build_absolute_uri(reverse("campus_vote") + "?" + urlencode({"ref": vote.share_ref}))
    text = _(
        "I asked for our faculty to join Internest so we get connected with startups and jobs! "
        "Join me and add your vote so we pass 500 students and send the request to the dean: %(link)s"
    ) % {"link": link}
    return {
        "link": link,
        "text": text,
        "whatsapp": "https://wa.me/?text=" + quote(text),
        "telegram": "https://t.me/share/url?" + urlencode({"url": link, "text": text.replace(link, "").strip()}),
    }


def campus_vote(request):
    student = _get_student_profile(request.user) if request.user.is_authenticated else None
    is_partner = request.user.is_authenticated and _get_partner_profile(request.user) is not None
    ref_vote = CampusDemandVote.from_ref(request.GET.get("ref") or request.POST.get("ref"))

    if request.method == "POST":
        if student is None or is_partner:
            messages.error(request, _("Please log in with a student account to vote."))
            return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
        form = CampusVoteForm(request.POST, student=student)
        if form.is_valid():
            vote = form.save(commit=False)
            vote.student = student
            if ref_vote and ref_vote.student_id != student.pk:
                vote.referred_by = ref_vote
            try:
                with transaction.atomic():
                    vote.save()
            except IntegrityError:
                messages.error(request, _("You have already voted for this university."))
                return redirect("campus_vote")
            messages.success(request, _("Your vote is in! Share your campaign card to reach 500 votes."))
            return redirect(f"{reverse('campus_vote')}?{urlencode({'card': vote.referral_code})}")
    else:
        initial = {}
        if ref_vote:
            initial = {k: getattr(ref_vote, k) for k in ("university_name", "faculty_name", "department")}
        elif student is not None:
            initial = {"university_name": student.university or "", "department": student.major or ""}
        form = CampusVoteForm(initial=initial, student=student)

    card_vote = CampusDemandVote.objects.filter(referral_code=request.GET.get("card", "")).first()
    leaderboard = (
        CampusDemandVote.objects.values("campus_key")
        .annotate(votes=Count("id"), university=Max("university_name"), faculty=Max("faculty_name"))
        .order_by("-votes")[:10]
    )
    context = get_user_context(request)
    context.update({
        "form": form,
        "student": student,
        "can_vote": student is not None and not is_partner,
        "ref_vote": ref_vote,
        "ref": request.GET.get("ref", ""),
        "card_vote": card_vote,
        "share": _share_payload(request, card_vote) if card_vote else None,
        "open_vote": bool(ref_vote) or request.GET.get("open") == "1" or (request.method == "POST"),
        "leaderboard": [dict(row, percent=min(100, row["votes"] * 100 // CAMPAIGN_GOAL)) for row in leaderboard],
        "goal": CAMPAIGN_GOAL,
        "login_url": f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}",
    })
    return render(request, "campus/vote.html", context)
