"""AI engine policy of the CV builder (loaded from InternestStudentsConfig.ready).

cv_polish is private: external providers only get redacted text, and the builder never sends contact fields at all
(see cv_builder_service.polish_content). Every key is optional: a missing section means "keep the student's text".
"""
from internest_ai import router, tasks

POLISH_TASK = "cv_polish"
POLISH_SCHEMA = {"summary?": str, "education?": [dict], "experiences?": [dict], "projects?": [dict], "skills?": [str]}
POLISH_MOCK = {"summary": "Motivated student who turns coursework into working projects."}

tasks.register_task(POLISH_TASK, schema=POLISH_SCHEMA, private=True, mock=POLISH_MOCK,
                    description="CV builder: rewrite resume content (no contact fields are ever sent).")


def available() -> bool:
    return bool(router.eligible(tasks.get_task(POLISH_TASK)))
