from django.urls import path

from . import views

urlpatterns = [
    path("app/skills/", views.skills_hub, name="skills_hub"),
    path("app/skills/<int:student_skill_id>/start/", views.challenge_start, name="skills_challenge_start"),
    path("app/skills/challenge/<uuid:token>/", views.challenge_run, name="skills_challenge"),
    path("app/skills/challenge/<uuid:token>/result/", views.challenge_result, name="skills_result"),
    path("api/skills/challenge/<uuid:token>/next/", views.api_next, name="skills_api_next"),
    path("api/skills/challenge/<uuid:token>/answer/", views.api_answer, name="skills_api_answer"),
    path("api/skills/challenge/<uuid:token>/event/", views.api_event, name="skills_api_event"),
    path("api/skills/<int:student_skill_id>/recommendations/", views.api_recommendations, name="skills_api_recommendations"),
    path("partner/skills/student/<int:student_id>/", views.student_skill_report, name="skills_student_report"),
    path("partner/skills/university/", views.university_dashboard, name="skills_university_dashboard"),
]
