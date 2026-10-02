from django.contrib import admin
from django.db.models import Count

from .models import HiddenReason, LoungeComment, LoungeFlag, LoungePost, Visibility


@admin.action(description="Restore selected (make visible, clear flags)")
def restore(modeladmin, request, queryset):
    if queryset.model is LoungePost:
        LoungeFlag.objects.filter(post__in=queryset).delete()
    queryset.update(visibility=Visibility.VISIBLE, hidden_reason=HiddenReason.NONE)


@admin.action(description="Remove selected")
def remove(modeladmin, request, queryset):
    queryset.update(visibility=Visibility.REMOVED)


class LoungeCommentInline(admin.TabularInline):
    model = LoungeComment
    extra = 0
    fields = ("author", "body", "visibility", "hidden_reason", "created_at")
    readonly_fields = ("created_at",)


@admin.register(LoungePost)
class LoungePostAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "category", "visibility", "hidden_reason", "flag_count", "created_at")
    list_filter = ("visibility", "hidden_reason", "category")
    search_fields = ("title", "body", "author__company_name")
    readonly_fields = ("body_fingerprint", "created_at")
    actions = [restore, remove]
    inlines = [LoungeCommentInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_flags=Count("flags"))

    @admin.display(ordering="_flags", description="Flags")
    def flag_count(self, obj):
        return obj._flags


@admin.register(LoungeComment)
class LoungeCommentAdmin(admin.ModelAdmin):
    list_display = ("post", "author", "visibility", "hidden_reason", "created_at")
    list_filter = ("visibility", "hidden_reason")
    search_fields = ("body", "author__company_name")
    actions = [restore, remove]


@admin.register(LoungeFlag)
class LoungeFlagAdmin(admin.ModelAdmin):
    list_display = ("post", "reporter", "created_at")
