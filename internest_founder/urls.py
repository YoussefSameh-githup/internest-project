from django.urls import path

from . import views

app_name = "founder"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("search/", views.search, name="search"),
    path("users/", views.users, name="users"),
    path("users/<int:pk>/", views.user_detail, name="user_detail"),
    path("users/<int:pk>/active/", views.user_set_active, name="user_set_active"),
    path("companies/", views.companies, name="companies"),
    path("internships/", views.internships, name="internships"),
    path("internships/<int:pk>/active/", views.internship_set_active, name="internship_set_active"),
    path("applications/", views.applications, name="applications"),
    path("finance/", views.finance, name="finance"),
    path("analytics/", views.analytics, name="analytics"),
    path("system/", views.system, name="system"),
    path("performance/", views.performance, name="performance"),
    path("automations/", views.automations, name="automations"),
    path("notifications/", views.notifications, name="notifications"),
    path("security/", views.security, name="security"),
    path("settings/", views.founder_settings, name="settings"),
    path("api/health/", views.health_api, name="health_api"),
]
