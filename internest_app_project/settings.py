"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
import logging
# 💡 ضروري لدمج LOCALE_PATHS
from django.utils.translation import gettext_lazy as _ 

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- (1. المفتاح السري) ---
SECRET_KEY = os.environ.get(
    'SECRET_KEY', 
    'django-insecure-default-key-for-development' # (ده مفتاح وهمي)
)


# --- (2. وضع التشغيل) ---
DEBUG = True


# --- (3. الـ HOSTS) ---
ALLOWED_HOSTS = ['youssefsameh.pythonanywhere.com', '127.0.0.1', 'localhost']


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.sites', 
    'django.contrib.staticfiles',
    'internest_core.apps.InternestCoreConfig',
]

SITE_ID = 1


MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # 🚀 التعديل 1: إضافة LocaleMiddleware لدعم تغيير اللغة
    'django.middleware.locale.LocaleMiddleware', 
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'internest_app_project.urls'
WSGI_APPLICATION = 'internest_app_project.wsgi.application'

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

# --- (4. قاعدة البيانات) ---
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# ==========================================================
# 🚀 إعدادات التدويل (Internationalization Settings) 🚀
# ==========================================================

# 1. اللغات المدعومة
LANGUAGES = [
    ('ar', _('Arabic')),
    ('en', _('English')),
]

# 2. مسارات ملفات الترجمة
LOCALE_PATHS = [
    os.path.join(BASE_DIR, 'locale'),
]

# اللغة الافتراضية
LANGUAGE_CODE = 'ar' 

TIME_ZONE = 'Africa/Cairo' 
USE_I18N = True # تفعيل التدويل
USE_TZ = True

# --- (5. إعدادات الملفات الثابتة والوسائط) ---
STATIC_URL = '/static/' 
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles') 
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]

# إعدادات رفع الملفات (الشعارات والصور)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- (تحديد صفحات الدخول والخروج) ---
LOGIN_REDIRECT_URL = 'list'
LOGIN_URL = 'login_or_signup'
LOGOUT_REDIRECT_URL = 'landing'

# ==========================================================
# 🚀 6. إعدادات البريد الإلكتروني (EMAIL CONFIGURATION) 🚀
# ==========================================================

# 📝 ملاحظة هامة: هذا الإعداد هو لبيئة الإنتاج (SendGrid SMTP).
# للاختبار المحلي، قم بتغييره إلى: 'django.core.mail.backends.console.EmailBackend'

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend' 

# إعدادات SendGrid SMTP
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_PORT = 587             
EMAIL_USE_TLS = True         

# بيانات الدخول باستخدام مفتاح API Key
EMAIL_HOST_USER = 'apikey' 
# 🔑 مفتاح API Key السري
EMAIL_HOST_PASSWORD = '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' 

# تحديد إيميلات الإرسال
DEFAULT_FROM_EMAIL = 'internest.opportunities@gmail.com' 
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# ==========================================================
# 📝 7. إعدادات Logging (لتتبع الإشعارات)
# ==========================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        # logger خاص بملف signals.py لتتبع ما يحدث
        'internest_core.signals': { 
            'handlers': ['console'],
            'level': 'INFO', 
            'propagate': True,
        },
    },
}