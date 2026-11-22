"""Build a DISPOSABLE benchmark database and signed-in sessions for bench/loadgen.py.

    SQLITE_PATH=bench/out/bench.sqlite3 python bench/seed.py --students 1000 --reviewers 200

It refuses to run against the real db.sqlite3. Creates: the skill taxonomy (seed_skills), one verified company with an
open internship requiring 5 skills, N onboarded students (university/major/study level, 5 claimed skills, a saved CV),
R staff reviewers each with one written answer awaiting review, and a session + CSRF secret for everyone
(bench/out/sessions.json, bench/out/reviewers.json). Every account gets its own fake client IP so per-IP limits
behave as they would with real visitors.
"""
import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "internest_app_project.settings")

parser = argparse.ArgumentParser()
parser.add_argument("--students", type=int, default=1000)
parser.add_argument("--reviewers", type=int, default=200)
parser.add_argument("--out", default="bench/out")
args = parser.parse_args()

target = os.environ.get("SQLITE_PATH", "")
if not target or Path(target).resolve() == (ROOT / "db.sqlite3").resolve():
    sys.exit("Set SQLITE_PATH to a disposable file (e.g. bench/out/bench.sqlite3); the real database is never seeded.")

import django  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model  # noqa: E402
from django.contrib.auth.hashers import make_password  # noqa: E402
from django.contrib.sessions.backends.db import SessionStore  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.middleware.csrf import _get_new_csrf_string  # noqa: E402
from django.utils import timezone  # noqa: E402

call_command("migrate", verbosity=0)
call_command("seed_skills", verbosity=0)

from internest_core.models import Internship, PartnerProfile, StudentProfile  # noqa: E402
from internest_skills.models import (ChallengeAttempt, ChallengeItem, ChallengeResponse, Skill,  # noqa: E402
                                     StudentSkill, SubSkill)
from internest_students.models import StudentCV  # noqa: E402

User = get_user_model()
PASSWORD = make_password("bench-only-password")  # one hash, reused: hashing 1,000 passwords would take minutes
skills = list(Skill.objects.filter(is_active=True, sub_skills__isnull=False).distinct().order_by("pk")[:5])
if len(skills) < 5:
    sys.exit("seed_skills produced fewer than 5 skills with sub-skills.")

partner_user, _ = User.objects.get_or_create(username="bench_company", defaults={"password": PASSWORD, "email": "co@bench.test"})
partner, _ = PartnerProfile.objects.get_or_create(user=partner_user, defaults={
    "company_name": "Bench Company", "partner_code": "BENCH-CO", "is_fully_verified": True})
internship, _ = Internship.objects.get_or_create(partner=partner, title="Backend Developer Intern", defaults={
    "description": "Build and test REST APIs with Django and SQL.", "location": "Cairo", "required_majors": "Computer Science",
    "deadline": (timezone.now() + timedelta(days=60)).date(), "is_active": True})
for skill in skills:
    skill.opportunities.add(internship)

study_level = StudentProfile._meta.get_field("study_level").choices[0][0]
existing = set(User.objects.filter(username__startswith="bench_s").values_list("username", flat=True))
new_users = [User(username=f"bench_s{i:05d}", password=PASSWORD, email=f"s{i}@bench.test", first_name="Bench", last_name=f"S{i}")
             for i in range(args.students) if f"bench_s{i:05d}" not in existing]
User.objects.bulk_create(new_users, batch_size=500)
students_users = list(User.objects.filter(username__startswith="bench_s").order_by("username")[:args.students])
have_profile = set(StudentProfile.objects.filter(user__in=students_users).values_list("user_id", flat=True))
StudentProfile.objects.bulk_create([
    StudentProfile(user=u, university="Cairo University", major="Computer Science", study_level=study_level,
                   personal_email=None, bio="Backend-focused student.")
    for u in students_users if u.pk not in have_profile], batch_size=500)
profiles = list(StudentProfile.objects.filter(user__in=students_users).select_related("user"))
have_skill = set(StudentSkill.objects.filter(student__in=profiles).values_list("student_id", "skill_id"))
StudentSkill.objects.bulk_create([StudentSkill(student=p, skill=s) for p in profiles for s in skills
                                  if (p.pk, s.pk) not in have_skill], batch_size=1000)
cv_template = {"name": "", "email": "", "phone": "", "linkedin": "", "github": "",
               "summary": "Computer science student building backend services.",
               "education": [{"university": "Cairo University", "major": "Computer Science", "start_date": "2022-09",
                              "end_date": "", "is_current": True}],
               "experiences": [{"title": "Backend Intern", "organization": "Acme", "achievements": [
                   "Built a REST API for orders", "Wrote tests for the payment module"]}],
               "projects": [{"title": "Library system", "description": "Django app for borrowing books.",
                             "tech_stack": "Django, SQLite", "link": ""}],
               "skills": [s.name for s in skills]}
have_cv = set(StudentCV.objects.filter(student__in=profiles).values_list("student_id", flat=True))
StudentCV.objects.bulk_create([StudentCV(student=p, data={**cv_template, "name": p.user.get_full_name(),
                                                          "email": p.user.email}) for p in profiles if p.pk not in have_cv],
                              batch_size=500)

# Reviewers: staff accounts, each with one written answer waiting for review (feature 4 benchmark).
sub_skill = SubSkill.objects.filter(skill=skills[0]).first()
item, _ = ChallengeItem.objects.get_or_create(skill=skills[0], sub_skill=sub_skill, kind=ChallengeItem.KIND_PRACTICAL,
                                              prompt="Design a REST endpoint that lists a user's orders with pagination.",
                                              defaults={"rubric": {"criteria": ["pagination", "auth", "status codes"]}})
reviewer_users = []
for i in range(args.reviewers):
    user, _ = User.objects.get_or_create(username=f"bench_r{i:04d}", defaults={"password": PASSWORD, "is_staff": True,
                                                                               "email": f"r{i}@bench.test"})
    reviewer_users.append(user)
answers = []
for i, student_skill in enumerate(StudentSkill.objects.filter(student__in=profiles[:args.reviewers], skill=skills[0])):
    attempt = ChallengeAttempt.objects.create(student_skill=student_skill, review_status=ChallengeAttempt.REVIEW_PENDING)
    answers.append(ChallengeResponse.objects.create(
        attempt=attempt, item=item, answer_text="GET /api/orders/?page=2 returns 20 orders, 401 when signed out.").pk)


def sessions_for(users, extra=lambda i, u: {}):
    rows = []
    for i, user in enumerate(users):
        store = SessionStore()
        store[SESSION_KEY] = str(user.pk)
        store[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        store.create()
        rows.append({"username": user.username, "sessionid": store.session_key, "csrftoken": _get_new_csrf_string(),
                     "ip": f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}", **extra(i, user)})
    return rows


out = Path(args.out)
out.mkdir(parents=True, exist_ok=True)
(out / "sessions.json").write_text(json.dumps(sessions_for(
    students_users, lambda i, u: {"skill_id": skills[i % len(skills)].pk, "internship_id": internship.pk}), indent=0))
(out / "reviewers.json").write_text(json.dumps(sessions_for(
    reviewer_users, lambda i, u: {"response_id": answers[i] if i < len(answers) else None}), indent=0))
print(f"Seeded {len(students_users)} students, {len(reviewer_users)} reviewers, internship {internship.pk}, "
      f"skills {[s.name for s in skills]} into {target}")
