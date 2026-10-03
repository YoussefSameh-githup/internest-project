from django import template

from ..tiers import FREE_POSTS_PER_MONTH, is_pro, posts_this_month

register = template.Library()


@register.filter
def is_pro_partner(partner):
    return is_pro(partner)


@register.inclusion_tag("startups/_pro_badge.html")
def pro_badge(partner):
    """Golden crest for Internest Pro only; Free accounts show their name without a badge."""
    return {"show": is_pro(partner)}


@register.simple_tag
def free_posts_used(partner):
    return {"used": posts_this_month(partner), "limit": FREE_POSTS_PER_MONTH}
