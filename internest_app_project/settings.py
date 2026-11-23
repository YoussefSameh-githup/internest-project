"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
import sys
import environ
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent

# =========================================================================
# django-environ: load configuration from BASE_DIR/.env so the WSGI app,
# manage.py, and `python manage.py shell` all see the same variables.
# overwrite=False means values already present in os.environ (e.g. set
# in the PythonAnywhere WSGI file) still take precedence over the .env
# file — useful when you want to override a single value per deploy.
# =========================================================================
env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_SECURE_SSL_REDIRECT=(bool, False),
    DJANGO_EMAIL_USE_TLS=(bool, True),
)
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)


TESTING = "test" in sys.argv[1:2]
# PythonAnywhere: web apps cannot start threads and /home is a network filesystem (no SQLite WAL / mmap there).
ON_PYTHONANYWHERE = bool(os.getenv("PYTHONANYWHERE_DOMAIN") or os.getenv("PYTHONANYWHERE_SITE"))


def env_bool(name: str, default: bool = False) -> bool:
    return env.bool(name, default=default)


def env_list(name: str, default=None):
    raw = env.str(name, default="")
    if not raw:
        return default or []
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- (1) Security ---
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="")
DEBUG = env.bool("DJANGO_DEBUG", default=False)

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-prod"
    else:
        raise RuntimeError(
            "DJANGO_SECRET_KEY environment variable must be set in production."
        )

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    default=["youssefsameh.pythonanywhere.com", "127.0.0.1", "localhost"],
)

CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=["https://youssefsameh.pythonanywhere.com"],
)

INSTALLED_APPS = [
    "unfold",  # modern admin theme; must come before django.contrib.admin
    "unfold.contrib.filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "anymail",  # SendGrid HTTP API backend (works on PythonAnywhere free tier)
    # Social login (internest_core/adapters.py). Only the providers below are installed; keys come from .env.
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.linkedin_oauth2",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.microsoft",
    "internest_core.apps.InternestCoreConfig",
    "internest_skills.apps.InternestSkillsConfig",
    "internest_lounge.apps.InternestLoungeConfig",
    "internest_campus.apps.InternestCampusConfig",
    "internest_startups.apps.InternestStartupsConfig",
    "internest_students.apps.InternestStudentsConfig",
    "internest_ai.apps.InternestAIConfig",
    "internest_careers.apps.InternestCareersConfig",
]

SITE_ID = 1

MIDDLEWARE = [
    # Internest: one id per request (X-Request-ID header, log lines, Sentry tag).
    "internest_core.request_id.RequestIDMiddleware",
    # Internest: per-response script nonce + Content-Security-Policy (no 'unsafe-inline' scripts; internest_core/csp.py).
    # Early, so pages answered by the middleware below (413, 429, gates) also get a nonce and the header.
    "internest_core.csp.ContentSecurityPolicyMiddleware",
    # Internest: refuse oversized request bodies (uploads included) before they are read.
    "internest_core.middleware.RequestSizeLimitMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Internest: gzip HTML/JSON (70-85% smaller pages), then 304 Not Modified via ETag / Last-Modified.
    "internest_core.middleware.TextGZipMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Internest: renew the 30-day session at most once a day (instead of saving it on every request).
    "internest_core.middleware.SlidingSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Internest: /api/ai/ answers stay JSON when the rate limiter or an onboarding gate answers first.
    "internest_careers.middleware.APIJSONErrorsMiddleware",
    # Internest: per-IP / per-user limits on login, signup, password reset and uploads.
    "internest_core.ratelimit.RateLimitMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Internest: lock unverified students on the verify-email page.
    "internest_core.middleware.EmailVerificationGateMiddleware",
    # Startups must complete their company profile before using the dashboard.
    "internest_startups.gate.CompanyProfileGateMiddleware",
]

ROOT_URLCONF = "internest_app_project.urls"
WSGI_APPLICATION = "internest_app_project.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "internest_core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "internest_core.csp.nonce_context",
                "internest_core.context_processors.skill_gate",
                "internest_core.context_processors.site_contact",
                "internest_core.context_processors.social_providers",
            ],
        },
    },
]

# --- (2) Database ---
# DATABASE_URL (e.g. postgres://user:pass@host:5432/internest) switches to a client/server database with
# persistent connections; nothing else changes. Without it: SQLite, tuned for concurrent web workers.
if os.getenv("DATABASE_URL"):
    DATABASES = {"default": env.db("DATABASE_URL")}
    DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=600)
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
else:
    # WAL lets readers and the writer work at the same time; it needs a local disk, so it is off on PythonAnywhere
    # (network filesystem) unless SQLITE_WAL=1 is set explicitly. busy_timeout waits for a lock instead of failing.
    from internest_core.db_tuning import sqlite_options

    SQLITE_WAL = env.bool("SQLITE_WAL", default=not ON_PYTHONANYWHERE)
    # SQLITE_PATH points a run at another file with the same tuning (benchmarks use a disposable copy).
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": env.str("SQLITE_PATH", default=str(BASE_DIR / "db.sqlite3")),
                             "OPTIONS": sqlite_options(SQLITE_WAL)}}

# --- (2b) Cache: Redis when REDIS_URL is set (shared by every worker/server), else a file cache shared by the
# workers of one machine (PythonAnywhere), local memory in tests. Used by sessions, rate limits, AI answers, alerts.
if os.getenv("REDIS_URL") and not TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": os.getenv("REDIS_URL"),
                          "KEY_PREFIX": "internest", "TIMEOUT": 300}}
elif TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "internest-tests"}}
else:
    CACHES = {"default": {"BACKEND": "internest_core.cache_backends.ThriftyFileBasedCache",
                          "LOCATION": str(BASE_DIR / ".cache" / "django"), "TIMEOUT": 300,
                          "OPTIONS": {"MAX_ENTRIES": 20000, "CULL_FREQUENCY": 4}}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- (3) i18n ---
LANGUAGES = [
    ("ar", _("Arabic")),
    ("en", _("English")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_CODE = "ar"
TIME_ZONE = "Africa/Cairo"
USE_I18N = True
USE_L10N = True
USE_TZ = True
LANGUAGE_COOKIE_NAME = "internest_lang"
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365  # one year

# --- (3b) Session persistence: stay logged in across tab/browser closes ---
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30          # 30 days
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
# Sessions are read from the cache and written to the DB only when they change (cached_db). The 30-day expiry
# still slides: internest_core.middleware.SlidingSessionMiddleware refreshes it at most once a day per user,
# instead of a database write on every single request.
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_SAVE_EVERY_REQUEST = False
SESSION_REFRESH_INTERVAL = 24 * 3600
SESSION_COOKIE_NAME = "internest_sessionid"

# --- (4) Static / Media ---
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "internest_core" / "static"]
# Django 5.1 removed STATICFILES_STORAGE: without STORAGES the hashed + pre-compressed storage was silently off.
# Hashed names are cached by browsers for a year (immutable); a missing manifest entry falls back to the plain name
# (WHITENOISE_MANIFEST_STRICT=False) instead of a 500, so a deploy that forgot collectstatic still works.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if TESTING
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_MAX_AGE = 31536000
# Serve app static files (e.g. internest_skills/static) even if collectstatic wasn't re-run after a deploy.
WHITENOISE_USE_FINDERS = True

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
if TESTING:  # audit L-2: test uploads used to land in the real media/ folder
    import tempfile
    MEDIA_ROOT = Path(tempfile.gettempdir()) / "internest-test-media"

# Upload limits (5 MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- (5) Auth redirects ---
LOGIN_REDIRECT_URL = "home_redirect"
LOGIN_URL = "login_or_signup"
LOGOUT_REDIRECT_URL = "landing"

# --- (5b) Social login (django-allauth) ---
# Keep Django's ModelBackend as the only backend: existing code calls login(request, user) without a backend and allauth
# falls back to ModelBackend. Credentials are read from .env; a provider with a missing key is simply not offered.
SOCIALACCOUNT_ADAPTER = "internest_core.adapters.SocialAccountAdapter"
ACCOUNT_ADAPTER = "internest_core.adapters.AccountAdapter"
ACCOUNT_EMAIL_VERIFICATION = "none"          # Internest verifies student emails itself (OTP); allauth's flow is unused
ACCOUNT_LOGIN_METHODS = {"username"}
SOCIALACCOUNT_LOGIN_ON_GET = False           # our buttons POST (CSRF-checked) to social_start; a bare GET gets allauth's confirm page
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_STORE_TOKENS = False           # we never call the provider again, so we never keep its tokens
# Auto-connect to an existing account by email ONLY when the provider vouches for that email (allauth checks
# `verified`; the adapter adds the Internest-side checks). Unverified provider emails never link to anything.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True
SOCIAL_LOGIN_PROVIDER_ORDER = ("google", "linkedin_oauth2", "github", "microsoft")  # Google first (primary button)


def _social_app(client_id_var: str, secret_var: str) -> dict:
    client_id, secret = os.getenv(client_id_var, ""), os.getenv(secret_var, "")
    return {"APP": {"client_id": client_id, "secret": secret, "key": ""}} if client_id and secret else {}


SOCIALACCOUNT_PROVIDERS = {
    "google": {**_social_app("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"), "SCOPE": ["profile", "email"], "AUTH_PARAMS": {"access_type": "online"}},
    "linkedin_oauth2": {**_social_app("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"), "SCOPE": ["openid", "profile", "email"]},
    "github": {**_social_app("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"), "SCOPE": ["user:email", "read:user"]},
    # "common" accepts university (.edu.eg, work/school) and personal Microsoft accounts.
    "microsoft": {**_social_app("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"), "TENANT": os.getenv("MICROSOFT_TENANT", "common"), "SCOPE": ["User.Read"]},
}
# Cookies: the session and CSRF cookies are never readable from JavaScript (all page scripts take the token from the DOM),
# and stay SameSite=Lax so the provider's redirect back to /oauth/<provider>/login/callback/ keeps the session.
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# --- (6) Email (Gmail SMTP) ---
# .env is loaded into os.environ above by environ.Env.read_env().
# EMAIL_HOST_PASSWORD must be a Gmail App Password (requires 2-Step Verification).
# Backend: DJANGO_EMAIL_BACKEND / EMAIL_BACKEND from .env, else SendGrid (django-anymail) when ANYMAIL_SENDGRID_API_KEY is set,
# else the Gmail SMTP settings below.
MONITORED_EMAIL_BACKEND = (
    os.getenv("DJANGO_EMAIL_BACKEND") or os.getenv("EMAIL_BACKEND")
    or ("anymail.backends.sendgrid.EmailBackend" if os.getenv("ANYMAIL_SENDGRID_API_KEY") else "django.core.mail.backends.smtp.EmailBackend")
)
# Real SendGrid / SMTP delivery is wrapped so its failures raise admin alerts (internest_core/email_backend.py).
# Console / file / test backends are used as they are.
EMAIL_BACKEND = (
    "internest_core.email_backend.MonitoredEmailBackend"
    if MONITORED_EMAIL_BACKEND in ("anymail.backends.sendgrid.EmailBackend", "django.core.mail.backends.smtp.EmailBackend")
    else MONITORED_EMAIL_BACKEND
)
if os.getenv("ANYMAIL_SENDGRID_API_KEY") or "anymail" in MONITORED_EMAIL_BACKEND:
    ANYMAIL = {"SENDGRID_API_KEY": os.getenv("ANYMAIL_SENDGRID_API_KEY", "")}
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "internest.opportunities@gmail.com")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = os.getenv("DJANGO_DEFAULT_FROM_EMAIL") or os.getenv("DEFAULT_FROM_EMAIL") or f"Internest <{EMAIL_HOST_USER}>"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_TIMEOUT = 20

# Rate limits on login / signup / password reset / uploads (internest_core.ratelimit). Off in the test runner so
# hundreds of test logins from 127.0.0.1 don't trip it; the rate-limit tests switch it back on.
RATELIMIT_ENABLED = env.bool("RATELIMIT_ENABLED", default="test" not in sys.argv[1:2])

# Phase 7 security: audit-trail retention (manage.py purge_security_events, run daily) and optional ClamAV scanning of
# uploads (internest_core.upload_security; empty host = not configured, PythonAnywhere has no clamd).
# Phase 8: backups (manage.py backup_data / verify_backup / restore_backup). Outside the project, MEDIA_ROOT and
# STATIC_ROOT (enforced); default ~/backups/internest.
BACKUP_DIR = env.str("BACKUP_DIR", default="")
# Forgot password: minimum response time for "send me a code", so a sent email doesn't reveal that an account exists.
PASSWORD_RESET_MIN_SECONDS = 0 if TESTING else 1.0
MAX_REQUEST_BYTES = 20 * 1024 * 1024  # largest legitimate body: a 15 MB Voice-to-CV recording + form fields
SECURITY_EVENT_RETENTION_DAYS = env.int("SECURITY_EVENT_RETENTION_DAYS", default=365)
CLAMAV_HOST = env.str("CLAMAV_HOST", default="")
CLAMAV_PORT = env.int("CLAMAV_PORT", default=3310)

# --- (7) Production hardening (only when DEBUG=False) ---
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    X_FRAME_OPTIONS = "DENY"
else:
    SECURE_SSL_REDIRECT = False

# --- (8) Logging ---
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} [{request_id}]: {message}",
            "style": "{",
        },
    },
    "filters": {"request_id": {"()": "internest_core.request_id.RequestIDLogFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id"],
        },
        # AI provider failures (HTTP status + response body) also go to logs/ai.log for later debugging.
        "ai_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "ai.log"),
            "maxBytes": 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
            "formatter": "verbose",
            "filters": ["request_id"],
            "level": "WARNING",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "internest_core": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "internest_skills.llm": {
            "handlers": ["ai_file"],  # provider failures are logged quietly; the chain moves on
            "level": "INFO",
            "propagate": False,
        },
        "internest_ai": {
            "handlers": ["console", "ai_file"],  # circuit breaker, quota and routing decisions
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# --- Skill extraction LLM (OpenAI-compatible via AgentRouter); regex fallback when unset ---
# Secrets: set in .env, never commit. API_KEY_1 is the primary key (AGENTROUTER_API_KEY still works).
AGENTROUTER_API_KEY = os.getenv("API_KEY_1") or os.getenv("AGENTROUTER_API_KEY", "")
AGENTROUTER_BASE_URL = os.getenv("API_BASE_URL_1") or os.getenv("AGENTROUTER_BASE_URL", "https://agentrouter.org/v1")
# Failover key, used when API_KEY_1 hits a quota/rate limit, timeout or HTTP error. May point to another
# provider (e.g. Groq: https://api.groq.com/openai/v1); empty URL/model = same as the primary.
LLM_BACKUP_API_KEY = os.getenv("API_KEY_2", "")
LLM_BACKUP_BASE_URL = os.getenv("API_BASE_URL_2", "")
LLM_BACKUP_MODEL = os.getenv("API_MODEL_2", "")
# Direct Google Gemini key (Google AI Studio), tried after API_KEY_1/2. Reachable from PythonAnywhere free accounts.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
GOOGLE_BASE_URL = os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")
# Azure OpenAI / Copilot (v1 API): endpoint like https://<resource>.openai.azure.com/openai/v1/, model = deployment name.
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_COPILOT_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
# Empty = auto: gemini-2.5-flash for googleapis.com endpoints, gpt-4o-mini otherwise.
SKILLS_LLM_MODEL = env.str("SKILLS_LLM_MODEL", default="")
# Off by default: PythonAnywhere free accounts must reach external APIs through the platform proxy.
ENABLE_PROXY_BYPASS = env.bool("ENABLE_PROXY_BYPASS", default=False)

# --- internest_ai: the AI orchestration layer (providers, router, circuit breaker, quotas, redaction) ---
# Extra OpenAI-compatible providers, enabled only when their env vars are set (all registered in api_registry.py).
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")  # empty = a free ":free" model
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_AI_MODEL = os.getenv("CLOUDFLARE_AI_MODEL", "")
# Local model server (Ollama / llama.cpp, OpenAI-compatible): private data never leaves the machine. No key needed.
AI_LOCAL_BASE_URL = os.getenv("AI_LOCAL_BASE_URL", "")  # e.g. http://127.0.0.1:11434/v1
AI_LOCAL_MODEL = os.getenv("AI_LOCAL_MODEL", "")
AI_LOCAL_API_KEY = os.getenv("AI_LOCAL_API_KEY", "")
AI_LOCAL_LONG_CONTEXT = env.bool("AI_LOCAL_LONG_CONTEXT", default=False)
# Comma-separated registry keys (e.g. "local_llm,gemini,groq,agentrouter"). Empty = local first, then free, then paid.
AI_PROVIDER_ORDER = env_list("AI_PROVIDER_ORDER")
# Deterministic mock provider for tests and load benchmarks. Refused when DEBUG=False unless AI_MOCK_ALLOW_PRODUCTION=1.
AI_MOCK_PROVIDER = env.bool("AI_MOCK_PROVIDER", default=False)
AI_MOCK_ALLOW_PRODUCTION = env.bool("AI_MOCK_ALLOW_PRODUCTION", default=False)
AI_MOCK_LATENCY_MS = env.int("AI_MOCK_LATENCY_MS", default=0)
# Circuit breaker (Django cache): open after N consecutive failures within the window, then one half-open probe.
# Off in tests by default (the suite shares one cache); breaker tests switch it on.
AI_BREAKER_ENABLED = env.bool("AI_BREAKER_ENABLED", default=not TESTING)
AI_BREAKER_FAILURES = env.int("AI_BREAKER_FAILURES", default=3)
AI_BREAKER_WINDOW_SECONDS = env.int("AI_BREAKER_WINDOW_SECONDS", default=60)
AI_BREAKER_OPEN_SECONDS = env.int("AI_BREAKER_OPEN_SECONDS", default=60)
AI_BREAKER_MAX_RETRY_AFTER = env.int("AI_BREAKER_MAX_RETRY_AFTER", default=900)  # cap on a provider's 429 Retry-After
# Quotas (0 = unlimited). Per-task overrides of the per-user daily cap: AI_QUOTA_TASK_LIMITS=cv_polish=20,cv_voice=10
AI_QUOTAS_ENABLED = env.bool("AI_QUOTAS_ENABLED", default=True)
AI_QUOTA_USER_DAILY = env.int("AI_QUOTA_USER_DAILY", default=200)
AI_QUOTA_TASK_LIMITS = {task.strip(): int(limit) for task, _sep, limit in
                        (item.partition("=") for item in env_list("AI_QUOTA_TASK_LIMITS")) if limit.strip().isdigit()}
AI_QUOTA_PARTNER_DAILY = env.int("AI_QUOTA_PARTNER_DAILY", default=2000)
AI_QUOTA_GLOBAL_PER_MINUTE = env.int("AI_QUOTA_GLOBAL_PER_MINUTE", default=300)
AI_QUOTA_GLOBAL_PER_DAY = env.int("AI_QUOTA_GLOBAL_PER_DAY", default=20000)
AI_USAGE_RETENTION_DAYS = env.int("AI_USAGE_RETENTION_DAYS", default=90)  # AIUsage rows older than this are purged

# =========================================================================
# Performance: pooled outbound HTTP, AI response cache, async AI tasks
# =========================================================================
# internest_core/http_client.py: one keep-alive pool per process for every AI call (HTTP/2 when `h2` is installed).
HTTP_CLIENT_MAX_CONNECTIONS = env.int("HTTP_CLIENT_MAX_CONNECTIONS", default=100)
HTTP_CLIENT_MAX_KEEPALIVE = env.int("HTTP_CLIENT_MAX_KEEPALIVE", default=30)
HTTP_CLIENT_CONNECT_TIMEOUT = env.float("HTTP_CLIENT_CONNECT_TIMEOUT", default=5.0)
HTTP_CLIENT_READ_TIMEOUT = env.float("HTTP_CLIENT_READ_TIMEOUT", default=60.0)
HTTP_CLIENT_RETRIES = env.int("HTTP_CLIENT_RETRIES", default=2)  # connect errors and 502/503 only

# internest_core/ai_cache.py: identical temperature-0 prompts are answered from the cache (0 = off; off in tests).
AI_CACHE_TTL = env.int("AI_CACHE_TTL", default=0 if TESTING else 7 * 24 * 3600)

# internest_core/async_tasks.py: sync | thread | db | celery. PythonAnywhere web apps cannot start threads, so they
# default to "sync" (same behaviour as before); set "db" + an always-on `manage.py run_async_tasks` to go async there.
ASYNC_TASK_BACKEND = env.str("ASYNC_TASK_BACKEND", default="sync" if (TESTING or ON_PYTHONANYWHERE) else "thread")
ASYNC_TASK_WORKERS = env.int("ASYNC_TASK_WORKERS", default=4)
ASYNC_TASK_QUEUE_TIMEOUT = env.int("ASYNC_TASK_QUEUE_TIMEOUT", default=120)  # queued this long = no worker: fail it
ASYNC_TASK_RUN_TIMEOUT = env.int("ASYNC_TASK_RUN_TIMEOUT", default=300)
# Reliability (db worker): a running task whose lease expired (worker killed) is re-claimed; retryable errors are retried
# with exponential backoff up to ASYNC_TASK_MAX_ATTEMPTS, then the task is "dead".
ASYNC_TASK_LEASE_SECONDS = env.int("ASYNC_TASK_LEASE_SECONDS", default=ASYNC_TASK_RUN_TIMEOUT)
ASYNC_TASK_MAX_ATTEMPTS = env.int("ASYNC_TASK_MAX_ATTEMPTS", default=3)
ASYNC_TASK_RETRY_BASE_SECONDS = env.int("ASYNC_TASK_RETRY_BASE_SECONDS", default=5)
ASYNC_TASK_RETRY_MAX_SECONDS = env.int("ASYNC_TASK_RETRY_MAX_SECONDS", default=300)
# Admission control: per-user active tasks (429 + Retry-After) and a global queue cap (503 + Retry-After).
ASYNC_TASK_USER_MAX_ACTIVE = env.int("ASYNC_TASK_USER_MAX_ACTIVE", default=2)
ASYNC_TASK_USER_RETRY_AFTER = env.int("ASYNC_TASK_USER_RETRY_AFTER", default=10)
ASYNC_TASK_MAX_QUEUED = env.int("ASYNC_TASK_MAX_QUEUED", default=500)
ASYNC_TASK_BACKPRESSURE_RETRY_AFTER = env.int("ASYNC_TASK_BACKPRESSURE_RETRY_AFTER", default=30)
SKILLS_LLM_TIMEOUT = env.float("SKILLS_LLM_TIMEOUT", default=30.0)
# Voice-to-CV transcription (OpenAI-compatible Whisper). Without it, a Groq/OpenAI key above is reused.
WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "https://api.groq.com/openai/v1")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3")
# Freelance importer: False = an admin approves each imported gig (link + proof) before its skills get the
# "Verified skill" badge. True = skills are verified as soon as the AI reads the gig (easy to game with fake text).
FREELANCE_AUTO_VERIFY = env.bool("FREELANCE_AUTO_VERIFY", default=False)

# --- Regional Pro pricing (internest_startups.pricing) ---
DEFAULT_PRICING_COUNTRY = env("DEFAULT_PRICING_COUNTRY", default="EG")
# Optional MaxMind GeoLite2 folder (needs the geoip2 package). Without it: Cloudflare header, then the default.
GEOIP_PATH = env("GEOIP_PATH", default="") or None
PAYMENT_GATEWAYS = {"EGP": env("PAYMENT_GATEWAY_EGP", default="fawaterak"), "default": env("PAYMENT_GATEWAY_INTL", default="stripe")}
# Fawaterak (EGP checkout). Without an API key, checkout falls back to "our team contacts you to pay".
FAWATERAK_API_KEY = os.getenv("FAWATERAK_API_KEY", "")  # secret: set in .env, never commit
FAWATERAK_BASE_URL = os.getenv("FAWATERAK_BASE_URL", "https://app.fawaterk.com")  # staging: https://staging.fawaterk.com

# --- Public contact details (footer, policies, checkout) ---
# Manual Pro payments (checked by our team from the uploaded receipt). Public payee handles only — no phone numbers.
INSTAPAY_LINK = env("INSTAPAY_LINK", default="https://ipn.eg/S/you_zersif7/instapay/5UiNUX")
INSTAPAY_IPA = env("INSTAPAY_IPA", default="you_zersif7@instapay")
REDOTPAY_ID = env("REDOTPAY_ID", default="1588800300")
SALES_EMAIL = env("SALES_EMAIL", default="sales@internest.app")
SUPPORT_PHONE = env("SUPPORT_PHONE", default="01113999387")
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="engineerthe973@gmail.com")

# API health alerts (internest_core/api_monitor.py): failures and low balance go to this admin address ONLY.
ADMIN_ALERT_EMAIL = os.getenv("ADMIN_ALERT_EMAIL", "engineerthe973@gmail.com")
API_LOW_BALANCE_THRESHOLD = float(os.getenv("API_LOW_BALANCE_THRESHOLD", "2.0"))
API_ALERT_COOLDOWN_SECONDS = 30 * 60  # one email per (provider, alert type) per window

# --- Admin panel (django-unfold 0.81.0: last line we pin that runs on Python 3.10, used on PythonAnywhere) ---
from django.templatetags.static import static  # noqa: E402
from django.urls import reverse, reverse_lazy  # noqa: E402


def _admin_list(model, query=""):
    """Sidebar link to a model's changelist, optionally pre-filtered."""
    if not query:
        return reverse_lazy(f"admin:{model}_changelist")
    return lambda request: reverse(f"admin:{model}_changelist") + query


def _nav(title, icon, model, query="", badge=None):
    item = {"title": title, "icon": icon, "link": _admin_list(model, query)}
    if badge:
        item["badge"] = f"internest_core.admin_dashboard.{badge}"
    return item


UNFOLD = {
    "SITE_TITLE": "Internest Admin",
    "SITE_HEADER": "Internest",
    "SITE_SUBHEADER": _("Control center"),
    "SITE_URL": "/",
    "SITE_ICON": lambda request: static("img/internest_logo_192.jpg"),  # small copy; the original is 1024 px / 106 KB
    "SITE_FAVICONS": [{"rel": "icon", "type": "image/jpeg", "href": lambda request: static("img/internest_logo_192.jpg")}],
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "SHOW_LANGUAGES": True,  # AR / EN switch; the layout flips to RTL in Arabic
    "DASHBOARD_CALLBACK": "internest_core.admin_dashboard.dashboard_callback",
    "COLORS": {
        "primary": {  # Internest blue
            "50": "oklch(97% .014 254.604)", "100": "oklch(93.2% .032 255.585)", "200": "oklch(88.2% .059 254.128)",
            "300": "oklch(80.9% .105 251.813)", "400": "oklch(70.7% .165 254.624)", "500": "oklch(62.3% .214 259.815)",
            "600": "oklch(54.6% .245 262.881)", "700": "oklch(48.8% .243 264.376)", "800": "oklch(42.4% .199 265.638)",
            "900": "oklch(37.9% .146 265.522)", "950": "oklch(28.2% .091 267.935)",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {"title": _("🎓 Students & skills"), "collapsible": True, "items": [
                _nav(_("Student profiles"), "school", "internest_core_studentprofile"),
                _nav(_("Email verifications"), "mark_email_read", "internest_core_emailverification"),
                _nav(_("Student skills (claimed / verified)"), "verified", "internest_skills_studentskill"),
                _nav(_("Skill taxonomy"), "category", "internest_skills_skill"),
                _nav(_("Skill challenges"), "quiz", "internest_skills_challengeitem"),
                _nav(_("Skill-gap course catalogue"), "menu_book", "internest_skills_course"),
                _nav(_("Challenge attempts"), "timer", "internest_skills_challengeattempt"),
                _nav(_("Skill profiles (AI CV analysis)"), "psychology", "internest_skills_skillprofile"),
                _nav(_("CV builder"), "description", "internest_students_studentcv"),
                _nav(_("Projects"), "folder_open", "internest_students_studentproject"),
                _nav(_("Verified gig experience"), "workspace_premium", "internest_core_gigexperience"),
                _nav(_("Campus votes"), "how_to_vote", "internest_campus_campusdemandvote"),
                _nav(_("Gamification tasks"), "sports_esports", "internest_core_gamificationtask"),
            ]},
            {"title": _("🏢 Companies & opportunities"), "collapsible": True, "items": [
                _nav(_("University demo requests"), "school", "internest_core_universityinquiry", badge="university_inquiries_badge"),
                _nav(_("Partners"), "apartment", "internest_core_partnerprofile"),
                _nav(_("Startups awaiting verification"), "domain_verification", "internest_startups_pendingcompanyprofile", badge="pending_companies_badge"),
                _nav(_("Company profiles"), "storefront", "internest_startups_companyprofile"),
                _nav(_("Published opportunities"), "work", "internest_core_internship"),
                _nav(_("Opportunity submissions"), "pending_actions", "internest_core_partnerinternshipsubmission", badge="pending_submissions_badge"),
                _nav(_("Applications"), "inbox", "internest_core_application"),
                _nav(_("Talent Pool invitations"), "mail", "internest_startups_talentinvitation"),
                _nav(_("Course submissions"), "menu_book", "internest_core_partnercoursesubmission"),
                _nav(_("Pro payments"), "payments", "internest_startups_proupgraderequest", badge="pending_payments_badge"),
                _nav(_("Receipts to verify"), "receipt_long", "internest_startups_proupgraderequest", "?status__exact=verifying", badge="receipts_to_verify_badge"),
                _nav(_("Subscriptions"), "card_membership", "internest_skills_employersubscription"),
                _nav(_("Promo codes"), "sell", "internest_startups_promocode"),
                _nav(_("Founders Network posts"), "forum", "internest_lounge_loungepost"),
            ]},
            {"title": _("💼 Freelance import"), "collapsible": True, "items": [
                _nav(_("All imported work"), "cloud_download", "internest_skills_studentfreelancegig"),
                _nav(_("Needs proof"), "upload_file", "internest_skills_studentfreelancegig", "?status__exact=needs_proof", badge="needs_proof_badge"),
                _nav(_("Waiting for review"), "fact_check", "internest_skills_studentfreelancegig", "?status__exact=pending", badge="pending_gigs_badge"),
                _nav(_("Approved & verified"), "task_alt", "internest_skills_studentfreelancegig", "?status__exact=verified"),
            ]},
            {"title": _("🏅 Skill certificates"), "collapsible": True, "items": [
                _nav(_("All certificates"), "workspace_premium", "internest_skills_skillcertificate"),
                _nav(_("Under review"), "fact_check", "internest_skills_skillcertificate", "?status__exact=under_review", badge="pending_certificates_badge"),
            ]},
            {"title": _("📊 Analytics & system"), "collapsible": True, "items": [
                _nav(_("User accounts"), "person", "auth_user"),
                _nav(_("Groups & permissions"), "admin_panel_settings", "auth_group"),
                _nav(_("Opted out of opportunity emails"), "notifications_off", "internest_core_studentprofile", "?email_new_opportunities__exact=0"),
                _nav(_("Audit log"), "history", "admin_logentry"),
                _nav(_("API health & billing"), "monitor_heart", "internest_core_apiprovider"),
                _nav(_("API incidents"), "report", "internest_core_apiincident"),
                _nav(_("Skill relations"), "hub", "internest_skills_skillrelation"),
                _nav(_("External courses"), "school", "internest_skills_externalcourse"),
            ]},
        ],
    },
}


# =========================================================================
# Sentry (errors + performance). Off until SENTRY_DSN is set in .env; never in tests.
# Privacy: no PII, no local variables, no request bodies; emails, codes, tokens and keys masked
# (internest_core/observability.py). Check delivery: `manage.py sentry_test` or /ops/sentry-check/ (superusers).
# =========================================================================
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development" if DEBUG else "production")
SENTRY_RELEASE = os.getenv("SENTRY_RELEASE", "")
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1)
SENTRY_PROFILES_SAMPLE_RATE = env.float("SENTRY_PROFILES_SAMPLE_RATE", default=0.0)
# uWSGI on PythonAnywhere runs without threads: send inline there (see observability.py).
SENTRY_SYNC_TRANSPORT = env_bool("SENTRY_SYNC_TRANSPORT", default=ON_PYTHONANYWHERE)
if SENTRY_DSN and not TESTING:
    from internest_core.observability import detect_release, init_sentry

    init_sentry(SENTRY_DSN, environment=SENTRY_ENVIRONMENT, release=SENTRY_RELEASE or detect_release(BASE_DIR),
                traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE, profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
                sync_transport=SENTRY_SYNC_TRANSPORT)
