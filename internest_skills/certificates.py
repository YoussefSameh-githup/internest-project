"""Skill verification by certificate: submitted → under review → verified (skill badge) or rejected.

A link alone is never auto-verified (anyone can paste someone else's certificate URL); links on known
verification sites are only flagged so reviewers can check them first.
"""
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from .models import SkillCertificate, StudentSkill

TRUSTED_ISSUER_HOSTS = ("coursera.org", "credly.com", "udemy.com", "edx.org", "linkedin.com", "credential.net",
                        "accredible.com", "freecodecamp.org", "datacamp.com", "google.com", "microsoft.com", "aws.amazon.com")


def is_trusted_link(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in TRUSTED_ISSUER_HOSTS)


@transaction.atomic
def submit_certificate(student, skill, title, issuer="", url="", file=None) -> SkillCertificate:
    """Store the certificate (under review) and make sure the skill is on the student's list."""
    StudentSkill.objects.get_or_create(student=student, skill=skill,
                                       defaults={"source": StudentSkill.SOURCE_EXPLICIT, "evidence": f"Certificate: {title}"[:300]})
    return SkillCertificate.objects.create(student=student, skill=skill, title=title, issuer=issuer, url=url, file=file,
                                           trusted_issuer=is_trusted_link(url))


@transaction.atomic
def verify_certificate(cert) -> bool:
    """Approve: the skill becomes Verified (an earlier challenge score is kept). True if the skill was upgraded."""
    now = timezone.now()
    cert.status, cert.reviewed_at = SkillCertificate.STATUS_VERIFIED, now
    cert.save(update_fields=["status", "reviewed_at"])
    ss, _created = StudentSkill.objects.get_or_create(student=cert.student, skill=cert.skill,
                                                      defaults={"source": StudentSkill.SOURCE_EXPLICIT})
    if ss.status == StudentSkill.STATUS_VERIFIED:
        return False
    ss.status, ss.verified_via, ss.verified_at = StudentSkill.STATUS_VERIFIED, StudentSkill.VIA_CERTIFICATE, now
    ss.save(update_fields=["status", "verified_via", "verified_at", "updated_at"])
    return True


def reject_certificate(cert):
    cert.status, cert.reviewed_at = SkillCertificate.STATUS_REJECTED, timezone.now()
    cert.save(update_fields=["status", "reviewed_at"])
