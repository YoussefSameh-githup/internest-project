"""Lightweight server-side anti-spam for the Founders Network (no external services, no cache dependency)."""
import hashlib
import re
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _

POSTS_PER_HOUR = 3
COMMENTS_PER_HOUR = 20
DUPLICATE_WINDOW = timedelta(days=7)

# Rejected outright (never saved).
SPAM_KEYWORDS = re.compile(
    r"\b(crypto(?:currency)?|bitcoin|btc|usdt|forex|binary\s+options?|casino|betting|gambl\w*|"
    r"quick\s+cash|easy\s+money|get\s+rich|double\s+your\s+money|guaranteed\s+(?:profit|income)|"
    r"earn\s+\$?\d+\s*(?:per|a)\s*(?:day|hour)|"
    r"كريبتو|عملات?\s+رقمية|بيتكوين|ربح\s+سريع|كازينو|مراهنات)\b",
    re.IGNORECASE,
)

# Saved but auto-hidden for admin review.
SUSPICIOUS_LINK = re.compile(
    r"(?:t\.me/|telegram\.(?:me|org|dog)|wa\.me/|chat\.whatsapp\.com|api\.whatsapp\.com|"
    r"bit\.ly/|tinyurl\.com/|cutt\.ly/|discord\.gg/)",
    re.IGNORECASE,
)


class SpamRejected(Exception):
    pass


def fingerprint(text: str) -> str:
    normalized = re.sub(r"\W+", " ", (text or "").lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def post_rate_exceeded(author) -> bool:
    """Free tier: max POSTS_PER_HOUR posts per rolling hour (Pro accounts are exempt; see views)."""
    from .models import LoungePost

    since = timezone.now() - timedelta(hours=1)
    return LoungePost.objects.filter(author=author, created_at__gte=since).count() >= POSTS_PER_HOUR


def check_comment_rate(author):
    from .models import LoungeComment

    since = timezone.now() - timedelta(hours=1)
    if LoungeComment.objects.filter(author=author, created_at__gte=since).count() >= COMMENTS_PER_HOUR:
        raise SpamRejected(_("You are commenting too fast. Please try again later."))


def check_keywords(*texts):
    if any(SPAM_KEYWORDS.search(t or "") for t in texts):
        raise SpamRejected(_("Your message looks like spam (blocked keywords) and was not published."))


def auto_hide_reason(title, body, known_fingerprints=()) -> str:
    """Return a HiddenReason value if the content should be held for review, else ''."""
    from .models import HiddenReason

    if SUSPICIOUS_LINK.search(title or "") or SUSPICIOUS_LINK.search(body or ""):
        return HiddenReason.LINK_SPAM
    if fingerprint(body) in known_fingerprints:
        return HiddenReason.DUPLICATE
    return HiddenReason.NONE


def recent_fingerprints():
    from .models import LoungePost

    return set(
        LoungePost.objects.filter(created_at__gte=timezone.now() - DUPLICATE_WINDOW).values_list("body_fingerprint", flat=True)
    )
