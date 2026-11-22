"""Career features: structured interview kits (partners and student practice) and career-path recommendations.

    interview_service.create_kit(user, target={"application": 7}, seniority="intern", use_ai=True)
    career_service.recommendations(student)            # cached per student, invalidated by signals

Everything is deterministic first (skills, assessments, open internships, curated question bank); the AI engine only
enriches (tasks "interview_questions" and "career_advice", registered in ai_tasks.py) and never sees personal data.
"""
