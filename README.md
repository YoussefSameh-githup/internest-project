# Internest

AI skill verification and internship matching for students in Egypt. Django 5.2 modular monolith, SQLite by
default (PostgreSQL via `DATABASE_URL`), DB-backed background queue, provider-agnostic AI engine.

## Run locally

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt   # Windows: venv\Scripts\pip
cp .env.example .env            # fill DJANGO_SECRET_KEY; every AI/email/payment key is optional
python manage.py migrate
python manage.py runserver
python manage.py run_async_tasks   # background AI jobs (or ASYNC_TASK_BACKEND=sync to run them inline)
```

With no AI key set, the AI features use their deterministic (non-AI) paths (keyword skill matching, question bank,
rule-based recommendations). For development without keys,
`AI_MOCK_PROVIDER=True` (DEBUG only) returns canned model answers.

## Apps

| App | What it holds |
| --- | --- |
| `internest_core` | accounts, profiles, opportunities, async task queue, API monitor/registry, caching, observability |
| `internest_ai` | AI providers (Gemini, Groq, OpenRouter, Cloudflare, local/Ollama, Azure OpenAI, AgentRouter, backup), router, circuit breaker, quotas, PII redaction, prompt isolation |
| `internest_skills` | CV analysis, skills extraction, assessments, answer evaluation, skills API |
| `internest_careers` | interview kits, career recommendations, ATS resume exports |
| `internest_students` | student workspace, CV builder, PDF export |
| `internest_startups`, `internest_campus`, `internest_lounge` | company side, university/campus features, community |

## Tests

```bash
python manage.py test internest_core internest_skills internest_lounge internest_campus internest_startups \
  internest_students internest_ai internest_careers --parallel 1 --noinput
```

## Load benchmark

`bench/` seeds a disposable database (`SQLITE_PATH` or `DATABASE_URL`, never `db.sqlite3`), starts a server plus
workers with the mock AI provider, and measures accepted vs completed jobs per feature:
`python bench/run_bench.py --plan smoke|features|sync|load|failure`.

## Deploy

- PythonAnywhere: `git pull`, `pip install -r requirements.txt`, `migrate`, `collectstatic`, Reload.
- Ubuntu VPS / Azure for Students VM: `deploy/azure/README.md` (one-command setup, CI-tested).
- Backups: `BACKUP_AND_RESTORE.md`.

Secrets live only in `.env` (gitignored). Never commit `.env`, `db.sqlite3` or exports.
