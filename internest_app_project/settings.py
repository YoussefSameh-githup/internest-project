"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
import dj_database_url  # (✨ قاعدة البيانات من البيئة)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- (✨ 1. المفتاح السري) ---
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-default-key-for-development'  # مفتاح تجريبي فقط
)

# --- (✨ 2. وضع الـ DEBUG والـ HOSTS) ---
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '.railway.app']
RAILWAY_PUBLIC_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
if RAILWAY_PUBLIC_DOMAIN:
    ALLOWED_HOSTS.append(RAILWAY_PUBLIC_DOMAIN)

# --- (✨ 3. التطبيقات) ---
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic',  # تشغيل Whitenoise محليًا
    'django.contrib.staticfiles',

    # تطبيقك
    'internest_core.apps.InternestCoreConfig',
]

# --- (✨ 4. الـ Middleware) ---
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Whitenoise لتقديم الملفات الثابتة
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# --- (✨ 5. روابط المشروع) ---
ROOT_URLCONF = 'Internest_Project.urls'
WSGI_APPLICATION = 'Internest_Project.wsgi.application'

# --- (✨ 6. إعداد القوالب) ---
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'internest_core', 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# --- (✨ 7. قاعدة البيانات) ---
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600
    )
}

# --- (✨ 8. التحقق من كلمات المرور) ---
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# --- (✨ 9. الإعدادات الدولية) ---
LANGUAGE_CODE = 'ar'
TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True

# --- (✨ 10. إعدادات الملفات الثابتة) ---
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# --- (✨ 11. إعدادات الملفات المرفوعة) ---
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# --- (✨ 12. إعدادات تسجيل الدخول والخروج) ---
LOGIN_REDIRECT_URL = 'list'
LOGIN_URL = 'login_or_signup'
LOGOUT_REDIRECT_URL = 'landing'

# --- (✨ 13. الإعداد الافتراضي للحقل الرئيسي) ---
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
