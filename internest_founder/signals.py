"""Business events from the existing models. Only real transitions emit (not every save), and everything is folded:
daily counters for registrations / publications, one alert per milestone level, one per payment state change."""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from internest_core.models import Application, Internship, PartnerProfile, StudentProfile
from internest_core.signals import internships_bulk_changed
from internest_startups.models import ProUpgradeRequest

from . import events

User = get_user_model()
LOGIN_ONLY = frozenset({"last_login"})


def _today():
    return timezone.localdate().isoformat()


def _skip(raw, update_fields):
    return raw or (update_fields is not None and set(update_fields) <= LOGIN_ONLY)


# ---------------------------------------------------------------- users and privileges
@receiver(pre_save, sender=User)
def _remember_privileges(sender, instance, raw=False, update_fields=None, **kwargs):
    if _skip(raw, update_fields) or not instance.pk:
        return
    instance._founder_was = User.objects.filter(pk=instance.pk).values("is_staff", "is_superuser").first() or {}


@receiver(post_save, sender=User)
def _user_saved(sender, instance, created, raw=False, update_fields=None, **kwargs):
    if _skip(raw, update_fields):
        return
    was = {} if created else getattr(instance, "_founder_was", {})
    granted = [flag for flag in ("is_staff", "is_superuser") if getattr(instance, flag) and not was.get(flag)]
    if granted and (created or was):
        events.emit("security.privileged_account", {"user_id": instance.pk, "granted": granted},
                    dedupe_key=f"privileged:{instance.pk}:{'-'.join(granted)}:{_today()}",
                    notify_as=("critical", "security", "Staff or superuser access granted", "/founder/security/"))
    if created:
        from .models import FounderSettings

        events.milestone("users", User.objects.count(), FounderSettings.load().users_milestone_step, "/founder/users/")


@receiver(post_save, sender=StudentProfile)
def _student_registered(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        events.emit("student.registered", {"user_id": instance.user_id})
        events.notify("info", "business", "New student registrations today", link="/founder/users/",
                      dedupe_key=f"students-registered:{_today()}")


# ---------------------------------------------------------------- companies
@receiver(pre_save, sender=PartnerProfile)
def _remember_verification(sender, instance, raw=False, **kwargs):
    if not raw and instance.pk:
        instance._founder_was_verified = PartnerProfile.objects.filter(pk=instance.pk, is_fully_verified=True).exists()


@receiver(post_save, sender=PartnerProfile)
def _company_saved(sender, instance, created, raw=False, **kwargs):
    if raw or instance.is_academic:
        return
    if created:
        events.emit("company.registered", {"company_id": instance.pk, "verified": instance.is_fully_verified},
                    dedupe_key=f"company-registered:{instance.pk}",
                    notify_as=("info", "business", "New company awaiting verification", "/founder/companies/?status=pending"))
    elif instance.is_fully_verified and not getattr(instance, "_founder_was_verified", True):
        events.emit("company.verified", {"company_id": instance.pk}, dedupe_key=f"company-verified:{instance.pk}")


# ---------------------------------------------------------------- opportunities and applications
def _internship_changed(pk, now_open, was_open):
    if now_open and not was_open:
        events.emit("internship.published", {"internship_id": pk})
        events.notify("info", "business", "Opportunities published today", link="/founder/internships/",
                      dedupe_key=f"internships-published:{_today()}")
    elif was_open and not now_open:
        events.emit("internship.closed", {"internship_id": pk})


@receiver(post_save, sender=Internship)
def _internship_saved(sender, instance, created, raw=False, **kwargs):
    if not raw:  # `_indexnow_was_open` is recorded by internest_core.signals.remember_public_state (one query)
        _internship_changed(instance.pk, instance.is_open, bool(getattr(instance, "_indexnow_was_open", False)))


@receiver(internships_bulk_changed)
def _internships_bulk(sender, pks, is_active, **kwargs):
    for pk in pks:  # only the rows whose public state really changed are sent by the admin action
        _internship_changed(pk, is_active, not is_active)


@receiver(post_save, sender=Application)
def _application_saved(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from .models import FounderSettings

        events.milestone("applications", Application.objects.count(), FounderSettings.load().applications_milestone_step,
                         "/founder/applications/")


# ---------------------------------------------------------------- payments
@receiver(pre_save, sender=ProUpgradeRequest)
def _remember_payment_status(sender, instance, raw=False, **kwargs):
    if not raw and instance.pk:
        instance._founder_was_status = ProUpgradeRequest.objects.filter(pk=instance.pk).values_list("status", flat=True).first()


@receiver(post_save, sender=ProUpgradeRequest)
def _payment_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    before = None if created else getattr(instance, "_founder_was_status", None)
    if before == instance.status:
        return
    data = {"order_id": instance.pk, "product": instance.product, "amount": str(instance.final_price),
            "currency": instance.currency, "gateway": instance.gateway, "status": instance.status}
    if instance.status == ProUpgradeRequest.STATUS_VERIFYING:
        events.emit("payment.receipt_uploaded", data, dedupe_key=f"payment:{instance.pk}:verifying",
                    notify_as=("warning", "business", "Payment receipt to verify", "/founder/finance/?status=verifying"))
    elif instance.status == ProUpgradeRequest.STATUS_ACTIVE:
        events.emit("payment.succeeded", data, dedupe_key=f"payment:{instance.pk}:activated",
                    notify_as=("milestone", "business", "Payment confirmed", "/founder/finance/"))
    elif instance.status == ProUpgradeRequest.STATUS_CANCELLED and before is not None:
        events.emit("payment.cancelled", data, dedupe_key=f"payment:{instance.pk}:cancelled",
                    notify_as=("warning", "business", "Payment order cancelled", "/founder/finance/?status=cancelled"))
