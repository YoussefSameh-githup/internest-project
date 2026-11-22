"""Internest AI layer: the ONE place that talks to language models.

    from internest_ai import engine, tasks

    tasks.register_task("cv_polish", schema={"name": str, "skills": [str]}, private=True)
    data = engine.run("cv_polish", system="You polish CVs ...", prompt="Polish this CV.", data={"cv": cv_text},
                      user=request.user, max_tokens=2500, timeout=45)

Modules: providers (adapters), router (order + policy), breaker (circuit breaker), quotas (+ AIUsage), redact (PII),
prompts (untrusted-data isolation + JSON shape checks), tasks (per-task policy + mock responses), engine (orchestration).
Plain calculations never go through here: only work that needs a language model.
"""
