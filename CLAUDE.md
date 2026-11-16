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

## Delivery workflow
Implement → add tests → AR/EN i18n (`scripts/i18n_catalog.py apply|check|compile`) → run the full suite → commit → `git push origin main`. End replies with the PythonAnywhere steps (`git pull`, plus migrate / pip install / collectstatic / compilemessages when needed, then Reload).

Full test command:
`../venv-win/Scripts/python.exe manage.py test internest_core internest_skills internest_lounge internest_campus internest_startups internest_students`

## Guardrails
- Never commit secrets (keys live in `.env`). Never commit `INTERNEST_CONTEXT.*` or `pdf_exports/`.
- No personal phone number in payment UI. Don't misrepresent the business model to payment providers or banks.
- Architecture reference: business logic in service modules (`internest_core/student_hub.py`), reusable markup in `templates/components/`.
