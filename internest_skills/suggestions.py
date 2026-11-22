"""Staff decisions on SkillSuggestion rows (the only way an AI-seen skill name reaches the taxonomy, audit M1)."""
from django.db import transaction
from django.utils.text import slugify

from .models import Skill, SkillSuggestion


def _unique_slug(name: str) -> str:
    base = slugify(name, allow_unicode=True)[:90] or "skill"
    slug, n = base, 2
    while Skill.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@transaction.atomic
def approve_suggestion(suggestion: SkillSuggestion) -> Skill:
    """Create the skill (active), or reuse a skill with the same name (then it is a merge)."""
    existing = Skill.objects.filter(name__iexact=suggestion.name).first()
    if existing is not None:
        return merge_suggestion(suggestion, existing)
    skill = Skill.objects.create(name=suggestion.name, slug=_unique_slug(suggestion.name), discipline=suggestion.discipline,
                                 aliases=suggestion.name, is_active=True)
    suggestion.status, suggestion.skill = SkillSuggestion.STATUS_APPROVED, skill
    suggestion.save(update_fields=["status", "skill", "last_seen_at"])
    return skill


@transaction.atomic
def merge_suggestion(suggestion: SkillSuggestion, target: Skill) -> Skill:
    """The suggested name becomes an alias of `target`, so the matcher maps it there from now on."""
    if suggestion.name.lower() not in {a.lower() for a in target.alias_list}:
        target.aliases = ", ".join(a for a in (target.aliases.strip(" ,"), suggestion.name) if a)
        target.save(update_fields=["aliases"])
    suggestion.status, suggestion.skill, suggestion.merge_into = SkillSuggestion.STATUS_MERGED, target, target
    suggestion.save(update_fields=["status", "skill", "merge_into", "last_seen_at"])
    return target
