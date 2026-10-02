from django.contrib import admin
from django.db.models import Count

from .models import CampusDemandVote


@admin.register(CampusDemandVote)
class CampusDemandVoteAdmin(admin.ModelAdmin):
    list_display = ("university_name", "faculty_name", "department", "masked_student_id", "referral_count", "referred_by", "created_at")
    list_filter = ("university_name", "faculty_name")
    search_fields = ("university_name", "faculty_name", "department", "student_number", "student__user__username")
    readonly_fields = ("university_key", "campus_key", "referral_code", "created_at")
    raw_id_fields = ("student", "referred_by")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_referrals=Count("referrals"))

    @admin.display(ordering="_referrals", description="Referrals")
    def referral_count(self, obj):
        return obj._referrals
