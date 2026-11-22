from django.urls import path

from . import api, views

urlpatterns = [
    # Pages
    path("app/careers/", views.career_paths, name="career_paths"),
    path("app/interview-practice/", views.interview_practice, name="interview_practice"),
    path("app/interview-kits/", views.interview_kits, name="interview_kits"),
    path("app/interview-kits/new/", views.interview_kit_new, name="interview_kit_new"),
    path("app/interview-kits/<int:kit_id>/", views.interview_kit, name="interview_kit"),
    path("app/interview-kits/<int:kit_id>/print/", views.interview_kit_print, name="interview_kit_print"),
    path("app/interview-kits/<int:kit_id>/enrich/", views.interview_kit_enrich, name="interview_kit_enrich"),
    # JSON API (session auth + CSRF)
    path("api/ai/interview-kits/", api.interview_kits, name="api_interview_kits"),
    path("api/ai/interview-kits/<int:kit_id>/", api.interview_kit, name="api_interview_kit"),
    path("api/ai/careers/", api.careers, name="api_careers"),
    path("api/ai/careers/advice/", api.career_advice, name="api_career_advice"),
]
