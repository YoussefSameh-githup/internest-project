"""Private B2B messaging between verified companies (/startups/messages/)."""
from django.contrib import messages
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.timesince import timesince
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from internest_core.models import PartnerProfile
from internest_core.views import get_user_context

from . import moderation
from .models import DirectMessage
from .permissions import founders_only

THREAD_LIMIT = 200
MAX_LENGTH = 2000


def _thread(me, other):
    """Messages between exactly these two companies — the only way threads are ever queried."""
    return DirectMessage.objects.filter(Q(sender=me, recipient=other) | Q(sender=other, recipient=me))


def _counterpart_or_404(me, partner_id):
    """A verified, non-academic company other than me."""
    if partner_id == me.pk:
        raise Http404
    return get_object_or_404(
        PartnerProfile.objects.select_related("user"), pk=partner_id, is_fully_verified=True, is_academic=False,
    )


def _conversations(me):
    """[{partner, last, unread}] newest first, built from my recent messages."""
    recent = (
        DirectMessage.objects.filter(Q(sender=me) | Q(recipient=me))
        .select_related("sender", "recipient").order_by("-created_at", "-pk")[:500]
    )
    convos = {}
    for msg in recent:
        other = msg.recipient if msg.sender_id == me.pk else msg.sender
        entry = convos.setdefault(other.pk, {"partner": other, "last": msg, "unread": 0})
        if msg.recipient_id == me.pk and not msg.is_read:
            entry["unread"] += 1
    return list(convos.values())


def _as_json(msg, me):
    return {"id": msg.pk, "text": msg.message_text, "mine": msg.sender_id == me.pk,
            "time": msg.created_at.strftime("%H:%M"), "ago": timesince(msg.created_at)}


def _send(request, me, other):
    """Validate + store a message. Returns (message, error)."""
    text = (request.POST.get("message_text") or "").strip()
    if not text:
        return None, _("Write a message first.")
    if len(text) > MAX_LENGTH:
        return None, _("Messages can be at most %(n)s characters.") % {"n": MAX_LENGTH}
    try:
        moderation.check_dm_rate(me)
        moderation.check_keywords(text)
        moderation.check_language(text)
    except moderation.SpamRejected as exc:
        return None, str(exc)
    return DirectMessage.objects.create(sender=me, recipient=other, message_text=text), None


@founders_only
def inbox(request, partner_id=None):
    me = request.founder
    other = _counterpart_or_404(me, partner_id) if partner_id else None
    wants_json = request.headers.get("x-requested-with") == "fetch"

    if request.method == "POST":
        if other is None:
            return redirect("lounge_inbox")
        msg, error = _send(request, me, other)
        if wants_json:
            return JsonResponse({"message": _as_json(msg, me)} if msg else {"error": error}, status=200 if msg else 400)
        if error:
            messages.error(request, error)
        return redirect("lounge_thread", partner_id=other.pk)

    thread = []
    if other is not None:
        thread = list(_thread(me, other).select_related("sender").order_by("-created_at", "-pk")[:THREAD_LIMIT])[::-1]
        _thread(me, other).filter(recipient=me, is_read=False).update(is_read=True)
    conversations = _conversations(me)
    if other is not None and all(c["partner"].pk != other.pk for c in conversations):
        conversations.insert(0, {"partner": other, "last": None, "unread": 0})  # new conversation
    context = get_user_context(request)
    context.update({"me": me, "other": other, "thread": thread, "conversations": conversations, "max_length": MAX_LENGTH})
    return render(request, "lounge/inbox.html", context)


@founders_only
@require_GET
def poll(request, partner_id):
    """New messages in a thread after ?after=<id> (used by the open chat every few seconds)."""
    me = request.founder
    other = _counterpart_or_404(me, partner_id)
    try:
        after = int(request.GET.get("after", 0))
    except ValueError:
        after = 0
    fresh = list(_thread(me, other).filter(pk__gt=after).order_by("created_at", "pk")[:100])
    _thread(me, other).filter(recipient=me, is_read=False, pk__in=[m.pk for m in fresh]).update(is_read=True)
    return JsonResponse({"messages": [_as_json(m, me) for m in fresh]})
