"""API-surface security sweep (OWASP API1 BOLA / API3 property exposure / API5 function-level authorization).

A victim student and a victim company own one of every private object reachable through a URL parameter. Every
named route of the site is then called with the victim's ids by attackers who must not reach them (anonymous,
another student, another company, a university), with GET and with POST. The sweep fails when:
  - any request ends in a server error (500 or an exception);
  - a victim's private marker (e-mail, LinkedIn, kit, screening, gig, message, CV analysis text) appears in a response;
  - any victim-owned row changes in the database (an attacker's POST must never modify someone else's data).
Per-route behaviour (exact status codes, owner flows) is covered by each app's own tests; this sweep is the net that
catches a route whose ownership check was forgotten.
"""
import hashlib
import json
import pathlib
import re
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.conf import settings
from django.core import serializers
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from internest_careers.models import InterviewKit
from internest_lounge.models import DirectMessage, LoungePost
from internest_skills.models import ChallengeAttempt, CVAnalysis, StudentFreelanceGig, StudentSkill
from internest_startups.models import CompanyProfile, ScreeningSubmission, ScreeningTask

from .models import Application, AsyncTask, Internship, PartnerApplicantData, PartnerProfile, StudentProfile
from .tests_e2e import MEDIA, PASSWORD, E2EBase

MARK = "ZQVICTIMPRIVATEZQ"
VICTIM_EMAIL = "zqvictim.private@uni.edu"
VICTIM_LINKEDIN = "https://linkedin.com/in/zqvictim-private"
SKIP = {
    "logout", "account_delete",  # would end the attacker's own session / account mid-sweep (owner-only, tested elsewhere)
    "set_language",
}


def named_routes():
    def walk(resolver, namespace=""):
        for entry in resolver.url_patterns:
            if isinstance(entry, URLResolver):
                if entry.namespace == "admin" or str(entry.pattern).startswith(("admin", "i18n")):
                    continue
                yield from walk(entry, f"{namespace}{entry.namespace}:" if entry.namespace else namespace)
            elif isinstance(entry, URLPattern) and entry.name:
                yield f"{namespace}{entry.name}", str(entry.pattern)  # namespaced apps (founder:...) are swept too
    return sorted(set(walk(get_resolver())))


@override_settings(MEDIA_ROOT=MEDIA, RATELIMIT_ENABLED=False)
class CrossTenantSweepTests(E2EBase):
    def _victims(self):
        v_user, v_profile = self.ready_student("zqvictim")
        v_user.email = VICTIM_EMAIL
        v_user.save(update_fields=["email"])
        StudentProfile.objects.filter(pk=v_profile.pk).update(personal_email=VICTIM_EMAIL, linkedin_url=VICTIM_LINKEDIN)
        co_user = User.objects.create_user("zqvictimco", email="owner@zqvictimco.io", password=PASSWORD)
        co = PartnerProfile.objects.create(user=co_user, company_name="Victim Co", partner_code="VCO1", is_fully_verified=True)
        CompanyProfile.objects.create(partner=co, industry="software", founded_year=2020, description="victim company")
        job = Internship.objects.create(partner=co, title="Victim job", description="d", location="Cairo",
                                        required_majors="any", deadline=timezone.localdate() + timedelta(days=9))
        app = Application.objects.create(internship=job, applicant=v_user, contact_consent_at=timezone.now())
        PartnerApplicantData.objects.create(partner=co, student=v_profile, internship=job, application=app)
        task = ScreeningTask.objects.create(internship=job, title="Task", instructions=f"{MARK} screening instructions")
        ScreeningSubmission.objects.create(task=task, application=app)
        kit = InterviewKit.objects.create(owner=co_user, role=f"{MARK} kit role")
        analysis = CVAnalysis.objects.create(user=v_user, file_sha256="0" * 64)
        gig = StudentFreelanceGig.objects.create(student=v_profile, platform="upwork", link=f"https://upwork.com/{MARK}",
                                                 raw_text=f"{MARK} gig")
        skill = StudentSkill.objects.filter(student=v_profile).first()
        attempt = ChallengeAttempt.objects.create(student_skill=skill)
        third = User.objects.create_user("zqthird", password=PASSWORD)
        third_co = PartnerProfile.objects.create(user=third, company_name="Third", partner_code="TH1", is_fully_verified=True)
        DirectMessage.objects.create(sender=co, recipient=third_co, message_text=f"{MARK} private message")
        post = LoungePost.objects.create(author=co, body="Victim lounge post", body_fingerprint="zq-fp")
        task_row = AsyncTask.objects.create(kind="cv_polish", user=v_user)
        ids = {
            "pk": job.pk, "internship_id": job.pk, "app_id": app.pk, "student_id": v_profile.pk,
            "student_skill_id": skill.pk, "skill_id": skill.skill_id, "analysis_id": analysis.pk, "gig_id": gig.pk,
            "kit_id": kit.pk, "token": attempt.token, "task_id": task_row.pk, "partner_id": third_co.pk,
            "response_id": 999999, "order_id": 1, "outcome": "success", "enrollment_id": 1, "course_id": 1,
            "certificate_id": uuid.uuid4(), "uuid": uuid.uuid4(), "key": "welcome", "section": "overview",
            "email_type": "personal", "role": "student", "provider": "google", "filepath": "cvs/x.pdf",
        }
        lounge_ids = {"pk": post.pk}  # lounge routes take a post id, not an internship id
        return ids, lounge_ids

    def _fingerprint(self):
        """Every victim-owned row, serialised: an attacker request must leave all of it byte-for-byte unchanged."""
        victim_users = User.objects.filter(username__in=["zqvictim", "zqvictimco"])
        querysets = [
            victim_users, StudentProfile.objects.filter(user__in=victim_users),
            PartnerProfile.objects.filter(user__in=victim_users), Internship.objects.filter(partner__user__in=victim_users),
            Application.objects.filter(applicant__in=victim_users), ScreeningTask.objects.all(),
            ScreeningSubmission.objects.all(), InterviewKit.objects.filter(owner__in=victim_users),
            CVAnalysis.objects.filter(user__in=victim_users), StudentFreelanceGig.objects.filter(student__user__in=victim_users),
            ChallengeAttempt.objects.filter(student_skill__student__user__in=victim_users),
            StudentSkill.objects.filter(student__user__in=victim_users), LoungePost.objects.filter(author__user__in=victim_users),
            DirectMessage.objects.filter(sender__user__in=victim_users), AsyncTask.objects.filter(user__in=victim_users),
        ]
        data = []
        for qs in querysets:
            rows = json.loads(serializers.serialize("json", qs.order_by("pk")))
            for row in rows:  # the session-sliding / last-seen bookkeeping of a user is not owned data
                row["fields"].pop("last_login", None)
                # Upvoting another founder's lounge post is the feature itself (asserted separately: founders only).
                row["fields"].pop("upvoters", None)
            data.append(rows)
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest(), data

    def _attackers(self):
        a_user, _ = self.ready_student("zqattacker")
        student = self.client_class()
        student.cookies["internest_lang"] = "en"
        self.sign_in_student(student, a_user)
        free_user = User.objects.create_user("zqfreeco", password=PASSWORD)
        free = PartnerProfile.objects.create(user=free_user, company_name="Free Co", partner_code="FR1", is_fully_verified=True)
        CompanyProfile.objects.create(partner=free, industry="software", founded_year=2021, description="other company")
        uni_user = User.objects.create_user("zquni", password=PASSWORD)
        PartnerProfile.objects.create(user=uni_user, company_name="Uni", partner_code="UN1", is_academic=True,
                                      is_fully_verified=True)
        clients = {"anonymous": self.client_class(), "student": student}
        for role, user in (("company", free_user), ("university", uni_user)):
            c = self.client_class()
            c.force_login(user)
            clients[role] = c
        for c in clients.values():
            c.cookies["internest_lang"] = "en"
            c.raise_request_exception = False
        return clients

    @staticmethod
    def _handled_unavailable(response):
        """503 with the shared JSON error shape (e.g. ai_unavailable, queue_full) is a designed answer, not a crash."""
        if response.status_code != 503 or "json" not in response.get("Content-Type", ""):
            return False
        try:
            return bool(json.loads(response.content).get("code"))
        except ValueError:
            return False

    def test_no_attacker_reads_or_changes_another_accounts_data(self):
        ids, lounge_ids = self._victims()
        clients = self._attackers()
        before, before_rows = self._fingerprint()
        errors, leaks, unbuildable, called = [], [], [], 0
        for name, pattern in named_routes():
            if name in SKIP:
                continue
            params = re.findall(r"<(?:\w+:)?(\w+)>|\(\?P<(\w+)>", pattern)
            names = [a or b for a, b in params]
            source = lounge_ids if name.startswith("lounge_") and "pk" in names else ids
            if name in ("take_task", "task_result"):  # gamification task ids are ints, async task ids are UUIDs
                source = {**ids, "task_id": 1}
            if any(n not in source and n not in ids for n in names):
                continue
            try:
                url = reverse(name, kwargs={n: source.get(n, ids.get(n)) for n in names})
            except NoReverseMatch:
                unbuildable.append(name)
                continue
            for role, client in clients.items():
                for method in ("get", "post"):
                    response = getattr(client, method)(url)
                    called += 1
                    if response.status_code >= 500 and not self._handled_unavailable(response):
                        errors.append(f"{method.upper()} {url} as {role}: {response.status_code}")
                        continue
                    body = b"" if getattr(response, "streaming", False) else response.content
                    for marker in (MARK, VICTIM_EMAIL, VICTIM_LINKEDIN):
                        if marker.encode() in body:
                            leaks.append(f"{method.upper()} {url} as {role}: shows {marker[:18]}…")
        after, after_rows = self._fingerprint()
        changed = []
        for rows_before, rows_after in zip(before_rows, after_rows):
            after_by_pk = {(r["model"], r["pk"]): r for r in rows_after}
            for row in rows_before:
                now = after_by_pk.get((row["model"], row["pk"]))
                if now is None:
                    changed.append(f"{row['model']} #{row['pk']} deleted")
                elif now != row:
                    diff = sorted(k for k in row["fields"] if row["fields"][k] != now["fields"].get(k))
                    changed.append(f"{row['model']} #{row['pk']} changed {diff}")
        self.assertEqual(unbuildable, [])
        self.assertGreater(called, 400)
        self.assertEqual(errors, [])
        self.assertEqual(leaks, [])
        self.assertEqual(changed, [])
        upvoters = set(LoungePost.objects.get(pk=lounge_ids["pk"]).upvoters.values_list("user__username", flat=True))
        self.assertTrue(upvoters <= {"zqfreeco"}, upvoters)  # never a student, a university or an anonymous visitor
        self.assertEqual(before, after)


class JsonErrorContractTests(SimpleTestCase):
    """Every JSON error the apps build carries the machine-readable "code" next to the human "error" message
    (docs: INTERNEST_API_DOCUMENTATION.md). Static scan, so a new endpoint cannot ship a code-less error."""

    ERROR_BODY = re.compile(r"""(JsonResponse\(\{|return\s+[45]\d\d,\s*\{)(?P<body>(?:[^{}]|\{[^{}]*\})*)\}""", re.S)

    def test_error_bodies_have_a_code(self):
        root = pathlib.Path(settings.BASE_DIR)
        missing = []
        for path in sorted(root.glob("internest_*/**/*.py")):
            if path.name.startswith("tests") or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for match in self.ERROR_BODY.finditer(text):
                body = match.group("body")
                if '"error":' in body and '"code"' not in body:
                    line = text.count("\n", 0, match.start()) + 1
                    missing.append(f"{path.relative_to(root).as_posix()}:{line}")
        self.assertEqual(missing, [])


class CsrfFailureShapeTests(TestCase):
    def test_api_csrf_failures_are_json_and_pages_keep_the_page(self):
        client = Client(enforce_csrf_checks=True)
        api = client.post(reverse("api_resume_polish"))
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api.json()["code"], "csrf_failed")
        page = client.post(reverse("login"), {"username": "x", "password": "y"})
        self.assertEqual(page.status_code, 403)
        self.assertIn("text/html", page["Content-Type"])
