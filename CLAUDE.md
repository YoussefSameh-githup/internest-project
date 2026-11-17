# Internest — permanent project rules

Django 5.2 + SQLite app (AI skill verification + internship matching for Egyptian students). Deployed on PythonAnywhere (Python 3.10).

## Skills (invoke automatically, no confirmation)
On every coding, UI or marketing prompt, use the relevant skills in `.claude/skills/`:
- `adversarial-quality-council` — always, before any final output.
- `django-dry-architect` — any Python/view/query/template change.
- `modern-saas-ui` — any layout, CSS, component or template work.
- `startup-growth-marketer` — any copy, landing page, onboarding or CTA.
- `frontend-design`, `webapp-testing`, `skill-creator` — as their descriptions fit.

## Non-negotiable UI rules (all future work)
1. **Zero duplication, single source of truth.** Never render the same stat card, widget, list or action button twice on one page, and never copy dashboard widgets (overview stats, full skill-assessment lists, application lists) that already live in the Student Workspace (`مساحتك الطلابية`: My applications / Tasks / Courses / Skills hub). Link to the workspace instead.
2. **Multi-device, RTL + LTR.** Every layout, avatar, tab bar and card grid is styled and checked at Desktop (>=1024px), Tablet (768-1023px) and Mobile (<768px), in Arabic (`dir="rtl"`) and English (`dir="ltr"`): no text/image overlap, no horizontal overflow.

## Working style
- **Scoped reads:** open only files tied to the task (grep first, read ranges). Do not scan unrelated apps.
- **Ultra-brief terminal replies:** result, what changed, what to run. No recaps, no filler. Reply in Egyptian Arabic to the user.
- Complete drop-in code only; no placeholders or truncation.

## External APIs
Every external API call (AI, payments, email, any future paid or free API) MUST go through `internest_core/api_monitor.py` (`monitored_api_call`, `@monitored`, `api_feature`, `failover_scope`, `BaseMonitoredAPIClient`).
Describe every API ONCE in `internest_core/api_registry.py` (`register(ProviderSpec(...))`: name, category, role, impact, `.env` key, code location, console/billing URL, optional balance tracking). That is all a new API needs for alert emails with full context, the incident log, the Django-admin budget row and `manage.py check_api_health`.
Alerts go to `ADMIN_ALERT_EMAIL` only (30-min cooldown per provider and type). Verify with `python manage.py test_api_alert` and `python manage.py check_api_health` (schedule the latter daily on PythonAnywhere).

## Performance
- Heavy AI work (> ~1 s) runs through `internest_core/async_tasks.py` (`@register` a job in `<app>/jobs.py`, answer `Prefer: respond-async` with `accepted(enqueue(...))`, poll with `static/js/async_task_poller.js`); outbound HTTP uses the pooled `internest_core/http_client.py`; idempotent AI answers go through `ai_cache` (quiz generation stays uncached).
- Profile lookups use `internest_core/identity.py` (never `user.partnerprofile` directly); shared aggregates use `page_cache.cached_value` + `bump()` in signals; anonymous public pages use `@cache_public_page`.
- `internest_core/tests_performance.py` holds query ceilings per page: a new query on those pages must be justified, not just the ceiling raised.

## Delivery workflow
Implement → add tests → AR/EN i18n (`scripts/i18n_catalog.py apply|check|compile`) → run the full suite → commit → `git push origin main`. End replies with the PythonAnywhere steps (`git pull`, plus migrate / pip install / collectstatic / compilemessages when needed, then Reload).

Full test command:
`../venv-win/Scripts/python.exe manage.py test internest_core internest_skills internest_lounge internest_campus internest_startups internest_students`

## Guardrails
- Never commit secrets (keys live in `.env`). Never commit `INTERNEST_CONTEXT.*` or `pdf_exports/`.
- No personal phone number in payment UI. Don't misrepresent the business model to payment providers or banks.
- Architecture reference: business logic in service modules (`internest_core/student_hub.py`), reusable markup in `templates/components/`.
