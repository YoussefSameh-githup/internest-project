"""API registry, rich alert emails, incident log, budget ledger, failover reporting, key validation and check_api_health."""
import io
from decimal import Decimal
from unittest import mock

import httpx
import openai
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.mail.backends.base import BaseEmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from internest_core import api_health, api_monitor as mon, api_registry
from internest_core.models import APIIncident, APIProvider

ADMIN = "admin-alerts@example.com"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def http_error(code, body=""):
    resp = httpx.Response(code, text=body, request=httpx.Request("POST", "https://api.example.test/v1"))
    cls = {401: openai.AuthenticationError, 429: openai.RateLimitError}.get(code, openai.APIStatusError)
    return cls(body or f"HTTP {code}", response=resp, body=None)


@override_settings(ADMIN_ALERT_EMAIL=ADMIN, EMAIL_BACKEND=LOCMEM, DEFAULT_FROM_EMAIL="Internest <noreply@example.com>")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def boom(self, exc, provider="gemini", feature="CV Analysis"):
        with self.assertRaises(type(exc)):
            mon.monitored_api_call(provider, feature, mock.Mock(side_effect=exc))


class RegistryTests(Base):
    def test_every_registered_api_has_a_complete_description(self):
        self.assertGreaterEqual(set(api_registry.REGISTRY), {"gemini", "agentrouter", "backup_llm", "azure_openai", "sendgrid", "fawaterak"})
        for key, spec in api_registry.REGISTRY.items():
            self.assertEqual(spec.key, key)
            for field in ("name", "category", "role", "impact", "env_var", "code_location", "dashboard_url"):
                self.assertTrue(getattr(spec, field), f"{key}.{field} is empty")
            self.assertIn(spec.category, api_registry.CATEGORIES)
            self.assertTrue(spec.dashboard_url.startswith("https://"), key)

    def test_migrate_creates_an_admin_row_per_registered_api(self):
        self.assertEqual(set(APIProvider.objects.filter(registered=True).values_list("key", flat=True)), set(api_registry.REGISTRY))

    def test_lookup_by_key_or_display_name(self):
        self.assertIs(api_registry.spec_for("gemini"), api_registry.spec_for("Google Gemini"))
        self.assertIsNone(api_registry.spec_for("nope"))

    def test_resync_refreshes_descriptions_but_never_the_budget(self):
        APIProvider.objects.filter(key="azure_openai").update(role="stale", initial_budget=Decimal("25"), estimated_spent=Decimal("3"))
        mon.sync_registry()
        row = APIProvider.objects.get(key="azure_openai")
        self.assertEqual(row.role, api_registry.REGISTRY["azure_openai"].role)
        self.assertEqual((row.initial_budget, row.estimated_spent), (Decimal("25"), Decimal("3")))

    def test_a_future_api_needs_one_register_call(self):
        spec = api_registry.ProviderSpec(
            key="serpapi", name="SerpAPI", category="search", role="Google results for the opportunity scraper",
            impact="New opportunities stop being imported.", env_var="SERPAPI_KEY", code_location="internest_core/scraper.py",
            dashboard_url="https://serpapi.com/dashboard", is_paid=True, tracking="ledger", cost_per_call=0.01)
        api_registry.register(spec)
        self.addCleanup(api_registry.REGISTRY.pop, "serpapi")
        self.boom(http_error(500, "serp down"), provider="serpapi", feature="Opportunity scraper")
        body = mail.outbox[0].body
        for expected in ("SerpAPI (Search / Scraping)", "Google results for the opportunity scraper", "New opportunities stop being imported.",
                         "SERPAPI_KEY", "internest_core/scraper.py", "https://serpapi.com/dashboard"):
            self.assertIn(expected, body)
        self.assertEqual(APIProvider.objects.get(key="serpapi").tracking_mode, "ledger")

    def test_an_unregistered_api_still_gets_monitored_and_says_so(self):
        self.boom(http_error(401, "bad"), provider="Brand New API", feature="Something")
        row = APIProvider.objects.get(key="brand_new_api")
        self.assertFalse(row.registered)
        self.assertIn("is not in internest_core/api_registry.py yet", mail.outbox[0].body)
        self.assertEqual(APIIncident.objects.filter(provider=row).count(), 1)


class AlertContentTests(Base):
    def test_the_email_carries_all_four_sections(self):
        self.boom(http_error(401, "API key not valid sk-abcdefghij123456"), provider="gemini", feature="Question Generation")
        body = mail.outbox[0].body
        for section in ("1) API PROVIDER & SERVICE PURPOSE", "2) AFFECTED FEATURES & FAILOVER STATUS", "3) TECHNICAL DIAGNOSTICS", "4) WHAT TO DO NOW"):
            self.assertIn(section, body)
        spec = api_registry.REGISTRY["gemini"]
        for expected in (spec.role, spec.impact, "Feature      : Question Generation", "HTTP status  : 401", "API key not valid",
                         "Env variable : GEMINI_API_KEY", "internest_skills/llm.py", spec.dashboard_url, "check_api_health", "Reload"):
            self.assertIn(expected, body)
        self.assertNotIn("abcdefghij123456", body)  # secrets masked

    def test_each_failure_kind_gets_its_own_steps(self):
        cases = [(http_error(401, "x"), "create or copy a valid API key"), (http_error(402, "x"), "top up the account"),
                 (http_error(429, "slow down"), "rate / quota limits"), (http_error(503, "x"), "status page"),
                 (httpx.ReadTimeout("t"), "outbound access"), (httpx.ConnectError("c"), "outbound access")]
        for index, (exc, step) in enumerate(cases):
            cache.clear()
            APIIncident.objects.all().delete()
            mail.outbox.clear()
            self.boom(exc, provider=("gemini", "agentrouter", "azure_openai", "backup_llm", "fawaterak", "sendgrid")[index])
            self.assertIn(step, mail.outbox[0].body, repr(exc))

    def test_every_alert_names_the_env_variable_and_console_of_its_provider(self):
        for key in ("agentrouter", "azure_openai", "sendgrid", "fawaterak"):
            cache.clear()
            mail.outbox.clear()
            self.boom(http_error(403, "forbidden"), provider=key)
            spec = api_registry.REGISTRY[key]
            self.assertIn(spec.env_var, mail.outbox[0].body)
            self.assertIn(spec.dashboard_url, mail.outbox[0].body)

    def test_401_403_429_5xx_and_timeouts_all_alert(self):
        kinds = [(http_error(401, "x"), "auth"), (http_error(403, "x"), "auth"), (http_error(429, "rate limit"), "rate_limited"),
                 (http_error(500, "x"), "server"), (http_error(502, "x"), "server"), (httpx.ReadTimeout("t"), "timeout"),
                 (httpx.ConnectError("c"), "connection")]
        for exc, kind in kinds:
            self.boom(exc, provider="gemini", feature=f"feature-{kind}-{type(exc).__name__}")
        self.assertEqual(set(APIIncident.objects.values_list("alert_type", flat=True)), {"auth", "rate_limited", "server", "timeout", "connection"})
        self.assertEqual(len(mail.outbox), 5)  # one per (provider, type): the 30-minute cooldown merges the repeats


class IncidentLogTests(Base):
    def test_every_incident_is_stored_including_the_suppressed_ones(self):
        for _ in range(4):
            self.boom(http_error(500, "boom"))
        incidents = list(APIIncident.objects.order_by("created_at"))
        self.assertEqual(len(incidents), 4)
        self.assertEqual([i.emailed for i in incidents], [True, False, False, False])
        first = incidents[0]
        self.assertEqual((first.provider.key, first.alert_type, first.http_status, first.feature), ("gemini", "server", 500, "CV Analysis"))
        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_email_is_retried_on_the_next_incident(self):
        with mock.patch.object(mon, "send_mail", side_effect=OSError("smtp down")), self.assertLogs("internest_core.api_monitor", "ERROR"):
            self.boom(http_error(500, "boom"))
        self.assertFalse(APIIncident.objects.get().emailed)
        self.boom(http_error(500, "boom"))
        self.assertEqual(len(mail.outbox), 1)

    def test_secrets_in_details_are_masked_before_storing(self):
        self.boom(http_error(401, "bad key SG.abcdefghijkl.mnopqrstuvwx or sk-abcdefghij123456"))
        detail = APIIncident.objects.get().detail
        self.assertNotIn("abcdefghijkl", detail)
        self.assertNotIn("abcdefghij123456", detail)

    def test_only_the_admin_gets_the_mail(self):
        self.boom(http_error(500, "x"))
        self.assertEqual([m.to for m in mail.outbox], [[ADMIN]])


class FailoverTests(Base):
    def test_a_backup_that_answers_is_reported_as_a_successful_failover(self):
        from internest_skills import llm

        providers = [llm.Provider("GEMINI_API_KEY", "k1", "https://g.example/v1", "m"), llm.Provider("AZURE_OPENAI", "k2", "https://a.example/v1", "m")]
        reply = mock.Mock(choices=[mock.Mock(message=mock.Mock(content='{"ok": 1}'), finish_reason="stop")])
        with mock.patch.object(llm, "providers", return_value=providers), \
                mock.patch("openai.resources.chat.completions.Completions.create", side_effect=[http_error(500, "gemini down"), reply]):
            with mon.api_feature("CV Analysis"):
                self.assertEqual(llm.chat_json("s", "u"), {"ok": 1})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Fallback worked: Azure OpenAI answered the request", mail.outbox[0].body)
        self.assertIn("Google Gemini", mail.outbox[0].subject)
        self.assertIn("Fallback worked", APIIncident.objects.get().failover_status)

    def test_when_every_provider_fails_the_email_says_the_feature_is_degraded(self):
        from internest_skills import llm

        providers = [llm.Provider("GEMINI_API_KEY", "k1", "https://g.example/v1", "m"), llm.Provider("AZURE_OPENAI", "k2", "https://a.example/v1", "m")]
        with mock.patch.object(llm, "providers", return_value=providers), \
                mock.patch("openai.resources.chat.completions.Completions.create", side_effect=[http_error(500, "a"), http_error(503, "b")]):
            with self.assertRaises(llm.LLMUnavailable):
                llm.chat_json("s", "u")
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertIn("No backup answered", message.body)
            self.assertIn("keyword matching", message.body)

    def test_a_service_without_a_backup_says_so(self):
        self.boom(http_error(500, "x"), provider="fawaterak", feature="Payment invoice link")
        self.assertIn("No automatic backup for this service", mail.outbox[0].body)
        self.assertIn("manual payment methods", mail.outbox[0].body)


class LedgerTests(Base):
    def _row(self, **values):
        APIProvider.objects.filter(key="azure_openai").update(**values)
        return APIProvider.objects.get(key="azure_openai")

    def _call(self, tokens=1000):
        reply = mock.Mock(usage=mock.Mock(total_tokens=tokens))
        return mon.monitored_api_call("azure_openai", "CV Analysis", lambda: reply)

    def test_each_successful_call_deducts_the_estimated_cost(self):
        self._row(initial_budget=Decimal("10"), low_balance_threshold=Decimal("2"), cost_per_call=Decimal("0.01"), cost_per_1k_tokens=Decimal("0.02"))
        self._call(tokens=1000)
        self._call(tokens=2000)
        row = APIProvider.objects.get(key="azure_openai")
        self.assertEqual(row.estimated_spent, Decimal("0.0800"))
        self.assertEqual(row.remaining_balance, Decimal("9.9200"))
        self.assertEqual(mail.outbox, [])

    def test_low_balance_sends_an_urgent_email_with_full_context(self):
        self._row(initial_budget=Decimal("1"), low_balance_threshold=Decimal("0.5"), cost_per_call=Decimal("0.3"), cost_per_1k_tokens=Decimal("0"))
        self._call()
        self.assertEqual(mail.outbox, [])  # 0.70 left
        self._call()  # 0.40 left <= 0.50
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("URGENT: Low Balance — Azure OpenAI", message.subject)
        for expected in ("0.40 USD left", "threshold 0.50", api_registry.REGISTRY["azure_openai"].dashboard_url, "AZURE_OPENAI_API_KEY",
                         "Initial budget"):
            self.assertIn(expected, message.body)
        self._call()
        self.assertEqual(len(mail.outbox), 1)  # cooldown: one email, not one per call

    def test_no_budget_entered_means_tracking_is_off_and_nothing_is_sent(self):
        self._row(initial_budget=Decimal("0"), cost_per_call=Decimal("5"))
        self._call()
        self.assertEqual(mail.outbox, [])
        self.assertIsNone(APIProvider.objects.get(key="azure_openai").remaining_balance)

    def test_failed_calls_cost_nothing_and_free_providers_have_no_ledger(self):
        self._row(initial_budget=Decimal("10"), cost_per_call=Decimal("1"))
        self.boom(http_error(500, "x"), provider="azure_openai")
        mon.monitored_api_call("gemini", "x", lambda: mock.Mock(usage=mock.Mock(total_tokens=5000)))
        self.assertEqual(APIProvider.objects.get(key="azure_openai").estimated_spent, Decimal("0"))
        self.assertEqual(APIProvider.objects.get(key="gemini").estimated_spent, Decimal("0"))

    def test_a_ledger_error_never_breaks_the_call(self):
        with mock.patch.object(mon, "provider_row", side_effect=RuntimeError("db")), self.assertLogs("internest_core.api_monitor", "ERROR"):
            self.assertEqual(mon.monitored_api_call("azure_openai", "x", lambda: "fine"), "fine")


class KeyValidationAndHealthTests(Base):
    def _response(self, status, payload=None):
        return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", "https://api.example.test"))

    def test_a_missing_key_is_skipped_not_alerted(self):
        with override_settings(GEMINI_API_KEY=""), mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(api_health.validate_key(api_registry.REGISTRY["gemini"])[0], api_health.SKIP)
        self.assertEqual(mail.outbox, [])

    @override_settings(GEMINI_API_KEY="g-key", GOOGLE_BASE_URL="https://g.example/v1beta/openai/")
    def test_valid_key(self):
        with mock.patch.object(api_health.httpx, "get", return_value=self._response(200)) as get:
            status, note = api_health.validate_key(api_registry.REGISTRY["gemini"])
        self.assertEqual(status, api_health.OK)
        self.assertEqual(get.call_args.args[0], "https://g.example/v1beta/openai/models")
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer g-key"})
        self.assertEqual(mail.outbox, [])

    @override_settings(GEMINI_API_KEY="g-key", GOOGLE_BASE_URL="https://g.example/v1beta/openai/")
    def test_rejected_key_alerts_with_the_env_variable(self):
        with mock.patch.object(api_health.httpx, "get", return_value=self._response(401, {"error": "bad"})):
            status, note = api_health.validate_key(api_registry.REGISTRY["gemini"])
        self.assertEqual(status, api_health.FAIL)
        self.assertIn("key rejected", note)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("GEMINI_API_KEY", mail.outbox[0].body)
        self.assertIn("check_api_health", mail.outbox[0].body)

    @override_settings(GEMINI_API_KEY="g-key", GOOGLE_BASE_URL="https://g.example/v1beta/openai/")
    def test_unreachable_provider_alerts_and_an_odd_status_is_only_a_warning(self):
        with mock.patch.object(api_health.httpx, "get", side_effect=httpx.ConnectTimeout("slow")):
            self.assertEqual(api_health.validate_key(api_registry.REGISTRY["gemini"])[0], api_health.FAIL)
        self.assertEqual(len(mail.outbox), 1)
        cache.clear()
        with mock.patch.object(api_health.httpx, "get", return_value=self._response(404)):
            self.assertEqual(api_health.validate_key(api_registry.REGISTRY["gemini"])[0], api_health.WARN)
        self.assertEqual(len(mail.outbox), 1)  # inconclusive: no extra email

    @override_settings(FAWATERAK_API_KEY="f-key")
    def test_an_api_without_a_safe_validation_request_is_a_warning(self):
        self.assertEqual(api_health.validate_key(api_registry.REGISTRY["fawaterak"])[0], api_health.WARN)

    @override_settings(ANYMAIL={"SENDGRID_API_KEY": "SG.x"})
    def test_live_balance_is_read_and_alerts_when_low(self):
        spec = api_registry.REGISTRY["sendgrid"]
        row = APIProvider.objects.get(key="sendgrid")
        APIProvider.objects.filter(pk=row.pk).update(low_balance_threshold=Decimal("100"))
        row.refresh_from_db()
        with mock.patch.dict("os.environ", {"ANYMAIL_SENDGRID_API_KEY": "SG.x"}), \
                mock.patch.object(mon.httpx, "get", return_value=self._response(200, {"total": 40000, "used": 39950, "remain": 50})):
            status, note = api_health.check_balance(spec, row)
        self.assertEqual(status, api_health.FAIL)
        self.assertIn("LOW", note)
        self.assertEqual(APIProvider.objects.get(key="sendgrid").last_balance, Decimal("50"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("URGENT: Low Balance — SendGrid", mail.outbox[0].subject)

    def test_ledger_provider_without_a_budget_is_a_hint_not_an_alert(self):
        spec, row = api_registry.REGISTRY["azure_openai"], APIProvider.objects.get(key="azure_openai")
        status, note = api_health.check_balance(spec, row)
        self.assertEqual(status, api_health.WARN)
        self.assertIn("Initial budget", note)
        self.assertEqual(mail.outbox, [])

    def test_ledger_provider_below_threshold_alerts(self):
        APIProvider.objects.filter(key="azure_openai").update(initial_budget=Decimal("10"), estimated_spent=Decimal("9"), low_balance_threshold=Decimal("2"))
        status, note = api_health.check_balance(api_registry.REGISTRY["azure_openai"], APIProvider.objects.get(key="azure_openai"))
        self.assertEqual(status, api_health.FAIL)
        self.assertEqual(len(mail.outbox), 1)

    def test_free_providers_are_not_balance_checked(self):
        self.assertEqual(api_health.check_balance(api_registry.REGISTRY["gemini"], APIProvider.objects.get(key="gemini"))[0], api_health.SKIP)


class CheckCommandTests(Base):
    def _run(self):
        out = io.StringIO()
        call_command("check_api_health", stdout=out)
        return out.getvalue()

    def test_clean_run_prints_a_table_and_sends_nothing(self):
        with mock.patch.dict("os.environ", {}, clear=True), override_settings(
                GEMINI_API_KEY="", AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="", AZURE_OPENAI_API_KEY="", WHISPER_API_KEY="",
                EMAIL_HOST_PASSWORD="", FAWATERAK_API_KEY=""):
            output = self._run()
        for name in ("Google Gemini", "AgentRouter", "Azure OpenAI", "SendGrid", "Fawaterak"):
            self.assertIn(name, output)
        self.assertIn("All configured APIs look healthy.", output)
        self.assertEqual(mail.outbox, [])  # no fake test alerts

    @override_settings(AGENTROUTER_API_KEY="r-key", AGENTROUTER_BASE_URL="https://r.example/v1", GEMINI_API_KEY="")
    def test_a_rejected_key_fails_the_command_and_alerts_once(self):
        def fake_get(url, **kwargs):
            return httpx.Response(401 if "r.example" in url else 200, json={}, request=httpx.Request("GET", url))

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(api_health.httpx, "get", side_effect=fake_get), \
                override_settings(LLM_BACKUP_API_KEY="", AZURE_OPENAI_API_KEY="", WHISPER_API_KEY="", EMAIL_HOST_PASSWORD="", FAWATERAK_API_KEY=""):
            with self.assertRaises(CommandError) as caught:
                self._run()
        self.assertIn("AgentRouter", str(caught.exception))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("AgentRouter", mail.outbox[0].subject)

    def test_old_incidents_are_pruned(self):
        from datetime import timedelta

        from django.utils import timezone

        self.boom(http_error(500, "x"))
        APIIncident.objects.update(created_at=timezone.now() - timedelta(days=api_health.INCIDENT_RETENTION_DAYS + 1))
        with mock.patch.dict("os.environ", {}, clear=True), override_settings(
                GEMINI_API_KEY="", AGENTROUTER_API_KEY="", LLM_BACKUP_API_KEY="", AZURE_OPENAI_API_KEY="", WHISPER_API_KEY="",
                EMAIL_HOST_PASSWORD="", FAWATERAK_API_KEY=""):
            self._run()
        self.assertEqual(APIIncident.objects.count(), 0)


class EmailBackendTests(Base):
    """SendGrid / SMTP failures are reported like any other API, and the alert still reaches the admin when SendGrid is the one down."""

    @override_settings(MONITORED_EMAIL_BACKEND="anymail.backends.sendgrid.EmailBackend")
    def test_a_sendgrid_failure_is_reported_and_re_raised(self):
        from internest_core.email_backend import MonitoredEmailBackend

        class Down(BaseEmailBackend):
            def send_messages(self, messages):
                raise http_error(401, "SendGrid: invalid key")

        with mock.patch("internest_core.email_backend.get_connection", return_value=Down()):
            backend = MonitoredEmailBackend()
            with self.assertRaises(openai.AuthenticationError):
                backend.send_messages([mock.Mock()])
            self.assertEqual(MonitoredEmailBackend(fail_silently=True).send_messages([mock.Mock()]), 0)
        self.assertEqual(APIIncident.objects.filter(provider__key="sendgrid", alert_type="auth").count(), 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Transactional email", mail.outbox[0].subject)
        self.assertIn("ANYMAIL_SENDGRID_API_KEY", mail.outbox[0].body)

    @override_settings(MONITORED_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend")
    def test_smtp_failures_belong_to_gmail(self):
        from internest_core.email_backend import provider_key

        self.assertEqual(provider_key("django.core.mail.backends.smtp.EmailBackend"), "gmail_smtp")
        self.assertEqual(provider_key("anymail.backends.sendgrid.EmailBackend"), "sendgrid")

    def test_the_alert_email_itself_never_raises_another_alert(self):
        from internest_core.email_backend import MonitoredEmailBackend

        class Down(BaseEmailBackend):
            def send_messages(self, messages):
                raise OSError("down")

        with override_settings(EMAIL_BACKEND="internest_core.email_backend.MonitoredEmailBackend", MONITORED_EMAIL_BACKEND="anymail.backends.sendgrid.EmailBackend",
                               EMAIL_HOST_PASSWORD=""), mock.patch("internest_core.email_backend.get_connection", return_value=Down()):
            self.boom(http_error(500, "boom"))  # the alert goes through the failing backend
        self.assertEqual(APIIncident.objects.count(), 1)  # the original incident only: no loop

    @override_settings(MONITORED_EMAIL_BACKEND="anymail.backends.sendgrid.EmailBackend", EMAIL_HOST_PASSWORD="app-password")
    def test_when_sendgrid_is_down_the_admin_alert_falls_back_to_gmail_smtp(self):
        calls = []

        def fake_send(subject, body, sender, recipients, fail_silently=True, connection=None):
            calls.append(connection)
            return 0 if connection is None else 1

        with mock.patch.object(mon, "send_mail", side_effect=fake_send):
            self.assertTrue(mon._send_admin("subject", "body"))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIn("smtp", type(calls[1]).__module__)


class AdminTests(Base):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(self.root)

    def test_provider_admin_lists_descriptions_and_budget(self):
        APIProvider.objects.filter(key="azure_openai").update(initial_budget=Decimal("50"), estimated_spent=Decimal("12.5"))
        page = self.client.get(reverse("admin:internest_core_apiprovider_changelist"))
        self.assertContains(page, "Azure OpenAI")
        self.assertContains(page, "37.50 USD")
        change = self.client.get(reverse("admin:internest_core_apiprovider_change", args=[APIProvider.objects.get(key="azure_openai").pk]))
        for text in (api_registry.REGISTRY["azure_openai"].role[:40], "AZURE_OPENAI_API_KEY", "portal.azure.com"):
            self.assertContains(change, text)
        self.assertEqual(self.client.get(reverse("admin:internest_core_apiprovider_add")).status_code, 403)

    def test_budget_is_editable_and_descriptions_are_read_only(self):
        row = APIProvider.objects.get(key="agentrouter")
        change = self.client.get(reverse("admin:internest_core_apiprovider_change", args=[row.pk]))
        self.assertContains(change, 'name="initial_budget"')
        self.assertContains(change, 'name="low_balance_threshold"')
        self.assertNotContains(change, 'name="role"')

    def test_top_up_action_resets_the_spend(self):
        row = APIProvider.objects.get(key="agentrouter")
        APIProvider.objects.filter(pk=row.pk).update(estimated_spent=Decimal("7"))
        self.client.post(reverse("admin:internest_core_apiprovider_changelist"),
                         {"action": "reset_spend_after_top_up", "_selected_action": [row.pk]})
        self.assertEqual(APIProvider.objects.get(pk=row.pk).estimated_spent, Decimal("0"))

    def test_incident_admin_shows_the_log_read_only(self):
        self.boom(http_error(500, "upstream exploded"))
        page = self.client.get(reverse("admin:internest_core_apiincident_changelist"))
        self.assertContains(page, "CV Analysis")
        self.assertContains(page, "server")
        self.assertEqual(self.client.get(reverse("admin:internest_core_apiincident_add")).status_code, 403)
