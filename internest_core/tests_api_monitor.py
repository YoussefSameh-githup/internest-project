"""Admin API health + low-balance alerts: classification, cooldown, admin-only recipient, wiring into the real integrations."""
import json
from io import StringIO
from unittest import mock

import httpx
import openai
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from internest_core import api_monitor as mon

ADMIN = "admin-alerts@example.com"


def _http_error(code, body="", cls=None):
    resp = httpx.Response(code, text=body, request=httpx.Request("POST", "https://api.example.test/v1"))
    cls = cls or {401: openai.AuthenticationError, 429: openai.RateLimitError}.get(code, openai.APIStatusError)
    return cls(body or f"HTTP {code}", response=resp, body=None)


@override_settings(ADMIN_ALERT_EMAIL=ADMIN, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", API_ALERT_COOLDOWN_SECONDS=1800,
                   API_LOW_BALANCE_THRESHOLD=2.0, DEFAULT_FROM_EMAIL="Internest <noreply@example.com>")
class MonitorBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def trigger(self, exc, provider="Google Gemini", feature="Question Generation"):
        def boom():
            raise exc

        with self.assertRaises(type(exc)):
            mon.monitored_api_call(provider, feature, boom)


class ClassificationTests(MonitorBase):
    def test_each_failure_kind(self):
        cases = [
            (_http_error(401, "bad key"), mon.AUTH, 401), (_http_error(403, "forbidden"), mon.AUTH, 403),
            (_http_error(500, "boom"), mon.SERVER, 500), (_http_error(503, "down"), mon.SERVER, 503),
            (_http_error(404, "no model"), mon.HTTP_ERROR, 404),
            (httpx.ReadTimeout("slow"), mon.TIMEOUT, None), (openai.APITimeoutError(request=httpx.Request("POST", "https://x")), mon.TIMEOUT, None),
            (httpx.ConnectError("refused"), mon.CONNECTION, None), (ConnectionResetError("reset"), mon.CONNECTION, None),
            (KeyError("choices"), mon.UNEXPECTED, None),
        ]
        for exc, kind, status in cases:
            failure = mon.classify(exc)
            self.assertEqual((failure.alert_type, failure.status), (kind, status), repr(exc))

    def test_quota_exhaustion_signals(self):
        for exc in (_http_error(402, "Payment required"), _http_error(429, '{"error":{"code":"insufficient_quota"}}'),
                    _http_error(429, "You exceeded your current quota, check your plan and billing details"),
                    _http_error(429, "Your credit balance is too low"), _http_error(403, "quota exceeded for this key")):
            self.assertEqual(mon.classify(exc).alert_type, mon.QUOTA, repr(exc))

    def test_plain_rate_limit_is_a_429_alert_but_not_urgent(self):
        failure = mon.classify(_http_error(429, "Rate limit reached for requests per minute. Please retry."))
        self.assertEqual(failure.alert_type, mon.RATE_LIMITED)
        self.trigger(_http_error(429, "Rate limit reached for requests per minute"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Rate limit hit (429)", mail.outbox[0].subject)
        self.assertNotIn("URGENT", mail.outbox[0].subject)

    def test_secrets_are_masked_in_the_alert_text(self):
        failure = mon.classify(_http_error(401, "Incorrect API key sk-abcdefghijklmnop1234 and ?key=AIzaSyABCDEFGH12345"))
        self.assertNotIn("abcdefghijklmnop1234", failure.detail)
        self.assertNotIn("AIzaSyABCDEFGH12345", failure.detail)
        self.assertIn("sk-***", failure.detail)


class AlertEmailTests(MonitorBase):
    def test_failure_alert_has_provider_feature_status_details_and_timestamp(self):
        self.trigger(_http_error(500, "upstream exploded"), provider="Google Gemini", feature="CV Analysis")
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("Google Gemini", msg.subject)
        self.assertIn("CV Analysis", msg.subject)
        for expected in ("Provider     : Google Gemini (AI Generation)", "Feature      : CV Analysis", "HTTP status  : 500", "upstream exploded",
                         "Env variable : GEMINI_API_KEY", "Timestamp    :", "UTC"):
            self.assertIn(expected, msg.body)

    def test_timeout_alert_says_there_was_no_response(self):
        self.trigger(httpx.ReadTimeout("no answer"), provider="AgentRouter", feature="Question Generation")
        self.assertIn("n/a (no response)", mail.outbox[0].body)
        self.assertIn("Request timed out", mail.outbox[0].subject)

    def test_quota_alert_is_urgent_and_separate(self):
        self.trigger(_http_error(429, "insufficient_quota: You exceeded your current quota"), provider="Azure OpenAI", feature="CV Analysis")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("URGENT: Low Balance / Quota Exhausted", mail.outbox[0].subject)
        self.assertIn("top up", mail.outbox[0].body)

    def test_402_is_urgent_too(self):
        self.trigger(_http_error(402, "Payment Required"))
        self.assertIn("URGENT", mail.outbox[0].subject)

    def test_original_exception_and_result_are_untouched(self):
        self.assertEqual(mon.monitored_api_call("P", "F", lambda a, b=0: a + b, 2, b=3), 5)
        sentinel = _http_error(500, "x")
        with self.assertRaises(openai.APIStatusError) as caught:
            mon.monitored_api_call("P", "F", mock.Mock(side_effect=sentinel))
        self.assertIs(caught.exception, sentinel)

    def test_error_status_in_a_returned_response_alerts_and_returns_it(self):
        resp = httpx.Response(401, text="Unauthorized", request=httpx.Request("POST", "https://pay.example.test"))
        self.assertIs(mon.monitored_api_call("Fawaterak", "Payment invoice link", lambda: resp), resp)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("HTTP status  : 401", mail.outbox[0].body)
        ok = httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://pay.example.test"))
        self.assertIs(mon.monitored_api_call("Fawaterak", "x", lambda: ok), ok)
        self.assertEqual(len(mail.outbox), 1)

    def test_decorator_client_and_feature_context(self):
        @mon.monitored("Decorated", "Decorated feature")
        def call():
            raise httpx.ConnectError("nope")

        with self.assertRaises(httpx.ConnectError):
            call()

        class Client(mon.BaseMonitoredAPIClient):
            provider_name = "Future API"

        with self.assertRaises(KeyError):
            Client().call("Future feature", mock.Mock(side_effect=KeyError("x")))
        with mon.api_feature("Labelled by context"), self.assertRaises(httpx.ConnectError):
            mon.monitored_api_call("Ctx API", "", mock.Mock(side_effect=httpx.ConnectError("x")))
        subjects = " | ".join(m.subject for m in mail.outbox)
        for expected in ("Decorated · Decorated feature", "Future API · Future feature", "Ctx API · Labelled by context"):
            self.assertIn(expected, subjects)


class CooldownTests(MonitorBase):
    def test_a_spike_sends_one_email_per_provider_and_type(self):
        for _ in range(25):
            self.trigger(_http_error(500, "boom"))
        self.assertEqual(len(mail.outbox), 1)

    def test_other_provider_or_type_is_not_suppressed(self):
        self.trigger(_http_error(500, "boom"), provider="Google Gemini")
        self.trigger(_http_error(500, "boom"), provider="AgentRouter")
        self.trigger(_http_error(401, "bad key"), provider="Google Gemini")
        self.trigger(_http_error(402, "pay"), provider="Google Gemini")
        self.assertEqual(len(mail.outbox), 4)

    def test_uses_cache_add_with_the_thirty_minute_window(self):
        with mock.patch.object(mon.cache, "add", return_value=True) as add:
            self.trigger(_http_error(500, "boom"), provider="Google Gemini")
        key, _value, seconds = add.call_args.args
        self.assertEqual((key, seconds), ("api-alert:gemini:server", 1800))

    def test_an_email_is_allowed_again_after_the_window(self):
        from datetime import timedelta

        from django.utils import timezone

        from internest_core.models import APIIncident

        self.trigger(_http_error(500, "boom"))
        cache.clear()
        APIIncident.objects.update(created_at=timezone.now() - timedelta(minutes=31))  # the 30 minutes have passed
        self.trigger(_http_error(500, "boom"))
        self.assertEqual(len(mail.outbox), 2)

    def test_the_cooldown_also_holds_across_workers(self):
        self.trigger(_http_error(500, "boom"))
        cache.clear()  # another worker process has its own empty cache; the incident table still remembers the email
        self.trigger(_http_error(500, "boom"))
        self.assertEqual(len(mail.outbox), 1)


class RecipientAndSafetyTests(MonitorBase):
    def test_only_the_admin_address_is_ever_emailed(self):
        self.trigger(_http_error(500, "contact student@uni.edu or company@startup.io"), provider="P1")
        self.trigger(_http_error(402, "pay"), provider="P2")
        for msg in mail.outbox:
            self.assertEqual((msg.to, msg.cc, msg.bcc), ([ADMIN], [], []))
        self.assertEqual(len(mail.outbox), 2)

    def test_nothing_is_sent_when_no_admin_is_configured(self):
        with override_settings(ADMIN_ALERT_EMAIL=""):
            self.trigger(_http_error(500, "boom"))
        self.assertEqual(mail.outbox, [])

    def test_the_admin_address_is_not_written_to_the_log(self):
        with self.assertLogs("internest_core.api_monitor", "WARNING") as logs:
            self.trigger(_http_error(500, "boom"))
        self.assertNotIn(ADMIN, "\n".join(logs.output))

    def test_a_broken_mail_server_never_breaks_the_call(self):
        with mock.patch.object(mon, "send_mail", side_effect=OSError("smtp down")), self.assertLogs("internest_core.api_monitor", "ERROR"):
            self.trigger(_http_error(500, "boom"))
        self.assertEqual(mail.outbox, [])

    def test_a_bug_inside_the_monitor_never_masks_the_original_error(self):
        with mock.patch.object(mon, "classify", side_effect=RuntimeError("monitor bug")), self.assertLogs("internest_core.api_monitor", "ERROR"):
            self.trigger(_http_error(500, "boom"))

    def test_a_missing_admin_setting_means_no_mail_and_no_crash(self):
        from django.conf import settings

        with override_settings():
            del settings.ADMIN_ALERT_EMAIL
            self.assertFalse(mon._send_admin("subject", "body"))
        self.assertEqual(mail.outbox, [])


class SettingsDefaultsTests(SimpleTestCase):
    def test_admin_recipient_and_thresholds(self):
        import os

        from django.conf import settings

        if "ADMIN_ALERT_EMAIL" not in os.environ:
            self.assertEqual(settings.ADMIN_ALERT_EMAIL, "engineerthe973@gmail.com")
        if "API_LOW_BALANCE_THRESHOLD" not in os.environ:
            self.assertEqual(settings.API_LOW_BALANCE_THRESHOLD, 2.0)
        self.assertEqual(settings.API_ALERT_COOLDOWN_SECONDS, 1800)


class BalanceTests(MonitorBase):
    def _reply(self, payload, status=200):
        return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://api.example.test/credits"))

    def test_low_balance_warns_before_zero(self):
        with mock.patch.object(mon.httpx, "get", return_value=self._reply({"data": {"balance": 1.5}})):
            self.assertEqual(mon.check_api_balance("OpenRouter", "https://api.example.test/credits", {"Authorization": "Bearer k"}), 1.5)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("URGENT: Low Balance — OpenRouter", mail.outbox[0].subject)
        self.assertIn("1.5", mail.outbox[0].body)
        self.assertIn("threshold 2", mail.outbox[0].body)

    def test_healthy_balance_is_silent_and_threshold_is_configurable(self):
        with mock.patch.object(mon.httpx, "get", return_value=self._reply({"credits": 40})):
            self.assertEqual(mon.check_api_balance("X", "https://api.example.test/credits"), 40.0)
            self.assertEqual(mail.outbox, [])
            mon.check_api_balance("X", "https://api.example.test/credits", threshold=50)
        self.assertEqual(len(mail.outbox), 1)

    def test_custom_parser_and_repeat_checks_respect_the_cooldown(self):
        with mock.patch.object(mon.httpx, "get", return_value=self._reply({"usage": {"left_usd": "0.40"}})):
            for _ in range(3):
                mon.check_api_balance("Y", "https://api.example.test/b", parse=lambda d: d["usage"]["left_usd"])
        self.assertEqual(len(mail.outbox), 1)

    def test_unreadable_balance_is_reported_not_raised(self):
        with mock.patch.object(mon.httpx, "get", return_value=self._reply({"nothing": "here"})):
            self.assertIsNone(mon.check_api_balance("Z", "https://api.example.test/b"))
        self.assertEqual(len(mail.outbox), 1)
        with mock.patch.object(mon.httpx, "get", side_effect=httpx.ConnectError("down")):
            self.assertIsNone(mon.check_api_balance("Z2", "https://api.example.test/b"))
        self.assertEqual(len(mail.outbox), 2)

    def test_balance_endpoint_error_status_is_an_auth_alert(self):
        with mock.patch.object(mon.httpx, "get", return_value=self._reply({"error": "bad key"}, status=401)):
            self.assertIsNone(mon.check_api_balance("K", "https://api.example.test/b"))
        self.assertIn("API key rejected", mail.outbox[0].subject)


class ManagementCommandTests(MonitorBase):
    def test_sends_both_samples_to_the_admin_ignoring_the_cooldown(self):
        out = StringIO()
        call_command("test_api_alert", stdout=out)
        call_command("test_api_alert", "--type", "failure", stdout=out)
        self.assertEqual(len(mail.outbox), 3)
        self.assertTrue(all(m.to == [ADMIN] for m in mail.outbox))
        self.assertIn("URGENT: Low Balance", mail.outbox[1].subject)
        self.assertIn("TEST ALERT", mail.outbox[0].body)
        self.assertIn(f"Sent to {ADMIN}", out.getvalue())

    def test_reports_when_mail_could_not_be_sent(self):
        with mock.patch.object(mon, "send_mail", return_value=0), self.assertRaises(CommandError):
            call_command("test_api_alert", "--type", "failure", stdout=StringIO())

    def test_requires_an_admin_address(self):
        with override_settings(ADMIN_ALERT_EMAIL=""), self.assertRaises(CommandError):
            call_command("test_api_alert", stdout=StringIO())


@override_settings(ADMIN_ALERT_EMAIL=ADMIN, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", GEMINI_API_KEY="g-key",
                   GOOGLE_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/", AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="",
                   AZURE_OPENAI_API_KEY="", AZURE_OPENAI_ENDPOINT="")
class WiringTests(TestCase):
    """The real integrations report through the monitor and keep their own return values and fallbacks."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_llm_failure_alerts_with_provider_and_feature_and_still_fails_over(self):
        from internest_skills import llm

        with mock.patch("openai.resources.chat.completions.Completions.create", side_effect=_http_error(401, "API key not valid")):
            with mon.api_feature("CV Analysis"), self.assertRaises(llm.LLMUnavailable):
                llm.chat_json("sys", "user")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Google Gemini", mail.outbox[0].subject)
        self.assertIn("Feature      : CV Analysis", mail.outbox[0].body)
        self.assertIn("HTTP status  : 401", mail.outbox[0].body)

    def test_llm_success_sends_nothing_and_returns_the_data(self):
        from internest_skills import llm

        reply = mock.Mock(choices=[mock.Mock(message=mock.Mock(content=json.dumps({"ok": 1})), finish_reason="stop")])
        with mock.patch("openai.resources.chat.completions.Completions.create", return_value=reply):
            self.assertEqual(llm.chat_json("sys", "user"), {"ok": 1})
        self.assertEqual(mail.outbox, [])

    def test_quiz_generation_labels_its_feature(self):
        from internest_skills import quizgen

        seen = []

        def fake_chat(*args, **kwargs):
            seen.append(mon.current_feature())
            return {"q": []}

        with mock.patch.object(quizgen, "build_system_prompt", return_value="s"), mock.patch.object(quizgen, "build_user_prompt", return_value="u"):
            quizgen.request_raw_items(mock.Mock(), chat=fake_chat)
        self.assertEqual(seen, ["Question Generation"])

    def test_fawaterak_error_status_alerts_and_keeps_raising_its_own_error(self):
        from internest_startups import fawaterak

        order = mock.Mock(customer_name="Nour Ali", final_price=800, currency="EGP", customer_email="n@x.com", customer_phone="010", pk=5)
        resp = httpx.Response(401, text="Unauthorized", request=httpx.Request("POST", "https://fawaterak.test"))
        with override_settings(FAWATERAK_BASE_URL="https://fawaterak.test", FAWATERAK_API_KEY="k"), \
                mock.patch.object(fawaterak.httpx, "post", return_value=resp), self.assertRaises(fawaterak.FawaterakError):
            fawaterak.create_invoice_link(order, "Pro", {"success": "/s", "fail": "/f", "pending": "/p"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Fawaterak", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [ADMIN])

    def test_voice_transcription_failure_alerts(self):
        from internest_students import cv_builder_service as svc

        provider = svc.llm.Provider("API_KEY_2", "k", "https://api.groq.com/openai/v1", "whisper-large-v3")
        with mock.patch.object(svc, "transcription_providers", return_value=[provider]), \
                mock.patch("openai.resources.audio.transcriptions.Transcriptions.create", side_effect=_http_error(500, "whisper down")), \
                self.assertRaises(svc.VoiceUnavailable):
            svc.transcribe(b"x", "a.webm", "audio/webm")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Voice-to-CV Transcription", mail.outbox[0].subject)
        self.assertIn("Backup LLM", mail.outbox[0].subject)
