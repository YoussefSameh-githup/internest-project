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
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "anymail",  # SendGrid HTTP API backend (works on PythonAnywhere free tier)
    "internest_core.apps.InternestCoreConfig",
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
    ("de", _("German")),
    ("fr", _("French")),
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

# --- (6) Email (SendGrid HTTPS API via Anymail) ---
# Read straight from the .env file via django-environ so the same value
# is visible to the web app, to `manage.py shell`, and to any management
# command, with zero ambiguity.
EMAIL_BACKEND = env.str("DJANGO_EMAIL_BACKEND", default="anymail.backends.sendgrid.EmailBackend")
ANYMAIL = {
    "SENDGRID_API_KEY": env.str("ANYMAIL_SENDGRID_API_KEY", default=""),
}
DEFAULT_FROM_EMAIL = env.str("DJANGO_DEFAULT_FROM_EMAIL", default="internest.opportunities@gmail.com")
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
