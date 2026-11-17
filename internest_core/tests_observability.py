"""Sentry: off without a DSN, privacy-safe events, quiet sampling for noise endpoints."""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from internest_core import observability

FAKE_DSN = "https://public@o0.ingest.sentry.io/0"


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
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertIs(options["before_send"], observability.before_send)
        self.assertEqual((options["environment"], options["release"]), ("production", "abc123"))
        names = {type(i).__name__ for i in options["integrations"]}
        self.assertEqual(names, {"DjangoIntegration", "LoggingIntegration"})


class ScrubbingTests(SimpleTestCase):
    def test_request_bodies_cookies_and_auth_headers_are_removed(self):
        event = {"request": {"data": {"cv": "my whole CV", "password": "hunter2"}, "cookies": {"internest_sessionid": "abc"},
                             "headers": {"Authorization": "Bearer sk-livekey123456789", "Cookie": "a=b", "X-CSRFToken": "t",
                                         "User-Agent": "Firefox"},
                             "query_string": "token=supersecret&page=2"}}
        out = observability.before_send(event, {})
        request = out["request"]
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
        out = observability.before_send(event, {})
        dumped = str(out)
        for secret in ("abcdefghijklmnop1234", "AIzaSyABCDEFGH12345", "sendgridkey12345678", "letmein"):
            self.assertNotIn(secret, dumped)
        self.assertIn("sk-***", out["exception"]["values"][0]["value"])

    def test_event_without_request_is_fine(self):
        self.assertEqual(observability.before_send({"message": "hello"}, {}), {"message": "hello"})


class SamplingTests(SimpleTestCase):
    def test_noise_endpoints_are_never_traced(self):
        sampler = observability.make_traces_sampler(0.25)
        for path in ("/healthz/", "/static/css/styles.css", "/media/logos/x.png", "/api/tasks/123/status/"):
            self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": path}}), 0.0, path)
        self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": "/list/"}}), 0.25)
        self.assertEqual(sampler({"asgi_scope": {"path": "/profile/"}}), 0.25)
        self.assertEqual(sampler({"wsgi_environ": {"PATH_INFO": "/list/"}, "parent_sampled": True}), 1.0)  # follow the caller


class CommandTests(SimpleTestCase):
    def test_refuses_when_sentry_is_off(self):
        with override_settings(SENTRY_DSN=""), self.assertRaises(CommandError):
            call_command("sentry_test", stdout=StringIO())

    def test_sends_and_flushes(self):
        client = mock.Mock(is_active=mock.Mock(return_value=True))
        out = StringIO()
        with override_settings(SENTRY_DSN=FAKE_DSN), mock.patch("sentry_sdk.get_client", return_value=client), \
                mock.patch("sentry_sdk.capture_message", return_value="evt123") as capture, mock.patch("sentry_sdk.flush") as flush:
            call_command("sentry_test", stdout=out)
        capture.assert_called_once()
        flush.assert_called_once()
        self.assertIn("evt123", out.getvalue())
