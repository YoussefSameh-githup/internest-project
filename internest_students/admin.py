from django.contrib import admin

from .models import StudentCV, StudentProject


@admin.register(StudentCV)
class StudentCVAdmin(admin.ModelAdmin):
    list_display = ("student", "updated_at", "ai_day", "ai_uses")
    search_fields = ("student__user__username",)
    readonly_fields = ("updated_at",)


@admin.register(StudentProject)
class StudentProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "student", "tech_stack", "source")
    search_fields = ("title", "student__user__username")
