from django import template

from ..permissions import lounge_member

register = template.Library()


@register.simple_tag
def is_lounge_member(user):
    """True only for verified startup/company partners — never students, guests or universities."""
    return lounge_member(user) is not None
