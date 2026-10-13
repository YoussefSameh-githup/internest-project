from django.contrib import admin

from internest_core.admin_base import ModelAdmin

from .models import StudentCV, StudentProject


@admin.register(StudentCV)
class StudentCVAdmin(ModelAdmin):
    list_display = ("student", "updated_at", "ai_day", "ai_uses")
    list_filter = ("updated_at",)
    search_fields = ("student__user__username", "student__user__email")
    readonly_fields = ("updated_at", "ai_day", "ai_uses")


@admin.register(StudentProject)
class StudentProjectAdmin(ModelAdmin):
    list_display = ("title", "student", "tech_stack", "source")
    list_filter = ("source",)
    search_fields = ("title", "tech_stack", "student__user__username")
