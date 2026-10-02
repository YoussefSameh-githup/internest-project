from django.urls import path

from . import views

urlpatterns = [
    path("startups/register/", views.startup_register, name="startup_register"),
    path("startups/company-profile/", views.company_profile, name="startup_company_profile"),
]
