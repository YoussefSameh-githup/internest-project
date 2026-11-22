"""Start a benchmark server + queue workers on a disposable database, run a plan of load scenarios, save results.

    python bench/run_bench.py --plan smoke                 # quick sanity check
    python bench/run_bench.py --plan features --mock-latency 1500 --workers 4 --threads 8

Safety: AI calls go to the deterministic mock provider only (every real provider key is blanked), email goes to the
in-memory backend, the database is bench/out/bench.sqlite3 (seed it first with bench/seed.py). Nothing external is
contacted. Results: bench/out/results/<plan>/<name>.json plus a summary.json.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT = ROOT / "bench" / "out"

PLANS = {
    "smoke": [("cv_analysis", "burst", 5), ("sync_healthz", "burst", 20)],
    # Same-feature contention: every AI feature on its own, growing same-second bursts.
    "features": [(f, "burst", n) for f in ("cv_analysis", "skills_extraction", "assessment_generation",
                                           "interview_questions", "career_recommendations", "resume_builder")
                 for n in (10, 50, 100, 250, 500, 1000)]
                + [("answer_evaluation", "burst", n) for n in (10, 50, 100, 200)],
    # Normal traffic: synchronous pages and reads.
    "sync": [(f, "burst", n) for f in ("sync_healthz", "sync_landing", "sync_feed", "sync_careers", "sync_resume_ats",
                                       "sync_resume_pdf") for n in (10, 50, 100, 250)],
    # Ramp (gradual), sustained and mixed workloads.
    "load": [("cv_analysis", "ramp", 300), ("cv_analysis", "sustained", 600),
             ("cv_analysis,skills_extraction,assessment_generation,interview_questions,career_recommendations,resume_builder",
              "burst", 600)],
}


def bench_env(args):
    env = dict(os.environ)
    blank = ("GEMINI_API_KEY", "AGENTROUTER_API_KEY", "API_KEY_1", "API_KEY_2", "LLM_BACKUP_API_KEY", "AZURE_OPENAI_API_KEY",
             "GROQ_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "WHISPER_API_KEY", "ANYMAIL_SENDGRID_API_KEY",
             "EMAIL_HOST_PASSWORD", "SENTRY_DSN", "FAWATERAK_API_KEY", "AI_LOCAL_BASE_URL")
    env.update({k: "" for k in blank})
    env.update({
        "SQLITE_PATH": str(OUT / "bench.sqlite3"), "DJANGO_DEBUG": "False", "DJANGO_SECURE_SSL_REDIRECT": "False",
        "DJANGO_EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "ASYNC_TASK_BACKEND": "db", "AI_MOCK_PROVIDER": "1", "AI_MOCK_ALLOW_PRODUCTION": "1",
        "AI_MOCK_LATENCY_MS": str(args.mock_latency), "AI_CACHE_TTL": "0", "PYTHONUNBUFFERED": "1",
    })
    for item in args.env or []:
        key, _, value = item.partition("=")
        env[key] = value
    return env


def start(args, env):
    server = subprocess.Popen([PY, "-m", "waitress", "--host=127.0.0.1", f"--port={args.port}", f"--threads={args.threads}",
                               "--connection-limit=2000", "--channel-timeout=120", "internest_app_project.wsgi:application"],
                              cwd=ROOT, env=env, stdout=open(OUT / "server.log", "w"), stderr=subprocess.STDOUT)
    workers = [subprocess.Popen([PY, "manage.py", "run_async_tasks", "--idle", "0.2"], cwd=ROOT, env=env,
                                stdout=open(OUT / f"worker{i}.log", "w"), stderr=subprocess.STDOUT)
               for i in range(args.workers)]
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{args.port}/healthz/", timeout=2).status_code == 200:
                return server, workers
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    stop(server, workers)
    sys.exit("The benchmark server did not start; see bench/out/server.log")


def stop(server, workers):
    for p in [server, *workers]:
        p.terminate()
    for p in [server, *workers]:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", choices=sorted(PLANS), default="smoke")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--mock-latency", type=int, default=1500, help="simulated model latency per AI call (ms)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tag", default="", help="suffix for the results folder")
    parser.add_argument("--env", nargs="*", help="extra KEY=VALUE for the server and workers")
    parser.add_argument("--only", default="", help="comma list of features to keep from the plan")
    args = parser.parse_args()
    if not (OUT / "bench.sqlite3").exists():
        sys.exit("Seed first: SQLITE_PATH=bench/out/bench.sqlite3 python bench/seed.py")
    env = bench_env(args)
    folder = OUT / "results" / (args.plan + (f"-{args.tag}" if args.tag else ""))
    folder.mkdir(parents=True, exist_ok=True)
    server, workers = start(args, env)
    pids = [server.pid] + [w.pid for w in workers]
    summary, offset = [], 0
    try:
        for feature, scenario, users in PLANS[args.plan]:
            if args.only and feature not in args.only.split(","):
                continue
            name = f"{feature.split(',')[0] if ',' not in feature else 'mixed'}_{scenario}_{users}"
            sessions = OUT / ("reviewers.json" if feature == "answer_evaluation" else "sessions.json")
            cmd = [PY, str(ROOT / "bench" / "loadgen.py"), "--base", f"http://127.0.0.1:{args.port}", "--sessions", str(sessions),
                   "--feature", feature, "--scenario", scenario, "--users", str(users), "--offset", str(offset),
                   "--duration", "60" if scenario != "burst" else "0", "--poll", "0" if feature.startswith("sync_") else "1",
                   "--job-timeout", "900", "--pids", *map(str, pids), "--db", env["SQLITE_PATH"],
                   "--out", str(folder / f"{name}.json")]
            print(f"== {name}", flush=True)
            run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            if run.returncode:
                print(run.stderr[-2000:], flush=True)
                continue
            result = json.loads((folder / f"{name}.json").read_text())["result"]
            summary.append({"name": name, **{k: result.get(k) for k in (
                "submitted", "accepted", "throttled", "errors", "completed", "failed", "incoming_rps", "accepted_per_s",
                "completed_per_s", "submit_ms", "end_to_end_ms", "queue_wait_ms", "job_run_ms", "error_rate", "resources")}})
            print(json.dumps(summary[-1]), flush=True)
            offset += users
    finally:
        stop(server, workers)
    meta = {"plan": args.plan, "workers": args.workers, "threads": args.threads, "mock_latency_ms": args.mock_latency,
            "server": "waitress (Windows dev laptop)", "database": "SQLite WAL", "queue": "db backend", "env": args.env or []}
    (folder / "summary.json").write_text(json.dumps({"meta": meta, "runs": summary}, indent=1))
    print(f"Saved {folder / 'summary.json'}")


if __name__ == "__main__":
    main()
