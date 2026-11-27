from django import template
from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html

from ..perf import FIELD_PAGES

register = template.Library()


@register.simple_tag(takes_context=True)
def rum_script(context):
    """Real-visitor Web Vitals beacon: anonymous visitors on public pages only, sampled. Empty string otherwise."""
    request = context.get("request")
    rate = float(getattr(settings, "FOUNDER_RUM_SAMPLE_RATE", 0) or 0)
    match = getattr(request, "resolver_match", None)
    if not request or rate <= 0 or request.user.is_authenticated or not match or match.url_name not in FIELD_PAGES:
        return ""
    return format_html('<script src="{}" data-rate="{}" data-endpoint="{}" defer></script>',
                       static("founder/vitals.js"), rate, reverse("rum_vitals"))
