"""Sentry: off without a DSN, privacy-safe events, delivery that works without threads (PythonAnywhere), monitored."""
import gzip
import json
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from internest_core import api_registry, observability

FAKE_DSN = "https://public@o0.ingest.sentry.io/0"
# Defined far from the code that raises, so they cannot appear in the source lines Sentry shows around an error.
PRIVATE_EMAIL = "mona.ali" + "@student.edu.eg"
PRIVATE_CODE = str(700000 + 31904)


class FakeSentry:
    """A local ingest endpoint: records every envelope, answers with `status`."""

    def __init__(self, status=200):
        self.envelopes, self.status = [], status
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                encoding = self.headers.get("Content-Encoding", "")
                if encoding == "gzip":
                    body = gzip.decompress(body)
                elif encoding == "deflate":
                    body = zlib.decompress(body)
                elif encoding == "br":
                    import brotli
                    body = brotli.decompress(body)
                fake.envelopes.append(body.decode("utf-8", "replace"))
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"ok"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def dsn(self):
        return f"http://public@127.0.0.1:{self.server.server_address[1]}/1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def events(self):
        """The JSON items of every envelope (headers skipped)."""
        items = []
        for envelope in self.envelopes:
            for line in envelope.splitlines():
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and ("exception" in data or "message" in data or "logentry" in data):
                    items.append(data)
        return items


class LiveClientMixin:
    """Starts the real SDK against a FakeSentry, and switches it off again afterwards."""

    def start(self, dsn, sync=True):
        import sentry_sdk

        self._previous_client = sentry_sdk.get_global_scope().client
        observability.init_sentry(dsn, environment="test", release="abc123", traces_sample_rate=0.0, sync_transport=sync)
        transport = type(__import__("sentry_sdk").get_client().transport)
        transport.last_status = None
        if hasattr(transport, "_down_until"):
            transport._down_until = 0.0

    def tearDown(self):
        import sentry_sdk

        previous = getattr(self, "_previous_client", None)
        if previous is not None:  # back to the inactive client the rest of the test run uses
            sentry_sdk.get_client().close(timeout=0)
            sentry_sdk.get_global_scope().set_client(previous)
        super().tearDown()


class InitTests(SimpleTestCase):
    def test_off_without_dsn_and_in_tests(self):
        import sentry_sdk

        self.assertFalse(observability.init_sentry("", environment="test"))
        self.assertFalse(sentry_sdk.get_client().is_active())  # the test runner never reports to Sentry

    def test_privacy_options(self):
        with mock.patch("sentry_sdk.init") as init:
            self.assertTrue(observability.init_sentry(FAKE_DSN, environment="production", release="abc123", traces_sample_rate=0.2))
        options = init.call_args.kwargs
        self.assertFalse(options["send_default_pii"])
        self.assertFalse(options["include_local_variables"])  # frames carry no values: no emails, codes or CV text
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertIs(options["before_send"], observability.scrub)
        self.assertIs(options["before_send_transaction"], observability.scrub)
        self.assertEqual((options["environment"], options["release"]), ("production", "abc123"))
        names = {type(i).__name__ for i in options["integrations"]}
        self.assertEqual(names, {"DjangoIntegration", "LoggingIntegration"})
        self.assertEqual(options["transport"].__name__, "MonitoredTransport")
        from sentry_sdk.integrations.logging import _IGNORED_LOGGERS
        self.assertIn("django.security.DisallowedHost", _IGNORED_LOGGERS)  # bots probing other Host headers: noise

    def test_sync_mode_starts_no_background_threads(self):
        with mock.patch("sentry_sdk.init") as init:
            observability.init_sentry(FAKE_DSN, environment="production", profiles_sample_rate=0.5, sync_transport=True)
        options = init.call_args.kwargs
        self.assertEqual(options["transport"].__name__, "SyncMonitoredTransport")
        self.assertEqual(options["profiles_sample_rate"], 0.0)
        self.assertFalse(options["auto_session_tracking"])
        self.assertFalse(options["enable_backpressure_handling"])

    def test_sentry_is_a_registered_monitored_api(self):
        spec = api_registry.spec_for("sentry")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.env_var, "SENTRY_DSN")

    def test_release_comes_from_the_deployed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = Path(tmp) / ".git"
            (git / "refs" / "heads").mkdir(parents=True)
            (git / "HEAD").write_text("ref: refs/heads/main\n")
            (git / "refs" / "heads" / "main").write_text("6b77127aabbccddeeff00112233445566778899\n")
            self.assertEqual(observability.detect_release(tmp), "6b77127aabbc")
            (git / "refs" / "heads" / "main").unlink()  # after `git gc` the ref only lives in packed-refs
            (git / "packed-refs").write_text("# pack-refs\nc7855cb00112233445566778899aabbccddeeff refs/heads/main\n")
            self.assertEqual(observability.detect_release(tmp), "c7855cb00112")
            (git / "HEAD").write_text("deadbeefcafe0011223344556677889900aabbcc\n")  # detached HEAD
            self.assertEqual(observability.detect_release(tmp), "deadbeefcafe")
        self.assertEqual(observability.detect_release("/no/such/dir"), "")


class ScrubbingTests(SimpleTestCase):
    def test_request_bodies_cookies_and_auth_headers_are_removed(self):
        event = {"request": {"data": {"cv": "my whole CV", "password": "hunter2"}, "cookies": {"internest_sessionid": "abc"},
                             "headers": {"Authorization": "Bearer sk-livekey123456789", "Cookie": "a=b", "X-CSRFToken": "t",
                                         "User-Agent": "Firefox"},
                             "query_string": "token=supersecret&page=2"}}
        request = observability.scrub(event, {})["request"]
        self.assertNotIn("data", request)
        self.assertNotIn("cookies", request)
        self.assertEqual(request["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(request["headers"]["Cookie"], "[Filtered]")
        self.assertEqual(request["headers"]["X-CSRFToken"], "[Filtered]")
        self.assertEqual(request["headers"]["User-Agent"], "Firefox")
        self.assertEqual(request["query_string"], "token=***&page=2")

    def test_secrets_in_exception_messages_and_logs_are_masked(self):
        event = {"exception": {"values": [{"type": "AuthenticationError", "value": "Incorrect API key sk-abcdefghijklmnop1234"}]},
                 "logentry": {"message": "Gemini key AIzaSyABCDEFGH12345 rejected", "formatted": "SG.sendgridkey12345678 failed"},
                 "message": "password=letmein"}
        out = observability.scrub(event, {})
        dumped = str(out)
        for secret in ("abcdefghijklmnop1234", "AIzaSyABCDEFGH12345", "sendgridkey12345678", "letmein"):
            self.assertNotIn(secret, dumped)
        self.assertIn("sk-***", out["exception"]["values"][0]["value"])

    def test_personal_data_is_masked_everywhere(self):
        token = "1c3f1d0e-8a5b-4c2d-9e7f-0a1b2c3d4e5f"
        event = {
            "event_id": "0123456789abcdef0123456789abcdef",
            "contexts": {"trace": {"trace_id": "aaaabbbbccccddddeeeeffff00001111", "span_id": "1111222233334444"}},
            "request": {"url": f"https://internest.app/app/skills/challenge/{token}/", "query_string": "email=sara%40uni.edu&code=482913"},
            "exception": {"values": [{"type": "IntegrityError", "value": "UNIQUE failed for sara.ahmed@uni.edu.eg",
                                      "stacktrace": {"frames": [{"function": "verify", "vars": {"code": "482913", "cv_text": "..."}}]}}]},
            "breadcrumbs": {"values": [{"message": "Sent reset code to omar@example.com", "data": {"to": ["omar@example.com"]}},
                                       {"message": "GET /accounts/reset/MTI/abc-123def/ 302"}]},
            "spans": [{"description": f"GET /verify/{token}/letter.pdf"}],
        }
        out = observability.scrub(event)
        dumped = json.dumps(out)
        for private in ("sara.ahmed", "omar@example.com", "sara%40uni.edu", "482913", token, "abc-123def", "cv_text"):
            self.assertNotIn(private, dumped)
        self.assertIn("[email]", dumped)
        self.assertIn("/app/skills/challenge/[uuid]/", out["request"]["url"])
        self.assertIn("/accounts/reset/[token]/[token]/", out["breadcrumbs"]["values"][1]["message"])
        self.assertNotIn("vars", out["exception"]["values"][0]["stacktrace"]["frames"][0])
        self.assertEqual(out["event_id"], event["event_id"])  # ids Sentry needs stay intact
        self.assertEqual(out["contexts"]["trace"]["trace_id"], "aaaabbbbccccddddeeeeffff00001111")

    def test_event_without_request_is_fine(self):
        self.assertEqual(observability.scrub({"message": "hello"}, {}), {"message": "hello"})


class SamplingTests(SimpleTestCase):
    def test_noise_endpoints_are_never_traced(self):
        sampler = observability.make_traces_sampler(0.25)
        for path in ("/healthz/", "/static/css/styles.css", "/media/logos/x.png", "/api/tasks/123/status/"):
            self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": path}}), 0.0, path)
        self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": "/list/"}}), 0.25)
        self.assertEqual(sampler({"asgi_scope": {"path": "/profile/"}}), 0.25)
        self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": "/list/"}, "parent_sampled": True}), 1.0)  # follow the caller


class DeliveryTests(LiveClientMixin, SimpleTestCase):
    """The real SDK, sending to a local fake Sentry."""

    def test_sync_delivery_is_confirmed_private_and_threadless(self):
        import sentry_sdk

        with FakeSentry() as fake:
            self.start(fake.dsn, sync=True)
            threads_before = threading.active_count()

            def verify_code(email, code):
                raise ValueError(f"no live code for {email}")

            try:
                verify_code(PRIVATE_EMAIL, PRIVATE_CODE)
            except ValueError as exc:
                event_id = sentry_sdk.capture_exception(exc)
            self.assertEqual(threading.active_count(), threads_before)  # uWSGI without threads: nothing to wait for
            self.assertEqual(observability.last_delivery_status(), 200)
            self.assertEqual(observability.transport_name(), "synchronous")
            [event] = fake.events()
        self.assertEqual(event["event_id"], event_id)
        dumped = json.dumps(event)
        self.assertNotIn("mona.ali", dumped)
        self.assertNotIn(PRIVATE_CODE, dumped)  # the local variable never left
        self.assertIn("[email]", event["exception"]["values"][0]["value"])
        self.assertEqual((event["environment"], event["release"]), ("test", "abc123"))

    def test_rejected_dsn_alerts_the_admin_through_the_api_monitor(self):
        import sentry_sdk

        with FakeSentry(status=401) as fake, mock.patch("internest_core.api_monitor.report_failure") as report:
            self.start(fake.dsn, sync=True)
            sentry_sdk.capture_message("hello")
            sentry_sdk.capture_message("again")  # the rejected DSN is not retried on every request
            self.assertEqual(len(fake.envelopes), 1)
        self.assertEqual(observability.last_delivery_status(), "error: DeliveryError")
        report.assert_called_once()
        self.assertEqual(report.call_args.args[:2], ("sentry", "Error reporting"))
        self.assertEqual(report.call_args.args[2].status_code, 401)

    def test_unreachable_sentry_is_skipped_for_a_minute(self):
        import socket

        import sentry_sdk

        with socket.socket() as sock:  # a port nobody listens on
            sock.bind(("127.0.0.1", 0))
            dead_dsn = f"http://public@127.0.0.1:{sock.getsockname()[1]}/1"
        with mock.patch("internest_core.api_monitor.report_failure") as report:
            self.start(dead_dsn, sync=True)
            transport = sentry_sdk.get_client().transport
            with mock.patch.object(type(transport), "_send_envelope", wraps=transport._send_envelope) as send:
                started = time.monotonic()
                sentry_sdk.capture_message("first")   # fails fast (no retries), opens the breaker
                sentry_sdk.capture_message("second")  # not even attempted
                sentry_sdk.capture_message("third")
                self.assertLess(time.monotonic() - started, observability.SYNC_TIMEOUT_SECONDS + 1)  # one attempt only
            self.assertEqual(send.call_count, 1)
        report.assert_called_once()
        self.assertTrue(str(observability.last_delivery_status()).startswith("error:"))

    def test_a_hanging_sentry_never_holds_a_request_longer_than_the_timeout(self):
        import socket

        import sentry_sdk

        with socket.socket() as sock, mock.patch("internest_core.api_monitor.report_failure") as report:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)  # accepts the connection, never answers
            self.start(f"http://public@127.0.0.1:{sock.getsockname()[1]}/1", sync=True)
            started = time.monotonic()
            sentry_sdk.capture_message("slow")
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, observability.SYNC_TIMEOUT_SECONDS + 1.5)
        report.assert_called_once()

    def test_background_transport_still_delivers_elsewhere(self):
        import sentry_sdk

        with FakeSentry() as fake:
            self.start(fake.dsn, sync=False)
            sentry_sdk.capture_message("from a threaded server")
            sentry_sdk.flush(timeout=5)
            self.assertEqual(observability.transport_name(), "background thread")
            self.assertEqual(len(fake.events()), 1)


class CommandTests(LiveClientMixin, SimpleTestCase):
    def test_refuses_when_sentry_is_off(self):
        with override_settings(SENTRY_DSN=""), self.assertRaises(CommandError):
            call_command("sentry_test", stdout=StringIO())

    def test_reports_a_confirmed_delivery(self):
        out = StringIO()
        with FakeSentry() as fake, override_settings(SENTRY_DSN=FAKE_DSN):
            self.start(fake.dsn, sync=True)
            call_command("sentry_test", stdout=out)
            [event] = fake.events()
        self.assertIn("Delivered.", out.getvalue())
        self.assertIn("transport: synchronous", out.getvalue())
        self.assertEqual(event["exception"]["values"][0]["type"], "SentryCheckError")

    def test_fails_loudly_when_sentry_refuses(self):
        with FakeSentry(status=403) as fake, override_settings(SENTRY_DSN=FAKE_DSN), \
                mock.patch("internest_core.api_monitor.report_failure"):
            self.start(fake.dsn, sync=True)
            with self.assertRaisesMessage(CommandError, "did not accept"):
                call_command("sentry_test", stdout=StringIO())


class OpsCheckTests(TestCase):
    def test_only_superusers_can_send_a_check(self):
        User = get_user_model()
        url = reverse("sentry_check")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(User.objects.create_user("staffer", password="x", is_staff=True))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(User.objects.create_superuser("root", "root@example.com", "x"))
        with mock.patch("internest_core.observability.send_check", return_value={"active": True, "delivery": 200}) as check:
            response = self.client.get(url)
        self.assertEqual(response.json(), {"active": True, "delivery": 200})
        check.assert_called_once_with("/ops/sentry-check/")
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertEqual(self.client.post(url).status_code, 405)
