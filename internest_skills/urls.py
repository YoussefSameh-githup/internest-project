from django.urls import path

from . import api, views, views_review

urlpatterns = [
    path("app/skills/", views.skills_hub, name="skills_hub"),
    path("app/skills/analysis/<int:analysis_id>/confirm/", views.cv_confirm_skills, name="skills_cv_confirm"),
    path("app/skills/freelance/", views.freelance_importer, name="freelance_importer"),
    path("app/skills/freelance/<int:gig_id>/proof/", views.freelance_gig_proof, name="freelance_gig_proof"),
    path("app/skills/share/", views.skills_share_university, name="skills_share_university"),
    path("student/dashboard/", views.student_dashboard, name="student_dashboard"),
    path("student/certificates/", views.certificate_submit, name="certificate_submit"),
    path("app/skills/<int:student_skill_id>/start/", views.challenge_start, name="skills_challenge_start"),
    path("app/skills/test/<int:skill_id>/", views.challenge_start_for_skill, name="skills_challenge_start_for_skill"),
    path("app/skills/challenge/<uuid:token>/", views.challenge_run, name="skills_challenge"),
    path("app/skills/challenge/<uuid:token>/result/", views.challenge_result, name="skills_result"),
    path("api/skills/challenge/<uuid:token>/next/", views.api_next, name="skills_api_next"),
    path("api/skills/challenge/<uuid:token>/answer/", views.api_answer, name="skills_api_answer"),
    path("api/skills/challenge/<uuid:token>/event/", views.api_event, name="skills_api_event"),
    path("api/skills/<int:student_skill_id>/recommendations/", views.api_recommendations, name="skills_api_recommendations"),
    # AI features JSON API (internest_skills/api.py)
    path("api/ai/cv-analysis/", api.cv_analysis_create, name="skills_api_cv_analysis"),
    path("api/ai/cv-analysis/<int:analysis_id>/", api.cv_analysis_detail, name="skills_api_cv_analysis_detail"),
    path("api/ai/skills/extract/", api.skills_extract, name="skills_api_skills_extract"),
    path("api/ai/assessments/", api.assessments_create, name="skills_api_assessments"),
    path("api/ai/answers/<int:response_id>/evaluate/", api.answer_evaluate, name="skills_api_answer_evaluate"),
    path("partner/skills/student/<int:student_id>/", views.student_skill_report, name="skills_student_report"),
    path("partner/skills/university/", views.university_dashboard, name="skills_university_dashboard"),
    path("staff/assessment-review/", views_review.review_queue, name="skills_review_queue"),
    path("staff/assessment-review/<uuid:token>/", views_review.review_detail, name="skills_review_detail"),
]
