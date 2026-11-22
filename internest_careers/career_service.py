"""Career recommendations: role paths derived from open internships, matched against the student's skills.

    recommendations(student)   -> {"roles": [...], "internships": [...], ...}  plain data (template + JSON API)
    role_profiles()            -> the shared role paths (cached for everyone)
    role_by_key("data analyst")-> {"title", "skills": [Skill]} for interview practice

Role paths: open internships grouped by normalised title (seniority / "intern" words dropped); groups with the same
discipline and core skills are merged. Core skills = skills required by at least half of a path's internships.
Every item carries a "why" sentence built only from these numbers. Coverage uses the semantic matcher
(internest_skills.semantic), gaps get courses (recommendations.courses_for_skill) and a curated project idea,
best-fit internships come from matching.bulk_match_scores. No AI: the optional narrative is `advice()`.

Caching: role paths live in page_cache group "careers" (bumped when an internship, its skills or a course change);
each student's result lives in group "careers:<student id>" (bumped when one of their skills changes) and its key
carries the role paths' token, the day and the language. See signals.py.
"""
import re
import uuid
from collections import Counter
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.text import slugify
from django.utils.translation import get_language, gettext as _

from internest_core.models import Internship
from internest_core.page_cache import cached_value
from internest_skills.matching import MATCH_THRESHOLD, SKILL_COVERED, bulk_match_scores
from internest_skills.models import Discipline, Skill, StudentSkill
from internest_skills.recommendations import courses_for_skill
from internest_skills.semantic import aggregate_score, relation_map

from . import question_bank

MAX_INTERNSHIPS = 300      # open internships considered (most recent first)
MAX_CORE_SKILLS = 8
MAX_ROLES = 6
MAX_GAPS = 3
MAX_BEST_FIT = 5
COURSES_PER_GAP = 2
ALMOST_THRESHOLD = 40
ROLES_TTL = 6 * 3600
STUDENT_TTL = 6 * 3600

STOPWORDS = {
    "intern", "interns", "internship", "internships", "trainee", "training", "junior", "jr", "senior", "sr", "part",
    "time", "full", "remote", "summer", "winter", "program", "programme", "position", "role", "opportunity", "student",
    "students", "graduate", "fresh", "entry", "level", "and", "the", "of", "for", "in", "at", "a", "an",
    "متدرب", "متدربة", "متدربين", "تدريب", "تدريبي", "تدريبية", "صيفي", "صيفية", "فرصة", "برنامج", "جونيور",
    "عن", "بعد", "دوام", "جزئي", "كامل", "طالب", "طلاب", "حديث", "التخرج", "في",
}


def normalize_title(title: str) -> str:
    words = re.findall(r"[a-z0-9+#]+|[؀-ۿ]+", (title or "").lower())
    return " ".join(w for w in words if w not in STOPWORDS and not w.isdigit())


ACRONYMS = {"ui", "ux", "seo", "hr", "qa", "it", "ai", "sql", "php", "erp", "crm"}


def _display(key: str) -> str:
    """"ui ux designer" -> "UI UX Designer"; Arabic words stay as they are."""
    return " ".join(w.upper() if w in ACRONYMS else w if re.search(r"[؀-ۿ]", w) else w.capitalize()
                    for w in key.split())


# ---------------------------------------------------------------- shared role paths
def _compute_role_profiles() -> dict:
    internships = (Internship.objects.open().prefetch_related("required_skills").order_by("-created_at")
                   .only("id", "title")[:MAX_INTERNSHIPS])
    groups = {}
    for internship in internships:
        skills = list(internship.required_skills.all())
        key = normalize_title(internship.title)
        if not skills or not key:
            continue
        group = groups.setdefault(key, {"ids": [], "demand": Counter(), "skills": {}})
        group["ids"].append(internship.id)
        for skill in skills:
            group["demand"][skill.id] += 1
            group["skills"][skill.id] = skill
    merged = {}
    # Most openings first, then the shortest (most generic) title names a merged path.
    for key, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]["ids"]), len(kv[0]), kv[0])):
        n = len(group["ids"])
        ranked = sorted(group["demand"].items(), key=lambda kv: (-kv[1], group["skills"][kv[0]].name))
        core = [sid for sid, count in ranked if count * 2 >= n][:MAX_CORE_SKILLS] or [ranked[0][0]]
        discipline = Counter(group["skills"][sid].discipline for sid in core).most_common(1)[0][0]
        signature = (discipline, frozenset(core))
        if signature in merged:  # "Marketing Intern" and "Digital Marketing Trainee" asking for the same skills
            target = merged[signature]
            target["internship_ids"] += group["ids"]
            for item in target["skills"]:
                item["demand"] += group["demand"].get(item["id"], 0)
            continue
        merged[signature] = {
            "key": slugify(key, allow_unicode=True), "title": _display(key), "discipline": discipline,
            "internship_ids": list(group["ids"]),
            "skills": [{"id": sid, "name": group["skills"][sid].name, "discipline": group["skills"][sid].discipline,
                        "demand": group["demand"][sid]} for sid in core],
        }
    roles = list(merged.values())
    for role in roles:
        role["openings"] = len(role["internship_ids"])
    return {"token": uuid.uuid4().hex[:12], "roles": roles}


def role_profiles() -> dict:
    """{"token", "roles": [...]} shared by every student (plain data)."""
    return cached_value("careers", f"roles:{timezone.localdate():%Y%m%d}", ROLES_TTL, _compute_role_profiles)


def role_by_key(key: str):
    role = next((r for r in role_profiles()["roles"] if r["key"] == key), None)
    if role is None:
        return None
    return {"key": role["key"], "title": role["title"],
            "skills": list(Skill.objects.filter(id__in=[s["id"] for s in role["skills"]]).order_by("name"))}


# ---------------------------------------------------------------- per student
def _readiness(coverage: int) -> tuple[str, str]:
    if coverage >= MATCH_THRESHOLD:
        return "ready", _("Ready to apply")
    if coverage >= ALMOST_THRESHOLD:
        return "almost", _("Almost there")
    return "foundations", _("Build the foundations")


def _skill_state(skill_id, coverage, statuses) -> str:
    status = statuses.get(skill_id)
    if status == StudentSkill.STATUS_VERIFIED:
        return "verified"
    if coverage >= SKILL_COVERED:
        return "related"
    return "claimed" if status else "missing"


def _gap(item, role, statuses, courses_cache) -> dict:
    skill_id, name = item["id"], item["name"]
    if skill_id not in courses_cache:
        skill = Skill(id=skill_id, name=name)  # the cached role data already holds id + name: no extra query
        skill._state.adding = False
        courses_cache[skill_id] = courses_for_skill(skill, limit=COURSES_PER_GAP)
    claimed = bool(statuses.get(skill_id))
    why = _("Open %(title)s internships that require it: %(n)s of %(total)s.") % {
        "n": item["demand"], "total": role["openings"], "title": role["title"]}
    if claimed:
        why += " " + _("You listed it, but it is not verified yet: verifying it counts right away.")
    return {"skill": name, "skill_id": skill_id, "claimed": claimed, "why": why,
            "courses": [{k: c.get(k) for k in ("title", "provider", "url", "price", "is_partner", "opens_externally")}
                        for c in courses_cache[skill_id]],
            "project": question_bank.project_idea(name, item["discipline"], seed=skill_id)}


def _role_card(role, verified, weights, statuses, courses_cache) -> dict:
    required = [SimpleNamespace(id=s["id"], name=s["name"]) for s in role["skills"]]
    coverage, per_skill = aggregate_score(required, verified, weights)
    skills = [{"name": s["name"], "state": _skill_state(s["id"], per_skill[s["id"]], statuses), "demand": s["demand"]}
              for s in role["skills"]]
    covered = [s for s in skills if s["state"] in ("verified", "related")]
    to_verify = sum(1 for s in skills if s["state"] == "claimed")
    readiness, label = _readiness(coverage)
    why = _("Open internships asking for these skills: %(openings)s.") % {"openings": role["openings"]}
    why += " " + _("You cover %(covered)s of %(total)s core skills (%(coverage)s%%).") % {
        "covered": len(covered), "total": len(skills), "coverage": coverage}
    if to_verify:
        why += " " + _("Verify the skills you already listed (%(n)s) to raise it.") % {"n": to_verify}
    gaps = [s for s in role["skills"] if per_skill[s["id"]] < SKILL_COVERED][:MAX_GAPS]
    return {
        "key": role["key"], "title": role["title"], "discipline": role["discipline"],
        "discipline_label": str(Discipline(role["discipline"]).label) if role["discipline"] in Discipline.values else "",
        "openings": role["openings"], "coverage": coverage, "readiness": readiness, "readiness_label": label, "why": why,
        "skills": skills, "covered_count": len(covered), "core_count": len(skills), "to_verify": to_verify,
        "gaps": [_gap(item, role, statuses, courses_cache) for item in gaps],
        "practice_url": reverse("interview_practice") + "?" + urlencode({"role": role["key"]}),
        "feed_url": reverse("list") + "?" + urlencode({"q": role["title"]}),
    }


def _best_fit(student, verified, weights) -> list[dict]:
    internships = list(Internship.objects.open().select_related("partner").prefetch_related("required_skills")
                       .order_by("deadline", "-created_at")[:MAX_INTERNSHIPS])
    if not verified or not internships:
        return []
    scores = bulk_match_scores(internships, student)
    ranked = sorted((i for i in internships if scores.get(i.id)), key=lambda i: (-scores[i.id], i.deadline, i.id))[:MAX_BEST_FIT]
    out = []
    for internship in ranked:
        required = list(internship.required_skills.all())
        _score, per_skill = aggregate_score(required, verified, weights)
        matched = [s.name for s in required if per_skill[s.id] >= SKILL_COVERED]
        missing = [s.name for s in required if per_skill[s.id] < SKILL_COVERED]
        why = _("You cover %(matched)s of %(required)s required skills.") % {"matched": len(matched), "required": len(required)}
        if matched:
            why += " " + _("Strong: %(names)s.") % {"names": ", ".join(matched)}
        if missing:
            why += " " + _("To close: %(names)s.") % {"names": ", ".join(missing)}
        out.append({"id": internship.id, "title": internship.title, "company": internship.partner.company_name,
                    "score": scores[internship.id], "deadline": internship.deadline.isoformat(), "why": why,
                    "missing": missing, "url": reverse("internship_detail", args=[internship.id]),
                    "unlocked": scores[internship.id] >= MATCH_THRESHOLD})
    return out


def _compute(student, profiles) -> dict:
    rows = list(StudentSkill.objects.filter(student=student).select_related("skill"))
    statuses = {ss.skill_id: ss.status for ss in rows}
    verified = [ss.skill for ss in rows if ss.status == StudentSkill.STATUS_VERIFIED]
    ids = {s["id"] for r in profiles["roles"] for s in r["skills"]} | {v.id for v in verified}
    weights = relation_map(ids)
    courses_cache = {}
    cards = [_role_card(role, verified, weights, statuses, courses_cache) for role in profiles["roles"]]
    cards.sort(key=lambda c: (-c["coverage"], -c["openings"], c["title"]))
    return {
        "roles": cards[:MAX_ROLES], "internships": _best_fit(student, verified, weights),
        "verified_skills": sorted(v.name for v in verified), "claimed_skills": sorted(
            ss.skill.name for ss in rows if ss.status != StudentSkill.STATUS_VERIFIED),
        "has_skills": bool(rows), "role_count": len(profiles["roles"]),
        "generated_at": timezone.now().isoformat(),
    }


def recommendations(student) -> dict:
    profiles = role_profiles()
    name = f"{get_language() or 'ar'}:{timezone.localdate():%Y%m%d}:{profiles['token']}"
    return cached_value(f"careers:{student.pk}", name, STUDENT_TTL, lambda: _compute(student, profiles))


# ---------------------------------------------------------------- optional AI narrative
ADVICE_SYSTEM = ("You are a career coach for university students in Egypt. You receive anonymous data: role paths with "
                 "skill coverage and gaps, and the skills the student verified or listed. Write a short, encouraging and "
                 "honest plan grounded ONLY in this data: narrative (max 90 words) and 3 to 5 next_steps (each max 20 "
                 "words, starting with a verb). Never promise jobs, salaries or scores; never invent skills, courses, "
                 "companies or numbers.")


def advice_payload(recs: dict) -> dict:
    """Anonymous input for the "career_advice" task: titles, numbers and skill names only."""
    return {"roles": [{"title": r["title"], "coverage": r["coverage"], "readiness": r["readiness"],
                       "covered": [s["name"] for s in r["skills"] if s["state"] in ("verified", "related")],
                       "gaps": [g["skill"] for g in r["gaps"]]} for r in recs["roles"][:4]],
            "verified_skills": recs["verified_skills"][:20], "listed_unverified_skills": recs["claimed_skills"][:20]}


def advice(student, user) -> dict:
    """{"narrative", "next_steps"} from the engine. Raises AIUnavailable / QuotaExceeded."""
    from internest_ai import engine

    from .ai_tasks import CAREER_TASK

    language = "Arabic (Modern Standard, simple)" if (get_language() or "").startswith("ar") else "English"
    reply = engine.run(CAREER_TASK, system=ADVICE_SYSTEM, data=advice_payload(recommendations(student)), user=user,
                       prompt=f"Write the plan in {language} as {{\"narrative\": \"...\", \"next_steps\": [\"...\"]}}.",
                       max_tokens=700, timeout=30)
    steps = [" ".join(str(s).split())[:200] for s in reply.get("next_steps") or [] if str(s).strip()][:5]
    return {"narrative": " ".join(str(reply.get("narrative") or "").split())[:900], "next_steps": steps}
