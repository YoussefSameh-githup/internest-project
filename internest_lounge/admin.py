from django.contrib import admin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from internest_core.admin_base import BAD, OK, WAIT, ModelAdmin, TabularInline, status_badge

from .models import HiddenReason, LoungeComment, LoungeFlag, LoungePost, Visibility


@admin.action(description="Restore selected (make visible, clear flags)")
def restore(modeladmin, request, queryset):
    if queryset.model is LoungePost:
        LoungeFlag.objects.filter(post__in=queryset).delete()
    queryset.update(visibility=Visibility.VISIBLE, hidden_reason=HiddenReason.NONE)


@admin.action(description="Remove selected")
def remove(modeladmin, request, queryset):
    queryset.update(visibility=Visibility.REMOVED)


class LoungeCommentInline(TabularInline):
    model = LoungeComment
    extra = 0
    fields = ("author", "body", "visibility", "hidden_reason", "created_at")
    readonly_fields = ("created_at",)


@admin.register(LoungePost)
class LoungePostAdmin(ModelAdmin):
    list_display = ("display_title", "author", "category", "visibility_col", "hidden_reason", "flag_count", "created_at")
    list_filter = ("visibility", "hidden_reason", "category", "created_at")
    date_hierarchy = "created_at"
    visibility_col = status_badge("visibility", {Visibility.VISIBLE: OK, Visibility.HIDDEN: WAIT, Visibility.REMOVED: BAD}, _("Visibility"))
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
class LoungeCommentAdmin(ModelAdmin):
    list_display = ("post", "author", "visibility_col", "hidden_reason", "created_at")
    list_filter = ("visibility", "hidden_reason", "created_at")
    visibility_col = status_badge("visibility", {Visibility.VISIBLE: OK, Visibility.HIDDEN: WAIT, Visibility.REMOVED: BAD}, _("Visibility"))
    search_fields = ("body", "author__company_name")
    actions = [restore, remove]


@admin.register(LoungeFlag)
class LoungeFlagAdmin(ModelAdmin):
    list_display = ("post", "reporter", "created_at")
    list_filter = ("created_at",)
    search_fields = ("post__body", "reporter__company_name")
