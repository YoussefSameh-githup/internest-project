"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
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
    "internest_core.apps.InternestCoreConfig",
    "internest_skills.apps.InternestSkillsConfig",
    "internest_lounge.apps.InternestLoungeConfig",
    "internest_campus.apps.InternestCampusConfig",
    "internest_startups.apps.InternestStartupsConfig",
    "internest_students.apps.InternestStudentsConfig",
]

SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
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
                "internest_core.context_processors.skill_gate",
                "internest_core.context_processors.site_contact",
            ],
        },
    },
]

# --- (2) Database ---
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

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
SESSION_SAVE_EVERY_REQUEST = True               # sliding expiry on every request
SESSION_COOKIE_NAME = "internest_sessionid"

# --- (4) Static / Media ---
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "internest_core" / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
# Serve app static files (e.g. internest_skills/static) even if collectstatic wasn't re-run after a deploy.
WHITENOISE_USE_FINDERS = True

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Upload limits (5 MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- (5) Auth redirects ---
LOGIN_REDIRECT_URL = "home_redirect"
LOGIN_URL = "login_or_signup"
LOGOUT_REDIRECT_URL = "landing"

# --- (6) Email (Gmail SMTP) ---
# .env is loaded into os.environ above by environ.Env.read_env().
# EMAIL_HOST_PASSWORD must be a Gmail App Password (requires 2-Step Verification).
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "internest.opportunities@gmail.com")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = f"Internest <{EMAIL_HOST_USER}>"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_TIMEOUT = 20

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
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
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
# Empty = auto: gemini-2.5-flash for googleapis.com endpoints, gpt-4o-mini otherwise.
SKILLS_LLM_MODEL = env.str("SKILLS_LLM_MODEL", default="")
# Off by default: PythonAnywhere free accounts must reach external APIs through the platform proxy.
ENABLE_PROXY_BYPASS = env.bool("ENABLE_PROXY_BYPASS", default=False)
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
SUPPORT_PHONE = env("SUPPORT_PHONE", default="01113999387")
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="engineerthe973@gmail.com")

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
    "SITE_ICON": lambda request: static("img/internest_logo.jpg"),
    "SITE_FAVICONS": [{"rel": "icon", "type": "image/jpeg", "href": lambda request: static("img/internest_logo.jpg")}],
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
                _nav(_("Golden list"), "military_tech", "internest_core_goldenliststudent"),
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
            {"title": _("📊 Analytics & system"), "collapsible": True, "items": [
                _nav(_("User accounts"), "person", "auth_user"),
                _nav(_("Groups & permissions"), "admin_panel_settings", "auth_group"),
                _nav(_("Opted out of opportunity emails"), "notifications_off", "internest_core_studentprofile", "?email_new_opportunities__exact=0"),
                _nav(_("Audit log"), "history", "admin_logentry"),
                _nav(_("Skill relations"), "hub", "internest_skills_skillrelation"),
                _nav(_("External courses"), "school", "internest_skills_externalcourse"),
            ]},
        ],
    },
}
