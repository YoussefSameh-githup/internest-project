"""Founder Control Center: access control, real aggregates, actions + audit, events / webhooks, health, performance.
No real network: the probe and webhook HTTP calls are mocked."""
import io
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import Permission, User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core import async_tasks
from internest_core.models import Application, AsyncTask, Internship, PartnerProfile, SecurityEvent, StudentProfile
from internest_startups.models import ProUpgradeRequest

from . import events, health, metrics, perf, watch
from .models import AutomationEvent, FounderNotification, FounderSettings, PerformanceMeasurement, WebVitalDaily

SECRET = "s" * 40
N8N = dict(N8N_WEBHOOK_URL="https://n8n.example/webhook/abc", N8N_WEBHOOK_SECRET=SECRET, ASYNC_TASK_BACKEND="db")
PAGES = ["founder:overview", "founder:users", "founder:companies", "founder:internships", "founder:applications",
         "founder:finance", "founder:analytics", "founder:system", "founder:performance", "founder:automations",
         "founder:notifications", "founder:security", "founder:settings", "founder:search", "founder:health_api"]


def http_response(status, **kw):
    return httpx.Response(status, request=httpx.Request("POST", "https://n8n.example/webhook/abc"), **kw)


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.founder = User.objects.create_superuser("founder", "f@x.com", "pw")
        cls.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        cls.student_user = User.objects.create_user("student", password="pw")
        StudentProfile.objects.create(user=cls.student_user)
        cls.company_user = User.objects.create_user("acme", password="pw")
        cls.partner = PartnerProfile.objects.create(user=cls.company_user, company_name="Acme", is_academic=False)

    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # cached aggregates and cached public pages must not leak between tests
        from django.utils import translation

        translation.activate("en")  # assertions read English messages; earlier requests may leave Arabic active
        self.addCleanup(translation.deactivate)

    def internship(self, days=10, **kw):
        fields = {"partner": self.partner, "title": "Data intern", "description": "x" * 200, "location": "Cairo",
                  "required_majors": "any", "deadline": timezone.localdate() + timedelta(days=days)}
        fields.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return Internship.objects.create(**fields)


class AccessTests(Base):
    def test_anonymous_is_sent_to_login_everywhere(self):
        for name in PAGES:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertIn("/accounts/login", response["Location"])

    def test_signed_in_non_founders_get_403_on_pages_and_api(self):
        for user in (self.student_user, self.company_user, self.staff):
            self.client.force_login(user)
            for name in PAGES:
                response = self.client.get(reverse(name))
                # 403, or a gate of the app itself (e.g. finish company onboarding) that sends them elsewhere
                self.assertIn(response.status_code, (302, 403), f"{user} {name}")
                if response.status_code == 302:
                    self.assertFalse(response["Location"].startswith("/founder/"))
                else:
                    self.assertNotIn(b"fc-shell", response.content)

    def test_posts_are_refused_for_non_founders(self):
        job = self.internship()
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("founder:internship_set_active", args=[job.pk]), {"active": "0"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("founder:user_set_active", args=[self.student_user.pk]), {"active": "0"}).status_code, 403)
        self.assertTrue(Internship.objects.get(pk=job.pk).is_active)
        self.assertTrue(User.objects.get(pk=self.student_user.pk).is_active)

    def test_staff_with_the_explicit_permission_gets_in(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="access_control_center"))
        self.client.force_login(User.objects.get(pk=self.staff.pk))
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 200)

    def test_permission_without_staff_flag_is_not_enough(self):
        self.student_user.user_permissions.add(Permission.objects.get(codename="access_control_center"))
        self.client.force_login(User.objects.get(pk=self.student_user.pk))
        self.assertEqual(self.client.get(reverse("founder:overview")).status_code, 403)

    def test_every_page_renders_for_the_founder_and_is_never_cached(self):
        self.internship()
        self.client.force_login(self.founder)
        for name in PAGES:
            response = self.client.get(reverse(name), {"q": "acme"} if name == "founder:search" else {})
            self.assertEqual(response.status_code, 200, name)
            self.assertIn("no-cache", response.get("Cache-Control", ""), name)

    def test_csrf_is_enforced_on_actions(self):
        from django.test import Client

        job = self.internship()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.founder)
        self.assertEqual(client.post(reverse("founder:internship_set_active", args=[job.pk]), {"active": "0"}).status_code, 403)

    def test_founder_paths_are_disallowed_for_robots(self):
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /founder/", robots)

    def test_secrets_never_reach_the_page(self):
        self.client.force_login(self.founder)
        with override_settings(**N8N):
            body = self.client.get(reverse("founder:automations")).content.decode()
        self.assertNotIn(SECRET, body)
        self.assertNotIn("/webhook/abc", body)  # only the host is shown
        self.assertIn("n8n.example", body)


class MetricsTests(Base):
    def test_overview_counts_match_the_database(self):
        open_job = self.internship()
        self.internship(days=-2)
        self.internship(is_active=False)
        Application.objects.create(internship=open_job, applicant=self.student_user)
        Application.objects.create(internship=open_job, applicant=self.company_user, status=Application.STATUS_ACCEPTED)
        ProUpgradeRequest.objects.create(partner=self.partner, list_price=Decimal("500"), final_price=Decimal("450"),
                                         status=ProUpgradeRequest.STATUS_ACTIVE, activated_at=timezone.now())
        data = metrics.overview(30)
        t = data["totals"]
        self.assertEqual((t["users"], t["students"], t["companies"]), (User.objects.count(), 1, 1))
        self.assertEqual((t["open_internships"], t["expired_internships"], t["closed_internships"]), (1, 1, 1))
        self.assertEqual((t["applications"], t["placements"]), (2, 1))
        self.assertEqual(data["revenue"]["all_time"], {"EGP": Decimal("450.00")})
        self.assertIsNone(data["growth"]["users"]["growth"])  # nothing in the previous window: no invented percentage

    def test_overview_query_count_is_bounded(self):
        for _i in range(5):
            self.internship()
        self.client.force_login(self.founder)
        self.client.get(reverse("founder:overview"))  # warm the session
        from django.core.cache import cache

        cache.clear()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("founder:overview"))
        self.assertLessEqual(len(queries), 32)  # aggregates, not one query per row; cached for 60 s afterwards
        with CaptureQueriesContext(connection) as cached:
            self.client.get(reverse("founder:overview"))
        self.assertLess(len(cached), len(queries))

    def test_lists_do_not_grow_queries_per_row(self):
        self.client.force_login(self.founder)
        for _i in range(3):
            self.internship()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.get(reverse("founder:internships"))  # warm-up (session, permissions)
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse("founder:internships"))
        for _i in range(10):
            self.internship()
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse("founder:internships"))
        self.assertEqual(len(few), len(many))

    def test_analytics_weekly_series_has_12_weeks(self):
        self.internship()
        data = metrics.analytics()
        self.assertEqual(len(data["internships"]), 12)
        self.assertEqual(sum(s["n"] for s in data["internships"]), 1)


class ActionTests(Base):
    def setUp(self):
        self.client.force_login(self.founder)

    def test_close_and_reopen_go_through_save_and_are_audited(self):
        job = self.internship()
        with mock.patch("internest_core.indexnow.notify_internships") as indexnow:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("founder:internship_set_active", args=[job.pk]), {"active": "0"})
        self.assertFalse(Internship.objects.get(pk=job.pk).is_active)
        indexnow.assert_called_once_with([job.pk])  # the existing publication logic still runs
        event = SecurityEvent.objects.get(action=SecurityEvent.FOUNDER_ACTION)
        self.assertEqual((event.actor, event.target, event.detail["op"]), (self.founder, f"internship:{job.pk}", "internship_set_active"))
        self.assertTrue(AutomationEvent.objects.filter(kind="internship.closed").exists())
        self.client.post(reverse("founder:internship_set_active", args=[job.pk]), {"active": "1"})
        self.assertTrue(Internship.objects.get(pk=job.pk).is_active)

    def test_suspend_and_reactivate_a_student(self):
        url = reverse("founder:user_set_active", args=[self.student_user.pk])
        self.client.post(url, {"active": "0"})
        self.assertFalse(User.objects.get(pk=self.student_user.pk).is_active)
        self.client.post(url, {"active": "1"})
        self.assertTrue(User.objects.get(pk=self.student_user.pk).is_active)
        self.assertEqual(SecurityEvent.objects.filter(detail__op="user_set_active").count(), 2)

    def test_superusers_and_self_cannot_be_suspended(self):
        other = User.objects.create_superuser("root2", "r@x.com", "pw")
        for target in (other, self.founder):
            self.client.post(reverse("founder:user_set_active", args=[target.pk]), {"active": "0"})
            self.assertTrue(User.objects.get(pk=target.pk).is_active)
        self.assertEqual(SecurityEvent.objects.filter(detail__ok=False).count(), 2)

    def test_unknown_ids_are_404(self):
        self.assertEqual(self.client.post(reverse("founder:internship_set_active", args=[99999]), {"active": "0"}).status_code, 404)
        self.assertEqual(self.client.get(reverse("founder:user_detail", args=[99999])).status_code, 404)

    def test_open_redirect_is_refused(self):
        job = self.internship()
        response = self.client.post(reverse("founder:internship_set_active", args=[job.pk]), {"active": "0", "next": "https://evil.example/"})
        self.assertEqual(response["Location"], reverse("founder:internships"))

    def test_settings_are_validated_and_audited(self):
        url = reverse("founder:settings")
        bad = self.client.post(url, {"disk_alert_percent": 10, "users_milestone_step": 50, "applications_milestone_step": 100,
                                     "queue_backlog_alert": 50, "slow_page_ms": 800})
        self.assertEqual(bad.status_code, 200)
        self.client.post(url, {"alert_email": "ops@example.com", "email_critical": "on", "disk_alert_percent": 90,
                               "users_milestone_step": 25, "applications_milestone_step": 100, "queue_backlog_alert": 50, "slow_page_ms": 800})
        self.assertEqual(FounderSettings.load().users_milestone_step, 25)
        self.assertTrue(SecurityEvent.objects.filter(detail__op="settings_changed").exists())

    def test_notifications_mark_read(self):
        events.notify("warning", "system", "Disk", dedupe_key="disk:1")
        note = FounderNotification.objects.get(dedupe_key="disk:1")
        self.client.post(reverse("founder:notifications"), {"id": note.pk})
        self.assertIsNotNone(FounderNotification.objects.get(pk=note.pk).read_at)

    def test_search_and_xss_escaping(self):
        PartnerProfile.objects.filter(pk=self.partner.pk).update(company_name="<script>alert(1)</script>")
        body = self.client.get(reverse("founder:search"), {"q": "script"}).content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class EventTests(Base):
    def test_notifications_fold_repeats(self):
        for _i in range(3):
            events.notify("warning", "system", "Queue backlog", dedupe_key="queue:1")
        note = FounderNotification.objects.get(dedupe_key="queue:1")
        self.assertEqual(note.count, 3)

    def test_milestones_fire_once_per_level(self):
        events.milestone("users", 50, 50, "/founder/users/")
        events.milestone("users", 51, 50, "/founder/users/")
        events.milestone("users", 99, 50, "/founder/users/")
        events.milestone("users", 100, 50, "/founder/users/")
        self.assertEqual(list(AutomationEvent.objects.filter(kind="milestone.users").values_list("dedupe_key", flat=True).order_by("pk")),
                         ["milestone:users:50", "milestone:users:100"])
        self.assertEqual(FounderNotification.objects.filter(severity="milestone").count(), 2)

    def test_privileged_account_is_a_critical_security_alert(self):
        before = AutomationEvent.objects.filter(kind="security.privileged_account").count()  # the fixtures' superuser
        user = User.objects.create_user("eve", password="pw")
        user.is_staff = True
        user.save()
        event = AutomationEvent.objects.filter(kind="security.privileged_account").latest("pk")
        self.assertEqual(event.payload, {"user_id": user.pk, "granted": ["is_staff"]})
        self.assertEqual(FounderNotification.objects.get(dedupe_key=event.dedupe_key).severity, "critical")
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])  # logins never trigger privilege checks
        user.save()  # no change in privileges: no new alert
        self.assertEqual(AutomationEvent.objects.filter(kind="security.privileged_account").count(), before + 1)

    def test_payment_transitions(self):
        order = ProUpgradeRequest.objects.create(partner=self.partner, list_price=Decimal("500"), final_price=Decimal("500"))
        order.status = ProUpgradeRequest.STATUS_VERIFYING
        order.save()
        order.status = ProUpgradeRequest.STATUS_ACTIVE
        order.save()
        order.save()  # no transition: no new event
        kinds = list(AutomationEvent.objects.filter(kind__startswith="payment.").values_list("kind", flat=True).order_by("pk"))
        self.assertEqual(kinds, ["payment.receipt_uploaded", "payment.succeeded"])
        payload = AutomationEvent.objects.get(kind="payment.succeeded").payload
        self.assertNotIn("customer_email", json.dumps(payload))

    def test_payloads_have_no_personal_data(self):
        user = User.objects.create_user("newbie", email="newbie@example.com", password="pw")
        StudentProfile.objects.create(user=user)
        payload = json.dumps(list(AutomationEvent.objects.values_list("payload", flat=True)))
        self.assertNotIn("newbie@example.com", payload)
        self.assertNotIn("newbie", payload)

    def test_events_are_recorded_but_not_sent_without_n8n(self):
        self.internship()
        event = AutomationEvent.objects.get(kind="internship.published")
        self.assertEqual(event.status, AutomationEvent.SKIPPED)
        self.assertFalse(AsyncTask.objects.filter(kind=events.DELIVER_JOB).exists())

    def test_core_flows_survive_a_broken_event_layer(self):
        with mock.patch("internest_founder.models.AutomationEvent.objects.create", side_effect=RuntimeError("db down")):
            job = self.internship()  # publishing still works
        self.assertTrue(Internship.objects.filter(pk=job.pk).exists())

    def test_critical_alert_email_is_deduplicated(self):
        FounderSettings.objects.update_or_create(pk=1, defaults={"alert_email": "ops@example.com"})
        with self.captureOnCommitCallbacks(execute=True):
            events.notify("critical", "system", "Worker down", dedupe_key="worker:1")
        with self.captureOnCommitCallbacks(execute=True):
            events.notify("critical", "system", "Worker down", dedupe_key="worker:1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Worker down", mail.outbox[0].subject)


@override_settings(**N8N)
class WebhookTests(Base):
    def emit(self):
        with self.captureOnCommitCallbacks(execute=True):
            return events.emit("company.verified", {"company_id": 7})

    def test_signature_and_replay_protection(self):
        body, ts = b'{"a":1}', int(time.time())
        sig = events.sign(body, ts, SECRET)
        self.assertTrue(events.verify(body, str(ts), sig, SECRET))
        self.assertFalse(events.verify(body + b" ", str(ts), sig, SECRET))       # tampered body
        self.assertFalse(events.verify(body, str(ts), sig, "other" * 10))       # wrong secret
        self.assertFalse(events.verify(body, str(ts - 3600), events.sign(body, ts - 3600, SECRET), SECRET))  # replayed later

    def test_delivery_is_signed_async_and_idempotent(self):
        with mock.patch("internest_core.http_client.post", return_value=http_response(200)) as post:
            event = self.emit()
            self.assertEqual(post.call_count, 0)  # nothing sent in the request
            call_command("run_async_tasks", "--once", stdout=io.StringIO())
            self.assertEqual(post.call_count, 1)
            events.deliver(event.pk)  # a duplicate run does nothing
            self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        headers, body = kwargs["headers"], kwargs["content"]
        self.assertEqual(headers["X-Internest-Event-Id"], str(event.event_id))
        self.assertTrue(events.verify(body, headers["X-Internest-Timestamp"], headers["X-Internest-Signature"], SECRET))
        self.assertEqual(json.loads(body)["data"], {"company_id": 7})
        self.assertEqual(AutomationEvent.objects.get(pk=event.pk).status, AutomationEvent.DELIVERED)

    def test_5xx_and_timeouts_retry_then_fail_once(self):
        with mock.patch("internest_core.http_client.post", side_effect=httpx.ReadTimeout("slow")):
            event = self.emit()
            with self.assertLogs("internest_core.async_tasks", "WARNING"):
                async_tasks.run_next()
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts), (AutomationEvent.PENDING, 1))
        AutomationEvent.objects.filter(pk=event.pk).update(attempts=events.MAX_ATTEMPTS - 1)
        with mock.patch("internest_core.http_client.post", return_value=http_response(503)):
            self.assertEqual(events.deliver(event.pk)[0], 502)
        event.refresh_from_db()
        self.assertEqual(event.status, AutomationEvent.FAILED)
        self.assertTrue(FounderNotification.objects.filter(category="automation").exists())

    def test_4xx_is_not_retried_and_can_be_retried_by_the_founder(self):
        with mock.patch("internest_core.http_client.post", return_value=http_response(401)):
            event = self.emit()
            self.assertEqual(events.deliver(event.pk)[0], 502)
        event.refresh_from_db()
        self.assertEqual(event.status, AutomationEvent.FAILED)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(events.retry(event))
        self.assertEqual(AutomationEvent.objects.get(pk=event.pk).status, AutomationEvent.PENDING)

    def test_http_webhook_urls_are_refused(self):
        with override_settings(N8N_WEBHOOK_URL="http://n8n.example/hook"):
            self.assertFalse(events.is_configured())
        with override_settings(N8N_WEBHOOK_SECRET="short"):
            self.assertFalse(events.is_configured())


class HealthTests(Base):
    def test_snapshot_states(self):
        snap = health.snapshot(use_cache=False)
        by_key = {c["key"]: c["state"] for c in snap["checks"]}
        self.assertEqual(by_key["database"], health.HEALTHY)
        self.assertEqual(by_key["website"], health.UNKNOWN)  # no probe: never "healthy" without evidence
        self.assertIn(snap["overall"], (health.UNKNOWN, health.DEGRADED, health.UNAVAILABLE, health.HEALTHY))

    @override_settings(ASYNC_TASK_BACKEND="db")
    def test_worker_heartbeat(self):
        from django.core.cache import cache

        cache.delete(async_tasks.HEARTBEAT_KEY)
        self.assertEqual(health.worker({"wait": 0})["state"], health.UNKNOWN)
        self.assertEqual(health.worker({"wait": 600})["state"], health.UNAVAILABLE)
        async_tasks.heartbeat()
        self.assertEqual(health.worker({"wait": 0})["state"], health.HEALTHY)
        cache.set(async_tasks.HEARTBEAT_KEY, time.time() - 900, 600)
        self.assertEqual(health.worker({"wait": 0})["state"], health.UNAVAILABLE)

    def test_stale_probe_is_unknown(self):
        PerformanceMeasurement.objects.create(source="probe", url=perf.absolute("/"), measured_at=timezone.now() - timedelta(hours=5),
                                              status_code=200, ttfb_ms=100)
        self.assertEqual(health.website()["state"], health.UNKNOWN)

    @override_settings(ASYNC_TASK_BACKEND="db")
    def test_watch_raises_deduplicated_alerts(self):
        from django.core.cache import cache

        cache.delete(async_tasks.HEARTBEAT_KEY)
        AsyncTask.objects.create(kind="x", status=AsyncTask.STATUS_QUEUED, created_at=timezone.now() - timedelta(minutes=10))
        AsyncTask.objects.filter(kind="x").update(created_at=timezone.now() - timedelta(minutes=10), next_run_at=timezone.now() - timedelta(minutes=10))
        prefs = FounderSettings.load()
        self.assertIn("system.worker_down", watch.check_alerts(prefs))
        watch.check_alerts(prefs)
        self.assertEqual(AutomationEvent.objects.filter(kind="system.worker_down").count(), 1)

    def test_daily_summary_once(self):
        FounderSettings.objects.update_or_create(pk=1, defaults={"daily_summary": True, "alert_email": "ops@example.com"})
        prefs = FounderSettings.load()
        noon = timezone.now().replace(hour=12)
        self.assertTrue(watch.daily_summary(prefs, now=noon))
        self.assertFalse(watch.daily_summary(prefs, now=noon))
        self.assertEqual(len(mail.outbox), 1)


@override_settings(SITE_URL="https://internest.app")
class PerformanceTests(Base):
    def fake_client(self, status=200, headers=None):
        class Stream:
            def __init__(self, *a, **k):
                self.status_code, self.headers = status, httpx.Headers(headers or {"content-type": "text/html", "content-encoding": "gzip"})

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_raw(self):
                yield b"x" * 1000

        client = mock.MagicMock()
        client.__enter__.return_value.stream.side_effect = lambda method, url: Stream()
        return client

    def test_probe_records_and_is_rate_limited(self):
        with mock.patch("internest_founder.perf.httpx.Client", return_value=self.fake_client()):
            rows = perf.probe()
            self.assertGreaterEqual(len(rows), 5)
            self.assertEqual(perf.probe(), [])  # within 5 minutes: nothing
        home = PerformanceMeasurement.objects.filter(url="https://internest.app/").first()
        self.assertEqual((home.status_code, home.bytes, home.data["content_encoding"]), (200, 1000, "gzip"))

    def test_two_failed_probes_raise_one_critical_alert(self):
        with mock.patch("internest_founder.perf.httpx.Client", return_value=self.fake_client(status=522)):
            perf.probe(force=True)
            perf.probe(force=True)
            perf.probe(force=True)
        self.assertEqual(FounderNotification.objects.filter(severity="critical", category="system").count(), 1)

    def test_uncompressed_text_is_a_recommendation(self):
        with mock.patch("internest_founder.perf.httpx.Client", return_value=self.fake_client(headers={"content-type": "text/html"})):
            perf.probe()
        recs = perf.recommendations(FounderSettings.load())
        self.assertTrue(any("Uncompressed" in r["problem"] for r in recs))
        self.assertTrue(all(r["evidence"] for r in recs))

    def lighthouse(self, url="https://internest.app/"):
        return {"lighthouseVersion": "12.8.2", "finalDisplayedUrl": url, "fetchTime": "2026-11-27T10:00:00.000Z",
                "configSettings": {"formFactor": "mobile", "throttlingMethod": "simulate"},
                "categories": {"performance": {"score": 0.72}, "seo": {"score": 1}},
                "audits": {"largest-contentful-paint": {"numericValue": 3400}, "cumulative-layout-shift": {"numericValue": 0.01},
                           "total-blocking-time": {"numericValue": 180}, "total-byte-weight": {"numericValue": 900000},
                           "render-blocking-resources": {"title": "Eliminate render-blocking resources", "score": 0.3,
                                                         "details": {"type": "opportunity", "overallSavingsMs": 450}}}}

    def test_lighthouse_import_and_upload(self):
        run = perf.import_lighthouse(self.lighthouse())
        self.assertEqual((run.data["scores"]["performance"], run.data["metrics"]["LCP"]), (0.72, 3400))
        self.assertEqual(run.data["opportunities"][0]["id"], "render-blocking-resources")
        with self.assertRaises(ValueError):
            perf.import_lighthouse(self.lighthouse(url="https://other.example/"))
        self.client.force_login(self.founder)
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("lh.json", json.dumps(self.lighthouse()).encode(), content_type="application/json")
        self.client.post(reverse("founder:performance"), {"report": upload})
        self.assertEqual(PerformanceMeasurement.objects.filter(source="lab").count(), 2)
        from django.utils import translation

        with translation.override("en"):  # the request above activated the site's default language (Arabic)
            recs = perf.recommendations(FounderSettings.load())
        self.assertTrue(any("LCP" in r["problem"] for r in recs))
        self.assertTrue(any("Lighthouse estimate" in r["benefit"] for r in recs))

    def test_vitals_beacon_counts_only_valid_public_data(self):
        url = reverse("rum_vitals")
        good = {"p": "/", "d": "mobile", "m": [{"n": "LCP", "v": 1800}, {"n": "CLS", "v": 0.3}, {"n": "BAD", "v": 1}, {"n": "INP", "v": -5}]}
        self.assertEqual(self.client.post(url, json.dumps(good), content_type="application/json").status_code, 204)
        self.client.post(url, json.dumps({"p": "/accounts/login/", "d": "mobile", "m": [{"n": "LCP", "v": 1}]}), content_type="application/json")
        self.client.post(url, "not json", content_type="application/json")
        self.client.post(url, json.dumps({"p": "/", "m": [{"n": "LCP", "v": 1}]}) + " " * 3000, content_type="application/json")
        rows = {(r.metric, r.page): r for r in WebVitalDaily.objects.all()}
        self.assertEqual(set(rows), {("LCP", "landing"), ("CLS", "landing")})
        self.assertEqual((rows[("LCP", "landing")].good, rows[("CLS", "landing")].poor), (1, 1))
        summary = perf.field_summary()
        self.assertEqual({s["metric"] for s in summary}, {"LCP", "CLS"})

    def test_vitals_script_only_for_anonymous_public_pages(self):
        home = self.client.get("/").content.decode()
        self.assertIn("founder/vitals", home)
        self.client.force_login(self.student_user)
        self.assertNotIn("founder/vitals", self.client.get("/").content.decode())

    @override_settings(FOUNDER_RUM_SAMPLE_RATE=0)
    def test_vitals_can_be_switched_off(self):
        self.assertNotIn("founder/vitals", self.client.get("/").content.decode())
        self.client.post(reverse("rum_vitals"), json.dumps({"p": "/", "d": "mobile", "m": [{"n": "LCP", "v": 1}]}), content_type="application/json")
        self.assertFalse(WebVitalDaily.objects.exists())
