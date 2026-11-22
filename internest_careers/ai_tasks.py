"""AI engine policies of the career features (loaded from InternestCareersConfig.ready).

Both tasks receive only skills, levels, role titles and seniority: never names, contacts or free text about a person.
The mock answers are schema-valid, so load tests can run with AI_MOCK_PROVIDER=1.
"""
from internest_ai import router, tasks

INTERVIEW_TASK = "interview_questions"
CAREER_TASK = "career_advice"

INTERVIEW_SCHEMA = {"questions": [{"question": str, "purpose": str, "expected_evidence": str, "criteria": [str],
                                   "follow_ups": [str], "difficulty": str, "skill": str}]}
CAREER_SCHEMA = {"narrative": str, "next_steps": [str]}

INTERVIEW_MOCK = {"questions": [{
    "question": "Describe how you would explain a technical result to a non-technical teammate.",
    "purpose": "Check clear communication of results.",
    "expected_evidence": "A concrete example, plain language and a check that the listener understood.",
    "criteria": ["Uses plain language", "Structures the message", "Checks understanding"],
    "follow_ups": ["What questions did they ask?", "What would you change next time?"],
    "difficulty": "medium", "skill": "",
}]}
CAREER_MOCK = {"narrative": "You already cover part of the skills these roles ask for. Close the top gap first, then verify it.",
               "next_steps": ["Verify one skill you already listed", "Start the first recommended course",
                              "Build the suggested project and add it to your CV"]}

tasks.register_task(INTERVIEW_TASK, schema=INTERVIEW_SCHEMA, private=False, user_scoped_cache=True, mock=INTERVIEW_MOCK,
                    description="Extra structured interview questions from skills, levels and role (no personal data).")
tasks.register_task(CAREER_TASK, schema=CAREER_SCHEMA, private=False, user_scoped_cache=True, mock=CAREER_MOCK,
                    description="Short career narrative from role coverage and skill gaps (no personal data).")


def available(task: str) -> bool:
    """A provider (or the mock) is configured for this task."""
    return bool(router.eligible(tasks.get_task(task)))
