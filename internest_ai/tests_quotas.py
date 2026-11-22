"""Quotas: per-user/task, per-partner and global caps, atomic accounting (also under real thread concurrency), usage rows."""
import threading
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings

from . import engine, quotas
from .errors import AIUnavailable, QuotaExceeded
from .models import AIQuotaCounter, AIUsage
from .testing import CREATE, NO_PROVIDERS, reply

UNLIMITED = dict(AI_QUOTA_USER_DAILY=0, AI_QUOTA_PARTNER_DAILY=0, AI_QUOTA_GLOBAL_PER_MINUTE=0, AI_QUOTA_GLOBAL_PER_DAY=0,
                 AI_QUOTA_TASK_LIMITS={})
AI = dict(NO_PROVIDERS, GEMINI_API_KEY="g", GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/")


@override_settings(**UNLIMITED)
class QuotaTests(TestCase):
    @override_settings(AI_QUOTA_USER_DAILY=2)
    def test_per_user_daily_cap_per_task(self):
        quotas.reserve("cv_polish", user_id=1)
        quotas.reserve("cv_polish", user_id=1)
        with self.assertRaises(QuotaExceeded) as ctx:
            quotas.reserve("cv_polish", user_id=1)
        self.assertEqual((ctx.exception.scope, ctx.exception.limit), ("user_daily", 2))
        self.assertTrue(0 < ctx.exception.retry_after <= 86400)
        quotas.reserve("cv_polish", user_id=2)      # another user
        quotas.reserve("cv_analysis", user_id=1)    # another task
        self.assertIsInstance(ctx.exception, AIUnavailable)  # legacy callers fall back as for an outage

    @override_settings(AI_QUOTA_USER_DAILY=50, AI_QUOTA_TASK_LIMITS={"cv_voice": 1})
    def test_per_task_override(self):
        quotas.reserve("cv_voice", user_id=1)
        with self.assertRaises(QuotaExceeded):
            quotas.reserve("cv_voice", user_id=1)

    @override_settings(AI_QUOTA_PARTNER_DAILY=1)
    def test_per_partner_daily_cap(self):
        quotas.reserve("screening", user_id=1, partner_id=9)
        with self.assertRaises(QuotaExceeded) as ctx:
            quotas.reserve("screening", user_id=2, partner_id=9)
        self.assertEqual(ctx.exception.scope, "partner_daily")

    @override_settings(AI_QUOTA_GLOBAL_PER_MINUTE=1)
    def test_global_per_minute_cap_says_when_to_retry(self):
        quotas.reserve("a")
        with self.assertRaises(QuotaExceeded) as ctx:
            quotas.reserve("b", user_id=3)
        self.assertEqual(ctx.exception.scope, "global_minute")
        self.assertTrue(1 <= ctx.exception.retry_after <= 60)

    @override_settings(AI_QUOTA_GLOBAL_PER_DAY=1)
    def test_global_per_day_cap(self):
        quotas.reserve("a")
        with self.assertRaisesMessage(QuotaExceeded, "global_daily"):
            quotas.reserve("a")

    @override_settings(AI_QUOTA_USER_DAILY=5, AI_QUOTA_GLOBAL_PER_MINUTE=1)
    def test_a_refused_request_takes_nothing(self):
        quotas.reserve("t", user_id=1)
        with self.assertRaises(QuotaExceeded):
            quotas.reserve("t", user_id=1)  # user cap fine, global minute full
        self.assertEqual(quotas.remaining("t", user_id=1)["user_daily"], 4)  # the refused call was rolled back

    @override_settings(AI_QUOTA_USER_DAILY=1)
    def test_release_gives_the_unit_back(self):
        quotas.release(quotas.reserve("t", user_id=1))
        quotas.reserve("t", user_id=1)

    def test_unlimited_when_every_cap_is_zero(self):
        self.assertEqual(quotas.reserve("t", user_id=1).buckets, [])
        self.assertFalse(AIQuotaCounter.objects.exists())

    @override_settings(AI_QUOTAS_ENABLED=False, AI_QUOTA_USER_DAILY=1)
    def test_switch(self):
        quotas.reserve("t", user_id=1)
        quotas.reserve("t", user_id=1)


@override_settings(**UNLIMITED)
class QuotaRaceTests(TestCase):
    @override_settings(AI_QUOTA_USER_DAILY=2)
    def test_row_created_by_a_concurrent_request(self):
        """exists() says no row, but another request inserts it before our create: IntegrityError -> conditional update."""
        bucket = quotas.buckets_for("race", user_id=1)[0]
        AIQuotaCounter.objects.create(key=bucket.key, used=1, expires_at=bucket.expires_at)
        manager = type(AIQuotaCounter.objects)
        real_filter = manager.filter
        state = {"first": True}

        def filter_(self, *args, **kwargs):
            if state["first"] and "used__lt" in kwargs:  # the first conditional UPDATE runs "before" the row exists
                state["first"] = False
                return real_filter(self, pk=-1)
            if set(kwargs) == {"key"}:
                return real_filter(self, pk=-1)  # ...and so does the exists() check
            return real_filter(self, *args, **kwargs)

        with mock.patch.object(manager, "filter", filter_):
            quotas.reserve("race", user_id=1)
        self.assertEqual(AIQuotaCounter.objects.get(key=bucket.key).used, 2)
        with self.assertRaises(QuotaExceeded):
            quotas.reserve("race", user_id=1)


@override_settings(**dict(UNLIMITED, AI_QUOTA_USER_DAILY=5))
class ConcurrentQuotaTests(TransactionTestCase):
    """Real threads, real connections: 12 simultaneous requests against a cap of 5 -> exactly 5 get through."""

    def test_concurrent_reservations_never_overshoot(self):
        results, barrier = [], threading.Barrier(12)
        lock = threading.Lock()

        def worker():
            from django.db import connections

            barrier.wait()
            try:
                quotas.reserve("burst", user_id=1)
                outcome = "ok"
            except QuotaExceeded as exc:
                outcome = exc.scope
            finally:
                connections.close_all()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(results), 12)
        used = AIQuotaCounter.objects.get(key=quotas.buckets_for("burst", user_id=1)[0].key).used
        self.assertEqual(results.count("ok"), used)  # every success was counted (no fail-open)
        self.assertEqual(used, 5)
        self.assertEqual(results.count("user_daily"), 7)


@override_settings(**AI, **UNLIMITED)
class EngineQuotaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("q-user", password="x")

    @override_settings(AI_QUOTA_USER_DAILY=1)
    def test_quota_is_checked_before_any_provider_call(self):
        with mock.patch(CREATE, return_value=reply({"ok": 1})):
            engine.run("cv_polish", system="s", data="cv", user=self.user)
        with mock.patch(CREATE) as create, self.assertRaises(QuotaExceeded):
            engine.run("cv_polish", system="s", data="another cv", user=self.user)
        create.assert_not_called()

    @override_settings(AI_QUOTA_USER_DAILY=1, AI_CACHE_TTL=3600,
                       CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "quota-cache"}})
    def test_cache_hits_are_free(self):
        with mock.patch(CREATE, return_value=reply({"ok": 1})) as create:
            for _ in range(3):
                self.assertEqual(engine.run("cv_polish", system="s", data="same cv", user=self.user), {"ok": 1})
        self.assertEqual(create.call_count, 1)
        self.assertEqual(AIUsage.objects.filter(cached=True).count(), 2)

    @override_settings(AI_QUOTA_USER_DAILY=1)
    def test_an_outage_gives_the_quota_back(self):
        with mock.patch(CREATE, side_effect=RuntimeError("down")), self.assertLogs("internest_ai", "WARNING"), \
                self.assertRaises(AIUnavailable):
            engine.run("cv_polish", system="s", data="cv", user=self.user)
        with mock.patch(CREATE, return_value=reply({"ok": 1})):
            self.assertEqual(engine.run("cv_polish", system="s", data="cv", user=self.user), {"ok": 1})

    def test_every_attempt_is_logged_in_ai_usage(self):
        with mock.patch(CREATE, return_value=reply({"ok": 1}, prompt_tokens=20, completion_tokens=4)):
            engine.run("cv_polish", system="s", data="cv", user=self.user)
        row = AIUsage.objects.get()
        self.assertEqual((row.user, row.task, row.provider, row.model, row.total_tokens, row.ok, row.cached),
                         (self.user, "cv_polish", "gemini", "gemini-2.5-flash", 24, True, False))
        with mock.patch(CREATE, side_effect=RuntimeError("down")), self.assertLogs("internest_ai", "WARNING"), \
                self.assertRaises(AIUnavailable):
            engine.run("cv_polish", system="s", data="cv 2", user=self.user)
        failed = AIUsage.objects.get(ok=False)
        self.assertIn("RuntimeError: down", failed.error)

    @override_settings(AI_QUOTA_USER_DAILY=1)
    def test_no_database_fails_open_but_lock_contention_fails_closed(self):
        from django.db import OperationalError

        with mock.patch.object(quotas, "_take", side_effect=OperationalError("no such table: internest_ai_aiquotacounter")):
            self.assertEqual(quotas.reserve("t", user_id=1).buckets, [])
        with mock.patch.object(quotas, "_take", side_effect=OperationalError("database is locked")), \
                mock.patch.object(quotas.time, "sleep"), self.assertRaises(QuotaExceeded) as ctx:
            quotas.reserve("t", user_id=1)
        self.assertEqual(ctx.exception.scope, "busy")
