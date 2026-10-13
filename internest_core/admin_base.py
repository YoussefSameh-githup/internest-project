"""Shared Django-admin building blocks on top of django-unfold: base classes and coloured status badges."""
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.admin import StackedInline, TabularInline  # noqa: F401  (re-exported for the apps' admin.py)
from unfold.decorators import display

# Badge colours understood by unfold: success (green), warning (amber), danger (red), info (blue), primary.
OK, WAIT, BAD, INFO = "success", "warning", "danger", "info"


class ModelAdmin(UnfoldModelAdmin):
    list_per_page = 50
    warn_unsaved_form = True
    compressed_fields = True


def status_badge(field, colors, description=_("Status")):
    """list_display column: the choice label of `field` as a coloured badge (colors: value → OK/WAIT/BAD/INFO)."""
    @display(description=description, label=colors, ordering=field)
    def column(self, obj):
        value = getattr(obj, field)
        label = getattr(obj, f"get_{field}_display", lambda: value)()
        return value, label
    column.__name__ = f"{field}_badge"
    return column


def flag_badge(description, true_label, false_label, true_color=OK, false_color=WAIT):
    """list_display column for a boolean computed by `test(obj)`; decorate with @flag_badge(...)."""
    def decorator(test):
        @display(description=description, label={True: true_color, False: false_color})
        def column(self, obj):
            value = bool(test(self, obj))
            return value, true_label if value else false_label
        column.__name__ = test.__name__
        return column
    return decorator
