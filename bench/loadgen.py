"""Internest load generator: same-second bursts, ramps and sustained load against one AI feature or a mix.

It separates what the prompt asks to keep apart:
  submitted  = requests sent                    accepted  = 202 (job queued)
  throttled  = 429/503 (quota / backpressure)   errors    = anything else (5xx, timeouts)
  completed  = jobs that reached status "done"  failed    = jobs that ended failed/dead/timeout
and records submit latency (p50/p95/p99), queue wait (created -> started) and job time (started -> finished),
plus CPU/RAM of the server and worker processes and the queue depth sampled from the database.

  python bench/loadgen.py --base http://127.0.0.1:8765 --sessions bench/out/sessions.json \
      --feature cv_analysis --scenario burst --users 100 --out bench/out/cv_burst_100.json

Run it with the project venv (needs httpx; psutil optional). Features are described in bench/features.py.
"""
import argparse
import asyncio
import json
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from features import FEATURES  # noqa: E402

try:
    import psutil
except ImportError:  # resource sampling is optional
    psutil = None


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return round(values[k], 1)


class Metrics:
    def __init__(self):
        self.submit_ms, self.end_to_end_ms = [], []
        self.status = {}
        self.accepted = self.throttled = self.errors = self.completed = self.failed = 0
        self.first_submit = self.last_submit = None
        self.completion_times = []
        self.task_ids = []

    def summary(self, wall):
        span = max(1e-6, (self.last_submit or 0) - (self.first_submit or 0)) if self.first_submit else 0
        done_span = (max(self.completion_times) - self.first_submit) if self.completion_times else 0
        submitted = sum(self.status.values())
        return {
            "submitted": submitted, "accepted": self.accepted, "throttled": self.throttled, "errors": self.errors,
            "completed": self.completed, "failed": self.failed, "status_codes": self.status,
            "submit_window_s": round(span, 3), "wall_s": round(wall, 2),
            "incoming_rps": round(submitted / span, 1) if span > 0.001 else None,
            "accepted_per_s": round(self.accepted / span, 1) if span > 0.001 else None,
            "completed_per_s": round(self.completed / done_span, 2) if done_span else None,
            "submit_ms": {"avg": round(statistics.mean(self.submit_ms), 1) if self.submit_ms else None,
                          "p50": pct(self.submit_ms, 50), "p95": pct(self.submit_ms, 95), "p99": pct(self.submit_ms, 99),
                          "max": round(max(self.submit_ms), 1) if self.submit_ms else None},
            "end_to_end_ms": {"p50": pct(self.end_to_end_ms, 50), "p95": pct(self.end_to_end_ms, 95),
                              "p99": pct(self.end_to_end_ms, 99)},
            "error_rate": round((self.errors + self.failed) / submitted, 4) if submitted else None,
        }


async def one_user(client, session, feature, metrics, poll, timeout):
    spec = FEATURES[feature]
    cookies = {} if spec.get("anonymous") else {"internest_sessionid": session["sessionid"], "csrftoken": session["csrftoken"]}
    headers = {"X-CSRFToken": session["csrftoken"], "Prefer": "respond-async", "Accept": "application/json",
               "Referer": client.base_url.__str__(), "X-Real-IP": session.get("ip", "127.0.0.1")}
    method, path, kwargs = spec["request"](session)
    started = time.perf_counter()
    metrics.first_submit = metrics.first_submit or started
    try:
        response = await client.request(method, path, headers=headers, cookies=cookies, **kwargs)
    except Exception as exc:  # connection refused / timeout
        metrics.status[type(exc).__name__] = metrics.status.get(type(exc).__name__, 0) + 1
        metrics.errors += 1
        return
    finally:
        metrics.last_submit = time.perf_counter()
    elapsed = (time.perf_counter() - started) * 1000
    metrics.submit_ms.append(elapsed)
    metrics.status[str(response.status_code)] = metrics.status.get(str(response.status_code), 0) + 1
    if response.status_code in (429, 503):
        metrics.throttled += 1
        return
    if response.status_code not in (200, 201, 202):
        metrics.errors += 1
        return
    metrics.accepted += 1
    if response.status_code != 202 or not poll:  # synchronous answer: already complete
        if response.status_code in (200, 201):
            metrics.completed += 1
            metrics.completion_times.append(time.perf_counter())
            metrics.end_to_end_ms.append(elapsed)
        return
    data = response.json()
    status_url = data.get("status_url") or f"/api/tasks/{data.get('task_id')}/status/"
    metrics.task_ids.append(data.get("task_id"))
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        await asyncio.sleep(poll)
        try:
            status = (await client.get(status_url, cookies=cookies, headers={
                "Accept": "application/json", "X-Real-IP": session.get("ip", "127.0.0.1")})).json()
        except Exception:
            continue
        state = status.get("status")
        if state == "done":
            metrics.completed += 1
            metrics.completion_times.append(time.perf_counter())
            metrics.end_to_end_ms.append((time.perf_counter() - started) * 1000)
            return
        if state in ("failed", "dead", "cancelled"):
            metrics.failed += 1
            return
    metrics.failed += 1  # never finished inside the timeout


async def sampler(pids, db_path, samples, stop):
    procs = []
    if psutil:
        for pid in pids:
            try:
                p = psutil.Process(pid)
                procs.append([p] + p.children(recursive=True))
            except psutil.Error:
                pass
        for group in procs:
            for p in group:
                p.cpu_percent(None)
    while not stop.is_set():
        row = {"t": time.time()}
        if psutil and procs:
            cpu = mem = 0.0
            for group in procs:
                for p in group:
                    try:
                        cpu += p.cpu_percent(None)
                        mem += p.memory_info().rss
                    except psutil.Error:
                        pass
            row["cpu_pct"] = round(cpu, 1)
            row["rss_mb"] = round(mem / 2**20, 1)
            row["system_cpu_pct"] = psutil.cpu_percent(None)
        if db_path:
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
                row["queue"] = dict(con.execute(
                    "SELECT status, COUNT(*) FROM internest_core_asynctask GROUP BY status").fetchall())
                con.close()
            except sqlite3.Error:
                pass
        samples.append(row)
        await asyncio.sleep(0.5)


def queue_timing(db_path, task_ids):
    """Queue wait (created -> started) and run time (started -> finished) of this run's jobs, from the database."""
    if not db_path or not task_ids:
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    ids = [t.replace("-", "") for t in task_ids if t]
    waits, runs = [], []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for created, started, finished in con.execute(
                f"SELECT created_at, started_at, finished_at FROM internest_core_asynctask WHERE id IN ({q})", chunk):
            from datetime import datetime
            parse = lambda s: datetime.fromisoformat(s) if s else None  # noqa: E731
            c, s, f = parse(created), parse(started), parse(finished)
            if c and s:
                waits.append((s - c).total_seconds() * 1000)
            if s and f:
                runs.append((f - s).total_seconds() * 1000)
    con.close()
    return {"queue_wait_ms": {"p50": pct(waits, 50), "p95": pct(waits, 95), "max": round(max(waits), 1) if waits else None},
            "job_run_ms": {"p50": pct(runs, 50), "p95": pct(runs, 95), "max": round(max(runs), 1) if runs else None},
            "jobs_with_timing": len(runs)}


async def run(args):
    sessions = json.loads(Path(args.sessions).read_text())
    sessions = [s for s in sessions if all(s.get(k) is not None for k in FEATURES[args.feature.split(",")[0]].get("needs", ()))]
    start = args.offset % max(1, len(sessions))
    sessions = (sessions[start:] + sessions[:start])[:args.users]  # rotate so consecutive runs use other accounts
    if args.users > len(sessions):
        sys.exit(f"Only {len(sessions)} seeded sessions; seed more (bench/seed.py --students N).")
    metrics, samples, stop = Metrics(), [], asyncio.Event()
    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users + 10)
    features = args.feature.split(",")
    async with httpx.AsyncClient(base_url=args.base, timeout=args.request_timeout, limits=limits) as client:
        sampler_task = asyncio.create_task(sampler(args.pids, args.db, samples, stop))
        t0 = time.perf_counter()
        jobs = []
        if args.scenario == "burst":  # every user fires inside the same instant
            gate = asyncio.Event()

            async def gated(s, f):
                await gate.wait()
                await one_user(client, s, f, metrics, args.poll, args.job_timeout)
            jobs = [asyncio.create_task(gated(s, features[i % len(features)])) for i, s in enumerate(sessions)]
            await asyncio.sleep(0.2)
            gate.set()
        else:  # ramp / sustained: spread starts over --duration seconds at a steady rate
            interval = args.duration / max(1, len(sessions))
            for i, s in enumerate(sessions):
                jobs.append(asyncio.create_task(one_user(client, s, features[i % len(features)], metrics, args.poll,
                                                         args.job_timeout)))
                await asyncio.sleep(interval if args.scenario == "sustained" else interval * (1 - i / len(sessions)))
        await asyncio.gather(*jobs)
        wall = time.perf_counter() - t0
        await asyncio.sleep(0.6)
        stop.set()
        await sampler_task
    result = {"feature": args.feature, "scenario": args.scenario, "users": args.users, **metrics.summary(wall),
              **queue_timing(args.db, metrics.task_ids)}
    if samples:
        cpus = [s["cpu_pct"] for s in samples if "cpu_pct" in s]
        rss = [s["rss_mb"] for s in samples if "rss_mb" in s]
        depth = [sum(v for k, v in s.get("queue", {}).items() if k in ("queued", "running")) for s in samples]
        result["resources"] = {"proc_cpu_pct_max": max(cpus) if cpus else None,
                               "proc_cpu_pct_avg": round(statistics.mean(cpus), 1) if cpus else None,
                               "rss_mb_max": max(rss) if rss else None,
                               "system_cpu_pct_max": max((s.get("system_cpu_pct", 0) for s in samples), default=None),
                               "queue_depth_max": max(depth) if depth else None}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"result": result, "samples": samples}, indent=1))
    print(json.dumps(result, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--sessions", default="bench/out/sessions.json")
    parser.add_argument("--feature", required=True, help="a key of bench/features.py, or several joined by commas")
    parser.add_argument("--scenario", choices=["burst", "ramp", "sustained"], default="burst")
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--offset", type=int, default=0, help="first seeded account to use (rotates)")
    parser.add_argument("--duration", type=float, default=10.0, help="ramp/sustained: seconds over which users start")
    parser.add_argument("--poll", type=float, default=0.5, help="seconds between status polls (0 = don't follow jobs)")
    parser.add_argument("--job-timeout", type=float, default=300.0)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--pids", type=int, nargs="*", default=[], help="server + worker PIDs for CPU/RAM sampling")
    parser.add_argument("--db", default="", help="SQLite file the server uses (queue depth and wait times)")
    parser.add_argument("--out", default="")
    random.seed(7)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
