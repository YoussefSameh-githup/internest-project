"""
Django settings for internest_app_project project.
"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent 
DEBUG = True
SECRET_KEY = 'django-insecure-79en^8-x8$wsihq&!h!0$$%vv(x-$v4*-(9^dnvig3@sksbu6v' 
ALLOWED_HOSTS = []

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'internest_core.apps.InternestCoreConfig', 
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'internest_app_project.urls' 

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

WSGI_APPLICATION = 'internest_app_project.wsgi.application' 

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

LANGUAGE_CODE = 'ar-sa' 
TIME_ZONE = 'Africa/Cairo' 
USE_I18N = True
USE_TZ = True

# === إعدادات STATIC و MEDIA ===
STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles') 
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

LOGIN_REDIRECT_URL = '/list/' # تم توجيه الدخول مباشرة إلى قائمة التدريبات
LOGOUT_REDIRECT_URL = '/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# settings.py (في نهاية الملف)
# ...

# === إعدادات الجلسات (Sessions) ===

# إذا كان هذا True، سيتم إنهاء الجلسة (تسجيل الخروج) عند إغلاق المتصفح.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True 

# لضمان عدم تذكر المتصفح لجلسة الطالب/الشريك
SESSION_SAVE_EVERY_REQUEST = True