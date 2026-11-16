---
name: django-dry-architect
description: Enforce strict DRY architecture in the Internest Django project. Use whenever adding or changing views, queries, templates, or forms.
---

# Django DRY Architect (Internest)

## Backend
- Business logic and data assembly live in **service modules**, not views. Reference pattern: `internest_core/student_hub.py` (`profile_context`, `student_skills`, `student_applications`, `open_invitations`, `db_safe`).
- Views stay thin: auth, form handling, redirects, `render(...)` with a context built by a service.
- A query needed by more than one page goes into a shared selector function once. Never re-query what a list already holds (derive counts/groups from the loaded list).
- Optional tables (skills, CV, campus) are read through `db_safe(default)` so a page never fails because of them.
- Use `select_related`/`prefetch_related`; check query counts in tests (`mock.patch.object(..., wraps=...)` or `assertNumQueries`).

## Templates
- Any HTML block used more than once (same page or across pages) becomes a partial in `internest_core/templates/components/*.html`, used with `{% include "components/x.html" with a=b %}`.
- Existing components: `logo`, `avatar`, `progress_ring`, `stat_tile`, `empty_state`, `card_head`, `app_status`, `skill_badge`, `skill_verify_card`, `application_row`. Reuse before creating.
- First line of every component documents its context variables in a `{# ... #}` comment.
- Large pages are split into `internship/<page>/_*.html` partials; the main template only assembles.
- Strings: use `{% trans "..." as var %}` before an include when a component needs translated text (never `_("...")` in include args: the catalog tool misses them). Always add Arabic via `scripts/i18n_catalog.py apply|check|compile`.

## Definition of done
Tests added, full suite green, no duplicated query or markup left behind, AR/EN strings present.
