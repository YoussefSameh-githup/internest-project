"""Website performance: three honest sources, never mixed.

    probe()              synthetic: this server fetches the public pages THROUGH Cloudflare (sequential, 6 requests,
                         at most once per PROBE_MIN_INTERVAL) and stores status, TTFB, total time, size and the
                         caching / compression headers. It measures the server + network path, not rendering.
    import_lighthouse()  lab: a Lighthouse JSON report (run from any machine with Chrome) -> scores, lab vitals
                         and Lighthouse's own opportunities. One run is one controlled test, not all visitors.
    record_vitals()      field: real visitors' LCP / INP / CLS / FCP / TTFB from static/founder/vitals.js
                         (anonymous visitors on public pages, sampled), counted per day into good / NI / poor.
    recommendations()    rules over the stored evidence; every item cites the measurement it comes from.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.templatetags.static import static
from django.urls import Resolver404, resolve
from django.utils import timezone
from django.utils.translation import gettext as _

from internest_core.seo import PUBLIC_PAGES, site_url

PROBE_PATHS = ("/", "/trust/", "/universities/", "/accounts/login/", "/sitemap.xml")
PROBE_MIN_INTERVAL = timedelta(minutes=5)
PROBE_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
PROBE_RETENTION = timedelta(days=45)
LAB_RETENTION = 200  # newest lab runs kept

# web.dev thresholds: (good <=, poor >)
THRESHOLDS = {"LCP": (2500, 4000), "INP": (200, 500), "CLS": (0.1, 0.25), "FCP": (1800, 3000), "TTFB": (800, 1800)}
MAX_VALUE = {"LCP": 60000, "INP": 20000, "CLS": 10, "FCP": 60000, "TTFB": 60000}
FIELD_PAGES = frozenset(PUBLIC_PAGES) | {"internship_detail"}


def base() -> str:
    return site_url() or "https://internest.app"


def absolute(path: str) -> str:
    return f"{base()}{path}"


def static_probe_path() -> str:
    try:
        return static("css/styles.css")
    except Exception:
        return ""


# ---------------------------------------------------------------- synthetic probe
def _fetch(client, url):
    started = time.perf_counter()
    with client.stream("GET", url) as response:
        ttfb = (time.perf_counter() - started) * 1000
        size = sum(len(chunk) for chunk in response.iter_raw())
        total = (time.perf_counter() - started) * 1000
        headers = response.headers
        return response.status_code, ttfb, total, size, {
            "content_type": headers.get("content-type", "")[:60],
            "content_encoding": headers.get("content-encoding", ""),
            "cache_control": headers.get("cache-control", "")[:120],
            "cf_cache_status": headers.get("cf-cache-status", ""),
            "server": headers.get("server", "")[:30],
        }


def probe(force=False) -> list:
    from .models import PerformanceMeasurement

    now = timezone.now()
    last = PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE).values_list("measured_at", flat=True).first()
    if not force and last and now - last < PROBE_MIN_INTERVAL:
        return []  # rate limit: never hammer production
    paths = list(PROBE_PATHS) + ([static_probe_path()] if static_probe_path() else [])
    saved = []
    headers = {"User-Agent": "InternestProbe/1 (+founder center synthetic check)", "Accept-Encoding": "gzip, br"}
    with httpx.Client(timeout=PROBE_TIMEOUT, headers=headers, follow_redirects=False) as client:
        for path in paths:
            url = absolute(path)
            row = PerformanceMeasurement(source=PerformanceMeasurement.PROBE, url=url, measured_at=timezone.now(), data={"path": path})
            try:
                code, ttfb, total, size, info = _fetch(client, url)
                row.status_code, row.ttfb_ms, row.total_ms, row.bytes = code, round(ttfb), round(total), size
                row.data.update(info)
            except httpx.HTTPError as exc:
                row.error = type(exc).__name__
            saved.append(row)
    PerformanceMeasurement.objects.bulk_create(saved)
    PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE, measured_at__lt=now - PROBE_RETENTION).delete()
    _alert_on_downtime()
    return saved


def _alert_on_downtime():
    """Two failed homepage probes in a row -> one critical alert per hour (deduplicated)."""
    from . import events
    from .models import PerformanceMeasurement

    last_two = list(PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE, url=absolute("/"))[:2])
    if len(last_two) == 2 and all(m.status_code != 200 for m in last_two):
        hour = timezone.now().strftime("%Y-%m-%dT%H")
        detail = {"status": last_two[0].status_code, "error": last_two[0].error}
        events.emit("system.website_down", detail, dedupe_key=f"website-down:{hour}",
                    notify_as=("critical", "system", "Website is not answering (2 probes failed)", "/founder/system/"))


# ---------------------------------------------------------------- lab (Lighthouse)
LAB_AUDITS = {"LCP": "largest-contentful-paint", "CLS": "cumulative-layout-shift", "TBT": "total-blocking-time",
              "FCP": "first-contentful-paint", "SI": "speed-index", "TTFB": "server-response-time",
              "WEIGHT": "total-byte-weight"}


def parse_lighthouse(report: dict) -> dict:
    audits = report.get("audits") or {}
    if not isinstance(audits, dict) or "categories" not in report:
        raise ValueError("Not a Lighthouse JSON report")
    metrics = {name: audits.get(audit_id, {}).get("numericValue") for name, audit_id in LAB_AUDITS.items()}
    opportunities = []
    for audit_id, audit in audits.items():
        details = audit.get("details") or {}
        savings = details.get("overallSavingsMs") or 0
        savings_bytes = details.get("overallSavingsBytes") or 0
        if (savings >= 100 or savings_bytes >= 20_000) and (audit.get("score") is not None and audit["score"] < 0.9):
            opportunities.append({"id": audit_id, "title": str(audit.get("title", audit_id))[:140],
                                  "savings_ms": round(savings), "savings_kb": round(savings_bytes / 1024)})
    opportunities.sort(key=lambda o: (o["savings_ms"], o["savings_kb"]), reverse=True)
    scores = {k: (v or {}).get("score") for k, v in (report.get("categories") or {}).items()}
    config = report.get("configSettings") or {}
    return {"scores": scores, "metrics": metrics, "opportunities": opportunities[:10],
            "form_factor": config.get("formFactor", ""), "throttling": config.get("throttlingMethod", ""),
            "lighthouse": report.get("lighthouseVersion", ""), "fetch_time": report.get("fetchTime", "")}


def import_lighthouse(report: dict):
    from django.utils.dateparse import parse_datetime

    from .models import PerformanceMeasurement

    data = parse_lighthouse(report)
    url = str(report.get("finalDisplayedUrl") or report.get("finalUrl") or report.get("requestedUrl") or "")[:300]
    if not url.startswith(base()):
        raise ValueError("The report is not for this site")
    measured = parse_datetime(data["fetch_time"] or "") or timezone.now()
    row = PerformanceMeasurement.objects.create(
        source=PerformanceMeasurement.LAB, url=url, measured_at=measured,
        ttfb_ms=round(data["metrics"]["TTFB"]) if data["metrics"].get("TTFB") is not None else None,
        bytes=round(data["metrics"]["WEIGHT"]) if data["metrics"].get("WEIGHT") is not None else None, data=data)
    stale = PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.LAB).values_list("pk", flat=True)[LAB_RETENTION:]
    PerformanceMeasurement.objects.filter(pk__in=list(stale)).delete()
    return row


# ---------------------------------------------------------------- field (real visitors)
def rate(metric: str, value: float) -> str:
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else ("poor" if value > poor else "needs_improvement")


def page_name(path: str) -> str | None:
    try:
        match = resolve(path.split("?")[0])
    except Resolver404:
        return None
    return match.url_name if match.url_name in FIELD_PAGES else None


def record_vitals(payload: dict) -> int:
    """Validate one beacon and add it to today's counters. Returns how many metrics were counted."""
    from .models import WebVitalDaily

    if not isinstance(payload, dict):
        return 0
    page = page_name(str(payload.get("p", ""))[:200])
    device = "mobile" if payload.get("d") == "mobile" else "desktop"
    metrics = payload.get("m")
    if page is None or not isinstance(metrics, list):
        return 0
    day, counted = timezone.localdate(), 0
    for item in metrics[:5]:
        name, value = (item or {}).get("n"), (item or {}).get("v")
        if name not in THRESHOLDS or not isinstance(value, (int, float)) or not 0 <= value <= MAX_VALUE[name]:
            continue
        bucket = rate(name, value)
        key = {"day": day, "page": page, "metric": name, "device": device}
        updated = WebVitalDaily.objects.filter(**key).update(**{bucket: F(bucket) + 1}, total=F("total") + value)
        if not updated:
            try:
                with transaction.atomic():
                    WebVitalDaily.objects.create(**key, **{bucket: 1}, total=value)
            except IntegrityError:
                WebVitalDaily.objects.filter(**key).update(**{bucket: F(bucket) + 1}, total=F("total") + value)
        counted += 1
    return counted


def field_summary(days=28) -> list:
    """Per metric x device over the window: samples, % good / NI / poor, mean, and the CrUX-style rating
    (good when >= 75 % of samples are good, poor when > 25 % are poor)."""
    from .models import WebVitalDaily

    since = timezone.localdate() - timedelta(days=days)
    rows = (WebVitalDaily.objects.filter(day__gte=since).values("metric", "device")
            .annotate(good=Sum("good"), ni=Sum("needs_improvement"), poor=Sum("poor"), total=Sum("total")))
    out = []
    for r in rows:
        n = r["good"] + r["ni"] + r["poor"]
        if not n:
            continue
        good, poor = r["good"] * 100 / n, r["poor"] * 100 / n
        verdict = "good" if good >= 75 else ("poor" if poor > 25 else "needs_improvement")
        out.append({"metric": r["metric"], "device": r["device"], "samples": n, "good": round(good), "ni": round(100 - good - poor),
                    "poor": round(poor), "mean": r["total"] / n, "verdict": verdict})
    order = list(THRESHOLDS)
    return sorted(out, key=lambda r: (order.index(r["metric"]), r["device"]))


# ---------------------------------------------------------------- summaries
def latest_probes() -> list:
    """Newest probe per page with the median TTFB of the last 24 h (the trend the founder should read)."""
    from .models import PerformanceMeasurement

    since = timezone.now() - timedelta(hours=24)
    rows = list(PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE, measured_at__gte=since)
                .order_by("url", "-measured_at").values("url", "measured_at", "status_code", "ttfb_ms", "total_ms", "bytes", "data", "error")[:2000])
    by_url = {}
    for row in rows:
        by_url.setdefault(row["url"], []).append(row)
    out = []
    for url, items in by_url.items():
        ttfbs = [i["ttfb_ms"] for i in items if i["ttfb_ms"] is not None and i["status_code"] == 200]
        newest = items[0]
        out.append({**newest, "path": newest["data"].get("path", url), "samples": len(items),
                    "median_ttfb": round(statistics.median(ttfbs)) if ttfbs else None,
                    "ok_rate": round(sum(1 for i in items if i["status_code"] == 200) * 100 / len(items))})
    return sorted(out, key=lambda r: r["path"])


def daily_ttfb(days=14) -> list:
    """Median homepage TTFB per day (synthetic), for the trend chart and before/after comparisons."""
    from .models import PerformanceMeasurement

    since = timezone.now() - timedelta(days=days)
    values = PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE, url=absolute("/"), measured_at__gte=since,
                                                   status_code=200).values_list("measured_at", "ttfb_ms")[:5000]
    per_day = {}
    for at, ttfb in values:
        if ttfb is not None:
            per_day.setdefault(timezone.localtime(at).date(), []).append(ttfb)
    return [{"day": day, "median": round(statistics.median(v)), "n": len(v)} for day, v in sorted(per_day.items())]


def lab_runs(limit=6) -> list:
    from .models import PerformanceMeasurement

    return list(PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.LAB)[:limit])


# ---------------------------------------------------------------- recommendations
def _rec(problem, evidence, impact, severity, fix, effort, benefit, automatable=False):
    return {"problem": problem, "evidence": evidence, "impact": impact, "severity": severity, "fix": fix,
            "effort": effort, "benefit": benefit, "automatable": automatable}


def recommendations(prefs) -> list:
    recs = []
    probes = latest_probes()
    for p in probes:
        if p["median_ttfb"] and p["median_ttfb"] > prefs.slow_page_ms and not p["path"].startswith("/static/"):
            recs.append(_rec(
                _("Slow server response on %(path)s") % {"path": p["path"]},
                _("Median TTFB %(ms)s ms over %(n)s probes in 24 h (synthetic, from the server through Cloudflare)") % {"ms": p["median_ttfb"], "n": p["samples"]},
                _("Every visitor waits this long before anything renders; it adds directly to LCP."),
                "high" if p["median_ttfb"] > prefs.slow_page_ms * 2 else "medium",
                _("Profile the view (query count, template work); cache the anonymous page with @cache_public_page if it is the same for everyone."),
                _("1-3 h"), _("Lower TTFB and LCP for every visit (estimate)")))
        info = p["data"]
        text = any(t in info.get("content_type", "") for t in ("text/", "javascript", "json", "xml"))
        if p["status_code"] == 200 and text and not info.get("content_encoding"):
            recs.append(_rec(
                _("Uncompressed response: %(path)s") % {"path": p["path"]},
                _("No Content-Encoding header on the latest probe (%(type)s)") % {"type": info.get("content_type")},
                _("Text sent uncompressed is typically 3-5x larger on mobile networks."),
                "medium", _("Enable gzip/Brotli for this content type in nginx or Cloudflare."), _("15 min"),
                _("Smaller transfers (estimate)")))
        if p["path"].startswith("/static/") and p["status_code"] == 200 and "max-age=31536000" not in info.get("cache_control", ""):
            recs.append(_rec(
                _("Static files are not cached long-term"), _("Cache-Control: %(cc)s") % {"cc": info.get("cache_control") or "-"},
                _("Repeat visitors download CSS/JS again."), "medium",
                _("Serve hashed /static/ files with Cache-Control: public, max-age=31536000, immutable."), _("15 min"),
                _("Faster repeat visits (estimate)"), automatable=False))
    for run in lab_runs(limit=3):
        data, path = run.data, run.url[len(base()):] or "/"
        score = (data.get("scores") or {}).get("performance")
        m = data.get("metrics") or {}
        if m.get("LCP") and m["LCP"] > THRESHOLDS["LCP"][0]:
            recs.append(_rec(
                _("Lab LCP above 2.5 s on %(path)s") % {"path": path},
                _("Lighthouse %(v)s (%(ff)s): LCP %(lcp).1f s, performance score %(s)s") % {
                    "v": data.get("lighthouse"), "ff": data.get("form_factor"), "lcp": m["LCP"] / 1000, "s": round(score * 100) if score is not None else "-"},
                _("The main content appears late; users on slow phones may leave."), "high" if m["LCP"] > 4000 else "medium",
                _("Start with the Lighthouse opportunities listed for this run (largest savings first)."), _("2-6 h"),
                _("Better LCP (Lighthouse estimate, not measured)")))
        if m.get("CLS") is not None and m["CLS"] > THRESHOLDS["CLS"][0]:
            recs.append(_rec(_("Layout shifts on %(path)s") % {"path": path}, _("Lab CLS %(c).3f") % {"c": m["CLS"]},
                             _("Content jumps while loading; mis-taps."), "medium",
                             _("Reserve space for images/embeds; avoid animating layout properties."), _("1-2 h"), _("Stable layout")))
        for opp in data.get("opportunities", [])[:3]:
            recs.append(_rec(
                opp["title"], _("Lighthouse opportunity on %(path)s: ~%(ms)s ms / %(kb)s KB potential savings") % {
                    "path": path, "ms": opp["savings_ms"], "kb": opp["savings_kb"]},
                _("Reported by Lighthouse for this lab run."), "medium" if opp["savings_ms"] >= 300 else "low",
                _("See the Lighthouse audit '%(id)s' for the exact resources.") % {"id": opp["id"]}, _("varies"),
                _("Lighthouse estimate, not measured")))
    for row in field_summary():
        if row["samples"] >= 20 and row["verdict"] == "poor":
            recs.append(_rec(
                _("Real visitors: poor %(m)s on %(d)s") % {"m": row["metric"], "d": row["device"]},
                _("%(p)s%% of %(n)s sampled visits rated poor in 28 days (field data)") % {"p": row["poor"], "n": row["samples"]},
                _("This is what real users experience, not a lab run."), "high",
                _("Compare with the lab run for the same page to find the cause."), _("varies"), _("Measured improvement visible in field data after ~28 days")))
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(recs, key=lambda r: order[r["severity"]])


def vitals_script_enabled() -> bool:
    return float(getattr(settings, "FOUNDER_RUM_SAMPLE_RATE", 0.1) or 0) > 0


def report_json_size_ok(raw: bytes) -> bool:
    return len(raw) <= 8 * 1024 * 1024


def loads_report(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))
