from django.urls import path

from . import views

urlpatterns = [
    path("app/cv-builder/", views.cv_builder, name="cv_builder"),
    path("app/cv-builder/polish/", views.cv_polish, name="cv_builder_polish"),
    path("app/cv-builder/voice/", views.process_voice_cv, name="cv_builder_voice"),
    path("app/cv-builder/save/", views.cv_save, name="cv_builder_save"),
    path("app/cv-builder/cv.pdf", views.cv_pdf, name="cv_builder_pdf"),
    path("app/cv-builder/preview/", views.cv_preview, name="cv_builder_preview"),
]
