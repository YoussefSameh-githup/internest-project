from django.contrib import admin

from .models import InterviewKit


@admin.register(InterviewKit)
class InterviewKitAdmin(admin.ModelAdmin):
    list_display = ("role", "seniority", "owner", "partner", "source", "ai_status", "created_at")
    list_filter = ("seniority", "source", "ai_status")
    search_fields = ("role", "owner__username", "partner__company_name")
    raw_id_fields = ("owner", "partner", "internship", "application")
    readonly_fields = ("created_at", "updated_at")
