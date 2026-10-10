from django import template

from ..permissions import lounge_member

register = template.Library()


@register.simple_tag
def is_lounge_member(user):
    """True only for verified startup/company partners — never students, guests or universities."""
    return lounge_member(user) is not None


@register.simple_tag
def dm_unread_count(user):
    """Unread private messages for a verified company (0 for everyone else)."""
    from ..models import DirectMessage
    from ..permissions import lounge_member

    partner = lounge_member(user)
    return DirectMessage.objects.filter(recipient=partner, is_read=False).count() if partner else 0
