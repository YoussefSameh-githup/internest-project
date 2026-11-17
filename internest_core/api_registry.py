"""The ONE place where every external API Internest depends on is described.

Adding a new API = adding one `register(ProviderSpec(...))` call below (see docs in CLAUDE.md / `check_api_health`).
Everything else follows automatically: failure alerts, the incident log, the Django-admin row, balance / budget tracking and
the scheduled `manage.py check_api_health` run all read this registry. Alerts for an API that was never registered still
work (the row is auto-created as "unregistered"), but their email will say the API has no description yet.

Code is the source of truth for the descriptive fields; only the budget numbers are edited in the Django admin.
"""
import os
import re
from dataclasses import dataclass, field

from django.conf import settings

CATEGORIES = {
    "ai": "AI Generation",
    "email": "Email Delivery",
    "payments": "Payments",
    "search": "Search / Scraping",
    "other": "Other",
}
TRACKING_NONE, TRACKING_LIVE, TRACKING_LEDGER = "none", "live", "ledger"


@dataclass(frozen=True)
class ProviderSpec:
    key: str                      # stable slug, used everywhere in code: monitored_api_call("gemini", ...)
    name: str                     # shown in emails and the admin
    category: str                 # a key of CATEGORIES
    role: str                     # what this API does on Internest, in plain language
    impact: str                   # which user-facing features break or degrade when it is down
    env_var: str                  # the .env key that holds its secret
    code_location: str            # where the code calls it
    dashboard_url: str            # console / billing page for the instant fix
    degraded_behavior: str = ""   # what users get when no backup answers
    is_paid: bool = False
    tracking: str = TRACKING_NONE  # none | live (provider balance endpoint) | ledger (estimated spend kept in the database)
    key_setting: str = ""         # Django setting holding the key (falls back to the env var)
    base_url_setting: str = ""    # Django setting holding the API base URL (for key validation)
    validate_path: str = ""       # appended to the base URL (or used as-is when absolute) for the key-validation GET
    balance_url: str = ""         # live mode: the balance endpoint
    balance_parser: str = ""      # live mode: name in api_health.BALANCE_PARSERS
    # Defaults applied when the admin row is first created (edit the real numbers in Django admin afterwards).
    cost_per_call: float = 0.0
    cost_per_1k_tokens: float = 0.0
    currency: str = "USD"
    extra: dict = field(default_factory=dict, compare=False)

    def api_key(self) -> str:
        return (getattr(settings, self.key_setting, "") if self.key_setting else "") or os.getenv(self.env_var, "")

    def validate_url(self) -> str:
        if self.validate_path.startswith("https://"):
            return self.validate_path
        base = getattr(settings, self.base_url_setting, "") if self.base_url_setting else ""
        return f"{base.rstrip('/')}/{self.validate_path.lstrip('/')}" if base and self.validate_path else ""


REGISTRY: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    REGISTRY[spec.key] = spec
    return spec


def slugify_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "api").lower()).strip("_") or "api"


def spec_for(key_or_name: str):
    """The registered spec for a key ("gemini") or a display name ("Google Gemini"); None when it was never registered."""
    if key_or_name in REGISTRY:
        return REGISTRY[key_or_name]
    wanted = (key_or_name or "").strip().lower()
    return next((s for s in REGISTRY.values() if s.name.lower() == wanted), None)


# ---------------------------------------------------------------------------------------------------------------------
# Registered APIs
# ---------------------------------------------------------------------------------------------------------------------
_LLM_ROLE = ("CV analysis (skill extraction), AI-generated skill-test questions, CV Builder polish, freelance-gig analysis and "
             "semantic skill matching")
_LLM_IMPACT = ("CV parsing and skills extraction, matching scores, new skill-test questions and CV polish. Users are not blocked: "
               "the app falls back to the next AI provider, then to keyword matching and the saved question bank.")
_LLM_DEGRADED = "Skills are extracted with keyword matching only and quizzes reuse the saved question bank (fewer fresh questions)."

register(ProviderSpec(
    key="gemini", name="Google Gemini", category="ai", role=_LLM_ROLE, impact=_LLM_IMPACT, degraded_behavior=_LLM_DEGRADED,
    env_var="GEMINI_API_KEY", key_setting="GEMINI_API_KEY", base_url_setting="GOOGLE_BASE_URL", validate_path="models",
    code_location="internest_skills/llm.py: chat_json -> _ask (first provider in the failover chain)",
    dashboard_url="https://aistudio.google.com/usage",
))
register(ProviderSpec(
    key="agentrouter", name="AgentRouter", category="ai", role=_LLM_ROLE, impact=_LLM_IMPACT, degraded_behavior=_LLM_DEGRADED,
    env_var="API_KEY_1", key_setting="AGENTROUTER_API_KEY", base_url_setting="AGENTROUTER_BASE_URL", validate_path="models",
    code_location="internest_skills/llm.py: chat_json -> _ask (2nd in the chain)",
    dashboard_url="https://agentrouter.org/", is_paid=True, tracking=TRACKING_LEDGER, cost_per_1k_tokens=0.0004,
))
register(ProviderSpec(
    key="backup_llm", name="Backup LLM", category="ai", role=_LLM_ROLE + " (also Whisper voice transcription when it is Groq/OpenAI)",
    impact=_LLM_IMPACT, degraded_behavior=_LLM_DEGRADED, env_var="API_KEY_2", key_setting="LLM_BACKUP_API_KEY",
    base_url_setting="LLM_BACKUP_BASE_URL", validate_path="models",
    code_location="internest_skills/llm.py (3rd in the chain); internest_students/cv_builder_service.py: transcribe",
    dashboard_url="https://console.groq.com/", is_paid=True, tracking=TRACKING_LEDGER, cost_per_1k_tokens=0.0004,
))
register(ProviderSpec(
    key="azure_openai", name="Azure OpenAI", category="ai", role=_LLM_ROLE, impact=_LLM_IMPACT, degraded_behavior=_LLM_DEGRADED,
    env_var="AZURE_OPENAI_API_KEY", key_setting="AZURE_OPENAI_API_KEY", base_url_setting="AZURE_OPENAI_ENDPOINT", validate_path="models",
    code_location="internest_skills/llm.py (last provider in the chain)",
    dashboard_url="https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/overview",
    is_paid=True, tracking=TRACKING_LEDGER, cost_per_1k_tokens=0.0004,
))
register(ProviderSpec(
    key="whisper", name="Whisper transcription", category="ai", role="Speech-to-text for the CV Builder's voice mode (Arabic/English)",
    impact="Voice-to-CV stops working; typing the CV still works.", degraded_behavior="The voice button reports that transcription is unavailable.",
    env_var="WHISPER_API_KEY", key_setting="WHISPER_API_KEY", base_url_setting="WHISPER_BASE_URL", validate_path="models",
    code_location="internest_students/cv_builder_service.py: transcribe", dashboard_url="https://console.groq.com/",
))
register(ProviderSpec(
    key="sendgrid", name="SendGrid", category="email",
    role="Transactional email: verification codes (OTP), shortlist and invitation emails, opportunity notifications and these admin alerts",
    impact="Students cannot receive verification codes (sign-up and login re-verification are blocked), startups get no applicant news, "
           "and admin alerts fall back to Gmail SMTP when it is configured.",
    degraded_behavior="Emails are not delivered; verification-code emails fail silently until SendGrid works again.",
    env_var="ANYMAIL_SENDGRID_API_KEY", validate_path="https://api.sendgrid.com/v3/scopes",
    code_location="internest_core/email_backend.py (every Django email) and internest_core/email_helpers.py",
    dashboard_url="https://app.sendgrid.com/settings/billing", is_paid=True, tracking=TRACKING_LIVE,
    balance_url="https://api.sendgrid.com/v3/user/credits", balance_parser="sendgrid_credits", currency="credits",
))
register(ProviderSpec(
    key="gmail_smtp", name="Gmail SMTP", category="email", role="Email fallback when SendGrid is not configured (and for admin alerts when SendGrid is down)",
    impact="When SendGrid is not set up, ALL email goes through Gmail: verification codes and notifications stop if its password is rejected.",
    degraded_behavior="Emails are not delivered.", env_var="EMAIL_HOST_PASSWORD", key_setting="EMAIL_HOST_PASSWORD",
    code_location="internest_core/email_backend.py (django SMTP backend)", dashboard_url="https://myaccount.google.com/apppasswords",
))
register(ProviderSpec(
    key="fawaterak", name="Fawaterak", category="payments", role="Online payment gateway for Internest Pro subscriptions and paid courses",
    impact="Checkout cannot create invoice links; customers can still pay by InstaPay / RedotPay with a receipt.",
    degraded_behavior="The checkout page shows an error and offers the manual payment methods.",
    env_var="FAWATERAK_API_KEY", key_setting="FAWATERAK_API_KEY", code_location="internest_startups/fawaterak.py: create_invoice_link",
    dashboard_url="https://app.fawaterk.com/",
))
