"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
import dj_database_url # (✨ 1. استيراد)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- (✨ 2. إخفاء المفتاح السري) ---
# هو هيقراه من المنصة. لو مش لاقيه، هيستخدم واحد "وهمي" (للتجربة بس)
SECRET_KEY = os.environ.get(
    'SECRET_KEY', 
    'django-insecure-default-key-for-development'
)

# --- (✨ 3. إعدادات الـ DEBUG) ---
# هيقراها من المنصة. لو مش لاقيها، هيعتبر إننا "لوكال"
DEBUG = os.environ.get('DEBUG', 'True') == 'True'

# (✨ 4. أسماء الدومين)
# هيقراها من المنصة. لو "لوكال"، هيضيف 127.0.0.1
ALLOWED_HOSTS = []
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
else:
    ALLOWED_HOSTS = ['127.0.0.1', 'localhost']


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'whitenoise.runserver_nostatic', # (✨ 5. إضافة Whitenoise)
    'django.contrib.staticfiles',
    'internest_core.apps.InternestCoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', # (✨ 6. إضافة Whitenoise)
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'internest_app_project.urls' # (اتأكد إن ده اسم البروجكت الصح)

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

WSGI_APPLICATION = 'internest_app_project.wsgi.application' # (اتأكد إن ده اسم البروجكت الصح)


# --- (✨ 7. تعديل الداتا بيز) ---
# هيقراها من المنصة. لو "لوكال"، هيستخدم ملف sqlite
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600
    )
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# Internationalization
LANGUAGE_CODE = 'ar' 
TIME_ZONE = 'Africa/Cairo' 
USE_I18N = True
USE_TZ = True

# --- (✨ 8. إعدادات الـ Static Files للإنتاج) ---
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]
# ده المخزن اللي WhiteNoise هيستخدمه
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# إعدادات رفع الملفات (الشعارات والصور)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'