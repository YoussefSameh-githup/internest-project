"""Forgot password with a 6-digit email code (students, startups, universities: any account with an email).

    request_code(email)          always answers the same way; sends a code only to a matching, eligible account
    verify_code(email, code)     constant-time check; 5 tries per code, 20 wrong tries per address per day
    set_new_password(...)        re-checks the account, saves a validated password, spends the code, ends every other
                                 session, records an audit event and emails a "password changed" notice

Anti-enumeration: the page never says whether an address is registered. Limits are kept per address hash in the
database (PasswordResetThrottle) for registered and unregistered addresses alike, every failure reads the same, and
a registered address's request is padded to a minimum time so sending the email doesn't stand out. Staff/admin
accounts are excluded (they use `manage.py changepassword`). Only verified addresses receive codes.

Guessing budget for one address: 20 wrong codes per day → at most 2 × 10⁻⁵ chance per day of hitting a 6-digit code.
Only wrong tries against a code that was really sent count, and the owner is emailed when the budget runs out. Residual
trade-off (documented): someone who keeps requesting codes for an address and burning them can block that address's
reset for a day; the account's normal sign-in is unaffected.
"""
import hashlib
import hmac
import logging
import random
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone, translation

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5                          # wrong tries per code
MAX_FAILURES_PER_DAY = 20                 # wrong tries per address per 24 h, across all its codes
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_CODES_PER_EMAIL_PER_HOUR = 5
VERIFIED_WINDOW = timedelta(minutes=10)   # time to choose the new password after a correct code
MIN_REQUEST_SECONDS = 1.0                 # response-time floor when a code may be sent (0 in tests)

SENT, COOLDOWN, LIMITED = "sent", "cooldown", "limited"


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def _email_key(email: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), b"pwreset:" + normalise(email).encode(), hashlib.sha256).hexdigest()


def _hash(code: str, salt: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{salt}:{code}".encode(), hashlib.sha256).hexdigest()


def find_user(email: str):
    """The one active, non-staff account using this address, else None.

    Matches the login email, or a student's personal email only if that address was proven with an email code at
    some point (EmailVerification.verified_at). The profile's verified flag can't be used: every login clears it until
    the student enters a fresh code. An unverified or mistyped address never receives a code."""
    email = normalise(email)
    if not email:
        return None
    users = list(
        get_user_model().objects.filter(is_active=True, is_staff=False, is_superuser=False)
        .filter(Q(email__iexact=email)
                | Q(studentprofile__personal_email__iexact=email, email_verifications__email__iexact=email,
                    email_verifications__verified_at__isnull=False))
        .distinct()[:2]
    )
    if len(users) != 1:  # none, or two accounts share an address: send nothing rather than guess
        if len(users) > 1:
            logger.warning("Password reset requested for an address shared by several accounts")
        return None
    return users[0]


def _throttle(email):
    from .models import PasswordResetThrottle

    row, _ = PasswordResetThrottle.objects.get_or_create(email_key=_email_key(email))
    return row


def cooldown_remaining(email: str) -> int:
    from .models import PasswordResetThrottle

    row = PasswordResetThrottle.objects.filter(email_key=_email_key(email)).first()
    if row is None or row.last_sent_at is None:
        return 0
    return max(0, int((row.last_sent_at + RESEND_COOLDOWN - timezone.now()).total_seconds()))


def _claim_send(email):
    """Atomically take one send from the address's budget: COOLDOWN / LIMITED (nothing counted) or SENT."""
    from .models import PasswordResetThrottle

    now = timezone.now()
    row = _throttle(email)
    if row.last_sent_at and now - row.last_sent_at < RESEND_COOLDOWN:
        return COOLDOWN
    if now - row.window_start >= timedelta(hours=1):
        PasswordResetThrottle.objects.filter(pk=row.pk, window_start=row.window_start).update(window_start=now, sends=0)
        row.refresh_from_db()
    if row.sends >= MAX_CODES_PER_EMAIL_PER_HOUR:
        return LIMITED
    # Conditional update: of two parallel requests only one can take the slot; the other sees the cooldown.
    taken = PasswordResetThrottle.objects.filter(pk=row.pk, sends=row.sends, last_sent_at=row.last_sent_at).update(
        sends=F("sends") + 1, last_sent_at=now)
    return SENT if taken else COOLDOWN


def _send(user, email, template, subject, extra=None):
    """One bilingual email (Arabic + English, the visitor's language first). Codes are never logged."""
    ar_first = (translation.get_language() or settings.LANGUAGE_CODE).startswith("ar")
    context = {"username": user.get_username(), "ar_first": ar_first, "langs": ["ar", "en"] if ar_first else ["en", "ar"],
               "minutes": int(CODE_TTL.total_seconds() // 60), **(extra or {})}
    subjects = []
    for lang in context["langs"]:
        with translation.override(lang):
            subjects.append(translation.gettext(subject))
    msg = EmailMultiAlternatives(subject=" · ".join(subjects), body=render_to_string(f"emails/{template}.txt", context),
                                 from_email=settings.DEFAULT_FROM_EMAIL, to=[email])
    msg.attach_alternative(render_to_string(f"emails/{template}.html", context), "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception:  # noqa: BLE001 - the page answers the same either way; the outage shows in the API monitor/logs
        logger.exception("Password reset email (%s) could not be sent", template)


def request_code(email: str) -> str:
    """Issue and email a code if the address belongs to an eligible account. Returns SENT / COOLDOWN / LIMITED; the
    answer depends only on the address's own limits, never on whether an account exists."""
    email = normalise(email)
    status = _claim_send(email)
    if status != SENT:
        return status
    started = time.monotonic()
    try:
        user = find_user(email)
        if user is not None:
            from .models import PasswordResetCode

            code = f"{secrets.randbelow(10 ** 6):06d}"
            salt = secrets.token_hex(16)
            now = timezone.now()
            with transaction.atomic():
                # Older unverified codes stop working; a code already verified in another browser keeps its window.
                PasswordResetCode.objects.filter(user=user, used_at__isnull=True, verified_at__isnull=True).update(used_at=now)
                PasswordResetCode.objects.create(user=user, email=email, salt=salt, code_hash=_hash(code, salt),
                                                 created_at=now, expires_at=now + CODE_TTL)
            _send(user, email, "password_reset_code", "Your Internest password reset code", {"code": code})
        return SENT
    finally:
        floor = getattr(settings, "PASSWORD_RESET_MIN_SECONDS", MIN_REQUEST_SECONDS)
        left = floor + (random.uniform(0, 0.3) if floor else 0) - (time.monotonic() - started)
        if left > 0:
            time.sleep(left)


def _live_code(email: str):
    from .models import PasswordResetCode

    return (PasswordResetCode.objects.filter(email=normalise(email), used_at__isnull=True, verified_at__isnull=True,
                                             expires_at__gt=timezone.now())
            .select_related("user").order_by("-created_at").first())


def _over_daily_failures(email) -> bool:
    from .models import PasswordResetThrottle

    row = _throttle(email)
    if timezone.now() - row.fail_window_start >= timedelta(days=1):
        PasswordResetThrottle.objects.filter(pk=row.pk).update(fail_window_start=timezone.now(), failures=0)
        return False
    return row.failures >= MAX_FAILURES_PER_DAY


def _count_failure(email) -> int:
    from .models import PasswordResetThrottle

    PasswordResetThrottle.objects.filter(email_key=_email_key(email)).update(failures=F("failures") + 1)
    return PasswordResetThrottle.objects.filter(email_key=_email_key(email)).values_list("failures", flat=True).first() or 0


def purge_stale(days=2) -> int:
    """Delete limit rows idle for `days` (run daily with purge_security_events)."""
    from .models import PasswordResetCode, PasswordResetThrottle

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = (PasswordResetThrottle.objects.filter(window_start__lt=cutoff, fail_window_start__lt=cutoff)
                  .filter(Q(last_sent_at__isnull=True) | Q(last_sent_at__lt=cutoff)).delete())
    PasswordResetCode.objects.filter(created_at__lt=cutoff - timedelta(days=28)).delete()  # spent codes, kept a month
    return deleted


def verify_code(email: str, code: str):
    """The PasswordResetCode when `code` is right, else None. The try is counted BEFORE the comparison with a
    conditional UPDATE, so parallel requests can't get more than 5 tries on one code."""
    from .models import PasswordResetCode

    code = (code or "").strip()
    if _over_daily_failures(email):
        return None
    row = _live_code(email)
    if row is None:  # nothing to guess: not counted, so nobody can lock an address without a code being sent to it
        return None
    if not PasswordResetCode.objects.filter(pk=row.pk, used_at__isnull=True, attempts__lt=MAX_ATTEMPTS).update(
            attempts=F("attempts") + 1):
        return None
    if len(code) == 6 and code.isdigit() and hmac.compare_digest(_hash(code, row.salt), row.code_hash):
        PasswordResetCode.objects.filter(pk=row.pk).update(verified_at=timezone.now())
        row.refresh_from_db()
        return row
    if _count_failure(email) == MAX_FAILURES_PER_DAY:  # just ran out: tell the owner, once
        _send(row.user, row.email, "password_reset_alert", "Many wrong codes were entered for your Internest account")
    PasswordResetCode.objects.filter(pk=row.pk, used_at__isnull=True, attempts__gte=MAX_ATTEMPTS).update(used_at=timezone.now())
    return None


def verified_code(code_id):
    """The verified, unspent code behind a session, still inside its window; else None."""
    from .models import PasswordResetCode

    if not code_id:
        return None
    return (PasswordResetCode.objects.filter(
        pk=code_id, used_at__isnull=True, verified_at__isnull=False,
        verified_at__gt=timezone.now() - VERIFIED_WINDOW).select_related("user").first())


def set_new_password(code_row, password: str, request=None):
    """Save the (already validated) password and spend the code. Returns the user, or None when the code was spent
    meanwhile or the account no longer matches the address (deactivated, made staff, email changed)."""
    from . import audit
    from .models import PasswordResetCode

    current = find_user(code_row.email)
    if current is None or current.pk != code_row.user_id:
        PasswordResetCode.objects.filter(pk=code_row.pk).update(used_at=timezone.now())
        return None
    with transaction.atomic():
        spent = PasswordResetCode.objects.filter(pk=code_row.pk, used_at__isnull=True).update(used_at=timezone.now())
        if not spent:
            return None
        user = current
        user.set_password(password)
        user.save(update_fields=["password"])
        PasswordResetCode.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
    audit.record(request, "password_reset", target=f"user:{user.pk}", actor=user)
    from allauth.socialaccount.models import SocialAccount

    names = {"google": "Google", "linkedin_oauth2": "LinkedIn", "github": "GitHub", "microsoft": "Microsoft"}
    providers = sorted({names.get(p, p) for p in SocialAccount.objects.filter(user=user).values_list("provider", flat=True)})
    _send(user, code_row.email, "password_changed", "Your Internest password was changed", {"providers": providers})
    return user
