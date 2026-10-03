from django.urls import path

from . import views

urlpatterns = [
    path("startups/register/", views.startup_register, name="startup_register"),
    path("startups/company-profile/", views.company_profile, name="startup_company_profile"),
    path("startups/company-profile/edit/", views.company_profile_edit, name="startup_company_profile_edit"),
    path("startups/company-profile/logo/", views.company_logo_update, name="startup_logo_update"),
    path("startups/upgrade/", views.upgrade, name="startup_upgrade"),
]
