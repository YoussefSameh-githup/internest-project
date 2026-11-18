from django import template

from ..entitlements import is_legacy_pro
from ..tiers import FREE_POSTS_PER_MONTH, posts_this_month

register = template.Library()


@register.filter
def is_pro_partner(partner):
    """The "PRO" badge and priority placement belong to legacy Pro subscriptions only (a Hiring Pass has no badge).
    Reads only the subscription row, so list pages do not query passes per company."""
    return is_legacy_pro(partner)


@register.inclusion_tag("startups/_pro_badge.html")
def pro_badge(partner):
    """Golden crest for legacy Internest Pro only; everyone else shows their name without a badge."""
    return {"show": is_legacy_pro(partner)}


@register.simple_tag
def free_posts_used(partner):
    return {"used": posts_this_month(partner), "limit": FREE_POSTS_PER_MONTH}
