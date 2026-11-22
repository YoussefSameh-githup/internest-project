"""AI-engine tasks of the skills app: policy, strict reply shapes, default per-student daily caps and mock answers.

Loaded once from InternestSkillsConfig.ready(). The mock answers satisfy each schema, so the whole flow (CV analysis,
quiz generation + verification, answer evaluation) runs offline with AI_MOCK_PROVIDER=1 for load tests.

Daily caps per user (internest_ai.quotas): DEFAULT_TASK_LIMITS fill settings.AI_QUOTA_TASK_LIMITS only where the
environment (AI_QUOTA_TASK_LIMITS=question_generation=10,...) does not set a value, so operators keep the last word.
"""
import json
import re

from django.conf import settings

from internest_ai.tasks import register_task

DEFAULT_TASK_LIMITS = {
    "question_generation": 6,   # audit M2: AI quizzes per student per day; afterwards the saved bank is used
    "quiz_verify": 12,          # one verification pass per generated quiz (+ retries)
    "cv_analysis": 15,          # hub uploads (5/day, counted separately) + /api/ai/skills/extract/
    "freelance_gig_analysis": 10,
    "answer_evaluation": 300,   # staff reviewers
}


def _section(messages, name):
    """The JSON value of one `data=` section, read back from the messages the engine built (mock answers only)."""
    lines = messages[-1]["content"].splitlines()
    try:
        return json.loads(lines[lines.index(f"[{name}]") + 1])
    except (ValueError, IndexError):
        return None


def _mock_quiz(messages):
    count = re.search(r"exactly (\d+) NEW", messages[-1]["content"])
    n = int(count.group(1)) if count else 10
    return {"q": [{"s": "Core concepts", "d": "emh"[i % 3], "t": f"Mock topic {i + 1}",
                   "p": f"Mock question {i + 1}: which option is the documented answer?", "k": "",
                   "c": [f"Option {i + 1}A (key)", f"Option {i + 1}B", f"Option {i + 1}C", f"Option {i + 1}D"], "a": 0}
                  for i in range(n)]}


def _mock_verify(messages):
    questions = _section(messages, "questions") or []
    return {"answers": [{"i": q["i"], "answer": next((n for n, c in enumerate(q["choices"]) if c.endswith("(key)")), 0)}
                        for q in questions]}


def _mock_answer_evaluation(messages):
    rubric = re.search(r"Rubric criteria \(JSON list\):\n(.+)", messages[-1]["content"])
    names = json.loads(rubric.group(1)) if rubric else ["Answer quality"]
    return {"criteria": [{"criterion": n, "met": i % 2 == 0, "note": "Mock check."} for i, n in enumerate(names)],
            "rationale": "Mock grading (AI_MOCK_PROVIDER)."}


def register_all():
    from .cv_analysis import SCHEMA as CV_SCHEMA
    from .quizgen import VERIFY_SCHEMA
    from .review import AI_SCHEMA

    register_task("cv_analysis", schema=CV_SCHEMA, private=True, description="CV facts enrichment and skill reading",
                  mock={"skills": [], "summary": "Mock analysis of the CV (AI_MOCK_PROVIDER).", "top_domains": ["general"],
                        "suggestions": [{"text": "Add numbers that show the results of your work."}], "roles": [], "degrees": []})
    register_task("question_generation", schema={"q": [dict]}, user_scoped_cache=True, mock=_mock_quiz,
                  description="Skill-challenge questions (never cached)")
    register_task("quiz_verify", schema=VERIFY_SCHEMA, mock=_mock_verify, description="Second-model answer-key check")
    register_task("answer_evaluation", schema=AI_SCHEMA, mock=_mock_answer_evaluation,
                  description="Advisory rubric check of a practical answer (reviewers only)")
    register_task("freelance_gig_analysis", schema={"extracted_skills?": [], "project_title?": str},
                  mock={"project_title": "Mock gig", "client_feedback": "", "rating": None, "deliverables": [],
                        "extracted_skills": []}, description="Freelance gig reading")
    limits = getattr(settings, "AI_QUOTA_TASK_LIMITS", None)
    if isinstance(limits, dict):
        for task, limit in DEFAULT_TASK_LIMITS.items():
            limits.setdefault(task, limit)
