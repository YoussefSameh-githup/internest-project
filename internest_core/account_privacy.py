"""Student self-service privacy (Phase 7): download a copy of your data, delete your account.

Both work from the same list: everything Django would delete with the user (NestedObjects walks every CASCADE relation),
so a new student-owned model is exported and erased without anyone remembering to add it here.

    collect_owned(user)          the user's own rows, grouped by model
    export_student_data(user)    JSON-ready dict of those rows; secrets (password hash, codes, tokens) left out
    delete_student_account(user) delete the rows and, after the commit, the uploaded files they point to

Companies are not self-service: their payment records must be kept, so they use the privacy contact route.
"""
from django.contrib.admin.utils import NestedObjects
from django.core import serializers
from django.db import models, router, transaction
from django.utils import timezone

from . import identity

# Never exported: credentials and one-time secrets.
SECRET_FIELDS = frozenset({"password", "code", "token", "token_secret", "secret", "key"})
# Assessment-integrity signals and reviewers' internal notes. Exporting them after every attempt would teach exactly
# which behaviour trips the cheating checks; the outcome (score, status, termination reason, credits) stays in.
# A student who wants the reviewer notes can ask through the privacy contact route (decided case by case).
HIDDEN_FIELDS = {
    "internest_skills.challengeattempt": {"focus_warnings", "integrity_events", "review_reasons", "reviewed_by", "review_note"},
    "internest_skills.challengeresponse": {"typing_stats", "integrity_flags", "ai_suggestion"},
    "internest_startups.screeningsubmission": {"reviewer_note", "similarity_flag"},
}
# Internal machinery, not personal data the student gave us (job queue payloads, provider tokens we do not store).
SKIP_MODELS = frozenset({"internest_core.asynctask", "socialaccount.socialtoken", "admin.logentry"})


def can_self_serve(user) -> bool:
    """Students only. Companies (payment records) and staff accounts go through the privacy contact route."""
    return (user.is_authenticated and not user.is_staff and not user.is_superuser
            and identity.partner_profile(user) is None and identity.student_profile(user) is not None)


def collect_owned(user):
    collector = NestedObjects(using=router.db_for_write(type(user)))
    collector.collect([user])
    return {model: list(instances) for model, instances in collector.data.items()}


def export_student_data(user) -> dict:
    records = {}
    for model, instances in sorted(collect_owned(user).items(), key=lambda kv: kv[0]._meta.label_lower):
        label = model._meta.label_lower
        if label in SKIP_MODELS:
            continue
        rows = []
        hidden = SECRET_FIELDS | HIDDEN_FIELDS.get(label, set())
        for row in serializers.serialize("python", sorted(instances, key=lambda o: o.pk)):
            fields = {k: v for k, v in row["fields"].items() if k not in hidden}
            rows.append({"id": row["pk"], **fields})
        records[label] = rows
    return {
        "generated_at": timezone.now(),
        "about": "Internest: a copy of the data your account holds. File fields show the stored file name; "
                 "download the files themselves from your profile.",
        "records": records,
    }


def _stored_files(owned):
    files = []
    for model, instances in owned.items():
        file_fields = [f for f in model._meta.concrete_fields if isinstance(f, models.FileField)]
        for obj in instances:
            for field in file_fields:
                value = getattr(obj, field.attname)
                if value:
                    files.append((field.storage, str(value)))
    return files


def delete_student_account(user):
    """Delete the account and everything it owns. Files go only once the database delete has committed."""
    owned = collect_owned(user)
    files = _stored_files(owned)

    def remove_files():
        for storage, name in files:
            try:
                storage.delete(name)
            except OSError:
                pass  # already gone; nothing else to do

    from .models import SecurityEvent

    with transaction.atomic():
        user.delete()
        # Older private files carry the uploader's name in their path; the trail keeps the event, not the name.
        names = [name for _storage, name in files]
        if names:
            SecurityEvent.objects.filter(target__in=names).update(target="(deleted account file)")
        transaction.on_commit(remove_files)
    return len(files)
