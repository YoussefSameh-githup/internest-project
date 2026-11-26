"""Performance guarantees: query ceilings, no per-request writes, caching layers, server tuning.

Ceilings are measured on a warm request (the once-a-day session refresh already done). They are upper bounds:
a change that adds a query to one of these pages fails here and must justify itself."""
import gzip
import importlib
import os
import runpy
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse
from django.template import engines
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from internest_core import db_tuning, identity, page_cache
from internest_core.middleware import SlidingSessionMiddleware, TextGZipMiddleware
from internest_core.models import Application, Internship, PartnerProfile, StudentProfile
from internest_skills.tests import Base

# Warm-request ceilings (inside the test transaction, so they include the SAVEPOINT pairs db_safe adds in tests;
# production runs in autocommit and executes fewer statements). Before this work every page also paid a session
# SELECT plus a session UPDATE (3 statements) on every request.
QUERY_CEILINGS = {
    "landing (anonymous)": 0,
    "login page (anonymous)": 0,
    "opportunity feed (student)": 8,
    "student profile hub": 28,
    "student market-fit dashboard": 9,
    "my applications (student)": 10,
    "partner dashboard (startup)": 12,  # was 13: applicant counts are annotated, no applications prefetch
    "partner dashboard (free startup)": 15,  # measured 13; the journey strip adds up to 2 when no opportunity is open
    "partner dashboard (university)": 10,
    # Phase 8: pages added in Phases 1-7 (measured 2026-11-19)
    "talent pool (paid startup)": 11,
    "applicant page (startup)": 9,
    "skills hub (student)": 6,
    "assessment review queue (staff)": 4,
    "trust page (anonymous)": 0,
    "plans page (free startup)": 19,
    "delete account page (student)": 3,
}


class QueryCeilingTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()
        for i in range(12):
            Internship.objects.create(partner=self.pro if i % 2 else self.free, title=f"Role {i}", description="d", location="Cairo",
                                      required_majors="any", deadline=timezone.localdate() + timedelta(days=3 + i))
        for job in Internship.objects.all()[:6]:
            Application.objects.create(internship=job, applicant=self.student_user)
        self._claim()

    def _queries(self, user, url):
        self.client.logout()
        if user:
            self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 200)  # warm-up (session refresh, caches)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx.captured_queries)

    def test_ceilings(self):
        pages = {
            "landing (anonymous)": (None, reverse("landing")),
            "login page (anonymous)": (None, reverse("login")),
            "opportunity feed (student)": (self.student_user, reverse("list")),
            "student profile hub": (self.student_user, reverse("profile")),
            "student market-fit dashboard": (self.student_user, reverse("student_dashboard")),
            "my applications (student)": (self.student_user, reverse("my_applications")),
            "partner dashboard (startup)": (self.pro_user, reverse("partner_dashboard")),
            "partner dashboard (free startup)": (self.free_user, reverse("partner_dashboard")),
            "partner dashboard (university)": (self.uni_user, reverse("partner_dashboard")),
            "talent pool (paid startup)": (self.pro_user, reverse("talent_pool")),
            "applicant page (startup)": (self.pro_user, reverse(
                "partner_applicant", args=[Application.objects.filter(internship__partner=self.pro).first().pk])),
            "skills hub (student)": (self.student_user, reverse("skills_hub")),
            "assessment review queue (staff)": (User.objects.create_user("perf-staff", password="pw", is_staff=True),
                                                reverse("skills_review_queue")),
            "trust page (anonymous)": (None, reverse("trust")),
            "plans page (free startup)": (self.free_user, reverse("startup_upgrade")),
            "delete account page (student)": (self.student_user, reverse("account_delete")),
        }
        measured = {label: self._queries(user, url) for label, (user, url) in pages.items()}
        if os.getenv("PERF_REPORT"):
            print("\nQUERY REPORT", measured)
        for label, count in measured.items():
            with self.subTest(page=label):
                self.assertLessEqual(count, QUERY_CEILINGS[label], f"{label}: {count} queries > ceiling {QUERY_CEILINGS[label]}")

    def test_feed_does_not_grow_with_the_number_of_opportunities(self):
        first = self._queries(self.student_user, reverse("list"))
        for i in range(20):
            Internship.objects.create(partner=self.pro, title=f"More {i}", description="d", location="Cairo",
                                      required_majors="any", deadline=timezone.localdate() + timedelta(days=9))
        self.assertEqual(self._queries(self.student_user, reverse("list")), first)  # no N+1 per card

    def test_no_session_write_on_ordinary_requests(self):
        self.client.force_login(self.student_user)
        self.client.get(reverse("list"))
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("list"))
            self.client.get(reverse("profile"))
        writes = [q["sql"] for q in ctx.captured_queries if "django_session" in q["sql"]]
        self.assertEqual(writes, [])  # cached_db: the session is read from the cache and not re-saved

    def test_profile_lookups_happen_once_per_request(self):
        self.client.force_login(self.student_user)
        self.client.get(reverse("profile"))
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("profile"))
        sql = [q["sql"] for q in ctx.captured_queries]
        self.assertEqual(sum('FROM "internest_core_studentprofile"' in s and "COUNT" not in s for s in sql), 1)
        self.assertEqual(sum('FROM "internest_core_partnerprofile" WHERE "internest_core_partnerprofile"."user_id"' in s for s in sql), 1)


class IdentityMemoTests(Base):
    def test_a_missing_profile_is_remembered_too(self):
        user = type(self.student_user).objects.get(pk=self.student_user.pk)
        with CaptureQueriesContext(connection) as ctx:
            for _ in range(5):
                self.assertIsNone(identity.partner_profile(user))
                self.assertEqual(identity.student_profile(user), self.student)
        self.assertEqual(len(ctx.captured_queries), 2)

    def test_a_profile_created_mid_request_is_seen(self):
        user = type(self.student_user).objects.create_user("newbie", password="pw")
        self.assertIsNone(identity.student_profile(user))
        profile = StudentProfile.objects.create(user=user)
        self.assertEqual(identity.student_profile(user), profile)
        profile.delete()
        self.assertIsNone(identity.student_profile(user))

    def test_anonymous(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertIsNone(identity.partner_profile(AnonymousUser()))
        self.assertIsNone(identity.student_profile(None))


class SQLiteConnectionTests(Base):
    def test_pragmas_are_active_on_the_connection(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite only")
        with connection.cursor() as cursor:
            for pragma, expected in (("busy_timeout", 10000), ("cache_size", -64000), ("temp_store", 2), ("foreign_keys", 1)):
                cursor.execute(f"PRAGMA {pragma}")
                self.assertEqual(cursor.fetchone()[0], expected, pragma)


class SessionTests(Base):
    def test_cached_db_engine_and_no_save_every_request(self):
        self.assertEqual(settings.SESSION_ENGINE, "django.contrib.sessions.backends.cached_db")
        self.assertFalse(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_login_still_slides_once_a_day(self):
        self.client.force_login(self.student_user)
        first = self.client.get(reverse("list"))
        self.assertIn(settings.SESSION_COOKIE_NAME, first.cookies)  # refreshed once after login
        second = self.client.get(reverse("list"))
        self.assertNotIn(settings.SESSION_COOKIE_NAME, second.cookies)  # not again the same day
        session = self.client.session
        session[SlidingSessionMiddleware.KEY] = 0  # a day later
        session.save()
        third = self.client.get(reverse("list"))
        self.assertIn(settings.SESSION_COOKIE_NAME, third.cookies)
        self.assertEqual(third.cookies[settings.SESSION_COOKIE_NAME]["max-age"], settings.SESSION_COOKIE_AGE)

    def test_anonymous_visitors_get_no_session(self):
        self.assertNotIn(settings.SESSION_COOKIE_NAME, self.client.get(reverse("landing")).cookies)


class SQLiteTuningTests(SimpleTestCase):
    def test_wal_only_on_local_disks(self):
        wal = db_tuning.sqlite_pragmas(wal=True)
        self.assertEqual(wal[:3], ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA mmap_size=268435456"])
        rollback = db_tuning.sqlite_pragmas(wal=False)
        self.assertFalse(any("WAL" in p or "mmap" in p or "synchronous" in p for p in rollback))
        for both in (wal, rollback):
            self.assertIn("PRAGMA busy_timeout=10000", both)

    def test_settings_choose_wal_by_platform(self):
        self.assertEqual(settings.DATABASES["default"]["OPTIONS"]["init_command"].count("journal_mode=WAL"), int(settings.SQLITE_WAL))

    def test_transactions_take_the_write_lock_up_front(self):
        # DEFERRED transactions fail at once with "database is locked" when they upgrade to a writer after another
        # process committed (WAL); IMMEDIATE waits on busy_timeout instead (found by the load benchmark).
        self.assertEqual(db_tuning.sqlite_options(True)["transaction_mode"], "IMMEDIATE")
        self.assertEqual(db_tuning.sqlite_options(False)["transaction_mode"], "IMMEDIATE")


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "page-tests"}})
class PageCacheTests(Base):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_landing_is_served_from_cache_with_a_fresh_csrf_token_per_visitor(self):
        first = self.client.get(reverse("landing"))
        self.assertEqual(first["X-Page-Cache"], "MISS")
        self.client.cookies.clear()
        self.client.cookies["internest_lang"] = "en"
        second = self.client.get(reverse("landing"))
        self.assertEqual(second["X-Page-Cache"], "HIT")
        html = second.content.decode()
        self.assertNotIn(page_cache.CSRF_PLACEHOLDER.decode(), html)
        token = html.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
        self.assertIn("csrftoken", second.cookies)  # the visitor gets their own cookie
        resp = self.client.post(reverse("set_language"), {"csrfmiddlewaretoken": token, "language": "en", "next": "/"})
        self.assertEqual(resp.status_code, 302)  # the swapped token is valid

    def test_cache_is_per_language(self):
        self.client.get(reverse("landing"))
        self.client.cookies["internest_lang"] = "ar"
        resp = self.client.get(reverse("landing"))
        self.assertEqual(resp["X-Page-Cache"], "MISS")
        self.assertIn('dir="rtl"', resp.content.decode())

    def test_signed_in_users_and_flash_messages_bypass_the_cache(self):
        self.client.get(reverse("landing"))
        self.client.force_login(self.student_user)
        self.assertEqual(self.client.get(reverse("landing")).status_code, 302)  # workspace redirect, never a cached page
        self.client.logout()
        self.client.cookies["messages"] = "x"
        self.assertNotIn("X-Page-Cache", self.client.get(reverse("landing")))

    def test_group_bump_invalidates(self):
        calls = []
        value = lambda: calls.append(1) or len(calls)  # noqa: E731
        self.assertEqual(page_cache.cached_value("g", "n", 60, value), 1)
        self.assertEqual(page_cache.cached_value("g", "n", 60, value), 1)
        page_cache.bump("g")
        self.assertEqual(page_cache.cached_value("g", "n", 60, value), 2)

    def test_campus_leaderboard_is_cached_and_invalidated_by_a_vote(self):
        from internest_campus.models import CampusDemandVote

        self.client.get(reverse("campus_vote"))
        with CaptureQueriesContext(connection) as warm:
            self.client.get(reverse("campus_vote"))
        self.assertFalse(any("GROUP BY" in q["sql"] for q in warm.captured_queries))  # aggregate served from cache
        CampusDemandVote.objects.create(student=self.student, university_name="Cairo University", faculty_name="Commerce", department="Finance")
        resp = self.client.get(reverse("campus_vote"))
        self.assertIn("Cairo University", [row["university"] for row in resp.context["leaderboard"]])


class HTTPTuningTests(SimpleTestCase):
    def _gzip(self, content_type, body=b"x" * 2000):
        request = RequestFactory().get("/", HTTP_ACCEPT_ENCODING="gzip")
        return TextGZipMiddleware(lambda r: HttpResponse(body, content_type=content_type))(request)

    def test_html_and_json_are_compressed(self):
        for content_type in ("text/html; charset=utf-8", "application/json"):
            response = self._gzip(content_type)
            self.assertEqual(response["Content-Encoding"], "gzip")
            self.assertEqual(gzip.decompress(response.content), b"x" * 2000)

    def test_binary_files_are_not_recompressed(self):
        for content_type in ("application/pdf", "image/png", "audio/webm"):
            self.assertFalse(self._gzip(content_type).has_header("Content-Encoding"))

    def test_middleware_order(self):
        mw = settings.MIDDLEWARE
        self.assertLess(mw.index("whitenoise.middleware.WhiteNoiseMiddleware"), mw.index("internest_core.middleware.TextGZipMiddleware"))
        self.assertLess(mw.index("internest_core.middleware.TextGZipMiddleware"), mw.index("django.middleware.http.ConditionalGetMiddleware"))
        self.assertLess(mw.index("django.middleware.http.ConditionalGetMiddleware"), mw.index("django.contrib.sessions.middleware.SessionMiddleware"))

    def test_templates_use_the_cached_loader(self):
        loaders = engines["django"].engine.template_loaders
        self.assertEqual(type(loaders[0]).__module__, "django.template.loaders.cached")

    def test_static_settings(self):
        self.assertFalse(settings.WHITENOISE_MANIFEST_STRICT)
        self.assertEqual(settings.WHITENOISE_MAX_AGE, 31536000)
        self.assertNotIn("STATICFILES_STORAGE", dir(settings._wrapped))  # removed in Django 5.1: STORAGES only

    def test_asgi_entry_point_uses_the_real_settings_module(self):
        module = importlib.import_module("internest_app_project.asgi")
        self.assertTrue(callable(module.application))
        with open(module.__file__, encoding="utf-8") as fh:
            self.assertIn("'internest_app_project.settings'", fh.read())

    def test_gunicorn_config(self):
        config = runpy.run_path(os.path.join(settings.BASE_DIR, "gunicorn.conf.py"))
        self.assertEqual(config["worker_class"], "uvicorn.workers.UvicornWorker")
        self.assertGreaterEqual(config["workers"], 3)
        self.assertEqual((config["max_requests"], config["max_requests_jitter"]), (2000, 200))
        self.assertTrue(callable(config["post_fork"]))
        if "GUNICORN_BIND" not in os.environ:
            self.assertEqual(config["bind"], "127.0.0.1:8000")  # loopback by default: only the proxy is public (L8)


class ConditionalGetTests(Base):
    def test_unchanged_page_answers_304(self):
        # HTML pages carry a per-visitor CSRF token (language switcher), so they are never byte-identical;
        # stable responses (robots, sitemap, JSON) get 304 Not Modified.
        first = self.client.get(reverse("robots"))
        etag = first["ETag"]
        again = self.client.get(reverse("robots"), HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")


class FrontEndTests(Base):
    def test_lists_lazy_load_logos_with_fixed_size(self):
        PartnerProfile.objects.filter(pk=self.pro.pk).update(logo="logos/pro.png")
        Internship.objects.create(partner=self.pro, title="Role", description="d", location="Cairo", required_majors="any",
                                  deadline=timezone.localdate() + timedelta(days=4))
        self.client.force_login(self.student_user)
        html = self.client.get(reverse("list")).content.decode()
        self.assertIn('width="48" height="48" loading="lazy" decoding="async"', html)

    def test_scripts_are_deferred_and_ui_libraries_are_first_party(self):
        html = self.client.get(reverse("landing")).content.decode()
        self.assertRegex(html, r'bootstrap\.bundle\.min(\.[0-9a-f]+)?\.js" defer')
        self.assertIn('async_task_poller', html)
        self.assertRegex(html, r'vendor/fonts/fonts-(rtl|ltr)')
        self.assertRegex(html, r'rel="preload" href="/static/vendor/fonts/(cairo-arabic|inter-latin)-wght-normal(\.[0-9a-f]+)?\.woff2" as="font"')
        for third_party in ("cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"):
            self.assertNotIn(third_party, html)  # UI libraries and fonts come from /static/vendor/: nothing else before first paint


@override_settings(AI_CACHE_TTL=3600, ASYNC_TASK_BACKEND="sync", GEMINI_API_KEY="g-key",
                   GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/", AGENTROUTER_API_KEY="",
                   LLM_BACKUP_API_KEY="", AZURE_OPENAI_API_KEY="", AZURE_OPENAI_ENDPOINT="")
class AIPipelineTests(Base):
    """End-to-end: a repeated CV polish costs one outbound API call; the second answer comes from the cache."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.client.force_login(self.student_user)

    def test_repeated_async_polish_hits_the_api_once(self):
        import json

        reply = mock.Mock(choices=[mock.Mock(message=mock.Mock(content=json.dumps({"name": "Polished", "summary": "ok"})), finish_reason="stop")],
                          usage=mock.Mock(total_tokens=10, prompt_tokens=5, completion_tokens=5))
        body = json.dumps({"cv": {"name": "Raw", "summary": "same text"}})
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply) as create:
            first = self.client.post(reverse("cv_builder_polish"), data=body, content_type="application/json", HTTP_PREFER="respond-async")
            second = self.client.post(reverse("cv_builder_polish"), data=body, content_type="application/json", HTTP_PREFER="respond-async")
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(first.json()["result"], second.json()["result"])
        self.assertEqual(create.call_count, 1)
