from django.urls import path

from . import api, views

urlpatterns = [
    path("app/cv-builder/", views.cv_builder, name="cv_builder"),
    path("app/cv-builder/polish/", views.cv_polish, name="cv_builder_polish"),
    path("app/cv-builder/voice/", views.process_voice_cv, name="cv_builder_voice"),
    path("app/cv-builder/save/", views.cv_save, name="cv_builder_save"),
    path("app/cv-builder/cv.pdf", views.cv_pdf, name="cv_builder_pdf"),
    path("app/cv-builder/cv.docx", views.cv_docx, name="cv_builder_docx"),
    path("app/cv-builder/preview/", views.cv_preview, name="cv_builder_preview"),
    # JSON API (session auth + CSRF)
    path("api/ai/resume/polish/", api.resume_polish, name="api_resume_polish"),
    path("api/ai/resume/ats/", api.resume_ats, name="api_resume_ats"),
    path("api/ai/resume/export.docx", api.resume_docx, name="api_resume_docx"),
    path("api/ai/resume/export.pdf", api.resume_pdf, name="api_resume_pdf"),
]
