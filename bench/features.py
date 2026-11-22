"""What one user's request looks like for each benchmarked endpoint: request(session) -> (method, path, httpx kwargs).

AI features answer 202 + a task id and are followed until the job finishes; "sync_*" entries are ordinary pages and
JSON reads measured for normal-traffic latency. Sessions come from bench/seed.py (students; reviewers for feature 4).
"""
import io
from functools import lru_cache


@lru_cache(maxsize=None)
def _cv_docx(n: int) -> bytes:
    """A small, realistic DOCX CV; the number changes the text so no two users share a file hash or a cache entry."""
    from docx import Document

    doc = Document()
    doc.add_paragraph(f"Bench Student {n}")
    doc.add_paragraph(f"bench{n}@example.com | 0101{n:07d}")
    for heading, lines in (
            ("Education", ["BSc Computer Science, Cairo University, 2022 - Present"]),
            ("Experience", [f"Backend Intern | Acme {n % 7}", "06/2024 - 09/2024",
                            "Built a REST API in Django serving 2,000 requests per day",
                            "Wrote unit tests that raised coverage from 40% to 75%"]),
            ("Projects", ["Library system: Django, SQLite, Bootstrap", "Chat app with WebSockets"]),
            ("Skills", ["Python, Django, SQL, Git, REST APIs, Docker"]),
            ("Certifications", ["Google Data Analytics Certificate"]),
            ("Languages", ["Arabic (native), English (fluent)"])):
        doc.add_heading(heading, level=2)
        for line in lines:
            doc.add_paragraph(line)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _n(session) -> int:
    return int("".join(ch for ch in session["username"] if ch.isdigit()) or 0)


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

FEATURES = {
    # --- the seven AI features (asynchronous: 202 -> poll) ---
    "cv_analysis": {"request": lambda s: ("POST", "/api/ai/cv-analysis/", {
        "files": {"cv_file": (f"cv_{_n(s)}.docx", _cv_docx(_n(s)), DOCX)}})},
    "skills_extraction": {"request": lambda s: ("POST", "/api/ai/skills/extract/", {"json": {
        "text": f"Student {_n(s)}: I build REST APIs with Django and PostgreSQL, write SQL queries, use Git and Docker, "
                "and have deployed a React front end."}})},
    "assessment_generation": {"request": lambda s: ("POST", "/api/ai/assessments/", {"json": {"skill_id": s["skill_id"]}})},
    "answer_evaluation": {"needs": ("response_id",), "request": lambda s: ("POST", f"/api/ai/answers/{s['response_id']}/evaluate/", {})},
    "interview_questions": {"request": lambda s: ("POST", "/api/ai/interview-kits/", {"json": {
        "target": f"internship:{s['internship_id']}", "seniority": "junior", "ai": True}})},
    "career_recommendations": {"request": lambda s: ("POST", "/api/ai/careers/advice/", {})},
    "resume_builder": {"request": lambda s: ("POST", "/api/ai/resume/polish/", {})},
    # --- normal traffic (synchronous) ---
    "sync_careers": {"request": lambda s: ("GET", "/api/ai/careers/", {})},
    "sync_resume_ats": {"request": lambda s: ("GET", "/api/ai/resume/ats/", {})},
    "sync_resume_pdf": {"request": lambda s: ("GET", "/api/ai/resume/export.pdf", {})},
    "sync_feed": {"request": lambda s: ("GET", "/list/", {})},
    "sync_landing": {"request": lambda s: ("GET", "/", {})},
    "sync_healthz": {"request": lambda s: ("GET", "/healthz/", {})},
}
